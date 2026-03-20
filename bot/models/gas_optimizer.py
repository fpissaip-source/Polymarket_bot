"""
Gas & Latency Optimizer — "The Mechanic"
=========================================
On Polygon, gas price spikes can silently eat your edge.
This module:

  1. Fetches current Polygon gas price (Gwei) from a free public endpoint.
  2. Estimates the USD cost of the on-chain settlement transaction.
  3. Vetoes the trade if:
       gas_cost_usd > GAS_VETO_RATIO * expected_pnl

  expected_pnl = edge * stake

In dry-run mode, uses a simulated MATIC price and median gas cost
so the model exercises the logic without live network calls.

Polygon gas API (free, no key required):
  https://gasstation.polygon.technology/v2
  Returns { safeLow: {maxFee, maxPriorityFee}, standard: {...}, fast: {...} }

MATIC price: fetched from CoinGecko (same source as spot prices in price_feed.py).
"""

import logging
import time
import requests

logger = logging.getLogger(__name__)

# Veto threshold: gas_cost must be < this fraction of expected edge PnL
GAS_VETO_RATIO = 0.30          # 30%

# Estimated gas units for a Polymarket CLOB settlement tx
GAS_UNITS_ESTIMATE = 120_000   # ~120k gas for ERC-20 + conditional token ops

# Cache gas data for this many seconds before re-fetching
GAS_CACHE_TTL = 60

# Fallback values used when API is unreachable
FALLBACK_GWEI   = 50.0         # 50 Gwei — conservative estimate
FALLBACK_MATIC  = 0.80         # $0.80 per MATIC — recent average

POLYGON_GAS_API = "https://gasstation.polygon.technology/v2"
MATIC_PRICE_API = "https://api.coingecko.com/api/v3/simple/price?ids=matic-network&vs_currencies=usd"


class GasOptimizer:
    """
    Singleton-style gas checker.
    Call `should_trade(edge, stake, dry_run)` → (bool, reason_str).
    """

    def __init__(self):
        self._cached_gwei: float = FALLBACK_GWEI
        self._cached_matic_usd: float = FALLBACK_MATIC
        self._last_fetch: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_trade(
        self,
        edge: float,
        stake: float,
        dry_run: bool = False,
    ) -> tuple[bool, str]:
        """
        Returns (True, "") if gas cost is acceptable.
        Returns (False, reason) if gas would eat >30% of expected PnL.

        edge  : expected EV fraction (e.g. 0.04 for 4%)
        stake : USD size of the trade
        """
        if dry_run:
            gas_usd, gwei, matic = self._simulate_gas()
        else:
            gas_usd, gwei, matic = self._fetch_gas_cost()

        expected_pnl = edge * stake
        if expected_pnl <= 0:
            return True, ""   # No expected PnL → gas check irrelevant

        ratio = gas_usd / expected_pnl
        reason = (
            f"gas={gas_usd:.4f}USD "
            f"({gwei:.1f} Gwei, MATIC=${matic:.3f}) "
            f"= {ratio:.1%} of expected PnL ${expected_pnl:.4f}"
        )

        if ratio > GAS_VETO_RATIO:
            logger.warning(
                f"[GAS VETO] Gas cost too high: {reason} "
                f"(threshold {GAS_VETO_RATIO:.0%})"
            )
            return False, f"Gas veto: {reason}"

        logger.debug(f"[GAS OK] {reason}")
        return True, ""

    def get_gas_info(self, dry_run: bool = False) -> dict:
        """Return current gas info as a dict for dashboard/logging."""
        if dry_run:
            gas_usd, gwei, matic = self._simulate_gas()
        else:
            gas_usd, gwei, matic = self._fetch_gas_cost()
        return {
            "gwei": round(gwei, 2),
            "matic_usd": round(matic, 4),
            "gas_cost_usd": round(gas_usd, 6),
            "gas_units": GAS_UNITS_ESTIMATE,
            "veto_ratio": GAS_VETO_RATIO,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _simulate_gas(self) -> tuple[float, float, float]:
        """Dry-run: use fixed representative values."""
        gwei = 35.0
        matic = FALLBACK_MATIC
        gas_usd = _gwei_to_usd(gwei, matic)
        return gas_usd, gwei, matic

    def _fetch_gas_cost(self) -> tuple[float, float, float]:
        now = time.time()
        if now - self._last_fetch < GAS_CACHE_TTL:
            gas_usd = _gwei_to_usd(self._cached_gwei, self._cached_matic_usd)
            return gas_usd, self._cached_gwei, self._cached_matic_usd

        gwei = _fetch_polygon_gas()
        matic = _fetch_matic_price()

        self._cached_gwei = gwei
        self._cached_matic_usd = matic
        self._last_fetch = now

        gas_usd = _gwei_to_usd(gwei, matic)
        logger.debug(
            f"[GAS] Fetched: {gwei:.1f} Gwei, MATIC=${matic:.4f} "
            f"→ tx cost ≈ ${gas_usd:.5f}"
        )
        return gas_usd, gwei, matic


# ------------------------------------------------------------------
# Module-level helpers (pure functions)
# ------------------------------------------------------------------

def _gwei_to_usd(gwei: float, matic_usd: float) -> float:
    """Convert gas usage to USD cost."""
    matic_cost = (gwei * 1e-9) * GAS_UNITS_ESTIMATE   # in MATIC
    return matic_cost * matic_usd


def _fetch_polygon_gas() -> float:
    """Fetch standard gas price in Gwei from Polygon Gas Station."""
    try:
        r = requests.get(POLYGON_GAS_API, timeout=5)
        r.raise_for_status()
        data = r.json()
        standard = data.get("standard") or data.get("safeLow") or {}
        gwei = float(standard.get("maxFee", FALLBACK_GWEI))
        return gwei
    except Exception as e:
        logger.debug(f"[GAS] Gas API unavailable ({e}), using fallback {FALLBACK_GWEI} Gwei")
        return FALLBACK_GWEI


def _fetch_matic_price() -> float:
    """Fetch MATIC/USD from CoinGecko."""
    try:
        r = requests.get(MATIC_PRICE_API, timeout=5)
        r.raise_for_status()
        data = r.json()
        price = float(data["matic-network"]["usd"])
        return price
    except Exception as e:
        logger.debug(f"[GAS] MATIC price fetch failed ({e}), using ${FALLBACK_MATIC}")
        return FALLBACK_MATIC
