"""
Stoikov Execution Model
=======================
Used for quality execution of an arbitrage structure.

The key reservation price formula:
  r = s - q * gamma * sigma^2 * (T - t)

  r     = internal quoting price (adjusted bid/ask)
  s     = mid price
  q     = current inventory (signed: positive = long, negative = short)
  gamma = risk aversion coefficient
  sigma^2 = variance of the market price
  (T-t) = remaining time until market closes

The larger the inventory imbalance and the higher the risk,
the more the bot adjusts its execution price.

The Stoikov layer:
  - Does not keep mechanically increasing the accumulated side
  - More actively completes the missing part of the structure
  - Repositions limit orders based on current inventory imbalance
  - Decides when to remain passive vs switch to aggressive execution
"""

from dataclasses import dataclass
from config import STOIKOV_GAMMA, STOIKOV_SIGMA_DEFAULT


@dataclass
class StoikovQuote:
    reservation_price: float   # r = s - q*gamma*sigma^2*(T-t)
    bid: float                 # reservation_price - half_spread
    ask: float                 # reservation_price + half_spread
    half_spread: float
    is_aggressive: bool        # True = use market order, False = limit order
    action: str                # human-readable recommendation


class StoikovModel:
    def __init__(
        self,
        gamma: float = STOIKOV_GAMMA,
        sigma: float = STOIKOV_SIGMA_DEFAULT,
    ):
        self.gamma = gamma
        self.sigma = sigma
        self.inventory: float = 0.0        # current net position (signed)
        self.target_inventory: float = 0.0  # desired net position

    def quote(
        self,
        mid_price: float,
        remaining_time: float,
        sigma: float | None = None,
    ) -> StoikovQuote:
        """
        Compute reservation price and optimal quotes.

        mid_price      : current mid price of the market (0 to 1)
        remaining_time : (T - t) in minutes, normalized to [0, 1]
        sigma          : variance override (uses default if None)
        """
        s = mid_price
        q = self.inventory
        gamma = self.gamma
        sigma2 = (sigma if sigma is not None else self.sigma) ** 2

        # Reservation price: penalize for holding inventory
        r = s - q * gamma * sigma2 * remaining_time
        r = max(0.01, min(0.99, r))

        # Optimal half-spread (simplified Stoikov formula)
        half_spread = gamma * sigma2 * remaining_time / 2.0
        half_spread = max(0.005, half_spread)  # minimum 0.5% spread

        bid = max(0.01, r - half_spread)
        ask = min(0.99, r + half_spread)

        # Decide passive vs aggressive based on inventory imbalance
        inventory_imbalance = abs(q - self.target_inventory)
        is_aggressive = inventory_imbalance > 0.5 or remaining_time < 0.1

        if is_aggressive:
            action = (
                f"AGGRESSIVE: inventory imbalance={inventory_imbalance:.2f}, "
                f"remaining_time={remaining_time:.2f} → use market order at {r:.4f}"
            )
        else:
            action = (
                f"PASSIVE: post bid={bid:.4f}, ask={ask:.4f} "
                f"(reservation={r:.4f}, half_spread={half_spread:.4f})"
            )

        return StoikovQuote(
            reservation_price=r,
            bid=bid,
            ask=ask,
            half_spread=half_spread,
            is_aggressive=is_aggressive,
            action=action,
        )

    def update_inventory(self, filled_qty: float, side: str):
        """
        side: "YES" = +qty, "NO" = -qty
        """
        if side == "YES":
            self.inventory += filled_qty
        else:
            self.inventory -= filled_qty

    def set_target(self, target: float):
        """Set the desired net inventory (e.g. 0 for delta-neutral)."""
        self.target_inventory = target

    def inventory_risk(self, sigma: float | None = None, remaining_time: float = 1.0) -> float:
        """
        Estimate current inventory risk exposure.
        Higher value = more urgent to rebalance.
        """
        sigma2 = (sigma if sigma is not None else self.sigma) ** 2
        return abs(self.inventory) * self.gamma * sigma2 * remaining_time

    def reposition_needed(self, threshold: float = 0.3) -> bool:
        return abs(self.inventory - self.target_inventory) > threshold
