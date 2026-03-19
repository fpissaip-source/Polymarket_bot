"""
Wallet Tracker
==============
Tracks top-performing wallets on Polymarket and extracts trading signals.

Strategy:
  1. Fetch leaderboard to find consistently profitable wallets
  2. Analyze their recent activity (which tokens, which side, timing)
  3. Compute per-wallet win rate and confidence score
  4. For active markets: check if smart money has a position → Bayesian boost
"""

import time
import logging
from collections import defaultdict
from dataclasses import dataclass, field

import requests

from config import DATA_API_HOST

logger = logging.getLogger(__name__)

# Only trust wallets with this many trades or more
MIN_TRADES_FOR_SIGNAL = 10
# Minimum win rate to be considered a "smart wallet"
MIN_WIN_RATE = 0.58
# How old (seconds) a trade can be and still count as a signal
SIGNAL_MAX_AGE = 300  # 5 minutes
# Cache leaderboard for this many seconds before refreshing
LEADERBOARD_TTL = 600  # 10 minutes
# Cache wallet activity for this many seconds
ACTIVITY_TTL = 60  # 1 minute


@dataclass
class WalletStats:
    address: str
    total_trades: int = 0
    winning_trades: int = 0
    total_pnl: float = 0.0
    recent_positions: list = field(default_factory=list)  # [{token_id, side, timestamp, size}]
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
    """Signal derived from smart wallet activity for a specific token."""
    token_id: str
    smart_wallet_count: int      # How many smart wallets hold this position
    dominant_side: str           # "YES" or "NO"
    avg_win_rate: float          # Average win rate of those wallets
    confidence_boost: float      # Value in [-0.15, +0.15] to add to Bayesian prior

    @property
    def has_signal(self) -> bool:
        return self.smart_wallet_count > 0


class WalletTracker:
    """
    Tracks profitable wallets and generates trading signals.
    Integrates with the Bayesian model as an additional data source.
    """

    def __init__(self, host: str = DATA_API_HOST, top_n: int = 50):
        self.host = host.rstrip("/")
        self.top_n = top_n
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

        self._wallets: dict[str, WalletStats] = {}
        self._leaderboard_cache: list[str] = []
        self._leaderboard_updated: float = 0.0

        # token_id → list of (address, side, timestamp)
        self._token_positions: dict[str, list] = defaultdict(list)

    # ------------------------------------------------------------------
    # Leaderboard
    # ------------------------------------------------------------------
    def _fetch_leaderboard(self) -> list[str]:
        """Fetch top wallet addresses from Polymarket leaderboard."""
        try:
            r = self._session.get(
                f"{self.host}/leaderboard",
                params={"limit": self.top_n, "window": "all"},
                timeout=10,
            )
            if r.status_code == 404:
                # Try alternate endpoint
                r = self._session.get(
                    f"{self.host}/leaderboard",
                    params={"limit": self.top_n},
                    timeout=10,
                )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                entries = data
            else:
                entries = data.get("data", data.get("leaderboard", []))

            addresses = []
            for entry in entries:
                addr = (entry.get("proxyWallet") or entry.get("proxy_wallet") or
                        entry.get("address") or entry.get("user"))
                if addr:
                    addresses.append(addr)
            logger.info(f"WalletTracker: fetched {len(addresses)} wallets from leaderboard")
            return addresses
        except Exception as e:
            logger.warning(f"WalletTracker: leaderboard fetch failed: {e}")
            return []

    def _refresh_leaderboard(self):
        """Refresh leaderboard cache if stale."""
        if time.time() - self._leaderboard_updated < LEADERBOARD_TTL:
            return
        addresses = self._fetch_leaderboard()
        if addresses:
            self._leaderboard_cache = addresses
            self._leaderboard_updated = time.time()
            # Initialize WalletStats for new wallets
            for addr in addresses:
                if addr not in self._wallets:
                    self._wallets[addr] = WalletStats(address=addr)

    # ------------------------------------------------------------------
    # Activity fetching
    # ------------------------------------------------------------------
    def _fetch_activity(self, address: str) -> list[dict]:
        """Fetch recent trade activity for a wallet."""
        try:
            r = self._session.get(
                f"{self.host}/activity",
                params={"user": address, "limit": 50},
                timeout=10,
            )
            if r.status_code != 200:
                return []
            data = r.json()
            if isinstance(data, list):
                return data
            return data.get("data", [])
        except Exception as e:
            logger.debug(f"WalletTracker: activity fetch failed for {address[:10]}...: {e}")
            return []

    def _fetch_positions(self, address: str) -> list[dict]:
        """Fetch current open positions for a wallet."""
        try:
            r = self._session.get(
                f"{self.host}/positions",
                params={"user": address},
                timeout=10,
            )
            if r.status_code != 200:
                return []
            data = r.json()
            if isinstance(data, list):
                return data
            return data.get("data", [])
        except Exception as e:
            logger.debug(f"WalletTracker: positions fetch failed for {address[:10]}...: {e}")
            return []

    def _update_wallet(self, address: str):
        """Update stats and positions for a single wallet."""
        stats = self._wallets.get(address)
        if not stats:
            stats = WalletStats(address=address)
            self._wallets[address] = stats

        # Don't refresh too often
        if time.time() - stats.last_updated < ACTIVITY_TTL:
            return

        activity = self._fetch_activity(address)
        positions = self._fetch_positions(address)

        # Compute win/loss from activity
        wins = losses = 0
        total_pnl = 0.0
        for trade in activity:
            pnl = float(trade.get("profit", trade.get("pnl", trade.get("cashPnl", 0))) or 0)
            total_pnl += pnl
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1

        if wins + losses > 0:
            stats.total_trades = wins + losses
            stats.winning_trades = wins
            stats.total_pnl = total_pnl

        # Extract recent positions with token IDs
        now = time.time()
        recent = []
        for pos in positions:
            token_id = (pos.get("asset") or pos.get("token_id") or
                        pos.get("tokenId") or pos.get("conditionId"))
            outcome = str(pos.get("outcome", pos.get("side", "")) or "").upper()
            size = float(pos.get("size", pos.get("currentValue", 0)) or 0)
            # Use current time as proxy if no timestamp
            ts = float(pos.get("timestamp", pos.get("createdAt", now)) or now)
            if token_id and size > 0:
                side = "YES" if outcome in ("YES", "1", "TRUE") else "NO"
                recent.append({
                    "token_id": token_id,
                    "side": side,
                    "timestamp": ts,
                    "size": size,
                })

        stats.recent_positions = recent
        stats.last_updated = now

        if stats.is_smart:
            logger.debug(
                f"Smart wallet {address[:10]}...: "
                f"win_rate={stats.win_rate:.1%}, trades={stats.total_trades}, "
                f"pnl=${stats.total_pnl:.2f}, positions={len(recent)}"
            )

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------
    def update(self, watched_token_ids: list[str] | None = None):
        """
        Refresh leaderboard and update a sample of smart wallets.
        Call this periodically (e.g. every 60s) from the bot loop.
        watched_token_ids: if provided, only update wallets holding these tokens.
        """
        self._refresh_leaderboard()

        # Update top wallets (staggered to avoid rate limits)
        for addr in self._leaderboard_cache[:self.top_n]:
            self._update_wallet(addr)

    def get_signal(self, token_id_yes: str, token_id_no: str) -> WalletSignal:
        """
        Return a signal for a market based on smart wallet positions.

        Checks which smart wallets have recent positions in this market
        and returns a confidence boost for the Bayesian model.
        """
        now = time.time()
        yes_smart = []
        no_smart = []

        for addr, stats in self._wallets.items():
            if not stats.is_smart:
                continue
            for pos in stats.recent_positions:
                tid = pos["token_id"]
                age = now - pos["timestamp"]
                if age > SIGNAL_MAX_AGE:
                    continue
                if tid == token_id_yes:
                    yes_smart.append(stats.win_rate)
                elif tid == token_id_no:
                    no_smart.append(stats.win_rate)

        yes_count = len(yes_smart)
        no_count = len(no_smart)

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
            avg_wr = sum(yes_smart) / yes_count if yes_smart else 0.0
            count = yes_count
            # Positive boost: smart money says YES
            boost = min(0.15, count * 0.03 * avg_wr)
        else:
            dominant = "NO"
            avg_wr = sum(no_smart) / no_count if no_smart else 0.0
            count = no_count
            # Negative boost: smart money says NO (reduces YES probability)
            boost = -min(0.15, count * 0.03 * avg_wr)

        if yes_count + no_count > 0:
            logger.info(
                f"WalletSignal: YES={yes_count} NO={no_count} smart wallets "
                f"→ {dominant} boost={boost:+.3f}"
            )

        return WalletSignal(
            token_id=token_id_yes,
            smart_wallet_count=yes_count + no_count,
            dominant_side=dominant,
            avg_win_rate=avg_wr,
            confidence_boost=boost,
        )

    def summary(self) -> str:
        """Short summary of tracked wallets for logging."""
        smart = [s for s in self._wallets.values() if s.is_smart]
        return (
            f"WalletTracker: {len(self._wallets)} tracked, "
            f"{len(smart)} smart (≥{MIN_WIN_RATE:.0%} win rate)"
        )
