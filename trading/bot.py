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

        self._markets: dict[str, MarketState] = {}
        self._running = False
        self._last_price_fetch = 0.0
        self._last_prices: dict[str, float] = {}

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
        Fetch active crypto markets via Gamma API (preferred) and register them.
        Falls back to CLOB-based discovery if Gamma returns nothing.
        """
        gamma = GammaClient()
        assets = POLYMARKET_ASSETS  # BTC, ETH, SOL, XRP, DOGE, BNB, HYPE
        registered = 0

        for asset in assets:
            logger.info(f"Discovering {asset} markets via Gamma API...")
            # find_crypto_markets with no args: tries 5-min keywords, auto-falls back to any match
            markets = gamma.find_crypto_markets(asset)
            if not markets:
                # Pass explicit None to skip keyword filter entirely
                markets = gamma.find_crypto_markets(asset, keywords=None)
            if not markets:
                logger.info(f"Gamma empty, falling back to CLOB for {asset}...")
                markets = self.data_client.find_crypto_5min_markets(asset)
            if not markets:
                logger.warning(f"No markets found for {asset} via any source — skipping")

            for m in markets[:3]:  # max 3 per asset
                tokens = m.get("tokens", [])
                if len(tokens) < 2:
                    continue
                yes_token = next(
                    (t["token_id"] for t in tokens if t.get("outcome", "").upper() == "YES"), None
                )
                no_token = next(
                    (t["token_id"] for t in tokens if t.get("outcome", "").upper() == "NO"), None
                )
                if not yes_token or not no_token:
                    continue
                condition_id = m.get("condition_id", "")
                market_id = f"{asset}_5m_{condition_id[:8]}"

                end_time = 0.0
                end_date = m.get("end_date_iso") or m.get("endDate") or m.get("close_time")
                if end_date:
                    try:
                        import datetime
                        dt = datetime.datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                        end_time = dt.timestamp()
                    except Exception:
                        pass

                self.register_market(
                    market_id=market_id,
                    token_id_yes=yes_token,
                    token_id_no=no_token,
                    asset=asset,
                    timeframe="5m",
                    end_time=end_time,
                )
                registered += 1

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

        # Apply growth tier (adjusts Kelly λ and MIN_EDGE based on bankroll)
        self._apply_growth_tier()

        # --- 1. Fetch crypto spot prices ---
        new_prices = self.price_feed.fetch()

        opportunities: list[TradeOpportunity] = []

        for market_id, state in self._markets.items():
            # --- 2. Fetch market prices from Polymarket ---
            yes_data = self.data_client.get_book_data(state.token_id_yes)
            no_data = self.data_client.get_book_data(state.token_id_no)
            p_yes = yes_data["mid_price"]
            p_no = no_data["mid_price"]
            if p_yes is None or p_no is None:
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

            # --- 4. Edge check (uses dynamic MIN_EDGE from current tier) ---
            edge_result = self.edge_model.evaluate_directional(q=q, p=p_yes)

            # Also check within-market arbitrage
            within_result = self.edge_model.evaluate_within_market(p_yes, p_no)
            if within_result.has_edge and within_result.ev_net > edge_result.ev_net:
                edge_result = within_result

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
