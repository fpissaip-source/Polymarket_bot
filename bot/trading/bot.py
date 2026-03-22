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
from dataclasses import dataclass, field, replace as dc_replace
from pathlib import Path

from models.bayesian import BayesianModel
from models.edge import EdgeModel, EdgeResult
from models.spread import SpreadMap, SpreadSignal
from models.stoikov import StoikovModel, StoikovQuote
from models.kelly import KellyModel, KellyResult
from models.monte_carlo import MonteCarloSimulator
from models.regime import RegimeDetector
from models.ofi import OFIModel
from models.gas_optimizer import GasOptimizer

from data.price_feed import PriceFeed
from data.market_data import PolymarketDataClient, GammaClient, DataApiClient, extract_clob_tokens, extract_gamma_prices
from data.wallet_tracker import WalletTracker
from data.sentiment_analyzer import EventSentimentAnalyzer
from data.weather_feed import WeatherFeed
from data.dry_run_tracker import DryRunTracker
from trading.order_executor import OrderExecutor, fetch_live_balance_usd
from models.adaptive import AdaptiveLearner
from models.rewards import is_rewarded, get_rewarded_condition_ids

from config import (
    POLL_INTERVAL_SECONDS,
    RELATED_MARKETS,
    BANKROLL,
    DRY_RUN_BANKROLL,
    MIN_EDGE,
    TOTAL_COST,
    CRYPTO_SYMBOLS,
    POLYMARKET_ASSETS,
    GROWTH_TIERS,
    BANKROLL_STATE_FILE,
    TP_RATIO,
    SL_RATIO,
    TP_RATIO_5MIN,
    SL_RATIO_5MIN,
    TP_RATIO_LOW,
    SL_RATIO_LOW,
    LOW_PRICE_THRESHOLD,
    TP_SL_CHECK_INTERVAL,
    TP_MIN_TIME_REMAINING,
    TP_NEAR_RESOLVED_THRESHOLD,
    MIN_SECONDS_BEFORE_EXPIRY,
    MIN_BET_SIZE,
    MAX_OPEN_TRADES,
    MAX_TOTAL_EXPOSURE_PCT,
    MAX_POSITION_HOLD_MINUTES,
    POLYMARKET_PROXY_ADDRESS,
    GEMINI_MIN_CONFIDENCE,
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
    spot_at_start: float = 0.0  # Crypto spot price when window opened
    condition_id: str = ""  # Full conditionId for Gamma API resolution
    tick_size: str = "0.01"  # Market tick size for order pricing
    neg_risk: bool = False   # True for multi-outcome (3+) markets


@dataclass
class TradeOpportunity:
    market_id: str
    edge_result: EdgeResult
    spread_signal: SpreadSignal | None
    stoikov_quote: StoikovQuote
    kelly_result: KellyResult
    q: float = 0.0               # Bayesian probability estimate
    gemini_confidence: float = 0.0  # Gemini confidence (0–1); 0 = no data
    end_time: float = 0.0        # Market expiry (Unix); 0 = unknown
    best_ask: float = 0.0        # Best ask price for the traded token (used for GTC limit orders)
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

        starting_bankroll = DRY_RUN_BANKROLL if dry_run else self._load_bankroll()

        # Always try to sync bankroll from live CLOB balance
        live_balance = fetch_live_balance_usd()
        if live_balance > 0:
            if dry_run:
                starting_bankroll = live_balance
                logger.info(f"[STARTUP] Live CLOB balance=${live_balance:.2f} → using as dry-run bankroll")
            else:
                starting_bankroll = live_balance
                logger.info(
                    f"[STARTUP] CLOB balance=${live_balance:.2f} → using as live bankroll"
                )
        else:
            logger.warning(
                f"[STARTUP] Could not fetch live CLOB balance — using configured bankroll ${starting_bankroll:.2f}"
            )

        kelly_lambda, self._current_min_edge = _get_tier(starting_bankroll)
        logger.info(
            f"Starting bankroll: ${starting_bankroll:.2f} ({'VIRTUAL' if dry_run else 'LIVE'}) | "
            f"Tier: Kelly λ={kelly_lambda:.2f}, MIN_EDGE={self._current_min_edge:.1%}"
        )

        self.kelly = KellyModel(bankroll=starting_bankroll, lambda_fraction=kelly_lambda)
        self.mc = MonteCarloSimulator()
        self.executor = None if dry_run else OrderExecutor()

        if not dry_run and self.executor:
            # Preserve orders that are tracked as pending buys — cancelling them would
            # cause the position to appear as a ghost and trigger a duplicate purchase.
            pending_buy_order_ids = {
                oid for oid, pos in self._live_positions.items()
                if not pos.get("buy_filled", True) and not oid.startswith("recovered_")
            }
            if pending_buy_order_ids:
                logger.info(
                    f"[STARTUP] Preserving {len(pending_buy_order_ids)} pending-buy order(s) "
                    f"from previous session — will not cancel"
                )
            # Cancel leftover GTC orders from previous runs before anything else
            n_cancelled = self.executor.cancel_all_open_orders(skip_order_ids=pending_buy_order_ids)
            # Re-sync bankroll after cancellations free up balance
            actual_balance = self.executor.get_available_balance_usd()
            if actual_balance > 0 and actual_balance != starting_bankroll:
                logger.info(
                    f"[STARTUP] Post-cancel CLOB balance=${actual_balance:.2f} → using as bankroll"
                )
                self.kelly.bankroll = actual_balance
                starting_bankroll = actual_balance
            elif actual_balance == 0:
                logger.warning(
                    f"[STARTUP] CLOB balance=$0 after cancelling {n_cancelled} orders. "
                    f"Check your proxy wallet has USDC.e."
                )

        # Persist the live-synced bankroll so the dashboard shows the correct value immediately
        if not dry_run and starting_bankroll > 0:
            try:
                with open(BANKROLL_STATE_FILE, "w") as _f:
                    json.dump({"bankroll": round(starting_bankroll, 4)}, _f)
            except Exception:
                pass

        self.wallet_tracker = WalletTracker()
        self._last_wallet_update = 0.0
        self.event_sentiment = EventSentimentAnalyzer()
        self.weather_feed = WeatherFeed()
        self.adaptive = AdaptiveLearner()
        self.regime = RegimeDetector()
        self.ofi_model = OFIModel()
        self.gas_optimizer = GasOptimizer()

        self._markets: dict[str, MarketState] = {}
        self._running = False
        self._last_price_fetch = 0.0
        self._last_prices: dict[str, float] = {}
        self._last_heartbeat = 0.0
        self._last_market_refresh = 0.0
        self._last_stats_log = 0.0
        self._last_adaptive_update = 0.0
        self._last_tp_sl_check = 0.0
        self._last_allowance_refresh = 0.0
        self._tick_count = 0
        # Live position tracking for TP/SL: order_id → position dict
        self._live_positions: dict[str, dict] = {}
        self._load_live_positions()  # Restore positions from previous session
        self.dry_run_tracker = DryRunTracker(virtual_bankroll=starting_bankroll)
        self._tier_base_lambda = self.kelly.lambda_fraction
        self._tier_base_edge = self._current_min_edge
        self._MARKET_REFRESH_INTERVAL = 30
        self._MARKET_WINDOW_MIN = MIN_SECONDS_BEFORE_EXPIRY
        self._MARKET_WINDOW_MAX = 930  # 15min + 30s buffer

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
    # Live position persistence (survives restarts)
    # ------------------------------------------------------------------
    LIVE_POSITIONS_FILE = "live_positions.json"

    def _save_live_positions(self):
        try:
            with open(self.LIVE_POSITIONS_FILE, "w") as f:
                json.dump(self._live_positions, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save live positions: {e}")

    def _load_live_positions(self):
        if not os.path.exists(self.LIVE_POSITIONS_FILE):
            return
        try:
            with open(self.LIVE_POSITIONS_FILE) as f:
                loaded = json.load(f)
        except Exception as e:
            logger.warning(f"Could not load live positions: {e}")
            return

        # Step 1: Drop expired entries (end_time past by >60s)
        now = time.time()
        active = {oid: pos for oid, pos in loaded.items()
                  if pos.get("end_time", 0) == 0 or pos["end_time"] > now - 60}

        # Step 2: Reconcile against actual on-chain token balances
        # Prevents ghost positions when bot was restarted after manual/external closure
        try:
            data_api = DataApiClient()
            real_positions = data_api.get_positions(POLYMARKET_PROXY_ADDRESS)
            # Build set of token_ids with a meaningful balance (>= 0.01 shares)
            real_token_ids: set[str] = set()
            for rp in real_positions:
                tid = str(rp.get("asset") or rp.get("token_id") or rp.get("tokenId") or "")
                size = float(rp.get("size") or rp.get("balance") or 0)
                if tid and size >= 0.01:
                    real_token_ids.add(tid)
            logger.info(f"[STARTUP] On-chain positions: {len(real_token_ids)} token(s) with balance")

            purged = []
            reconciled = {}
            for oid, pos in active.items():
                tid = pos.get("token_id", "")
                if tid and tid not in real_token_ids:
                    purged.append(pos.get("market_id", oid[:8]))
                    logger.warning(
                        f"[STARTUP] Ghost position purged: {pos.get('market_id', oid[:8])} "
                        f"— token {tid[:16]}... has no on-chain balance"
                    )
                else:
                    reconciled[oid] = pos
            if purged:
                logger.warning(
                    f"[STARTUP] Purged {len(purged)} ghost position(s): {purged} "
                    f"— these existed in live_positions.json but had no real token balance"
                )
            active = reconciled

            # Step 3: Recover orphaned on-chain positions not in live_positions.json.
            # This handles restarts where the bot placed + filled orders but crashed
            # before saving the position, or when positions were opened externally.
            tracked_token_ids = {pos.get("token_id", "") for pos in active.values()}
            for rp in real_positions:
                tid = str(rp.get("asset") or rp.get("token_id") or rp.get("tokenId") or "")
                rp_size = float(rp.get("size") or rp.get("balance") or 0)
                if not tid or rp_size < 4.5 or tid in tracked_token_ids:
                    continue
                # Orphaned position found — create a minimal tracking entry
                rp_price = float(rp.get("avgPrice") or rp.get("avg_price") or rp.get("price") or 0)
                rp_market = str(rp.get("market") or rp.get("condition_id") or rp.get("conditionId") or "")
                rp_side = str(rp.get("outcome") or rp.get("side") or "YES")
                if rp_price <= 0:
                    rp_price = 0.50  # Fallback — we don't know the real entry price
                synthetic_id = f"recovered_{tid[:16]}_{int(time.time())}"
                active[synthetic_id] = {
                    "market_id": rp_market or f"unknown_{tid[:12]}",
                    "side": rp_side,
                    "token_id": tid,
                    "entry_price": rp_price,
                    "entry_size": round(rp_size * rp_price, 4),
                    "shares": rp_size,
                    "tick_size": "0.01",
                    "neg_risk": False,
                    "entry_time": time.time(),
                    "end_time": 0,
                    "sl_order_id": "",
                    "buy_filled": True,  # Already on-chain = already filled
                    "tp_order_id": "",
                    "shares_confirmed": True,
                    "fill_confirmed_at": time.time() - 30,  # Skip settlement cooldown
                }
                logger.warning(
                    f"[STARTUP] Recovered orphaned position: {rp_market or tid[:16]} "
                    f"{rp_side} {rp_size:.2f} shares @ {rp_price:.4f} — now tracked for TP/SL"
                )
        except Exception as e:
            logger.warning(f"[STARTUP] Position reconciliation failed (keeping all tracked): {e}")

        self._live_positions = active
        if active:
            for pos in active.values():
                self.kelly.allocate(pos.get("entry_size", 0.0))
            committed = sum(p.get("entry_size", 0) for p in active.values())
            logger.info(
                f"[STARTUP] Restored {len(active)} live positions: "
                + ", ".join(f"{p['market_id']}" for p in active.values())
                + f" | committed_capital = ${committed:.2f}"
            )
        else:
            logger.info("[STARTUP] No active positions — bot starts with clean slate")

        # Persist reconciled state immediately
        self._save_live_positions()

    # ------------------------------------------------------------------
    # Dynamic tier adjustment
    # ------------------------------------------------------------------
    def _apply_growth_tier(self):
        """Adjust Kelly lambda and MIN_EDGE based on current bankroll.
        Always stores the tier base values for adaptive to build on."""
        kelly_lambda, min_edge = _get_tier(self.kelly.bankroll)
        old_lambda = self._tier_base_lambda
        old_edge = self._tier_base_edge
        self._tier_base_lambda = kelly_lambda
        self._tier_base_edge = min_edge
        self.kelly.lambda_fraction = kelly_lambda
        self._current_min_edge = min_edge
        self.edge_model.min_edge = min_edge
        if abs(kelly_lambda - old_lambda) > 0.001 or abs(min_edge - old_edge) > 0.0001:
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
        condition_id: str = "",
        neg_risk: bool = False,
        tick_size: str = "0.01",
        question: str = "",
        is_event: bool = False,
    ):
        spot = 0.0
        if hasattr(self, '_last_prices') and asset.upper() in (self._last_prices or {}):
            spot = self._last_prices[asset.upper()]
        elif hasattr(self, 'price_feed'):
            try:
                prices = self.price_feed.fetch()
                spot = prices.get(asset.upper(), 0.0)
            except Exception:
                pass
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
            spot_at_start=spot,
            condition_id=condition_id,
            neg_risk=neg_risk,
            tick_size=tick_size,
            question=question,
            is_event=is_event,
        )
        # Auto-register spread pairs: any two markets with the same asset
        for existing_id, existing in self._markets.items():
            if existing.asset == asset:
                self.spread_map.register_pair(existing_id, market_id)
                logger.info(f"Spread pair registered: {existing_id} <-> {market_id}")
        self._markets[market_id] = state
        logger.info(
            f"Registered market: {market_id} ({asset} {timeframe}) "
            f"neg_risk={neg_risk} tick={tick_size}"
        )

    def auto_discover_markets(self):
        """
        Discover high-volume event/prediction markets via Gamma API.
        No crypto time markets — this bot trades question predictions only.
        Gemini estimates the true probability; divergence from market price = edge.
        """
        import datetime
        from config import EVENT_MARKET_LIMIT, EVENT_MARKET_MIN_VOLUME
        gamma = GammaClient()
        registered = 0

        logger.info("Discovering event/question markets via Gamma API...")
        try:
            markets = gamma.discover_event_markets(limit=EVENT_MARKET_LIMIT)
        except Exception as e:
            logger.error(f"discover_event_markets failed: {e}")
            markets = []

        for m in markets:
            condition_id = (m.get("conditionId") or m.get("condition_id") or
                            m.get("id") or "unknown")

            # Volume quality filter
            volume = float(m.get("volume", m.get("volumeNum", 0)) or 0)
            if volume < EVENT_MARKET_MIN_VOLUME:
                continue

            yes_token, no_token = extract_clob_tokens(m)
            if not yes_token or not no_token:
                continue

            condition_id_str = str(condition_id)
            market_id = f"evt_{condition_id_str[:10]}"
            if market_id in self._markets:
                continue
            # Dedup by token ID: same YES token already registered under a different market_id
            if any(s.token_id_yes == yes_token or s.token_id_no == no_token
                   for s in self._markets.values()):
                continue

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
            market_neg_risk = bool(m.get("negRisk", m.get("neg_risk", False)))
            market_tick_size = str(m.get("minimumTickSize", m.get("minimum_tick_size", "0.01")))
            if market_tick_size not in ("0.1", "0.01", "0.001", "0.0001"):
                market_tick_size = "0.01"

            question_text = m.get("question", "")
            days_left = round((end_time - now_ts) / 86400, 1) if end_time > 0 else "?"
            logger.info(
                f"[EVENT] registered {market_id} | vol=${volume:.0f} | "
                f"{days_left}d left | q='{question_text[:60]}'"
            )
            self.register_market(
                market_id=market_id,
                token_id_yes=yes_token,
                token_id_no=no_token,
                asset="EVENT",
                timeframe="event",
                end_time=end_time,
                gamma_price_yes=gamma_price_yes,
                gamma_price_no=gamma_price_no,
                condition_id=condition_id_str,
                neg_risk=market_neg_risk,
                tick_size=market_tick_size,
                question=question_text,
                is_event=True,
            )
            registered += 1

        logger.info(f"Auto-discovery complete: {registered} event markets registered")
        self._last_market_refresh = time.time()
        return registered

    def _check_tp_sl(self, now: float):
        # ── Dry-run mode: virtual P&L tracking ──────────────────────────
        if self.dry_run:
            open_entries = self.dry_run_tracker.get_open_entries()
            if not open_entries:
                return
            for entry in open_entries:
                market = self._markets.get(entry.market_id)
                if not market:
                    continue
                try:
                    if entry.side == "YES":
                        data = self.data_client.get_book_data(market.token_id_yes)
                    else:
                        data = self.data_client.get_book_data(market.token_id_no)
                    current_price = data["mid_price"]
                    if current_price is None:
                        continue
                except Exception:
                    continue
                shares = entry.size / entry.exec_price if entry.exec_price > 0 else 0
                current_value = shares * current_price
                unrealized_pnl = current_value - entry.size
                pnl_ratio = unrealized_pnl / entry.size if entry.size > 0 else 0

                # 3-phase exit logic (mirrors live mode)
                time_to_expiry = market.end_time - now if market.end_time > 0 else float("inf")
                from decimal import Decimal
                _cp_dec = Decimal(str(round(current_price, 6)))

                if time_to_expiry < 7_200:
                    _phase = "ENDSPURT"
                elif _cp_dec >= Decimal("0.98"):
                    _phase = "RECYCLING"
                else:
                    _phase = "GROWTH"

                exit_reason = None
                if _phase == "RECYCLING":
                    exit_reason = f"RECYCLING(price={current_price:.4f}≥0.98)"
                elif _phase == "GROWTH" and pnl_ratio >= TP_RATIO:
                    exit_reason = f"TAKE_PROFIT({pnl_ratio:+.1%})"

                if exit_reason:
                    self.dry_run_tracker.early_exit(
                        entry.trade_id, current_price, exit_reason
                    )
                    self.kelly.bankroll = self.dry_run_tracker.virtual_bankroll
            return

        # ── Live mode: real order cancellation / sell ────────────────────
        if not self._live_positions:
            return
        to_remove = []
        for order_id, pos in list(self._live_positions.items()):
            token_id   = pos["token_id"]
            entry_price = pos["entry_price"]
            entry_size  = pos["entry_size"]
            tick_size   = pos.get("tick_size", "0.01")
            neg_risk    = pos.get("neg_risk", False)
            market_id   = pos.get("market_id", "")

            # ── Resolve end_time: stored value, live market dict, or 0 ──
            end_time = pos.get("end_time", 0)
            if end_time == 0:
                market_state = self._markets.get(market_id)
                if market_state and market_state.end_time > 0:
                    end_time = market_state.end_time
                    pos["end_time"] = end_time   # update in-memory so it stays resolved

            time_to_expiry = end_time - now if end_time > 0 else float("inf")

            # ── Stale ghost position: market expired >2 min ago, release capital ──
            if end_time > 0 and now > end_time + 120:
                logger.warning(
                    f"[GHOST_POSITION] {market_id} order={order_id[:8]} expired "
                    f"{(now - end_time)/60:.1f} min ago — releasing ${entry_size:.2f} capital"
                )
                to_remove.append(order_id)
                self.kelly.release(entry_size)
                self._save_bankroll()
                continue

            # ── Fill detection: wait for buy to fill before placing SL bracket ──────
            # Old positions without buy_filled field are assumed already filled (True).
            buy_filled = pos.get("buy_filled", True)
            if not buy_filled:
                filled_shares = self.executor.get_order_fills(order_id)

                # ── CTF balance fallback: CLOB API sometimes returns size_matched=0
                # even though tokens have been delivered to the wallet. Check on-chain
                # balance directly to avoid missing fills and orphaning positions.
                if filled_shares <= 0:
                    try:
                        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                        _fb_params = BalanceAllowanceParams(
                            asset_type=AssetType.CONDITIONAL, token_id=token_id
                        )
                        _fb_data = self.executor.client.get_balance_allowance(_fb_params)
                        _fb_bal = float(_fb_data.get("balance", "0") or "0") / 1_000_000
                        if _fb_bal >= 4.5:  # ~5 share CLOB minimum, allow small rounding
                            logger.info(
                                f"[FILL_FALLBACK] {market_id} CLOB API says 0 fills but "
                                f"CTF balance={_fb_bal:.2f} shares — treating as filled"
                            )
                            filled_shares = _fb_bal
                    except Exception as _fb_e:
                        logger.debug(f"[FILL_FALLBACK] Balance check failed: {_fb_e}")

                if filled_shares <= 0:
                    # Order not yet filled — check if it was cancelled without a fill
                    try:
                        order_info = self.executor.client.get_order(order_id)
                        order_status = order_info.get("status", "")
                        if order_status in ("CANCELED", "EXPIRED") and filled_shares == 0:
                            logger.warning(
                                f"[FILL_CHECK] {market_id} order={order_id[:8]} "
                                f"status={order_status} with 0 fills — releasing position"
                            )
                            to_remove.append(order_id)
                            self.kelly.release(entry_size)
                            self._save_bankroll()
                            continue
                    except Exception:
                        pass
                    # Fill-wait timeout: after >2 min cancel the order and release capital.
                    # Do NOT assume filled — attempting to sell tokens we don't own causes
                    # "not enough balance/allowance" errors on the CLOB.
                    wait_secs = time.time() - pos.get("entry_time", time.time())
                    if wait_secs > 120:
                        logger.warning(
                            f"[FILL_TIMEOUT] {market_id} order={order_id[:8]} "
                            f"waited {wait_secs:.0f}s with 0 confirmed fills — "
                            f"cancelling unfilled order and releasing ${entry_size:.2f}"
                        )
                        self.executor.cancel_order(order_id)
                        to_remove.append(order_id)
                        self.kelly.release(entry_size)
                        self._save_bankroll()
                        continue
                    elif wait_secs > 30:
                        # After 30s with no confirmed fill, start price monitoring using
                        # estimated shares. This catches TP at +179% even when the fill
                        # API lags. We can't place a SELL yet (no tokens confirmed), but
                        # we CAN cancel the unfilled BUY if price already hit SL/TP.
                        est_shares = pos.get("shares", 0)
                        if est_shares > 0:
                            try:
                                _em_data = self.data_client.get_book_data(token_id)
                                _em_price = _em_data.get("mid_price") or _em_data.get("last_price")
                                if _em_price is not None:
                                    _pnl_em = (_em_price - entry_price) / entry_price
                                    if _pnl_em >= TP_RATIO:
                                        logger.warning(
                                            f"[EARLY_EXIT] {market_id} pnl={_pnl_em:+.1%} "
                                            f"hit TP threshold before fill confirmed — "
                                            f"cancelling unfilled buy"
                                        )
                                        self.executor.cancel_order(order_id)
                                        to_remove.append(order_id)
                                        self.kelly.release(entry_size)
                                        self._save_bankroll()
                                        continue
                            except Exception:
                                pass
                        logger.debug(
                            f"[FILL_WAIT] {market_id} order={order_id[:8]} "
                            f"— waiting for fill ({wait_secs:.0f}s)"
                        )
                        continue
                    else:
                        # Still pending — skip TP/SL this cycle (no tokens to sell yet)
                        logger.debug(
                            f"[FILL_WAIT] {market_id} order={order_id[:8]} "
                            f"— waiting for fill ({wait_secs:.0f}s)"
                        )
                        continue

                # Buy confirmed filled — update position and place SL bracket now
                pos["buy_filled"] = True
                pos["fill_confirmed_at"] = now  # Track settlement cooldown

                # CLOB cache sync: tell the server to re-read our on-chain CTF balance.
                # Without this, the CLOB may reject sell orders with "not enough balance".
                try:
                    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                    self.executor.client.update_balance_allowance(
                        BalanceAllowanceParams(
                            asset_type=AssetType.CONDITIONAL, token_id=token_id
                        )
                    )
                    logger.info(f"[CLOB_SYNC] ✓ Synced CONDITIONAL balance for {token_id[:16]}...")
                except Exception as _sync_e:
                    logger.warning(f"[CLOB_SYNC] Cache sync failed: {_sync_e}")
                pos["shares"] = filled_shares
                pos["shares_confirmed"] = True  # Prevents sell path from re-querying API (which may return 0)
                # Correct entry_size to actual cost (partial fills are common).
                # Wrong entry_size causes fake SL: e.g. 1.59/5 shares filled →
                # actual cost=$0.59 but entry_size=$1.85 → pnl shows -68% at same price.
                actual_cost = round(filled_shares * entry_price, 4)
                if abs(actual_cost - entry_size) > 0.01:
                    logger.info(
                        f"[FILL_CONFIRMED] Correcting entry_size: ${entry_size:.2f} → "
                        f"${actual_cost:.2f} (filled {filled_shares:.2f} of "
                        f"{round(entry_size/entry_price,2):.2f} shares)"
                    )
                    pos["entry_size"] = actual_cost
                    entry_size = actual_cost
                    # Release over-allocated capital back to Kelly
                    over_allocated = pos.get("entry_size_original", entry_size + 0.01) - actual_cost
                    if over_allocated > 0.01:
                        self.kelly.release(over_allocated)
                pos.setdefault("entry_size_original", entry_size)
                logger.info(
                    f"[FILL_CONFIRMED] {market_id} order={order_id[:8]} "
                    f"filled={filled_shares:.2f} shares @ {entry_price:.4f} | "
                    f"actual_cost=${actual_cost:.2f}"
                )
                if filled_shares >= 5.0:
                    tp_ratio = TP_RATIO
                    tp_price = round(entry_price * (1.0 + tp_ratio), 4)

                    # No SL bracket — event markets hold to resolution.
                    logger.info(
                        f"[BRACKET] No SL (event market — hold to resolution). "
                        f"TP={tp_price:.4f} (+{tp_ratio:.0%}) monitored."
                    )

                    # TP bracket placed in background thread — avoids blocking SL monitoring.
                    # time.sleep(15+) inside TP placement was preventing SL from firing during
                    # the settlement wait window on fast-moving 5-minute markets.
                    if not pos.get("tp_order_id"):
                        import threading
                        def _place_tp_bracket(executor, tok_id, n_shares, t_price,
                                              t_size, n_risk, pos_ref, mkt_id):
                            # Wait for CTF tokens to settle on Polygon before placing TP bracket.
                            # Poll balance instead of blind sleep — avoids "not enough balance" errors.
                            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                            settled = False
                            for _settle_i in range(12):  # 12 × 5s = 60s max
                                time.sleep(5)
                                try:
                                    _cp = BalanceAllowanceParams(
                                        asset_type=AssetType.CONDITIONAL, token_id=tok_id
                                    )
                                    _cd = executor.client.get_balance_allowance(_cp)
                                    _cb = float(_cd.get("balance", "0") or "0") / 1_000_000
                                    if _cb >= n_shares * 0.90:
                                        logger.info(
                                            f"[BRACKET] CTF settled: {_cb:.2f} shares "
                                            f"after {(_settle_i+1)*5}s"
                                        )
                                        n_shares = _cb  # Use actual balance
                                        settled = True
                                        break
                                    elif _cb > 0:
                                        logger.info(
                                            f"[BRACKET] Settlement {_settle_i+1}/12: "
                                            f"CTF={_cb:.2f}/{n_shares:.2f} — waiting..."
                                        )
                                except Exception:
                                    pass
                            if not settled:
                                logger.warning(
                                    f"[BRACKET] CTF not settled after 60s — "
                                    f"skipping TP bracket, bot monitors TP manually"
                                )
                                return
                            tp_oid = None
                            for attempt in range(3):
                                tp_oid = executor.place_tp_sell_order(
                                    tok_id, n_shares, t_price,
                                    tick_size=t_size, neg_risk=n_risk,
                                )
                                if tp_oid:
                                    break
                                if attempt < 2:
                                    time.sleep(10 * (attempt + 1))
                            if tp_oid:
                                pos_ref["tp_order_id"] = tp_oid
                                logger.info(
                                    f"[BRACKET] TP placed: {tp_oid[:12]} | "
                                    f"{n_shares:.2f} shares @ {t_price:.4f} | {mkt_id}"
                                )
                            else:
                                logger.warning(f"[BRACKET] TP failed after 3 attempts — bot monitors manually")

                        threading.Thread(
                            target=_place_tp_bracket,
                            args=(self.executor, token_id, filled_shares, tp_price,
                                  tick_size, neg_risk, pos, market_id),
                            daemon=True,
                        ).start()
                        logger.info(f"[BRACKET] TP placement started in background thread")
                self._save_live_positions()

            try:
                data = self.data_client.get_book_data(token_id)
                current_price = data.get("mid_price") or data.get("last_price")
                if current_price is None:
                    raise ValueError("no mid_price or last_price")
                # Best bid = where buyers are = price we can SELL at immediately
                bids = data.get("bids", [])
                best_bid = float(bids[0]["price"]) if bids else current_price
            except Exception as e:
                # No book data = market likely closed / token invalid.
                # Purge if: expiry unknown, expiry past, or near expiry
                if end_time == 0 or time_to_expiry < 60:
                    logger.warning(
                        f"[FORCE_PURGE] {market_id} order={order_id[:8]} — "
                        f"book data error ({e}), "
                        f"{'unknown expiry' if end_time == 0 else f'{time_to_expiry:.0f}s left'}, "
                        f"releasing ${entry_size:.2f}"
                    )
                    to_remove.append(order_id)
                    self.kelly.release(entry_size)
                    self._save_bankroll()
                continue

            # Use stored share count if available (exact), else compute from size/price
            shares = pos.get("shares") or (entry_size / entry_price if entry_price > 0 else 0)
            current_value = shares * current_price
            pnl_ratio = (current_value - entry_size) / entry_size if entry_size > 0 else 0

            # ── 3-Phase exit logic ────────────────────────────────────────────
            # Phase 1 GROWTH    (>24h left): TP at +30%, no SL
            # Phase 2 ENDSPURT  (<2h left) : hold to resolution, cancel TP bracket
            # Phase 3 RECYCLING (price≥0.98): immediate exit — event factually decided
            from decimal import Decimal, ROUND_DOWN

            PHASE_ENDSPURT_SECS  = 7_200    # 2 hours
            PHASE_GROWTH_SECS    = 86_400   # 24 hours
            RECYCLING_THRESHOLD  = Decimal("0.98")
            TP_SPREAD_MAX        = 0.02     # max bid-ask spread before deferring TP sell

            _cp_dec = Decimal(str(round(current_price, 6)))
            _ep_dec = Decimal(str(round(entry_price, 6)))

            if time_to_expiry < PHASE_ENDSPURT_SECS:
                market_phase = "ENDSPURT"
            elif _cp_dec >= RECYCLING_THRESHOLD:
                market_phase = "RECYCLING"
            else:
                market_phase = "GROWTH"

            # Spread check helper (reuse already-fetched bids; asks fetched separately)
            def _spread_ok() -> bool:
                try:
                    _asks = data.get("asks", [])
                    if not bids or not _asks:
                        return True  # no book → allow (will fail gracefully at sell)
                    _bid = float(bids[0]["price"])
                    _ask = float(_asks[0]["price"])
                    _spread = _ask - _bid
                    if _spread > TP_SPREAD_MAX:
                        logger.info(
                            f"[SPREAD_GATE] {market_id} spread={_spread:.4f} > {TP_SPREAD_MAX:.2%} "
                            f"— deferring sell (illiquid)"
                        )
                        return False
                    return True
                except Exception:
                    return True

            tp = TP_RATIO
            can_tp = market_phase == "GROWTH" and pnl_ratio >= tp

            # Periodic position heartbeat (every 30 s visible in logs)
            entry_time = pos.get("entry_time", now)
            hold_minutes = (now - entry_time) / 60.0
            if int(now) % 30 == 0:
                logger.info(
                    f"[POS:{market_phase}] {market_id} {pos.get('side','')} "
                    f"entry={entry_price:.3f} now={current_price:.3f} "
                    f"pnl={pnl_ratio:+.1%} | {shares:.1f} shares | "
                    f"TP={tp:.0%} held={hold_minutes:.1f}min | "
                    f"expires={f'{time_to_expiry:.0f}s' if time_to_expiry < 9e8 else 'unknown'}"
                )

            # ── Settlement cooldown: CTF tokens need time to arrive on Polygon ──
            # Skip cooldown entirely if price moved significantly (don't miss exits).
            # The TP bracket GTC order handles selling during cooldown anyway.
            # NOTE: close_position() now has its own settlement wait, but this initial
            # cooldown avoids even attempting the sell (and wasting 30s polling) if
            # the fill just happened.
            SETTLEMENT_COOLDOWN = 15  # seconds — gives Polygon time to settle CTF tokens
            fill_confirmed_at = pos.get("fill_confirmed_at", 0)
            cooldown_remaining = SETTLEMENT_COOLDOWN - (now - fill_confirmed_at) if fill_confirmed_at > 0 else 0
            if cooldown_remaining > 0 and abs(pnl_ratio) < 0.50:
                # Only wait if price hasn't moved much — big moves need immediate action
                if int(now) % 5 == 0:
                    logger.info(
                        f"[SETTLEMENT] {market_id} waiting {cooldown_remaining:.0f}s for "
                        f"CTF token settlement before TP/SL | pnl={pnl_ratio:+.1%}"
                    )
                continue

            # ── Phase-based exit decision ─────────────────────────────────────
            reason = None

            if market_phase == "RECYCLING":
                # Phase 3: event factually decided — sell immediately at best-bid
                if _spread_ok():
                    reason = f"RECYCLING(price={current_price:.4f}≥0.98)"

            elif market_phase == "ENDSPURT":
                # Phase 2: <2h left — cancel any open TP bracket and hold to resolution
                # (oracle will pay 1.00; selling now captures 0.98 - fees = worse outcome)
                if pos.get("tp_order_id") and not pos.get("endspurt_tp_cancelled"):
                    try:
                        self.executor.cancel_order(pos["tp_order_id"])
                        pos["tp_order_id"] = ""
                        pos["endspurt_tp_cancelled"] = True
                        self._save_live_positions()
                        logger.info(
                            f"[ENDSPURT] {market_id} <2h left — cancelled TP bracket, "
                            f"holding to oracle resolution"
                        )
                    except Exception as _ec:
                        logger.warning(f"[ENDSPURT] Could not cancel TP bracket: {_ec}")
                # Force-close very close to expiry (90s) to avoid stuck positions
                if 0 < time_to_expiry <= 90:
                    reason = f"PRE_EXPIRY({time_to_expiry:.0f}s left)"

            else:
                # Phase 1: GROWTH — take profit at +30% if spread is OK
                if can_tp and _spread_ok():
                    reason = f"TAKE_PROFIT({pnl_ratio:+.1%})"

            # Hard limits regardless of phase
            if not reason and hold_minutes >= MAX_POSITION_HOLD_MINUTES:
                reason = f"MAX_HOLD({hold_minutes:.1f}min >= {MAX_POSITION_HOLD_MINUTES:.0f}min limit)"

            if reason:
                logger.info(
                    f"[{reason}] {market_id} order={order_id[:8]} "
                    f"entry={entry_price:.4f} now={current_price:.4f} pnl={pnl_ratio:+.1%} "
                    f"| {shares:.2f} shares | value=${current_value:.2f}"
                )

                # ── Handle TP bracket (only TP is placed as GTC in book) ─────────
                tp_order_id = pos.get("tp_order_id", "")
                tp_already_filled = False

                if tp_order_id:
                    tp_cancel_status = self.executor.cancel_order(tp_order_id)
                    if tp_cancel_status == "ALREADY_DONE":
                        logger.info(
                            f"[BRACKET_TP] CLOB auto-executed TP for {market_id} — "
                            f"position already sold ✓"
                        )
                        tp_already_filled = True
                    else:
                        logger.info(f"[BRACKET_TP] Cancelled TP bracket {tp_order_id[:12]}")

                if tp_already_filled:
                    to_remove.append(order_id)
                    received = shares * (entry_price * (1.0 + TP_RATIO))
                    profit = received - entry_size
                    logger.info(
                        f"[BRACKET_TP] Position closed by CLOB TP bracket: "
                        f"received=${received:.2f} (profit=${profit:.2f}) ✓"
                    )
                    self.kelly.release(entry_size)
                    self._save_bankroll()
                    continue

                cancel_status = self.executor.cancel_order(order_id)
                if cancel_status == "API_ERROR":
                    logger.warning(f"[SELL] Cancel API error — proceeding with sell anyway using estimated shares")

                # Use pos["shares"] directly after first confirmation (avoids re-querying
                # the BUY order which always returns TOTAL filled, not REMAINING after partial sells)
                if pos.get("shares_confirmed"):
                    actual_shares = shares  # Already verified + adjusted for partial sells
                    logger.info(f"[SELL] Using confirmed remaining shares={actual_shares:.2f}")
                else:
                    actual_shares = self.executor.get_order_fills(order_id)
                    if actual_shares < 0:
                        actual_shares = shares
                        logger.warning(f"[SELL] Fill API unavailable — using estimated shares={shares:.2f}")
                    elif actual_shares <= 0:
                        if cancel_status == "API_ERROR":
                            logger.warning(
                                f"[SELL] Cancel failed + zero fills confirmed — "
                                f"BUY order may still be open, retrying next cycle"
                            )
                            continue
                        logger.info(f"[SELL] Zero filled shares confirmed — nothing to sell")
                        to_remove.append(order_id)
                        self.kelly.release(entry_size)
                        self._save_bankroll()
                        continue
                    # Mark confirmed so next cycle uses pos["shares"] directly
                    pos["shares"] = actual_shares
                    pos["shares_confirmed"] = True
                    self._save_live_positions()

                # Clamp sell amount to actual on-chain CTF balance (fixes size mismatch
                # when taker fees deduct from token amount: buy 5 → receive 4.95)
                try:
                    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                    _bp = BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL, token_id=token_id
                    )
                    _bd = self.executor.client.get_balance_allowance(_bp)
                    ctf_real = float(_bd.get("balance", "0") or "0") / 1_000_000
                    if 0 < ctf_real < actual_shares:
                        import math
                        clamped = math.floor(ctf_real * 100) / 100
                        logger.info(
                            f"[SELL] Clamping shares {actual_shares:.2f} → {clamped:.2f} "
                            f"(actual CTF balance={ctf_real:.6f})"
                        )
                        actual_shares = clamped
                        pos["shares"] = actual_shares
                        pos["shares_confirmed"] = True
                except Exception as _be:
                    logger.warning(f"[SELL] CTF balance check failed: {_be}")

                # Dead market guard: best_bid near zero = no liquidity (market expired as loser)
                DEAD_BID = 0.02
                if best_bid <= DEAD_BID:
                    logger.warning(
                        f"[SELL] Dead market — best_bid={best_bid:.4f} ≤ {DEAD_BID:.2f} "
                        f"(no liquidity, expired as loser). Accepting full loss of ${entry_size:.2f}."
                    )
                    to_remove.append(order_id)
                    self.kelly.release(entry_size)
                    self._save_bankroll()
                    continue

                # Use aggressive worst-price to ensure fill: allow up to 20% slippage
                # from best_bid. The price param is the MINIMUM we accept — setting it
                # too high (= best_bid) causes FOK/FAK to fail when bids shift.
                sell_price = round(max(best_bid * 0.80, 0.01), 4)
                logger.info(
                    f"[SELL] cancel={cancel_status} mid={current_price:.4f} best_bid={best_bid:.4f} "
                    f"selling {actual_shares:.2f} shares @ {sell_price} (worst-price, 20% slippage)"
                )
                sell_order_id = self.executor.close_position(
                    token_id, actual_shares, sell_price,
                    tick_size=tick_size, neg_risk=neg_risk,
                )
                if sell_order_id == "BALANCE_ERROR":
                    # close_position already waited for settlement + re-approved + retried.
                    # If we still get BALANCE_ERROR, either tokens truly don't exist or
                    # the market is about to expire.
                    if time_to_expiry > 60:
                        # Still time — retry next TP/SL cycle (tokens may settle later)
                        logger.warning(
                            f"[SELL] BALANCE_ERROR after close_position retries — "
                            f"will retry next cycle ({time_to_expiry:.0f}s until expiry)"
                        )
                        continue
                    else:
                        logger.warning(
                            f"[SELL] Balance/allowance fatal — market expiring in {time_to_expiry:.0f}s, "
                            f"accepting full loss of ${entry_size:.2f} for {market_id}"
                        )
                        to_remove.append(order_id)
                        self.kelly.release(entry_size)
                        self._save_bankroll()
                        continue
                if not sell_order_id:
                    logger.error(f"[SELL] FAILED for {market_id} — retrying next cycle")
                    continue

                sold_shares = self.executor.get_order_fills(sell_order_id)
                if sold_shares < 0:
                    sold_shares = actual_shares
                    logger.warning(f"[SELL] Fill verify API unavailable — assuming full sell of {actual_shares:.2f}")

                DUST = 0.01
                remaining = max(0.0, actual_shares - sold_shares)
                if remaining <= DUST:
                    logger.info(f"[SELL] COMPLETE — sold={sold_shares:.2f} remaining={remaining:.2f} (dust)")
                    to_remove.append(order_id)
                    self.kelly.release(entry_size)
                    self._save_bankroll()
                else:
                    fraction_sold = sold_shares / actual_shares if actual_shares > 0 else 1.0
                    partial_usdc = entry_size * fraction_sold
                    logger.warning(
                        f"[SELL] PARTIAL — sold={sold_shares:.2f}/{actual_shares:.2f} "
                        f"remaining={remaining:.2f} shares — updating position, retrying next cycle"
                    )
                    pos["shares"] = remaining
                    pos["entry_size"] = entry_size - partial_usdc
                    self.kelly.release(partial_usdc)
                    self._save_live_positions()
                    self._save_bankroll()

        for oid in to_remove:
            self._live_positions.pop(oid, None)
        if to_remove:
            self._save_live_positions()

    def _refresh_markets(self, now: float):
        """
        Re-discover active markets and remove expired ones.
        Uses ONLY Gamma API for discovery.
        Resolves expired markets and triggers adaptive learning.
        """
        expired = [mid for mid, s in self._markets.items()
                   if s.end_time > 0 and (s.end_time - now) < 0 and not s.is_event]
        for mid in expired:
            state = self._markets[mid]
            if self.dry_run and state.asset != "EVENT":
                winning_side = None
                cond_id = state.condition_id or ""
                resolve_source = ""
                try:
                    gamma = GammaClient()
                    resolved = gamma.get_resolved_outcome(cond_id) if cond_id else None
                    if resolved:
                        winning_side = resolved
                        resolve_source = "gamma_settlement"
                except Exception:
                    pass
                if winning_side is None and state.spot_at_start > 0:
                    current_spot = 0.0
                    if hasattr(self, '_last_prices') and state.asset.upper() in (self._last_prices or {}):
                        current_spot = self._last_prices[state.asset.upper()]
                    else:
                        try:
                            prices = self.price_feed.fetch()
                            current_spot = prices.get(state.asset.upper(), 0.0)
                        except Exception:
                            pass
                    if current_spot > 0:
                        winning_side = "YES" if current_spot >= state.spot_at_start else "NO"
                        resolve_source = f"spot={state.spot_at_start:.2f}->{current_spot:.2f}"
                if winning_side is None:
                    try:
                        from data.market_data import ClobClient
                        clob = ClobClient()
                        mp = clob.get_midpoint(state.token_id_yes)
                        if mp is not None and 0 < mp < 1:
                            winning_side = "YES" if mp >= 0.5 else "NO"
                            resolve_source = f"clob_midpoint={mp:.4f}"
                    except Exception:
                        pass
                if winning_side is None and state.last_price > 0:
                    winning_side = "YES" if state.last_price >= 0.5 else "NO"
                    resolve_source = f"last_price={state.last_price:.4f}"
                if resolve_source:
                    logger.debug(f"[RESOLVE] {mid} via {resolve_source} => {winning_side}")
                if winning_side:
                    self.dry_run_tracker.resolve(mid, winning_side)
                    self.kelly.bankroll = self.dry_run_tracker.virtual_bankroll
            logger.debug(f"Removing expired market: {mid}")
            del self._markets[mid]

        if self.dry_run and now - self._last_stats_log >= 120:
            self._last_stats_log = now
            self.dry_run_tracker.log_stats()

        if self.dry_run and now - self._last_adaptive_update >= 180:
            self._last_adaptive_update = now
            resolved = self.dry_run_tracker.get_resolved_entries(last_n=50)
            if len(resolved) >= 5:
                result = self.adaptive.analyze_and_adapt(resolved)
                if result.get("status") == "adapted":
                    new_lambda = self.adaptive.get_kelly_lambda(self.kelly.lambda_fraction)
                    new_edge = self.adaptive.get_min_edge(self._current_min_edge)
                    self.kelly.lambda_fraction = new_lambda
                    self.edge_model.min_edge = new_edge
                    logger.info(
                        f"[ADAPTIVE] Applied: Kelly λ={new_lambda:.3f}, "
                        f"MIN_EDGE={new_edge:.4f}"
                    )

        # Event market refresh: add newly listed markets, prune expired ones
        import datetime
        from config import EVENT_MARKET_LIMIT, EVENT_MARKET_MIN_VOLUME
        gamma = GammaClient()
        new_count = 0

        # Prune expired markets (end_time passed by >60s and no open position)
        to_remove = []
        for mid, state in list(self._markets.items()):
            if state.end_time > 0 and state.end_time < now - 60:
                has_position = any(
                    p.get("market_id") == mid for p in self._live_positions.values()
                )
                if not has_position:
                    to_remove.append(mid)
        for mid in to_remove:
            del self._markets[mid]
            logger.info(f"[REFRESH] Pruned expired market {mid}")

        # Discover new event markets not yet tracked
        try:
            markets = gamma.discover_event_markets(limit=EVENT_MARKET_LIMIT)
        except Exception as e:
            logger.warning(f"[REFRESH] Event market discovery failed: {e}")
            markets = []

        for m in markets:
            condition_id = (m.get("conditionId") or m.get("condition_id") or
                            m.get("id") or "")
            if not condition_id:
                continue
            volume = float(m.get("volume", m.get("volumeNum", 0)) or 0)
            if volume < EVENT_MARKET_MIN_VOLUME:
                continue

            market_id = f"evt_{str(condition_id)[:10]}"
            if market_id in self._markets:
                continue

            yes_token, no_token = extract_clob_tokens(m)
            if not yes_token or not no_token:
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

            gamma_price_yes, gamma_price_no = extract_gamma_prices(m)
            market_neg_risk = bool(m.get("negRisk", m.get("neg_risk", False)))
            market_tick_size = str(m.get("minimumTickSize", m.get("minimum_tick_size", "0.01")))
            if market_tick_size not in ("0.1", "0.01", "0.001", "0.0001"):
                market_tick_size = "0.01"
            question_text = m.get("question", "")

            self.register_market(
                market_id=market_id,
                token_id_yes=yes_token,
                token_id_no=no_token,
                asset="EVENT",
                timeframe="event",
                end_time=end_time,
                gamma_price_yes=gamma_price_yes,
                gamma_price_no=gamma_price_no,
                condition_id=str(condition_id),
                neg_risk=market_neg_risk,
                tick_size=market_tick_size,
                question=question_text,
                is_event=True,
            )
            new_count += 1

        if new_count:
            logger.info(f"[REFRESH] +{new_count} new event markets | total={len(self._markets)}")

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

        self._apply_growth_tier()
        if self.dry_run and hasattr(self, 'adaptive'):
            self.kelly.lambda_fraction = self.adaptive.get_kelly_lambda(self._tier_base_lambda)
            self.edge_model.min_edge = self.adaptive.get_min_edge(self._tier_base_edge)
            self._current_min_edge = self.edge_model.min_edge

        # --- 0. Check Take-Profit / Stop-Loss on open trades ---
        if now - self._last_tp_sl_check >= TP_SL_CHECK_INTERVAL:
            self._last_tp_sl_check = now
            self._check_tp_sl(now)

        # --- 0b. Periodic allowance refresh + rewards cache (every 30 min) ---
        if not self.dry_run and (now - self._last_allowance_refresh) >= 1800:
            self._last_allowance_refresh = now
            try:
                self.executor._check_balance_and_allowance()
            except Exception as e:
                logger.warning(f"[ALLOWANCE] Periodic refresh failed: {e}")
            get_rewarded_condition_ids(force_refresh=True)

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

        # Sort markets by end_time ascending (soonest expiry first).
        # This ensures Gemini analyses near-term markets first — the ones where
        # a decision needs to be made NOW. Unknown expiry (0) goes last.
        # Within the same expiry tier, higher-volume markets come first.
        _sorted_markets = sorted(
            self._markets.items(),
            key=lambda kv: (
                kv[1].end_time if kv[1].end_time > 0 else float("inf"),
            ),
        )

        # Cap how many markets Gemini actively analyses per tick based on
        # available capital — no point analyzing 100 markets if we can only
        # afford 3 trades.
        _max_gemini_slots = max(
            3,
            int(self.kelly.available_capital / max(MIN_BET_SIZE, 1.0)) + 2,
        )
        _gemini_analyzed = 0

        for market_id, state in _sorted_markets:
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
            book_tick = yes_data.get("tick_size")
            book_neg = yes_data.get("neg_risk")
            if book_tick is not None and book_tick in ("0.1", "0.01", "0.001", "0.0001"):
                state.tick_size = book_tick
            if book_neg is not None:
                state.neg_risk = book_neg
            if p_yes is None and state.gamma_price_yes is not None:
                if abs(state.gamma_price_yes - 0.5) > 0.01:
                    p_yes = state.gamma_price_yes
            if p_no is None and state.gamma_price_no is not None:
                if abs(state.gamma_price_no - 0.5) > 0.01:
                    p_no = state.gamma_price_no
            if p_yes is not None and not (0.01 <= p_yes <= 0.99):
                p_yes = None
            if p_no is not None and not (0.01 <= p_no <= 0.99):
                p_no = None
            if p_yes is None or p_no is None:
                market_stats.append(f"{state.asset}({state.timeframe}):NO_DATA")
                continue
            if p_yes > 0.95 or p_yes < 0.05:
                market_stats.append(f"{state.asset}({state.timeframe}):RESOLVED(p={p_yes:.2f})")
                continue
            # Enforce minimum price floor — avoid buying at 5¢ where there's no
            # liquidity to sell on the losing side (dead market on expiry)
            from config import PRICE_FLOOR, PRICE_CEILING
            if p_yes < PRICE_FLOOR and p_no is not None and p_no < PRICE_FLOOR:
                market_stats.append(f"{state.asset}({state.timeframe}):TOO_CHEAP(p={p_yes:.2f})")
                continue

            ob_imbalance = yes_data["imbalance"] or 0.0
            ob_depth = yes_data["depth"] if yes_data["depth"] else 0.5

            # --- 2b. OFI analysis ---
            ofi_result = self.ofi_model.evaluate({
                "bids": yes_data.get("bids", []),
                "asks": yes_data.get("asks", []),
            })

            crypto_symbol = f"{state.asset}USDT"
            has_spot_price = crypto_symbol in new_prices

            # Update regime model with latest spot price
            if has_spot_price:
                self.regime.update(state.asset, new_prices[crypto_symbol])

            # --- 3. Bayesian update ---
            volatility = self.price_feed.get_volatility(crypto_symbol) if has_spot_price else 0.0
            bayesian_data = self.price_feed.build_bayesian_data(
                symbol=crypto_symbol if has_spot_price else None,
                new_prices=new_prices,
                elapsed_seconds=elapsed,
                volatility=volatility,
                ob_imbalance=ob_imbalance,
            )
            if self.dry_run and hasattr(self, 'adaptive'):
                from config import BAYESIAN_ALPHA, BAYESIAN_PRIOR
                adapted_alpha = self.adaptive.get_bayesian_alpha(state.asset, BAYESIAN_ALPHA)
                state.bayesian.set_alpha(adapted_alpha)
                adapted_prior = self.adaptive.get_bayesian_prior(state.asset, BAYESIAN_PRIOR)
                state.bayesian.prior = adapted_prior
            q = state.bayesian.update(bayesian_data)

            asset_bias = self.adaptive.get_asset_bias(state.asset)
            if asset_bias != 0.0:
                q = max(0.01, min(0.99, q + asset_bias))

            # --- 3b. Wallet signal adjustment ---
            wallet_signal = self.wallet_tracker.get_signal(
                state.token_id_yes, state.token_id_no
            )
            if wallet_signal.has_signal:
                q = max(0.01, min(0.99, q + wallet_signal.confidence_boost))

            # --- 3c. Gemini probability (PRIMARY signal for event markets) ---
            gemini_conf = 0.0
            gemini_reasoning = ""
            if state.is_event and state.question:
                # Attach real-time weather data for weather-related markets
                weather_ctx = self.weather_feed.get_context(state.question)
                if weather_ctx:
                    logger.debug(f"[WEATHER:{market_id[:20]}] {weather_ctx[:80]}…")

                # Trigger background Gemini analysis (cached, refreshes every 5min).
                # Only actively refresh within the slot budget — soonest markets first
                # (list is sorted by end_time so we naturally fill slots with urgency).
                _already_cached = self.event_sentiment.get_result(market_id) is not None

                # Force-refresh when edge is very large (>15% EV) AND Gemini already
                # has a cached result — re-confirm before a big trade.
                # Never force on first analysis (no cache yet = normal queue entry).
                _raw_edge = abs(p_yes - q) if q is not None else 0.0
                _force = _raw_edge > 0.15 and _already_cached

                if _already_cached or _gemini_analyzed < _max_gemini_slots or _force:
                    self.event_sentiment.analyze_async(
                        market_id=market_id,
                        question=state.question,
                        market_price=p_yes,
                        weather_context=weather_ctx,
                        end_time=state.end_time,
                        force_refresh=_force,
                    )
                    if not _already_cached:
                        _gemini_analyzed += 1
                    if _force:
                        logger.info(
                            f"[GEMINI_FORCE] {market_id[:20]} edge={_raw_edge:.1%} > 15% "
                            f"→ forcing fresh analysis before trade"
                        )
                gemini_prob = self.event_sentiment.get_probability(market_id)
                gemini_conf = self.event_sentiment.get_confidence(market_id)

                if gemini_prob is not None:
                    # --- Confidence gate: only trade when Gemini is well-informed ---
                    if gemini_conf < GEMINI_MIN_CONFIDENCE:
                        q = p_yes   # treat as no-edge until confidence rises
                        logger.debug(
                            f"[GEMINI:{market_id[:20]}] conf={gemini_conf:.2f} "
                            f"< {GEMINI_MIN_CONFIDENCE} → skipping (not confident enough)"
                        )
                    else:
                        # Gemini IS the primary probability estimate — overrides Bayesian
                        q = gemini_prob
                        logger.debug(
                            f"[GEMINI:{market_id[:20]}] p(YES)={gemini_prob:.3f} "
                            f"conf={gemini_conf:.2f} ✓ market={p_yes:.3f} → q={q:.3f}"
                        )
                        _gr = self.event_sentiment.get_result(market_id)
                        if _gr:
                            gemini_reasoning = _gr.reasoning
                else:
                    # No Gemini data yet — use market price so edge=0, skip this tick
                    q = p_yes
                    logger.debug(f"[GEMINI:{market_id[:20]}] awaiting first estimate → q=market")

            # --- 3d. OFI signal: refine q with live order-flow pressure ---
            if ofi_result.q_adjustment != 0.0:
                q = max(0.01, min(0.99, q + ofi_result.q_adjustment))
                logger.debug(
                    f"[OFI:{state.asset}] {ofi_result.signal} "
                    f"ofi={ofi_result.ofi:+.3f} → q_adj={ofi_result.q_adjustment:+.4f}"
                )

            # --- 4. Edge check (uses dynamic MIN_EDGE from current tier) ---
            edge_result = self.edge_model.evaluate_directional(q=q, p=p_yes)

            # Within-market arb disabled (binary markets always lose one leg)

            market_stats.append(
                f"{state.asset}({state.timeframe}):q={q:.3f},p={p_yes:.3f},EV={edge_result.ev_net:+.3f}"
            )

            if not edge_result.has_edge:
                if state.is_event and gemini_conf > 0:
                    self._log_gemini_decision(
                        state, q, p_yes, gemini_conf, edge_result.ev_net,
                        "NO_EDGE", gemini_reasoning,
                    )
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

            # --- 6. Stoikov execution quote (OFI-adjusted) ---
            remaining_time = self._estimate_remaining_time(state)
            # Shift mid-price by OFI signal so limit orders avoid aggressive sellers
            ofi_adjusted_mid = max(0.01, min(0.99, p_yes + ofi_result.stoikov_shift))
            stoikov_quote = state.stoikov.quote(
                mid_price=ofi_adjusted_mid,
                remaining_time=remaining_time,
            )

            # --- 7. Kelly position sizing + Regime multiplier ---
            is_passive = edge_result.is_passive
            exec_prob = 0.9 if is_passive else 0.7
            if edge_result.side == "NO":
                kelly_p = 1.0 - q
                kelly_market = p_no if p_no else 1.0 - p_yes
            else:
                kelly_p = q
                kelly_market = p_yes
            kelly_result = self.kelly.compute(
                p_success=kelly_p,
                market_price=kelly_market,
                exec_probability=exec_prob,
                ob_depth_factor=ob_depth,
            )

            # Apply regime Kelly multiplier (1.0 / 0.75 / 0.50)
            regime_mult = self.regime.get_multiplier(state.asset)
            if regime_mult < 1.0:
                original_size = kelly_result.position_size
                kelly_result = dc_replace(
                    kelly_result,
                    position_size=kelly_result.position_size * regime_mult,
                )
                logger.debug(
                    f"[REGIME:{state.asset}] "
                    f"{self.regime.get_state(state.asset).regime} "
                    f"→ size ${original_size:.3f} × {regime_mult:.2f} "
                    f"= ${kelly_result.position_size:.3f}"
                )

            # Enforce Polymarket minimum order size ($1.00)
            if kelly_result.is_viable and kelly_result.position_size > 0:
                if kelly_result.position_size < MIN_BET_SIZE:
                    kelly_result = dc_replace(
                        kelly_result, position_size=MIN_BET_SIZE
                    )
                    logger.debug(f"[MIN_BET] size clamped to ${MIN_BET_SIZE:.2f}")
            # Liquidity pre-check disabled: CLOB bids are often empty for these
            # short-term markets even when the AMM has liquidity. Dead market guard
            # during sell handles truly illiquid exits.

            if kelly_result.is_viable and kelly_result.position_size > 0:
                # Extract best ask for the side being traded (for competitive GTC limit orders)
                if edge_result.side == "NO":
                    _ask_list = no_data.get("asks", [])
                else:
                    _ask_list = yes_data.get("asks", [])
                _best_ask = float(_ask_list[0]["price"]) if _ask_list else 0.0

                opp = TradeOpportunity(
                    market_id=market_id,
                    edge_result=edge_result,
                    spread_signal=spread_signal,
                    stoikov_quote=stoikov_quote,
                    kelly_result=kelly_result,
                    q=q,
                    gemini_confidence=gemini_conf,
                    end_time=state.end_time,
                    best_ask=_best_ask,
                )
                opportunities.append(opp)
                if state.is_event:
                    self._log_gemini_decision(
                        state, q, p_yes, gemini_conf, edge_result.ev_net,
                        "OPPORTUNITY", gemini_reasoning,
                    )
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
            adaptive_info = ""
            if self.dry_run:
                a_state = self.adaptive.get_state()
                adaptive_info = (
                    f" | adaptive: kelly_adj={a_state['kelly_lambda_adj']:+.3f}, "
                    f"edge_adj={a_state['edge_threshold_adj']:+.4f}"
                )
            regime_summary = self.regime.summary()
            regime_info = " | ".join(
                f"{a}:{v['regime'].split('_')[0]}(×{v['kelly_mult']})"
                for a, v in regime_summary.items()
            )
            # Show top-5 soonest-expiring markets for priority overview
            _priority_log = []
            for _mid, _st in _sorted_markets[:5]:
                if _st.end_time > 0:
                    _h = (_st.end_time - now) / 3600
                    _label = (f"{_h:.1f}h" if _h < 48 else f"{_h/24:.1f}d")
                    _priority_log.append(f"{_mid[:12]}({_label})")
            if _priority_log:
                logger.info(f"[PRIORITY] Soonest markets: {' > '.join(_priority_log)} | Gemini slots: {_gemini_analyzed}/{_max_gemini_slots}")

            logger.info(
                f"[HEARTBEAT] tick={self._tick_count} | markets={len(self._markets)} "
                f"({no_data} no data, {len(active)} active) | "
                f"bankroll=${self.kelly.bankroll:.2f}{adaptive_info} | "
                f"{self.event_sentiment.summary()}"
            )
            if regime_info:
                logger.info(f"[HEARTBEAT] Regimes: {regime_info}")
            if active:
                logger.info(f"[HEARTBEAT] Market status: {' | '.join(active)}")
            # Re-sync bankroll from live CLOB every 10 ticks (live mode only)
            if not self.dry_run and self._tick_count % 10 == 0 and self.executor:
                try:
                    synced = self.executor.get_available_balance_usd()
                    if synced > 0 and abs(synced - self.kelly.bankroll) > 0.05:
                        logger.info(f"[SYNC] CLOB balance=${synced:.2f} (was ${self.kelly.bankroll:.2f}) → updated")
                        self.kelly.bankroll = synced
                        self._save_bankroll()
                except Exception:
                    pass
            # Persist regime + gas state for dashboard
            try:
                regime_file = Path(__file__).parent.parent / "regime_state.json"
                gas_info = self.gas_optimizer.get_gas_info(dry_run=self.dry_run)
                regime_file.write_text(json.dumps({
                    "regimes": regime_summary,
                    "gas": gas_info,
                    "tick": self._tick_count,
                    "ts": now,
                }, indent=2))
            except Exception:
                pass

        if opportunities:
            self._execute_opportunities(opportunities)

    # ── Gemini decision journal ───────────────────────────────────────────────
    _GEMINI_DECISIONS_FILE = Path(__file__).parent.parent / "gemini_decisions.json"
    _GEMINI_DECISIONS_MAX = 60

    def _log_gemini_decision(
        self,
        state: "MarketState",
        q: float,
        p_yes: float,
        gemini_conf: float,
        edge_ev: float,
        decision: str,
        reasoning: str,
    ) -> None:
        """Append one Gemini decision record to gemini_decisions.json."""
        record = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "market_id": state.market_id,
            "question": state.question[:120],
            "gemini_prob": round(q, 3),
            "confidence": round(gemini_conf, 2),
            "market_price": round(p_yes, 3),
            "edge_ev": round(edge_ev, 4),
            "decision": decision,
            "reasoning": reasoning,
        }
        try:
            f = self._GEMINI_DECISIONS_FILE
            existing: list = json.loads(f.read_text()) if f.exists() else []
            existing.append(record)
            f.write_text(json.dumps(existing[-self._GEMINI_DECISIONS_MAX:], indent=2))
        except Exception:
            pass

    @staticmethod
    def _urgency_bucket(end_time: float) -> int:
        """
        Returns a priority bucket based on time-to-expiry.
        Higher = more urgent. Markets expiring soon trade first.
          4 → expires within 1 hour
          3 → expires within 6 hours
          2 → expires within 24 hours
          1 → expires within 7 days
          0 → unknown expiry or far away
        """
        if end_time <= 0:
            return 0
        remaining = end_time - time.time()
        if remaining <= 3_600:
            return 4
        if remaining <= 21_600:
            return 3
        if remaining <= 86_400:
            return 2
        if remaining <= 604_800:
            return 1
        return 0

    def _execute_opportunities(self, opportunities: list[TradeOpportunity]):
        """Execute the best opportunities. Dry-run logs only; live mode places orders."""
        # Sort priority (all descending):
        #   1. Urgency bucket (expiring soonest first)
        #   2. Rewarded markets (extra LP income)
        #   3. Gemini confidence (high-confidence signals trade before low-confidence)
        #   4. EV (highest expected value last tie-breaker)
        opportunities.sort(
            key=lambda o: (
                self._urgency_bucket(o.end_time),
                int(is_rewarded(self._markets[o.market_id].condition_id)),
                o.gemini_confidence,
                o.edge_result.ev_net,
            ),
            reverse=True,
        )

        # Track tokens placed THIS execution cycle to block same-tick duplicates
        _placed_tokens_this_tick: set[str] = set()

        for opp in opportunities:
            market = self._markets[opp.market_id]
            raw_price = opp.stoikov_quote.reservation_price
            size = opp.kelly_result.position_size
            side = opp.edge_result.side          # "YES", "NO", or "BOTH"

            # 15-minute markets: use GTC at competitive price — FOK fails on thin books,
            # GTC sits in the order book and fills when matched.
            is_passive = opp.edge_result.is_passive
            if market.timeframe == "15m":
                is_passive = True
                if side == "NO":
                    price = round(1.0 - opp.stoikov_quote.bid, 6)
                else:
                    price = round(opp.stoikov_quote.ask, 6)
            else:
                # For event markets: use the actual ask price so the GTC limit order
                # fills immediately instead of sitting below the ask (never executing).
                # Fall back to Stoikov reservation price if ask is unavailable.
                if opp.best_ask > 0 and 0.01 <= opp.best_ask <= 0.99:
                    price = round(opp.best_ask, 6)
                else:
                    price = (1.0 - raw_price) if side == "NO" else raw_price

            exec_type = "GTC/MAKER" if is_passive else "FOK/TAKER"
            has_rewards = is_rewarded(market.condition_id)
            if has_rewards:
                logger.info(f"[REWARDS] {opp.market_id} has active CLOB LP rewards — prioritized")

            # ── CLOB 5-share minimum enforcement ────────────────────────────────
            # If Kelly produces a small bet, bump size up to the CLOB minimum.
            # This also raises the stake so the gas-cost ratio improves (EV scales
            # linearly with size while gas is fixed per transaction).
            if price > 0:
                clob_min_size = 5.0 * price  # 5 shares × price = minimum viable USD
                if size < clob_min_size:
                    if not self.dry_run:
                        if clob_min_size > self.kelly.available_capital:
                            logger.info(
                                f"[SKIP] {opp.market_id}: need ${clob_min_size:.2f} for CLOB "
                                f"5-share min but only ${self.kelly.available_capital:.2f} available"
                            )
                            continue
                    logger.info(
                        f"[CLOB_MIN] {opp.market_id}: bumping size "
                        f"${size:.2f} → ${clob_min_size:.2f} "
                        f"(5 shares × {price:.4f})"
                    )
                    size = clob_min_size

            # Realistic maker entry price for dry-run:
            # YES trades fill at bid (below mid); NO trades fill at 1 - ask (below mid on NO side)
            if side == "NO":
                exec_price = round(1.0 - opp.stoikov_quote.ask, 6)
            else:
                exec_price = round(opp.stoikov_quote.bid, 6)
            # Use true market mid (last observed p_yes/p_no) for the log comparison
            true_mid = market.last_price if side != "NO" else market.last_price_no
            spread_shown = round(abs(true_mid - exec_price), 6)

            if not self.dry_run:
                gas_ok, gas_reason = self.gas_optimizer.should_trade(
                    edge=opp.edge_result.ev_net,
                    stake=size,
                    dry_run=False,
                )
                if not gas_ok:
                    logger.info(f"[GAS SKIP] {opp.market_id}: {gas_reason}")
                    continue

                # Position slot limits — event markets only
                from config import MAX_TRADES_EVENT
                n_open = len(self._live_positions)
                if n_open >= MAX_OPEN_TRADES:
                    logger.info(
                        f"[SKIP] {opp.market_id}: {n_open}/{MAX_OPEN_TRADES} positions — full"
                    )
                    continue
                if n_open >= MAX_TRADES_EVENT:
                    logger.info(
                        f"[SKIP] {opp.market_id}: {n_open}/{MAX_TRADES_EVENT} event slots used"
                    )
                    continue

                # Hard cap: never exceed MAX_TOTAL_EXPOSURE_PCT of bankroll
                max_exposure = self.kelly.bankroll * MAX_TOTAL_EXPOSURE_PCT
                if self.kelly.committed_capital + size > max_exposure:
                    logger.info(
                        f"[SKIP] {opp.market_id}: exposure ${self.kelly.committed_capital:.2f}+"
                        f"${size:.2f} > max ${max_exposure:.2f} "
                        f"({MAX_TOTAL_EXPOSURE_PCT:.0%} of ${self.kelly.bankroll:.2f})"
                    )
                    continue

                # Hard cap: enough free capital for this trade
                if self.kelly.available_capital < size:
                    logger.info(
                        f"[SKIP] {opp.market_id}: available=${self.kelly.available_capital:.2f} "
                        f"< trade size ${size:.2f}"
                    )
                    continue

            if self.dry_run:
                logger.info(
                    f"[DRY RUN] {opp.market_id}: {side} ${size:.2f} @ {price:.4f} "
                    f"| EV={opp.edge_result.ev_net:.4f} | Kelly f={opp.kelly_result.f_kelly:.4f} "
                    f"| exec={exec_type}"
                )
                logger.info(
                    f"[MAKER] entry={exec_price:.4f} vs mid={true_mid:.4f} "
                    f"(spread={spread_shown:.4f})"
                )
                record_side = side if side != "BOTH" else "YES"
                window_start = ""
                window_end = ""
                if market.end_time > 0:
                    import datetime
                    end_dt = datetime.datetime.utcfromtimestamp(market.end_time)
                    start_dt = end_dt - datetime.timedelta(minutes=5)
                    window_start = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                    window_end = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                self.dry_run_tracker.record(
                    market_id=opp.market_id,
                    asset=market.asset,
                    side=record_side,
                    q=opp.q,
                    p=market.last_price,
                    edge=opp.edge_result.ev_net,
                    size=size,
                    exec_price=exec_price,
                    question=market.question,
                    timeframe=market.timeframe,
                    window_start=window_start,
                    window_end=window_end,
                    confidence=market.bayesian.confidence,
                    bayesian_prior=market.bayesian.prior,
                    kelly_lambda=self.kelly.lambda_fraction,
                    min_edge_used=self._current_min_edge,
                )
                continue

            # Within-market arb disabled: binary markets always have one losing leg
            if side == "BOTH":
                logger.debug(f"[SKIP] {opp.market_id}: arb (BOTH) disabled — directional only")
                continue
            if False and side == "BOTH":  # dead code, kept for reference
                # Apply same capital + dedup guards as directional trades
                open_token_ids_arb = {pos["token_id"] for pos in self._live_positions.values()}
                if (market.token_id_yes in open_token_ids_arb
                        or market.token_id_no in open_token_ids_arb):
                    logger.info(f"[SKIP] {opp.market_id}: arb token already has open position")
                    continue
                n_open_arb = len(self._live_positions)
                if n_open_arb + 2 > MAX_OPEN_TRADES:
                    logger.info(
                        f"[SKIP] {opp.market_id}: arb needs 2 slots, only "
                        f"{MAX_OPEN_TRADES - n_open_arb} free"
                    )
                    continue
                if self.kelly.available_capital < size:
                    logger.info(
                        f"[SKIP] {opp.market_id}: arb available=${self.kelly.available_capital:.2f}"
                        f" < ${size:.2f}"
                    )
                    continue
                self._place_arb_both_sides(market, size, not is_passive)
                continue

            # Directional: BUY YES or BUY NO
            token_id = market.token_id_yes if side == "YES" else market.token_id_no

            # Dedup guard 0: already placed in THIS execution cycle (same-tick duplicate)
            if token_id in _placed_tokens_this_tick:
                logger.info(
                    f"[SKIP] {opp.market_id}: token {token_id[:12]}... already placed this tick"
                )
                continue

            # Dedup guard 1: open position already tracked in live_positions
            open_token_ids = {pos["token_id"] for pos in self._live_positions.values()}
            if token_id in open_token_ids:
                logger.info(
                    f"[SKIP] {opp.market_id}: token {token_id[:12]}... already has an open position"
                )
                continue
            # Second guard: check live CLOB for any open buy order on this token
            # (catches the case where live_positions was cleared but order still exists)
            if not self.dry_run and self.executor:
                try:
                    clob_orders = self.executor.get_open_orders()
                    clob_token_ids = {
                        o.get("asset_id") or o.get("tokenId") or o.get("token_id", "")
                        for o in clob_orders
                        if (o.get("side") or "").upper() == "BUY"
                    }
                    if token_id in clob_token_ids:
                        logger.info(
                            f"[SKIP] {opp.market_id}: open CLOB buy order already exists "
                            f"for token {token_id[:12]}... — skipping duplicate"
                        )
                        continue
                except Exception:
                    pass

            logger.info(
                f"[LIVE] Placing order: {opp.market_id} BUY {side} ${size:.2f} @ {price:.4f} "
                f"({exec_type}) tick={market.tick_size} neg_risk={market.neg_risk}"
            )
            if is_passive:
                order_id = self.executor.place_limit_order(
                    token_id, "BUY", price, size,
                    tick_size=market.tick_size, neg_risk=market.neg_risk,
                )
            else:
                order_id = self.executor.place_fok_order(
                    token_id, "BUY", price, size,
                    tick_size=market.tick_size, neg_risk=market.neg_risk,
                )

            if order_id:
                logger.info(f"Order accepted: {order_id}")
                _placed_tokens_this_tick.add(token_id)  # block same-tick duplicates
                self.kelly.allocate(size)
                self._save_bankroll()

                # SL bracket is placed AFTER the buy fills (not now) because placing a
                # SELL order on tokens we don't hold yet causes 'not enough balance/allowance'.
                # The _check_tp_sl loop detects fills and places the bracket automatically.
                self._record_trade(
                    opp.market_id, side, size, price, order_id,
                    token_id=token_id,
                    tick_size=market.tick_size, neg_risk=market.neg_risk,
                    end_time=market.end_time, sl_order_id="",
                )
            else:
                logger.warning(f"Order rejected for {opp.market_id}")

    def _place_arb_both_sides(self, market: MarketState, size: float, aggressive: bool):
        """Place both YES and NO legs of a within-market arbitrage."""
        half = max(size / 2.0, MIN_BET_SIZE)
        yes_price = market.last_price
        no_price = market.last_price_no

        yes_min = 5.0 * yes_price if yes_price > 0 else 999
        no_min = 5.0 * no_price if no_price > 0 else 999
        needed = max(yes_min, no_min)
        if half < needed:
            if self.kelly.available_capital >= needed * 2:
                half = needed
            else:
                logger.warning(
                    f"Both legs rejected for {market.market_id}: "
                    f"need ${needed:.2f}/leg (5-share min) but only ${self.kelly.available_capital:.2f} available"
                )
                return

        logger.info(
            f"[LIVE] Within-market arb {market.market_id}: "
            f"BUY YES ${half:.2f} @ {yes_price:.4f} + BUY NO ${half:.2f} @ {no_price:.4f}"
        )
        ts = market.tick_size
        nr = market.neg_risk
        if aggressive:
            yes_id = self.executor.place_fok_order(market.token_id_yes, "BUY", yes_price, half,
                                                    tick_size=ts, neg_risk=nr)
            no_id = self.executor.place_fok_order(market.token_id_no, "BUY", no_price, half,
                                                   tick_size=ts, neg_risk=nr)
        else:
            yes_id = self.executor.place_limit_order(market.token_id_yes, "BUY", yes_price, half,
                                                      tick_size=ts, neg_risk=nr)
            no_id = self.executor.place_limit_order(market.token_id_no, "BUY", no_price, half,
                                                     tick_size=ts, neg_risk=nr)

        if yes_id and no_id:
            logger.info(f"Both legs accepted: YES={yes_id}, NO={no_id}")
            self.kelly.allocate(size)
            self._save_bankroll()
            self._record_trade(market.market_id, "YES", half, yes_price, yes_id,
                               token_id=market.token_id_yes, tick_size=ts, neg_risk=nr,
                               end_time=market.end_time)
            self._record_trade(market.market_id, "NO", half, no_price, no_id,
                               token_id=market.token_id_no, tick_size=ts, neg_risk=nr,
                               end_time=market.end_time)
        elif yes_id or no_id:
            logger.warning(
                f"Partial arb fill for {market.market_id}: "
                f"YES={yes_id}, NO={no_id} — directional exposure!"
            )
            self.kelly.allocate(half)
            self._save_bankroll()
            if yes_id:
                self._record_trade(market.market_id, "YES", half, yes_price, yes_id,
                                   token_id=market.token_id_yes, tick_size=ts, neg_risk=nr,
                                   end_time=market.end_time)
            if no_id:
                self._record_trade(market.market_id, "NO", half, no_price, no_id,
                                   token_id=market.token_id_no, tick_size=ts, neg_risk=nr,
                                   end_time=market.end_time)
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
        window = 15 * 60  # 15-minute markets assumed
        remaining = state.end_time - now
        if remaining <= 0:
            return 0.0
        return min(1.0, remaining / window)

    def _record_trade(self, market_id: str, side: str, size: float, price: float,
                      order_id: str, token_id: str = "",
                      tick_size: str = "0.01", neg_risk: bool = False,
                      end_time: float = 0.0, sl_order_id: str = ""):
        """Append a trade record to trades.json and register live position for TP/SL."""
        # Register live position for TP/SL monitoring
        if not self.dry_run and order_id and token_id:
            shares = size / price if price > 0 else 0
            self._live_positions[order_id] = {
                "market_id": market_id,
                "side": side,
                "token_id": token_id,
                "entry_price": price,
                "entry_size": size,
                "shares": shares,
                "tick_size": tick_size,
                "neg_risk": neg_risk,
                "entry_time": time.time(),
                "end_time": end_time,
                "sl_order_id": sl_order_id,
                "buy_filled": False,   # Set True once fill confirmed; SL+TP brackets placed then
                "tp_order_id": "",     # GTC TP bracket order ID (placed after buy fills)
            }
            self._save_live_positions()
            logger.info(
                f"[POSITION] Tracking live position: {market_id} {side} "
                f"${size:.2f} @ {price:.4f} shares={shares:.2f} | "
                f"TP={TP_RATIO:.0%} SL={SL_RATIO:.0%} | "
                f"Waiting for fill confirmation before SL bracket | "
                f"expires={time.strftime('%H:%M:%S', time.localtime(end_time)) if end_time else 'unknown'}"
            )

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
                "pnl": 0.0,
            })
            TRADES_FILE.write_text(json.dumps(trades[-500:]))
        except Exception as e:
            logger.warning(f"Could not record trade: {e}")

    def stop(self):
        self._running = False
        self._save_bankroll()
        logger.info("Bot stopped.")
