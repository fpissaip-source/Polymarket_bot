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
from data.market_data import PolymarketDataClient, GammaClient
from data.wallet_tracker import WalletTracker
from data.sentiment_analyzer import GeminiSentimentAnalyzer
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
    asset: str              # "BTC", "ETH", etc.
    timeframe: str          # "5m", "15m"
    bayesian: BayesianModel = field(default_factory=BayesianModel)
    stoikov: StoikovModel = field(default_factory=StoikovModel)
    last_price: float = 0.5
    last_price_no: float = 0.5
    end_time: float = 0.0   # Unix timestamp when market closes (0 = unknown)
    gamma_price_yes: float | None = None  # Fallback price from Gamma API
    gamma_price_no: float | None = None


@dataclass
class TradeOpportunity:
    market_id: str
    edge_result: EdgeResult
    spread_signal: SpreadSignal | None
    stoikov_quote: StoikovQuote
    kelly_result: KellyResult
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
        self.sentiment = GeminiSentimentAnalyzer()

        self._markets: dict[str, MarketState] = {}
        self._running = False
        self._last_price_fetch = 0.0
        self._last_prices: dict[str, float] = {}
        self._last_heartbeat = 0.0
        self._tick_count = 0

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

    @staticmethod
    def _extract_tokens(m: dict) -> tuple[str | None, str | None]:
        """
        Extract YES and NO token IDs from a market dict.
        Handles multiple Gamma/CLOB API response formats including
        cases where list fields are returned as JSON strings.
        """
        import json as _json

        def _parse_list(val):
            """Parse value to list – handles both actual lists and JSON strings."""
            if isinstance(val, list):
                return val
            if isinstance(val, str):
                try:
                    parsed = _json.loads(val)
                    if isinstance(parsed, list):
                        return parsed
                except Exception:
                    pass
            return []

        # Outcomes that map to YES (Up/High side)
        YES_OUTCOMES = {"YES", "1", "UP", "HOCH", "HIGH", "OVER", "ABOVE", "TRUE"}
        # Outcomes that map to NO (Down/Low side)
        NO_OUTCOMES = {"NO", "0", "DOWN", "RUNTER", "LOW", "UNDER", "BELOW", "FALSE"}

        # Format 1: tokens list with outcome field (CLOB format)
        tokens = _parse_list(m.get("tokens", []))
        if tokens and isinstance(tokens[0], dict):
            yes = next((t.get("token_id") for t in tokens
                        if t.get("outcome", "").upper() in YES_OUTCOMES), None)
            no = next((t.get("token_id") for t in tokens
                       if t.get("outcome", "").upper() in NO_OUTCOMES), None)
            if yes and no:
                return yes, no

        # Format 2: clobTokenIds list + outcomes list (Gamma format)
        # NOTE: Gamma often returns these as JSON strings, not real lists!
        clob_ids = _parse_list(m.get("clobTokenIds", []))
        outcomes = _parse_list(m.get("outcomes", []))
        if clob_ids and len(clob_ids) >= 2:
            if outcomes and len(outcomes) >= 2:
                yes = no = None
                for i, outcome in enumerate(outcomes):
                    if str(outcome).upper() in YES_OUTCOMES and i < len(clob_ids):
                        yes = clob_ids[i]
                    elif str(outcome).upper() in NO_OUTCOMES and i < len(clob_ids):
                        no = clob_ids[i]
                if yes and no:
                    return yes, no
            # Fallback: first = YES, second = NO
            return str(clob_ids[0]), str(clob_ids[1])

        # Format 3: token_id_yes / token_id_no directly
        yes = m.get("token_id_yes") or m.get("tokenIdYes")
        no = m.get("token_id_no") or m.get("tokenIdNo")
        if yes and no:
            return str(yes), str(no)

        return None, None

    def auto_discover_markets(self):
        """
        Fetch active crypto markets via Gamma API (preferred) and register them.
        Falls back to CLOB-based discovery if Gamma returns nothing.
        Handles multiple Gamma/CLOB API response formats robustly.
        """
        import datetime
        gamma = GammaClient()
        assets = POLYMARKET_ASSETS  # BTC, ETH, SOL, XRP, DOGE, BNB, HYPE
        registered = 0

        for asset in assets:
            logger.info(f"Discovering {asset} markets via Gamma API...")
            markets = gamma.find_crypto_markets(asset)
            if not markets:
                markets = gamma.find_crypto_markets(asset, keywords=None)
            if not markets:
                logger.info(f"Gamma empty, falling back to CLOB for {asset}...")
                markets = self.data_client.find_crypto_5min_markets(asset)
            if not markets:
                logger.warning(f"No markets found for {asset} — skipping")
                continue

            logger.debug(f"{asset}: {len(markets)} candidates, first keys: {list(markets[0].keys())}")

            registered_for_asset = 0
            for m in markets:
                if registered_for_asset >= 3:  # max 3 per asset
                    break

                # Condition ID from various field names
                condition_id = (m.get("conditionId") or m.get("condition_id") or
                                m.get("id") or "unknown")

                # Try to get authoritative token IDs directly from CLOB
                yes_token, no_token = None, None
                if condition_id and condition_id != "unknown":
                    clob_market = self.data_client.get_market(condition_id)
                    if clob_market:
                        yes_token, no_token = self._extract_tokens(clob_market)

                # Fall back to Gamma token IDs if CLOB lookup failed
                if not yes_token or not no_token:
                    yes_token, no_token = self._extract_tokens(m)

                if not yes_token or not no_token:
                    logger.warning(
                        f"{asset}: Could not extract tokens from market. "
                        f"Keys available: {list(m.keys())}"
                    )
                    continue
                market_id = f"{asset}_5m_{str(condition_id)[:8]}"

                # End time
                end_time = 0.0
                end_date = (m.get("endDate") or m.get("end_date_iso") or
                            m.get("closeTime") or m.get("close_time") or
                            m.get("expirationTime") or m.get("expiration"))
                if end_date:
                    try:
                        dt = datetime.datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
                        end_time = dt.timestamp()
                    except Exception:
                        pass

                # Extract Gamma prices as CLOB fallback
                gamma_price_yes, gamma_price_no = None, None
                import json as _json
                raw_prices = m.get("outcomePrices", [])
                if isinstance(raw_prices, str):
                    try:
                        raw_prices = _json.loads(raw_prices)
                    except Exception:
                        raw_prices = []
                if isinstance(raw_prices, list) and len(raw_prices) >= 2:
                    try:
                        gamma_price_yes = float(raw_prices[0])
                        gamma_price_no = float(raw_prices[1])
                    except Exception:
                        pass

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

        logger.info(f"Auto-discovery complete: {registered} markets registered")
        return registered

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

        # --- Wallet tracker update (every 60s) ---
        if now - self._last_wallet_update >= 60:
            self.wallet_tracker.update()
            logger.debug(self.wallet_tracker.summary())
            self._last_wallet_update = now

        opportunities: list[TradeOpportunity] = []
        market_stats = []  # collect for heartbeat

        for market_id, state in self._markets.items():
            # --- 2. Fetch market prices from Polymarket ---
            yes_data = self.data_client.get_book_data(state.token_id_yes)
            no_data = self.data_client.get_book_data(state.token_id_no)
            p_yes = yes_data["mid_price"]
            p_no = no_data["mid_price"]
            # Fallback: use Gamma API prices if CLOB has no data
            if p_yes is None and state.gamma_price_yes is not None:
                p_yes = state.gamma_price_yes
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

            ob_imbalance = yes_data["imbalance"]
            ob_depth = yes_data["depth"]

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

            # --- 3c. Gemini sentiment boost (cached, no delay) ---
            price_change_pct = 0.0
            if crypto_symbol in self._last_prices and crypto_symbol in new_prices:
                old_p = self._last_prices[crypto_symbol]
                if old_p > 0:
                    price_change_pct = (new_prices[crypto_symbol] - old_p) / old_p * 100
            # Trigger async refresh (returns immediately, updates cache in background)
            self.sentiment.update_async(
                asset=state.asset,
                current_price=new_prices.get(crypto_symbol, 0.0),
                price_change_pct=price_change_pct,
                volatility=volatility,
            )
            sentiment_boost = self.sentiment.get_boost(state.asset)
            if sentiment_boost != 0.0:
                q = max(0.01, min(0.99, q + sentiment_boost))
                logger.debug(f"[{market_id}] Gemini boost={sentiment_boost:+.3f} → q={q:.3f}")

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
            exec_prob = 0.9 if not stoikov_quote.is_aggressive else 0.7
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
                f"bankroll=${self.kelly.bankroll:.2f} | {self.sentiment.summary()}"
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
            is_aggressive = opp.stoikov_quote.is_aggressive

            if self.dry_run:
                logger.info(
                    f"[DRY RUN] {opp.market_id}: {side} ${size:.2f} @ {price:.4f} "
                    f"| EV={opp.edge_result.ev_net:.4f} | Kelly f={opp.kelly_result.f_kelly:.4f} "
                    f"| exec={'FOK' if is_aggressive else 'GTC'}"
                )
                continue

            # Within-market arb: buy both YES and NO simultaneously
            if side == "BOTH":
                self._place_arb_both_sides(market, size, is_aggressive)
                continue

            # Directional: BUY YES or BUY NO
            token_id = market.token_id_yes if side == "YES" else market.token_id_no
            logger.info(
                f"[LIVE] Placing order: {opp.market_id} BUY {side} ${size:.2f} @ {price:.4f} "
                f"({'FOK' if is_aggressive else 'GTC'})"
            )
            if is_aggressive:
                order_id = self.executor.place_fok_order(token_id, "BUY", price, size)
            else:
                order_id = self.executor.place_limit_order(token_id, "BUY", price, size)

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
