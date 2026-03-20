"""
Order Flow Imbalance (OFI) — "The Bloodhound"
==============================================
Analyses bid vs ask pressure from the Polymarket CLOB order book
BEFORE the spot price reacts.

Signal:
  OFI = (weighted_bid_volume - weighted_ask_volume)
        / (weighted_bid_volume + weighted_ask_volume)

  OFI ∈ [-1.0, +1.0]
  +1.0 = pure buy pressure  → bullish, raise q slightly
  -1.0 = pure sell pressure → bearish, lower q slightly

Integration with Stoikov:
  When sellers are aggressive (OFI < -THRESH), shift reservation
  price down so our limit-order is not overrun.

Integration with Bayesian q:
  OFI boost added to q (capped): q_adj = clip(q + ofi_boost, 0.01, 0.99)
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Top N levels to consider from the order book
TOP_LEVELS = 5

# How strongly OFI shifts q (max ±OFI_WEIGHT)
OFI_WEIGHT = 0.04

# Threshold below which we consider OFI negligible
OFI_SIGNAL_THRESH = 0.15

# Stoikov reservation price adjustment per unit OFI
STOIKOV_OFI_SHIFT = 0.012


@dataclass
class OFIResult:
    ofi: float              # [-1, +1]
    bid_pressure: float     # weighted bid volume
    ask_pressure: float     # weighted ask volume
    q_adjustment: float     # suggested additive change to Bayesian q
    stoikov_shift: float    # suggested shift to Stoikov reservation price
    signal: str             # "BUY_PRESSURE" | "SELL_PRESSURE" | "NEUTRAL"
    levels_used: int


def _weighted_volume(levels: list[dict], price_key: str, size_key: str, top: int) -> float:
    """
    Compute price-weighted volume for top N levels.
    Weight = price (bid) or (1 - price) for ask, to emphasise
    aggressive orders closest to mid.
    """
    total = 0.0
    for entry in levels[:top]:
        try:
            px = float(entry.get(price_key, 0) or 0)
            sz = float(entry.get(size_key, 0) or 0)
            if px > 0 and sz > 0:
                total += px * sz
        except (TypeError, ValueError):
            continue
    return total


class OFIModel:
    """
    Stateless OFI calculator.
    Call `evaluate(order_book_raw)` where order_book_raw is the dict
    returned by PolymarketDataClient.get_order_book().

    Expected structure:
      {
        "bids": [{"price": "0.52", "size": "120"}, ...],
        "asks": [{"price": "0.55", "size": "80"},  ...],
      }
    Bids sorted descending, asks ascending (standard CLOB format).
    """

    def evaluate(self, order_book: dict) -> OFIResult:
        bids = order_book.get("bids") or []
        asks = order_book.get("asks") or []

        if not bids and not asks:
            return OFIResult(
                ofi=0.0, bid_pressure=0.0, ask_pressure=0.0,
                q_adjustment=0.0, stoikov_shift=0.0,
                signal="NEUTRAL", levels_used=0,
            )

        bid_vol = _weighted_volume(bids, "price", "size", TOP_LEVELS)
        ask_vol = _weighted_volume(asks, "price", "size", TOP_LEVELS)

        total = bid_vol + ask_vol
        if total == 0:
            ofi = 0.0
        else:
            ofi = (bid_vol - ask_vol) / total

        # Clamp to [-1, +1] in case of numerical edge cases
        ofi = max(-1.0, min(1.0, ofi))

        levels_used = min(TOP_LEVELS, max(len(bids), len(asks)))

        if ofi > OFI_SIGNAL_THRESH:
            signal = "BUY_PRESSURE"
        elif ofi < -OFI_SIGNAL_THRESH:
            signal = "SELL_PRESSURE"
        else:
            signal = "NEUTRAL"

        # q adjustment: positive OFI → market more likely to go up
        q_adjustment = ofi * OFI_WEIGHT

        # Stoikov reservation price: shift DOWN when sellers dominate
        # (we don't want our limit hit at a bad price)
        stoikov_shift = ofi * STOIKOV_OFI_SHIFT

        if abs(ofi) > OFI_SIGNAL_THRESH:
            logger.debug(
                f"[OFI] {signal}: ofi={ofi:+.3f} "
                f"(bids={bid_vol:.1f} vs asks={ask_vol:.1f}) "
                f"→ q_adj={q_adjustment:+.4f}, stoikov_shift={stoikov_shift:+.4f}"
            )

        return OFIResult(
            ofi=ofi,
            bid_pressure=bid_vol,
            ask_pressure=ask_vol,
            q_adjustment=q_adjustment,
            stoikov_shift=stoikov_shift,
            signal=signal,
            levels_used=levels_used,
        )
