"""
Main Bot Orchestration
======================
Ties all 6 models together in a single trading loop:

1. Bayesian  → estimates true probability q for each market
2. Edge      → checks EV_net = q - p - c > MIN_EDGE
3. Spread    → detects cross-market dislocations via z-score
4. Stoikov   → computes optimal execution price and passive/aggressive mode
5. Kelly     → sizes the position based on edge and execution probability
6. MonteCarlo→ (runs offline / periodically) validates the strategy

Growth strategy: $5.27 → $100 → $1,000 → $10,000
  - Start aggressive (Kelly λ=0.50, MIN_EDGE=2%) to compound fast
  - Automatically reduce aggressiveness as bankroll grows
  - Bankroll is persisted to disk between restarts
"""

import json
import os
import time
import logging
from dataclasses import dataclass, field
from pathlib import Path

from models.bayesian import BayesianModel
from models.edge import EdgeModel, EdgeResult
from models.spread import SpreadMap, SpreadSignal
from models.stoikov import StoikovModel, StoikovQuote
from models.kelly import KellyModel, KellyResult
from models.monte_carlo import MonteCarloSimulator

from data.price_feed import PriceFeed
from data.market_data import PolymarketDataClient, GammaClient, extract_clob_tokens, extract_gamma_prices
from data.wallet_tracker import WalletTracker
from data.sentiment_analyzer import EventSentimentAnalyzer
from data.dry_run_tracker import DryRunTracker
from trading.order_executor import OrderExecutor

from config import (
    POLL_INTERVAL_SECONDS,
    RELATED_MARKETS,
    BANKROLL,
    MIN_EDGE,
    TOTAL_COST,
    CRYPTO_SYMBOLS,
    POLYMARKET_ASSETS,
    GROWTH_TIERS,
    BANKROLL_STATE_FILE,
)

TRADES_FILE = Path(__file__).parent.parent / "trades.json"


logger = logging.getLogger("polymarket_bot.bot")


@dataclass
class MarketState:
    market_id: str
    token_id_yes: str
    token_id_no: str
    asset: str              # "BTC", "ETH", etc. or "EVENT"
    timeframe: str          # "5m", "15m", "event"
    bayesian: BayesianModel = field(default_factory=BayesianModel)
    stoikov: StoikovModel = field(default_factory=StoikovModel)
    last_price: float = 0.5
    last_price_no: float = 0.5
    end_time: float = 0.0   # Unix timestamp when market closes (0 = unknown)
    gamma_price_yes: float | None = None  # Fallback price from Gamma API
    gamma_price_no: float | None = None
    question: str = ""      # Market question text (for event markets)
    is_event: bool = False  # True for politics/sports/etc. markets


@dataclass
class TradeOpportunity:
    market_id: str
    edge_result: EdgeResult
    spread_signal: SpreadSignal | None
    stoikov_quote: StoikovQuote
    kelly_result: KellyResult
    q: float = 0.0      # Bayesian probability estimate
    timestamp: float = field(default_factory=time.time)


def _get_tier(bankroll: float) -> tuple:
    """Return (kelly_lambda, min_edge) for the current bankroll tier."""
    for min_bal, max_bal, kelly_lambda, min_edge in GROWTH_TIERS:
        if min_bal <= bankroll < max_bal:
            return kelly_lambda, min_edge
    return GROWTH_TIERS[-1][2], GROWTH_TIERS[-1][3]


class ArbitrageBot:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.data_client = PolymarketDataClient()
        self.price_feed = PriceFeed()
        self.edge_model = EdgeModel()
        self.spread_map = SpreadMap()

        # Load persisted bankroll or use config default
        starting_bankroll = self._load_bankroll()
        kelly_lambda, self._current_min_edge = _get_tier(starting_bankroll)
        logger.info(
            f"Starting bankroll: ${starting_bankroll:.2f} | "
            f"Tier: Kelly λ={kelly_lambda:.2f}, MIN_EDGE={self._current_min_edge:.1%}"
        )

        self.kelly = KellyModel(bankroll=starting_bankroll, lambda_fraction=kelly_lambda)
        self.mc = MonteCarloSimulator()
        self.executor = None if dry_run else OrderExecutor()

        self.wallet_tracker = WalletTracker()
        self._last_wallet_update = 0.0
        self.event_sentiment = EventSentimentAnalyzer()

        self._markets: dict[str, MarketState] = {}
        self._running = False
        self._last_price_fetch = 0.0
        self._last_prices: dict[str, float] = {}
        self._last_heartbeat = 0.0
        self._last_market_refresh = 0.0
        self._last_stats_log = 0.0
        self._tick_count = 0
        self.dry_run_tracker = DryRunTracker()
        self._MARKET_REFRESH_INTERVAL = 30  # re-discover markets every 30s
        self._MARKET_WINDOW_MIN = 90        # skip if <90s remaining
        self._MARKET_WINDOW_MAX = 360       # skip if >6min remaining (not open yet)

        # Register known related pairs
        for m1, m2 in RELATED_MARKETS:
            self.spread_map.register_pair(m1, m2)

        self._mc_validated = False

    # ------------------------------------------------------------------
    # Bankroll persistence
    # ------------------------------------------------------------------
    def _load_bankroll(self) -> float:
        if os.path.exists(BANKROLL_STATE_FILE):
            try:
                with open(BANKROLL_STATE_FILE) as f:
                    data = json.load(f)
                    br = float(data.get("bankroll", BANKROLL))
                    logger.info(f"Loaded bankroll from state file: ${br:.2f}")
                    return br
            except Exception as e:
                logger.warning(f"Could not load bankroll state: {e}")
        return BANKROLL

    def _save_bankroll(self):
        try:
            with open(BANKROLL_STATE_FILE, "w") as f:
                json.dump({"bankroll": round(self.kelly.bankroll, 4)}, f)
        except Exception as e:
            logger.warning(f"Could not save bankroll state: {e}")

    # ------------------------------------------------------------------
    # Dynamic tier adjustment
    # ------------------------------------------------------------------
    def _apply_growth_tier(self):
        """Adjust Kelly lambda and MIN_EDGE based on current bankroll."""
        kelly_lambda, min_edge = _get_tier(self.kelly.bankroll)
        changed = False
        if abs(kelly_lambda - self.kelly.lambda_fraction) > 0.001:
            self.kelly.lambda_fraction = kelly_lambda
            changed = True
        if abs(min_edge - self._current_min_edge) > 0.0001:
            self._current_min_edge = min_edge
            self.edge_model.min_edge = min_edge
            changed = True
        if changed:
            logger.info(
                f"TIER CHANGE | Bankroll=${self.kelly.bankroll:.2f} | "
                f"Kelly λ={kelly_lambda:.2f} | MIN_EDGE={min_edge:.1%}"
            )

    # ------------------------------------------------------------------
    # Monte Carlo validation
    # ------------------------------------------------------------------
    def validate_with_monte_carlo(self) -> bool:
        logger.info("Running Monte Carlo validation...")
        result = self.mc.run(
            base_ev=self._current_min_edge,
            base_win_rate=0.55,
            avg_position_fraction=self.kelly.lambda_fraction,
        )
        logger.info(f"Monte Carlo result: {result.description}")
        self._mc_validated = result.is_viable
        return result.is_viable

    def register_market(
        self,
        market_id: str,
        token_id_yes: str,
        token_id_no: str,
        asset: str,
        timeframe: str,
        end_time: float = 0.0,
        gamma_price_yes: float | None = None,
        gamma_price_no: float | None = None,
    ):
        state = MarketState(
            market_id=market_id,
            token_id_yes=token_id_yes,
            token_id_no=token_id_no,
            asset=asset,
            timeframe=timeframe,
            bayesian=BayesianModel(market_id),
            stoikov=StoikovModel(),
            end_time=end_time,
            gamma_price_yes=gamma_price_yes,
            gamma_price_no=gamma_price_no,
        )
        # Auto-register spread pairs: any two markets with the same asset
        for existing_id, existing in self._markets.items():
            if existing.asset == asset:
                self.spread_map.register_pair(existing_id, market_id)
                logger.info(f"Spread pair registered: {existing_id} <-> {market_id}")
        self._markets[market_id] = state
        logger.info(f"Registered market: {market_id} ({asset} {timeframe})")

    def auto_discover_markets(self):
        """
        Discover active markets using ONLY Gamma API.
        Per docs: Use events endpoint → extract markets → get clobTokenIds.
        CLOB is used only for live data (book, midpoint, price).
        """
        import datetime
        gamma = GammaClient()
        assets = POLYMARKET_ASSETS
        registered = 0

        for asset in assets:
            logger.info(f"Discovering {asset} markets via Gamma API...")
            markets = gamma.discover_crypto_markets(asset)
            if not markets:
                logger.warning(f"No markets found for {asset} on Gamma — skipping")
                continue

            registered_for_asset = 0
            for m in markets:
                if registered_for_asset >= 3:
                    break

                condition_id = (m.get("conditionId") or m.get("condition_id") or
                                m.get("id") or "unknown")

                yes_token, no_token = extract_clob_tokens(m)
                if not yes_token or not no_token:
                    logger.warning(
                        f"{asset}: Could not extract clobTokenIds from Gamma market. "
                        f"Keys: {list(m.keys())}"
                    )
                    continue

                market_id = f"{asset}_5m_{str(condition_id)[:8]}"

                end_time = 0.0
                now_ts = time.time()
                end_date = (m.get("endDate") or m.get("end_date_iso") or
                            m.get("closeTime") or m.get("close_time") or
                            m.get("expirationTime") or m.get("expiration"))
                if end_date:
                    try:
                        dt = datetime.datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
                        ts = dt.timestamp()
                        if ts > now_ts - 86400:
                            end_time = ts
                    except Exception:
                        pass

                if end_time > 0 and end_time < now_ts:
                    continue

                gamma_price_yes, gamma_price_no = extract_gamma_prices(m)
                if gamma_price_yes is not None and not (0.05 <= gamma_price_yes <= 0.95):
                    continue

                self.register_market(
                    market_id=market_id,
                    token_id_yes=yes_token,
                    token_id_no=no_token,
                    asset=asset,
                    timeframe="5m",
                    end_time=end_time,
                    gamma_price_yes=gamma_price_yes,
                    gamma_price_no=gamma_price_no,
                )
                registered += 1
                registered_for_asset += 1

        logger.info(f"Auto-discovery complete: {registered} crypto markets registered")

        event_count = self._discover_event_markets(gamma)
        logger.info(f"Event markets discovered: {event_count}")

        self._last_market_refresh = time.time()
        return registered + event_count

    def _discover_event_markets(self, gamma: GammaClient) -> int:
        """
        Discover non-crypto event markets via Gamma events endpoint.
        Per docs: "Use the events endpoint and work backwards."
        """
        registered = 0
        try:
            event_markets = gamma.discover_event_markets(
                limit=50, exclude_assets=POLYMARKET_ASSETS,
            )
            for m in event_markets:
                question = m.get("question", "")
                yes_token, no_token = extract_clob_tokens(m)
                if not yes_token or not no_token:
                    continue

                market_id = m.get("conditionId") or m.get("id", "")
                if not market_id or market_id in self._markets:
                    continue

                gamma_price_yes, gamma_price_no = extract_gamma_prices(m)

                state = MarketState(
                    market_id=market_id,
                    token_id_yes=yes_token,
                    token_id_no=no_token,
                    asset="EVENT",
                    timeframe="event",
                    bayesian=BayesianModel(market_id),
                    stoikov=StoikovModel(),
                    question=question,
                    is_event=True,
                    gamma_price_yes=gamma_price_yes,
                    gamma_price_no=gamma_price_no,
                )
                self._markets[market_id] = state
                logger.info(f"Event market registered: {question[:60]}")
                registered += 1
                if registered >= 10:
                    break
        except Exception as e:
            logger.warning(f"Event market discovery failed: {e}")
        return registered

    def _refresh_markets(self, now: float):
        """
        Re-discover active markets and remove expired ones.
        Uses ONLY Gamma API for discovery.
        """
        expired = [mid for mid, s in self._markets.items()
                   if s.end_time > 0 and (s.end_time - now) < 0 and not s.is_event]
        for mid in expired:
            state = self._markets[mid]
            if self.dry_run and state.last_price > 0:
                winning_side = "YES" if state.last_price >= 0.5 else "NO"
                self.dry_run_tracker.resolve(mid, winning_side)
            logger.debug(f"Removing expired market: {mid}")
            del self._markets[mid]

        if self.dry_run and now - self._last_stats_log >= 300:
            self._last_stats_log = now
            self.dry_run_tracker.log_stats()

        gamma = GammaClient()
        new_count = 0
        for asset in POLYMARKET_ASSETS:
            markets = gamma.discover_crypto_markets(asset)
            for m in markets:
                import datetime
                condition_id = m.get("conditionId") or m.get("id", "")
                if not condition_id:
                    continue
                market_id = f"{asset}_5m_{str(condition_id)[:8]}"
                if market_id in self._markets:
                    continue

                end_time = 0.0
                end_date = (m.get("endDate") or m.get("end_date_iso") or
                            m.get("closeTime") or m.get("expirationTime"))
                if end_date:
                    try:
                        dt = datetime.datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
                        ts = dt.timestamp()
                        if ts > now - 86400:
                            end_time = ts
                    except Exception:
                        pass

                if end_time > 0 and end_time < now:
                    continue

                yes_token, no_token = extract_clob_tokens(m)
                if not yes_token or not no_token:
                    continue

                gamma_price_yes, gamma_price_no = extract_gamma_prices(m)
                if gamma_price_yes is not None and not (0.05 <= gamma_price_yes <= 0.95):
                    continue

                self.register_market(
                    market_id=market_id,
                    token_id_yes=yes_token,
                    token_id_no=no_token,
                    asset=asset,
                    timeframe="5m",
                    end_time=end_time,
                    gamma_price_yes=gamma_price_yes,
                    gamma_price_no=gamma_price_no,
                )
                new_count += 1

        if new_count:
            logger.info(f"[REFRESH] +{new_count} new markets | total={len(self._markets)}")

    def run(self):
        if not self.validate_with_monte_carlo():
            logger.warning("Monte Carlo validation failed. Strategy may not be viable. Continuing anyway.")

        logger.info(f"Starting bot main loop (mode: {'DRY RUN' if self.dry_run else 'LIVE'})...")
        logger.info(f"GROWTH GOAL: ${self.kelly.bankroll:.2f} → $100 → $1,000 → $10,000")
        self._running = True

        while self._running:
            try:
                self._tick()
            except KeyboardInterrupt:
                logger.info("Shutting down...")
                self._running = False
            except Exception as e:
                logger.error(f"Tick error: {e}", exc_info=True)
            time.sleep(POLL_INTERVAL_SECONDS)

    def _tick(self):
        now = time.time()
        elapsed = now - self._last_price_fetch if self._last_price_fetch else 1.0
        self._tick_count += 1

        # Apply growth tier (adjusts Kelly λ and MIN_EDGE based on bankroll)
        self._apply_growth_tier()

        # --- 1. Fetch crypto spot prices ---
        new_prices = self.price_feed.fetch()

        # --- Periodic market refresh (every 30s) ---
        if now - self._last_market_refresh >= self._MARKET_REFRESH_INTERVAL:
            self._last_market_refresh = now
            self._refresh_markets(now)

        # --- Wallet tracker update (every 60s) ---
        if now - self._last_wallet_update >= 60:
            self.wallet_tracker.update()
            logger.debug(self.wallet_tracker.summary())
            self._last_wallet_update = now

        opportunities: list[TradeOpportunity] = []
        market_stats = []  # collect for heartbeat

        for market_id, state in list(self._markets.items()):
            # Skip markets outside active window — but trade freely if end_time unknown (=0)
            if state.end_time > 0:
                remaining = state.end_time - now
                if remaining < self._MARKET_WINDOW_MIN:
                    market_stats.append(f"{state.asset}:EXPIRING({remaining:.0f}s)")
                    continue
                if not state.is_event and remaining > self._MARKET_WINDOW_MAX:
                    market_stats.append(f"{state.asset}:NOT_OPEN_YET({remaining:.0f}s)")
                    continue

            # --- 2. Fetch market prices from Polymarket ---
            yes_data = self.data_client.get_book_data(state.token_id_yes)
            no_data = self.data_client.get_book_data(state.token_id_no)
            p_yes = yes_data["mid_price"]
            p_no = no_data["mid_price"]
            # Fallback: use Gamma API prices if CLOB has no data
            # Note: Gamma prices are static (from discovery), so only use as last resort
            if p_yes is None and state.gamma_price_yes is not None:
                p_yes = state.gamma_price_yes
                # Mark as stale — Bayesian should not over-react to static prices
            if p_no is None and state.gamma_price_no is not None:
                p_no = state.gamma_price_no
            # Reject obviously invalid prices (0 or 1 are not real market prices)
            if p_yes is not None and not (0.01 <= p_yes <= 0.99):
                p_yes = None
            if p_no is not None and not (0.01 <= p_no <= 0.99):
                p_no = None
            if p_yes is None or p_no is None:
                market_stats.append(f"{state.asset}({state.timeframe}):NO_DATA")
                continue

            ob_imbalance = yes_data["imbalance"] or 0.0
            # Use CLOB depth if available; fall back to 0.5 for Gamma-only markets
            ob_depth = yes_data["depth"] if yes_data["depth"] else 0.5

            crypto_symbol = f"{state.asset}USDT"
            has_spot_price = crypto_symbol in new_prices

            # --- 3. Bayesian update ---
            volatility = self.price_feed.get_volatility(crypto_symbol) if has_spot_price else 0.0
            bayesian_data = self.price_feed.build_bayesian_data(
                symbol=crypto_symbol if has_spot_price else None,
                new_prices=new_prices,
                elapsed_seconds=elapsed,
                volatility=volatility,
                ob_imbalance=ob_imbalance,
            )
            q = state.bayesian.update(bayesian_data)

            # --- 3b. Wallet signal adjustment ---
            wallet_signal = self.wallet_tracker.get_signal(
                state.token_id_yes, state.token_id_no
            )
            if wallet_signal.has_signal:
                q = max(0.01, min(0.99, q + wallet_signal.confidence_boost))

            # --- 3c. Event sentiment boost (only for non-crypto markets, only above $100) ---
            if state.is_event and state.question:
                self.event_sentiment.analyze_async(
                    market_id=market_id,
                    question=state.question,
                    bankroll=self.kelly.bankroll,
                )
                sentiment_boost = self.event_sentiment.get_boost(market_id)
                if sentiment_boost != 0.0:
                    q = max(0.01, min(0.99, q + sentiment_boost))
                    logger.debug(f"[{market_id}] EventSentiment boost={sentiment_boost:+.3f} → q={q:.3f}")

            # --- 4. Edge check (uses dynamic MIN_EDGE from current tier) ---
            edge_result = self.edge_model.evaluate_directional(q=q, p=p_yes)

            # Also check within-market arbitrage
            within_result = self.edge_model.evaluate_within_market(p_yes, p_no)
            if within_result.has_edge and within_result.ev_net > edge_result.ev_net:
                edge_result = within_result

            market_stats.append(
                f"{state.asset}({state.timeframe}):q={q:.3f},p={p_yes:.3f},EV={edge_result.ev_net:+.3f}"
            )

            if not edge_result.has_edge:
                state.last_price = p_yes
                state.last_price_no = p_no
                continue

            # --- 5. Spread check (cross-market) ---
            spread_signal = None
            for m1_id, m2_id in self.spread_map.all_pairs():
                if market_id in (m1_id, m2_id):
                    other_id = m2_id if market_id == m1_id else m1_id
                    other = self._markets.get(other_id)
                    if other:
                        p_other = self.data_client.get_mid_price(other.token_id_yes)
                        if p_other:
                            sig = self.spread_map.update_pair(m1_id, m2_id, p_yes, p_other)
                            if sig.is_signal:
                                spread_signal = sig

            # --- 6. Stoikov execution quote ---
            remaining_time = self._estimate_remaining_time(state)
            stoikov_quote = state.stoikov.quote(
                mid_price=p_yes,
                remaining_time=remaining_time,
            )

            # --- 7. Kelly position sizing ---
            # Maker edge → override stoikov to passive; edge model already decided
            is_passive = edge_result.is_passive
            exec_prob = 0.9 if is_passive else 0.7
            kelly_result = self.kelly.compute(
                p_success=q,
                market_price=p_yes,
                exec_probability=exec_prob,
                ob_depth_factor=ob_depth,
            )

            if kelly_result.is_viable and kelly_result.position_size > 0:
                opp = TradeOpportunity(
                    market_id=market_id,
                    edge_result=edge_result,
                    spread_signal=spread_signal,
                    stoikov_quote=stoikov_quote,
                    kelly_result=kelly_result,
                    q=q,
                )
                opportunities.append(opp)
                logger.info(
                    f"OPPORTUNITY [{market_id}]: "
                    f"edge={edge_result.ev_net:.4f}, "
                    f"q={q:.3f}, p={p_yes:.3f}, "
                    f"size=${kelly_result.position_size:.2f}, "
                    f"bankroll=${self.kelly.bankroll:.2f}, "
                    f"exec={'AGGRESSIVE' if stoikov_quote.is_aggressive else 'PASSIVE'}"
                )

            state.last_price = p_yes
            state.last_price_no = p_no

        self._last_price_fetch = now
        self._last_prices = new_prices

        # --- Heartbeat: log status every 60 seconds ---
        if now - self._last_heartbeat >= 60:
            self._last_heartbeat = now
            no_data = sum(1 for s in market_stats if "NO_DATA" in s)
            active = [s for s in market_stats if "NO_DATA" not in s]
            logger.info(
                f"[HEARTBEAT] tick={self._tick_count} | markets={len(self._markets)} "
                f"({no_data} no data, {len(active)} active) | "
                f"bankroll=${self.kelly.bankroll:.2f} | {self.event_sentiment.summary()}"
            )
            if active:
                logger.info(f"[HEARTBEAT] Market status: {' | '.join(active)}")

        if opportunities:
            self._execute_opportunities(opportunities)

    def _execute_opportunities(self, opportunities: list[TradeOpportunity]):
        """Execute the best opportunities. Dry-run logs only; live mode places orders."""
        opportunities.sort(key=lambda o: o.edge_result.ev_net, reverse=True)

        for opp in opportunities:
            market = self._markets[opp.market_id]
            price = opp.stoikov_quote.reservation_price
            size = opp.kelly_result.position_size
            side = opp.edge_result.side          # "YES", "NO", or "BOTH"
            is_passive = opp.edge_result.is_passive
            exec_type = "GTC/MAKER" if is_passive else "FOK/TAKER"

            if self.dry_run:
                logger.info(
                    f"[DRY RUN] {opp.market_id}: {side} ${size:.2f} @ {price:.4f} "
                    f"| EV={opp.edge_result.ev_net:.4f} | Kelly f={opp.kelly_result.f_kelly:.4f} "
                    f"| exec={exec_type}"
                )
                # Record opportunity for later analysis
                record_side = side if side != "BOTH" else "YES"
                self.dry_run_tracker.record(
                    market_id=opp.market_id,
                    asset=market.asset,
                    side=record_side,
                    q=opp.q,
                    p=market.last_price,
                    edge=opp.edge_result.ev_net,
                    size=size,
                    exec_price=price,
                )
                continue

            # Within-market arb: buy both YES and NO simultaneously
            if side == "BOTH":
                self._place_arb_both_sides(market, size, not is_passive)
                continue

            # Directional: BUY YES or BUY NO
            token_id = market.token_id_yes if side == "YES" else market.token_id_no
            logger.info(
                f"[LIVE] Placing order: {opp.market_id} BUY {side} ${size:.2f} @ {price:.4f} "
                f"({exec_type})"
            )
            if is_passive:
                order_id = self.executor.place_limit_order(token_id, "BUY", price, size)
            else:
                order_id = self.executor.place_fok_order(token_id, "BUY", price, size)

            if order_id:
                logger.info(f"Order accepted: {order_id}")
                self.kelly.allocate(size)
                self._save_bankroll()
                self._record_trade(opp.market_id, side, size, price, order_id)
            else:
                logger.warning(f"Order rejected for {opp.market_id}")

    def _place_arb_both_sides(self, market: MarketState, size: float, aggressive: bool):
        """Place both YES and NO legs of a within-market arbitrage."""
        half = size / 2.0
        yes_price = market.last_price
        no_price = market.last_price_no

        logger.info(
            f"[LIVE] Within-market arb {market.market_id}: "
            f"BUY YES ${half:.2f} @ {yes_price:.4f} + BUY NO ${half:.2f} @ {no_price:.4f}"
        )
        if aggressive:
            yes_id = self.executor.place_fok_order(market.token_id_yes, "BUY", yes_price, half)
            no_id = self.executor.place_fok_order(market.token_id_no, "BUY", no_price, half)
        else:
            yes_id = self.executor.place_limit_order(market.token_id_yes, "BUY", yes_price, half)
            no_id = self.executor.place_limit_order(market.token_id_no, "BUY", no_price, half)

        if yes_id and no_id:
            logger.info(f"Both legs accepted: YES={yes_id}, NO={no_id}")
            self.kelly.allocate(size)
            self._save_bankroll()
        elif yes_id or no_id:
            logger.warning(
                f"Partial arb fill for {market.market_id}: "
                f"YES={yes_id}, NO={no_id} — directional exposure!"
            )
            self.kelly.allocate(half)
            self._save_bankroll()
        else:
            logger.warning(f"Both legs rejected for {market.market_id}")

    def _estimate_remaining_time(self, state: MarketState) -> float:
        """
        Estimate (T-t) as a normalized value [0, 1].
        Uses the market's end_time if available; falls back to 0.5.
        """
        if state.end_time <= 0:
            return 0.5
        now = time.time()
        window = 5 * 60  # 5-minute markets assumed
        remaining = state.end_time - now
        if remaining <= 0:
            return 0.0
        return min(1.0, remaining / window)

    def _record_trade(self, market_id: str, side: str, size: float, price: float, order_id: str):
        """Append a trade record to trades.json for the dashboard."""
        try:
            trades = []
            if TRADES_FILE.exists():
                trades = json.loads(TRADES_FILE.read_text())
            trades.append({
                "time": time.strftime("%H:%M:%S"),
                "market": market_id,
                "side": side,
                "size": round(size, 4),
                "price": round(price, 4),
                "order_id": order_id,
                "pnl": 0.0,  # Updated when market resolves
            })
            # Keep last 500 trades
            TRADES_FILE.write_text(json.dumps(trades[-500:]))
        except Exception as e:
            logger.warning(f"Could not record trade: {e}")

    def stop(self):
        self._running = False
        self._save_bankroll()
        logger.info("Bot stopped.")
