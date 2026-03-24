"""
Stealth Execution Engine
=========================
Implements execution discipline and trade masking techniques from the document.

Institutional-level execution that minimizes market impact (slippage) and
hides strategy patterns from HFT algorithms and broker flow analysis.

Techniques implemented:
  1. Position Size Variation — Never use exact round lot sizes
  2. Order Slicing (TWAP) — Split large orders over time intervals
  3. Noise Trading — Occasional random trades to mask the core edge
  4. Laddering — Buy in steps, ensuring each buy is below average price
  5. Execution Timing Jitter — Random delays to prevent pattern detection

This module wraps the OrderExecutor to add stealth behavior transparently.
"""

import logging
import random
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class StealthOrder:
    """A single slice of a stealth-executed order."""
    original_size: float        # total order size requested
    slice_size: float           # this slice's size
    slice_index: int            # which slice (0-based)
    total_slices: int           # total number of slices
    delay_seconds: float        # wait before placing this slice
    price_limit: float          # limit price for this slice
    size_noise: float           # random variation applied to size


class StealthExecutor:
    """
    Wraps order placement to add stealth execution techniques.

    Usage:
        stealth = StealthExecutor()
        slices = stealth.plan_execution(
            total_size=5.00,
            base_price=0.72,
            urgency=0.5,           # 0=patient, 1=aggressive
        )
        for s in slices:
            time.sleep(s.delay_seconds)
            executor.place_order(size=s.slice_size, price=s.price_limit)
    """

    # Below this USD amount, don't slice (not worth the complexity)
    MIN_SLICE_THRESHOLD = 3.00

    # Maximum number of slices for any single order
    MAX_SLICES = 5

    # Size variation: +/- this percentage to avoid round lots
    SIZE_NOISE_PCT = 0.08       # 8% variation

    # Base delay between slices (seconds)
    BASE_DELAY = 3.0

    def __init__(self):
        self._noise_trade_counter = 0
        self._noise_trade_interval = random.randint(15, 30)  # every 15-30 trades

    def plan_execution(
        self,
        total_size: float,
        base_price: float,
        urgency: float = 0.5,
        tick_size: float = 0.01,
    ) -> list[StealthOrder]:
        """
        Plan stealth execution of an order.

        total_size : total USD amount to trade
        base_price : target price (0-1 for prediction markets)
        urgency    : 0.0=very patient (more slices, more delay)
                     1.0=very urgent (fewer slices, less delay)
        tick_size  : minimum price increment

        Returns list of StealthOrders to execute sequentially.
        """
        # Apply size noise (never exact round numbers)
        total_size = self._apply_size_noise(total_size)

        # Small orders: single slice with noise
        if total_size < self.MIN_SLICE_THRESHOLD:
            return [StealthOrder(
                original_size=total_size,
                slice_size=round(total_size, 2),
                slice_index=0,
                total_slices=1,
                delay_seconds=0.0,
                price_limit=base_price,
                size_noise=0.0,
            )]

        # Determine number of slices based on size and urgency
        # Larger orders + lower urgency = more slices
        raw_slices = max(1, int(total_size / 2.0))  # ~1 slice per $2
        urgency_factor = max(1, int(raw_slices * (1.0 - urgency * 0.7)))
        n_slices = min(self.MAX_SLICES, max(1, urgency_factor))

        # Distribute size across slices (not equally — use varying sizes)
        slice_sizes = self._distribute_slices(total_size, n_slices)

        # Build execution plan with TWAP-style timing
        slices = []
        for i, size in enumerate(slice_sizes):
            # Delay: increases with slice index, decreases with urgency
            if i == 0:
                delay = 0.0  # first slice: immediate
            else:
                base = self.BASE_DELAY * (1.0 - urgency * 0.6)
                jitter = random.uniform(0.5, 2.0)  # timing jitter
                delay = base * jitter

            # Price improvement for later slices (laddering)
            # Each subsequent slice gets a slightly better price
            price_adjustment = i * float(tick_size)
            ladder_price = base_price - price_adjustment  # buying lower
            ladder_price = max(0.01, round(ladder_price / float(tick_size)) * float(tick_size))

            slices.append(StealthOrder(
                original_size=total_size,
                slice_size=round(size, 2),
                slice_index=i,
                total_slices=n_slices,
                delay_seconds=round(delay, 2),
                price_limit=ladder_price,
                size_noise=size / (total_size / n_slices) - 1.0,
            ))

        return slices

    def _apply_size_noise(self, size: float) -> float:
        """
        Add random variation to avoid exact round lot sizes.
        This makes the bot's orders harder to identify by pattern recognition.
        """
        noise = random.uniform(-self.SIZE_NOISE_PCT, self.SIZE_NOISE_PCT)
        noisy_size = size * (1.0 + noise)
        # Ensure we avoid exact round numbers ($1.00, $2.00, $5.00, etc.)
        rounded = round(noisy_size, 2)
        if rounded == round(rounded):  # exact dollar amount
            rounded += random.choice([-0.03, -0.07, 0.02, 0.04, 0.08, 0.13])
        return max(0.50, round(rounded, 2))

    def _distribute_slices(self, total: float, n: int) -> list[float]:
        """
        Distribute total size across N slices with variation.
        Not equal slices — uses random distribution to mask patterns.
        """
        if n == 1:
            return [total]

        # Generate random weights
        weights = [random.uniform(0.6, 1.4) for _ in range(n)]
        weight_sum = sum(weights)

        # Normalize and apply
        slices = [total * w / weight_sum for w in weights]

        # Ensure minimum slice size
        min_slice = max(0.50, total * 0.1)
        slices = [max(min_slice, s) for s in slices]

        # Adjust last slice to match total
        current_total = sum(slices[:-1])
        slices[-1] = max(min_slice, total - current_total)

        return slices

    def should_noise_trade(self) -> bool:
        """
        Determine if the bot should place a noise trade.
        Noise trades are small random trades that create statistical
        noise in the order log, making the real strategy harder to detect.
        """
        self._noise_trade_counter += 1
        if self._noise_trade_counter >= self._noise_trade_interval:
            self._noise_trade_counter = 0
            self._noise_trade_interval = random.randint(15, 30)
            return True
        return False

    def get_noise_trade_params(self, bankroll: float) -> dict:
        """
        Generate parameters for a noise trade.
        Size: 0.5-1.5% of bankroll (small enough to not matter).
        """
        size_pct = random.uniform(0.005, 0.015)
        size = round(bankroll * size_pct, 2)
        size = max(0.50, size)

        return {
            "size": size,
            "is_noise": True,
            "reason": "stealth_noise_trade",
        }

    def compute_execution_urgency(
        self,
        edge_size: float,
        time_remaining: float,
        book_depth: float = 1.0,
    ) -> float:
        """
        Compute how urgently we need to fill this order.

        Higher urgency → fewer slices, faster execution.
        Lower urgency → more slices, better average price.

        edge_size      : EV of the trade (larger = more urgent, edge may close)
        time_remaining : seconds until market closes
        book_depth     : order book depth relative to our size (0-1)
        """
        urgency = 0.5  # base

        # Large edge → more urgent (it will close)
        if edge_size > 0.10:
            urgency += 0.2
        elif edge_size > 0.05:
            urgency += 0.1

        # Low time remaining → more urgent
        if time_remaining < 300:  # <5 min
            urgency += 0.3
        elif time_remaining < 1800:  # <30 min
            urgency += 0.1

        # Shallow book → more patient (avoid impact)
        if book_depth < 0.5:
            urgency -= 0.2

        return max(0.0, min(1.0, urgency))
