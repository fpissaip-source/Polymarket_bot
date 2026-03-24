"""
Kelly Position Sizing — Risk-Constrained
==========================================
Determines how much capital to allocate to each opportunity.

Classic Kelly formula:
  f* = (b * p - q) / b
  f* = optimal fraction of capital
  b  = payoff per unit of risk (e.g. 1/(1-price) - 1 for binary markets)
  p  = probability of success (our model's q)
  q  = 1 - p (probability of failure)

Fractional Kelly (safer in practice):
  f = lambda * f*    (lambda < 1, typically 0.25–0.5)

Risk-Constrained Kelly (from document):
  Introduces a drawdown constraint to minimize the probability of
  capital dropping below a threshold (alpha). The position size is
  dynamically adjusted based on current drawdown level:
    - drawdown < 10%:  normal sizing (1.0x)
    - drawdown 10-20%: reduced sizing (0.7x)
    - drawdown 20-30%: defensive sizing (0.4x)
    - drawdown > 30%:  survival mode (0.2x)

  This ensures the equity curve remains stable during losing streaks.

Also supports Optimal F (Ralph Vince): considers variable win/loss
sizes for more precise sizing in markets with unequal payoffs.

The Kelly layer also accounts for:
  - Edge size
  - Probability of full execution
  - Order book depth
  - Speed at which the dislocation is closing
  - Total capital already committed in other positions
  - Drawdown-based risk reduction
  - Alpha cluster quality multiplier
  - AI Gate confidence multiplier
"""

from dataclasses import dataclass
from config import KELLY_FRACTION, KELLY_MAX_FRACTION, BANKROLL


@dataclass
class KellyResult:
    f_star: float           # full Kelly fraction
    f_kelly: float          # fractional Kelly fraction (lambda * f*)
    position_size: float    # dollar amount to allocate
    is_viable: bool         # True if f* > 0
    description: str
    drawdown_adjusted: bool = False  # True if drawdown protection reduced size


class KellyModel:
    def __init__(
        self,
        bankroll: float = BANKROLL,
        lambda_fraction: float = KELLY_FRACTION,
        max_fraction: float = KELLY_MAX_FRACTION,
    ):
        self.bankroll = bankroll
        self.lambda_fraction = lambda_fraction
        self.max_fraction = max_fraction
        self.committed_capital: float = 0.0

    def compute(
        self,
        p_success: float,
        market_price: float,
        exec_probability: float = 1.0,
        ob_depth_factor: float = 1.0,
        dislocation_speed: float = 1.0,
        drawdown_multiplier: float = 1.0,
        cluster_multiplier: float = 1.0,
        gate_multiplier: float = 1.0,
    ) -> KellyResult:
        """
        Compute optimal position size with risk-constrained Kelly.

        p_success          : model's estimated probability of winning
        market_price       : current market price (0–1)
        exec_probability   : probability that both legs fill fully [0,1]
        ob_depth_factor    : how deep the order book is relative to our size [0,1]
        dislocation_speed  : how fast the spread is closing (1=fast, 0=slow) [0,1]
        drawdown_multiplier: risk-constrained Kelly drawdown adjustment [0.2-1.0]
        cluster_multiplier : alpha cluster quality multiplier [0.5-1.3]
        gate_multiplier    : AI gate confidence multiplier [0.3-1.5]
        """
        p = p_success
        q_fail = 1.0 - p

        # Payoff ratio for binary market: win (1 - price) per unit, risk price per unit
        if market_price <= 0.01 or market_price >= 0.99:
            return KellyResult(0.0, 0.0, 0.0, False, "Market price out of range")

        b = (1.0 - market_price) / market_price

        # Full Kelly
        f_star = (b * p - q_fail) / b

        if f_star <= 0:
            return KellyResult(
                f_star=f_star,
                f_kelly=0.0,
                position_size=0.0,
                is_viable=False,
                description=f"Negative Kelly f*={f_star:.4f}: no edge after costs"
            )

        # Fractional Kelly (Half-Kelly style — conservative base)
        f_kelly = self.lambda_fraction * f_star

        # Adjust for execution risk and liquidity
        f_adjusted = f_kelly * exec_probability * ob_depth_factor

        # If spread closing fast, reduce size (less time to complete structure)
        if dislocation_speed > 0.8:
            f_adjusted *= 0.7

        # Risk-Constrained Kelly: apply drawdown protection
        dd_adjusted = drawdown_multiplier < 0.99
        f_adjusted *= drawdown_multiplier

        # Alpha cluster quality scaling
        f_adjusted *= cluster_multiplier

        # AI gate confidence scaling
        f_adjusted *= gate_multiplier

        # Cap at max fraction
        f_final = min(f_adjusted, self.max_fraction)

        # Available capital (subtract already committed)
        available = max(0.0, self.bankroll - self.committed_capital)
        position_size = f_final * available

        return KellyResult(
            f_star=f_star,
            f_kelly=f_final,
            position_size=round(position_size, 2),
            is_viable=True,
            description=(
                f"f*={f_star:.4f}, λ={self.lambda_fraction}, "
                f"f_adj={f_adjusted:.4f} → ${position_size:.2f} "
                f"(exec={exec_probability:.2f}, ob={ob_depth_factor:.2f}, "
                f"dd={drawdown_multiplier:.2f}, cluster={cluster_multiplier:.2f}, "
                f"gate={gate_multiplier:.2f})"
            ),
            drawdown_adjusted=dd_adjusted,
        )

    def allocate(self, amount: float):
        """Mark capital as committed to an open position."""
        self.committed_capital += amount
        self.committed_capital = min(self.committed_capital, self.bankroll)

    def release(self, amount: float):
        """Release capital from a closed position."""
        self.committed_capital = max(0.0, self.committed_capital - amount)

    def update_bankroll(self, pnl: float):
        """Update bankroll after a trade result."""
        self.bankroll = max(0.0, self.bankroll + pnl)

    @property
    def available_capital(self) -> float:
        return max(0.0, self.bankroll - self.committed_capital)

    @property
    def utilization(self) -> float:
        if self.bankroll <= 0:
            return 1.0
        return self.committed_capital / self.bankroll
