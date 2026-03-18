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
"""

import time
import logging
from dataclasses import dataclass, field

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
)

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


@dataclass
class TradeOpportunity:
    market_id: str
    edge_result: EdgeResult
    spread_signal: SpreadSignal | None
    stoikov_quote: StoikovQuote
    kelly_result: KellyResult
    timestamp: float = field(default_factory=time.time)


class ArbitrageBot:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.data_client = PolymarketDataClient()
        self.price_feed = PriceFeed()
        self.edge_model = EdgeModel()
        self.spread_map = SpreadMap()
        self.kelly = KellyModel(bankroll=BANKROLL)
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

    def validate_with_monte_carlo(self) -> bool:
        logger.info("Running Monte Carlo validation...")
        result = self.mc.run(
            base_ev=MIN_EDGE,
            base_win_rate=0.55,
            avg_position_fraction=0.25,
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
    ):
        state = MarketState(
            market_id=market_id,
            token_id_yes=token_id_yes,
            token_id_no=token_id_no,
            asset=asset,
            timeframe=timeframe,
            bayesian=BayesianModel(market_id),
            stoikov=StoikovModel(),
        )
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
            markets = gamma.find_crypto_markets(asset)  # uses default 5-min keyword variants
            if not markets:
                # Fallback: broader keyword search
                markets = gamma.find_crypto_markets(asset)
            if not markets:
                # Last resort: CLOB-based search
                logger.info(f"Gamma empty, falling back to CLOB for {asset}...")
                markets = self.data_client.find_crypto_5min_markets(asset)

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
                self.register_market(
                    market_id=market_id,
                    token_id_yes=yes_token,
                    token_id_no=no_token,
                    asset=asset,
                    timeframe="5m",
                )
                registered += 1

        logger.info(f"Auto-discovery complete: {registered} markets registered")
        return registered

    def run(self):
        if not self.validate_with_monte_carlo():
            logger.warning("Monte Carlo validation failed. Strategy may not be viable. Continuing anyway.")

        logger.info(f"Starting arbitrage bot main loop (mode: {'DRY RUN' if self.dry_run else 'LIVE'})...")
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

        # --- 1. Fetch crypto spot prices ---
        new_prices = self.price_feed.fetch()

        opportunities: list[TradeOpportunity] = []

        for market_id, state in self._markets.items():
            # --- 2. Fetch market prices from Polymarket ---
            p_yes = self.data_client.get_mid_price(state.token_id_yes)
            p_no = self.data_client.get_mid_price(state.token_id_no)
            if p_yes is None or p_no is None:
                continue

            ob_imbalance = self.data_client.get_order_book_imbalance(state.token_id_yes)
            ob_depth = self.data_client.get_order_book_depth(state.token_id_yes)

            # Determine crypto symbol from asset (HYPE not on Binance)
            crypto_symbol = f"{state.asset}USDT"
            has_spot_price = crypto_symbol in new_prices

            # --- 3. Bayesian update ---
            bayesian_data = self.price_feed.build_bayesian_data(
                symbol=crypto_symbol if has_spot_price else None,
                new_prices=new_prices,
                elapsed_seconds=elapsed,
                ob_imbalance=ob_imbalance,
            )
            q = state.bayesian.update(bayesian_data)

            # --- 4. Edge check ---
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
            side = opp.edge_result.side          # "BUY" or "SELL"
            price = opp.stoikov_quote.reservation_price
            size = opp.kelly_result.position_size
            token_id = self._markets[opp.market_id].token_id_yes

            if self.dry_run:
                logger.info(
                    f"[DRY RUN] {opp.market_id}: {side} ${size:.2f} @ {price:.4f} "
                    f"| EV={opp.edge_result.ev_net:.4f} | Kelly f={opp.kelly_result.f_kelly:.4f}"
                )
            else:
                logger.info(
                    f"[LIVE] Placing order: {opp.market_id} {side} ${size:.2f} @ {price:.4f}"
                )
                order_id = self.executor.place_limit_order(
                    token_id=token_id,
                    side=side,
                    price=price,
                    size=size,
                )
                if order_id:
                    logger.info(f"Order accepted: {order_id}")
                else:
                    logger.warning(f"Order rejected for {opp.market_id}")

    def _estimate_remaining_time(self, state: MarketState) -> float:
        """
        Estimate (T-t) as a normalized value [0, 1].
        For 5-minute markets, if we're at minute 4, remaining = 0.2.
        Without actual market close data, default to 0.5.
        """
        return 0.5  # placeholder; integrate with Polymarket end_date_iso

    def stop(self):
        self._running = False
        logger.info("Bot stopped.")
