"""
Wallet Tracker — Copy-Trading from Top Polymarket Bots
=======================================================
Monitors the top-performing bots listed on polybot-arena.com and the
Polymarket leaderboard, then uses Gemini to evaluate whether to copy
their trades.

Flow:
  1. Discover top wallets  →  leaderboard API + COPY_TRADE_WALLETS env
  2. Fetch their recent activity  →  data-api.polymarket.com/activity
  3. Detect fresh trades (< SIGNAL_MAX_AGE seconds old)
  4. Gemini evaluates: "is this trade worth copying?" (live search grounding)
  5. Return a CopySignal that the bot uses to adjust q

Known top bots from polybot-arena.com (add their proxy wallet addresses to
COPY_TRADE_WALLETS in .env to start copy-trading them immediately):
  - BoneReader  (+$457k profit, multi-timeframe)
  - vidarx      (+$274k profit, 5-min markets)
  - vague-sourdough (+$165k profit, 5-min markets)

To find a wallet address: open polymarket.com/@<username>, copy the 0x address
shown on the profile page, then add to COPY_TRADE_WALLETS (comma-separated).
"""

import os
import time
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field

import requests

from config import DATA_API_HOST

logger = logging.getLogger(__name__)

# Only trust wallets with this many trades or more
MIN_TRADES_FOR_SIGNAL = 10
# Minimum win rate to be considered a "smart wallet"
MIN_WIN_RATE = 0.55
# How old (seconds) a trade can be and still count as a copy signal
SIGNAL_MAX_AGE = 600   # 10 minutes — event markets move slowly
# Cache leaderboard for this many seconds before refreshing
LEADERBOARD_TTL = 600  # 10 minutes
# Cache wallet activity for this many seconds
ACTIVITY_TTL = 90      # 1.5 minutes


# ---------------------------------------------------------------------------
# Known top-bot Polymarket pseudonyms → look them up manually on polymarket.com
# and add their 0x proxy addresses to COPY_TRADE_WALLETS in your .env file.
# ---------------------------------------------------------------------------
_KNOWN_BOT_LABELS = {
    # address (lowercase) → human-readable label  (filled in at runtime from env)
}


def _load_env_wallets() -> list[str]:
    """Read COPY_TRADE_WALLETS from environment (comma-separated 0x addresses)."""
    raw = os.getenv("COPY_TRADE_WALLETS", "")
    wallets = [w.strip() for w in raw.split(",") if w.strip().startswith("0x")]
    return wallets


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class WalletStats:
    address: str
    label: str = ""
    total_trades: int = 0
    winning_trades: int = 0
    total_pnl: float = 0.0
    recent_trades: list = field(default_factory=list)   # list of trade dicts
    last_updated: float = 0.0

    @property
    def win_rate(self) -> float:
        if self.total_trades < MIN_TRADES_FOR_SIGNAL:
            return 0.0
        return self.winning_trades / self.total_trades

    @property
    def is_smart(self) -> bool:
        return self.win_rate >= MIN_WIN_RATE and self.total_trades >= MIN_TRADES_FOR_SIGNAL


@dataclass
class WalletSignal:
    """Signal derived from smart wallet activity for a specific token (legacy compat)."""
    token_id: str
    smart_wallet_count: int
    dominant_side: str           # "YES" or "NO"
    avg_win_rate: float
    confidence_boost: float      # [-0.15, +0.15] to add to Bayesian prior

    @property
    def has_signal(self) -> bool:
        return self.smart_wallet_count > 0


@dataclass
class CopyTrade:
    """A specific trade from a top bot that we might want to copy."""
    wallet_address: str
    wallet_label: str
    token_id: str
    condition_id: str
    market_title: str
    side: str           # "YES" or "NO"
    price: float
    size: float
    timestamp: float
    win_rate: float     # wallet's historical win rate

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp

    @property
    def is_fresh(self) -> bool:
        return self.age_seconds < SIGNAL_MAX_AGE


# ---------------------------------------------------------------------------
# Main tracker
# ---------------------------------------------------------------------------

class WalletTracker:
    """
    Tracks profitable wallets from polybot-arena.com and generates copy signals.
    Integrates with the Bayesian model as an additional data source.
    """

    def __init__(self, host: str = DATA_API_HOST, top_n: int = 50):
        self.host = host.rstrip("/")
        self.top_n = top_n
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": "polymarket-bot/1.0",
        })

        self._wallets: dict[str, WalletStats] = {}
        self._leaderboard_cache: list[str] = []
        self._leaderboard_updated: float = 0.0
        self._lock = threading.Lock()

        # token_id → list of CopyTrade (fresh only)
        self._copy_trades: dict[str, list[CopyTrade]] = defaultdict(list)

        # Load configured wallets immediately
        env_wallets = _load_env_wallets()
        if env_wallets:
            logger.info(f"WalletTracker: loaded {len(env_wallets)} wallets from COPY_TRADE_WALLETS")
            for addr in env_wallets:
                label = _KNOWN_BOT_LABELS.get(addr.lower(), addr[:10] + "...")
                self._wallets[addr] = WalletStats(address=addr, label=label)
                if addr not in self._leaderboard_cache:
                    self._leaderboard_cache.append(addr)
        else:
            logger.info(
                "WalletTracker: COPY_TRADE_WALLETS not set — will use leaderboard only. "
                "Tip: add top-bot wallet addresses to .env to copy-trade polybot-arena.com bots."
            )

        # Gemini client for copy-trade evaluation (optional)
        self._gemini = self._init_gemini()

    # ------------------------------------------------------------------
    # Gemini setup
    # ------------------------------------------------------------------
    def _init_gemini(self):
        """Return a (client, model_name) tuple if available, else None."""
        try:
            from google import genai
            api_key = os.getenv("GEMINI_API_KEY", "")
            if not api_key:
                return None
            client = genai.Client(api_key=api_key)
            # Auto-detect model
            model = os.getenv("GEMINI_MODEL", "")
            if not model:
                candidates = [
                    "gemini-2.5-flash",
                    "gemini-2.5-flash-preview-04-17",
                    "gemini-2.0-flash",
                    "gemini-2.0-flash-exp",
                    "gemini-1.5-flash",
                ]
                try:
                    available = {m.name for m in client.models.list()}
                    for c in candidates:
                        if f"models/{c}" in available:
                            model = c
                            break
                    if not model:
                        for m in client.models.list():
                            if "flash" in m.name.lower():
                                model = m.name.replace("models/", "")
                                break
                except Exception:
                    pass
                model = model or "gemini-2.0-flash"
            logger.debug(f"WalletTracker: Gemini client ready (model={model})")
            return client, model
        except Exception:
            return None

    def _gemini_should_copy(self, trade: CopyTrade) -> bool:
        """
        Ask Gemini (with Google Search) whether to copy this trade.
        Returns True if Gemini thinks it's a good idea, False otherwise.
        Falls back to True (trust the smart wallet) if Gemini is unavailable.
        """
        if self._gemini is None:
            return True  # no Gemini → trust smart wallet blindly

        try:
            from google.genai import types
            prompt = (
                f"A top Polymarket trader ({trade.wallet_label}, win rate "
                f"{trade.win_rate:.0%}) just bought {trade.side} on this market:\n\n"
                f"  \"{trade.market_title}\"\n\n"
                f"  Token ID: {trade.token_id}\n"
                f"  Price paid: {trade.price:.2%}\n"
                f"  Trade size: ${trade.size:.2f}\n"
                f"  Trade age: {trade.age_seconds:.0f}s\n\n"
                f"Search for the latest news about this topic. "
                f"Then answer with exactly ONE word: COPY or SKIP.\n"
                f"COPY = their trade makes sense given current events.\n"
                f"SKIP = outdated info or the market already moved against them."
            )

            # Try search grounding first, fall back to plain call
            search_tool = None
            try:
                search_tool = types.Tool(
                    google_search_retrieval=types.GoogleSearchRetrieval()
                )
            except (AttributeError, TypeError):
                try:
                    search_tool = types.Tool(google_search=types.GoogleSearch())
                except (AttributeError, TypeError):
                    pass

            _gemini_client, _gemini_model = self._gemini
            gen_kwargs: dict = {
                "model": _gemini_model,
                "contents": prompt,
            }
            if search_tool is not None:
                gen_kwargs["config"] = types.GenerateContentConfig(
                    tools=[search_tool]
                )

            resp = _gemini_client.models.generate_content(**gen_kwargs)
            decision = resp.text.strip().upper()
            should_copy = "COPY" in decision
            logger.info(
                f"[COPY-EVAL] {trade.wallet_label} bought {trade.side} on "
                f"\"{trade.market_title[:40]}\" → Gemini: {decision}"
            )
            return should_copy

        except Exception as e:
            logger.debug(f"WalletTracker: Gemini copy-eval failed: {e}")
            return True   # fallback: trust the smart wallet

    # ------------------------------------------------------------------
    # Leaderboard discovery
    # ------------------------------------------------------------------
    def _fetch_leaderboard(self) -> list[str]:
        """Fetch top wallet addresses from Polymarket leaderboard."""
        endpoints = [
            f"{self.host}/leaderboard",
            "https://data-api.polymarket.com/leaderboard",
            "https://gamma-api.polymarket.com/leaderboard",
        ]
        params_list = [
            {"limit": self.top_n, "window": "1m"},
            {"limit": self.top_n, "window": "all"},
            {"limit": self.top_n},
        ]
        for url in endpoints:
            for params in params_list:
                try:
                    r = self._session.get(url, params=params, timeout=8)
                    if r.status_code != 200:
                        continue
                    data = r.json()
                    entries = (
                        data if isinstance(data, list)
                        else data.get("data", data.get("leaderboard", []))
                    )
                    addresses = []
                    for entry in entries:
                        addr = (
                            entry.get("proxyWallet")
                            or entry.get("proxy_wallet")
                            or entry.get("address")
                            or entry.get("user")
                        )
                        if addr and addr.startswith("0x"):
                            addresses.append(addr)
                    if addresses:
                        logger.info(
                            f"WalletTracker: fetched {len(addresses)} wallets "
                            f"from {url} (params={params})"
                        )
                        return addresses
                except Exception:
                    continue
        logger.debug("WalletTracker: leaderboard unavailable — using configured wallets only")
        return []

    def _refresh_leaderboard(self):
        """Refresh leaderboard cache if stale."""
        with self._lock:
            if time.time() - self._leaderboard_updated < LEADERBOARD_TTL:
                return
        addresses = self._fetch_leaderboard()
        with self._lock:
            if addresses:
                # Merge with existing (env wallets take priority)
                for addr in addresses:
                    if addr not in self._leaderboard_cache:
                        self._leaderboard_cache.append(addr)
                    if addr not in self._wallets:
                        self._wallets[addr] = WalletStats(address=addr, label=addr[:10] + "...")
            self._leaderboard_updated = time.time()

    # ------------------------------------------------------------------
    # Activity fetching
    # ------------------------------------------------------------------
    def _fetch_activity(self, address: str) -> list[dict]:
        """Fetch recent trade activity for a wallet via Polymarket data API."""
        for url_tmpl in [f"{self.host}/activity", "https://data-api.polymarket.com/activity"]:
            try:
                r = self._session.get(
                    url_tmpl,
                    params={"user": address, "limit": 50, "type": "TRADE"},
                    timeout=10,
                )
                if r.status_code != 200:
                    continue
                data = r.json()
                trades = data if isinstance(data, list) else data.get("data", [])
                if trades:
                    return trades
            except Exception as e:
                logger.debug(f"WalletTracker: activity fetch failed for {address[:10]}…: {e}")
        return []

    def _fetch_positions(self, address: str) -> list[dict]:
        """Fetch current open positions for a wallet."""
        for url_tmpl in [f"{self.host}/positions", "https://data-api.polymarket.com/positions"]:
            try:
                r = self._session.get(
                    url_tmpl,
                    params={"user": address},
                    timeout=10,
                )
                if r.status_code != 200:
                    continue
                data = r.json()
                positions = data if isinstance(data, list) else data.get("data", [])
                if positions:
                    return positions
            except Exception as e:
                logger.debug(f"WalletTracker: positions fetch failed for {address[:10]}…: {e}")
        return []

    # ------------------------------------------------------------------
    # Wallet update
    # ------------------------------------------------------------------
    def _update_wallet(self, address: str):
        """Update stats and recent trades for a single wallet."""
        with self._lock:
            stats = self._wallets.get(address)
            if not stats:
                stats = WalletStats(address=address, label=address[:10] + "...")
                self._wallets[address] = stats
            if time.time() - stats.last_updated < ACTIVITY_TTL:
                return

        activity = self._fetch_activity(address)
        positions = self._fetch_positions(address)

        # Compute win/loss from historical activity
        wins = losses = 0
        total_pnl = 0.0
        recent_trades = []
        now = time.time()

        for trade in activity:
            pnl = float(
                trade.get("profit")
                or trade.get("pnl")
                or trade.get("cashPnl")
                or 0
            )
            total_pnl += pnl
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1

            # Collect recent trades for copy-signal detection
            ts_raw = trade.get("timestamp") or trade.get("createdAt") or 0
            ts = float(ts_raw) if ts_raw else now
            age = now - ts
            if age < SIGNAL_MAX_AGE:
                recent_trades.append(trade)

        with self._lock:
            if wins + losses > 0:
                stats.total_trades = wins + losses
                stats.winning_trades = wins
                stats.total_pnl = total_pnl
            stats.recent_trades = recent_trades
            stats.last_updated = now

            if stats.is_smart and recent_trades:
                logger.debug(
                    f"Smart wallet {stats.label}: "
                    f"win={stats.win_rate:.0%} trades={stats.total_trades} "
                    f"pnl=${stats.total_pnl:.0f} fresh={len(recent_trades)}"
                )

        # Build copy trades from recent activity
        self._extract_copy_trades(stats, recent_trades)

        # Also check open positions for persistent signals
        self._extract_position_signals(stats, positions)

    def _extract_copy_trades(self, stats: WalletStats, trades: list[dict]):
        """Parse raw activity records into CopyTrade objects."""
        if not stats.is_smart:
            return
        now = time.time()
        for t in trades:
            token_id = (
                t.get("asset")
                or t.get("tokenId")
                or t.get("token_id")
            )
            condition_id = t.get("conditionId") or t.get("condition_id") or ""
            title = t.get("title") or t.get("name") or t.get("market") or condition_id[:20]
            outcome = str(t.get("outcome") or t.get("side") or "").upper()
            side = "YES" if outcome in ("YES", "1", "TRUE", "BUY") else "NO"
            price = float(t.get("price") or 0)
            size = float(t.get("size") or t.get("usdcSize") or 0)
            ts_raw = t.get("timestamp") or t.get("createdAt") or now
            ts = float(ts_raw) if ts_raw else now

            if not token_id or price <= 0 or size <= 0:
                continue
            if now - ts > SIGNAL_MAX_AGE:
                continue

            ct = CopyTrade(
                wallet_address=stats.address,
                wallet_label=stats.label,
                token_id=token_id,
                condition_id=condition_id,
                market_title=title,
                side=side,
                price=price,
                size=size,
                timestamp=ts,
                win_rate=stats.win_rate,
            )
            with self._lock:
                # Avoid duplicates
                existing = self._copy_trades.get(token_id, [])
                already = any(
                    abs(c.timestamp - ct.timestamp) < 5
                    and c.wallet_address == ct.wallet_address
                    for c in existing
                )
                if not already:
                    self._copy_trades[token_id].append(ct)

    def _extract_position_signals(self, stats: WalletStats, positions: list[dict]):
        """Parse current open positions as persistent copy-signals."""
        if not stats.is_smart:
            return
        now = time.time()
        for pos in positions:
            token_id = (
                pos.get("asset")
                or pos.get("token_id")
                or pos.get("tokenId")
                or pos.get("conditionId")
            )
            condition_id = pos.get("conditionId") or pos.get("condition_id") or ""
            title = pos.get("title") or pos.get("name") or pos.get("market") or condition_id[:20]
            outcome = str(pos.get("outcome") or pos.get("side") or "").upper()
            side = "YES" if outcome in ("YES", "1", "TRUE") else "NO"
            size = float(pos.get("size") or pos.get("currentValue") or 0)
            if not token_id or size <= 0:
                continue

            ct = CopyTrade(
                wallet_address=stats.address,
                wallet_label=stats.label,
                token_id=token_id,
                condition_id=condition_id,
                market_title=title,
                side=side,
                price=0.0,       # no entry price in position data
                size=size,
                timestamp=now,   # treat as "now" so it stays fresh
                win_rate=stats.win_rate,
            )
            with self._lock:
                existing = self._copy_trades.get(token_id, [])
                already = any(c.wallet_address == ct.wallet_address for c in existing)
                if not already:
                    self._copy_trades[token_id].append(ct)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def update(self, watched_token_ids: list[str] | None = None):
        """
        Refresh leaderboard and update smart wallets.
        Call every ~60s from the bot loop.
        """
        self._refresh_leaderboard()

        # Evict stale copy trades
        cutoff = time.time() - SIGNAL_MAX_AGE
        with self._lock:
            for token_id in list(self._copy_trades):
                self._copy_trades[token_id] = [
                    ct for ct in self._copy_trades[token_id]
                    if ct.timestamp >= cutoff
                ]
                if not self._copy_trades[token_id]:
                    del self._copy_trades[token_id]

        with self._lock:
            wallets_to_update = list(self._leaderboard_cache[:self.top_n])

        for addr in wallets_to_update:
            self._update_wallet(addr)

    def get_signal(self, token_id_yes: str, token_id_no: str) -> WalletSignal:
        """
        Return a signal for a market based on smart wallet positions.
        Compatible with bot.py's existing call signature.
        """
        with self._lock:
            yes_trades = [ct for ct in self._copy_trades.get(token_id_yes, []) if ct.is_fresh]
            no_trades  = [ct for ct in self._copy_trades.get(token_id_no, [])  if ct.is_fresh]

        yes_wr = [ct.win_rate for ct in yes_trades]
        no_wr  = [ct.win_rate for ct in no_trades]

        yes_count = len(yes_wr)
        no_count  = len(no_wr)

        if yes_count == 0 and no_count == 0:
            return WalletSignal(
                token_id=token_id_yes,
                smart_wallet_count=0,
                dominant_side="",
                avg_win_rate=0.0,
                confidence_boost=0.0,
            )

        if yes_count >= no_count:
            dominant = "YES"
            avg_wr = sum(yes_wr) / yes_count
            count  = yes_count
            boost  = min(0.15, count * 0.03 * avg_wr)
        else:
            dominant = "NO"
            avg_wr = sum(no_wr) / no_count
            count  = no_count
            boost  = -min(0.15, count * 0.03 * avg_wr)

        logger.info(
            f"[WALLET-SIGNAL] YES={yes_count} NO={no_count} smart wallets "
            f"→ {dominant} boost={boost:+.3f}"
        )
        return WalletSignal(
            token_id=token_id_yes,
            smart_wallet_count=yes_count + no_count,
            dominant_side=dominant,
            avg_win_rate=avg_wr,
            confidence_boost=boost,
        )

    def get_copy_trades(self, token_id: str) -> list[CopyTrade]:
        """Return fresh copy-trade signals for a specific token."""
        with self._lock:
            return [ct for ct in self._copy_trades.get(token_id, []) if ct.is_fresh]

    def evaluate_copy(self, token_id: str) -> tuple[str, float]:
        """
        Evaluate whether to copy smart-wallet trades on this token.
        Returns (side, confidence_boost) where side is "YES", "NO" or "".
        Uses Gemini to validate the signal when available.
        """
        trades = self.get_copy_trades(token_id)
        if not trades:
            return "", 0.0

        # Pick the most recent trade from the highest-win-rate wallet
        best = max(trades, key=lambda ct: (ct.win_rate, -ct.age_seconds))

        if self._gemini is not None:
            should_copy = self._gemini_should_copy(best)
            if not should_copy:
                logger.info(
                    f"[COPY-SKIP] Gemini said SKIP for {best.wallet_label} "
                    f"{best.side} on \"{best.market_title[:40]}\""
                )
                return "", 0.0

        boost = min(0.15, best.win_rate * 0.15)
        if best.side == "NO":
            boost = -boost

        logger.info(
            f"[COPY-TRADE] Following {best.wallet_label} "
            f"(win={best.win_rate:.0%}) → {best.side} boost={boost:+.3f} "
            f"on \"{best.market_title[:50]}\""
        )
        return best.side, boost

    def summary(self) -> str:
        """Short summary of tracked wallets for logging."""
        with self._lock:
            total = len(self._wallets)
            smart = sum(1 for s in self._wallets.values() if s.is_smart)
            active = sum(len(v) for v in self._copy_trades.values())
        return (
            f"WalletTracker: {total} tracked, {smart} smart "
            f"(≥{MIN_WIN_RATE:.0%} win rate), {active} fresh copy signals"
        )
