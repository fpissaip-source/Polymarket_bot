"""
Monte Carlo Simulation
======================
Tests whether the overall system is viable in a real market.

Capital dynamics: W(t+1) = W(t) * (1 + r(t))
  W(t)  = capital at time t
  r(t)  = return of a trade or trade sequence

Maximum drawdown: DD = max((Peak(t) - W(t)) / Peak(t))

The simulation injects realistic market friction:
  - Different fill rates (partial fills)
  - Different levels of slippage
  - Random execution delays
  - Different percentages of incomplete structures
  - Probability estimation errors
  - Clusters of consecutive unsuccessful trades

Returns: expected growth, possible drawdown, and robustness metrics.
"""

import random
import math
from dataclasses import dataclass
from config import MC_SIMULATIONS, MC_TRADES, MC_MAX_DD_LIMIT


@dataclass
class MonteCarloResult:
    median_final_capital: float
    p5_final_capital: float
    p25_final_capital: float
    p75_final_capital: float
    p95_final_capital: float
    median_max_drawdown: float
    p95_max_drawdown: float
    survival_rate: float        # fraction of paths that did not blow up
    expected_growth_pct: float  # median growth as %
    is_viable: bool
    description: str


class MonteCarloSimulator:
    def __init__(
        self,
        n_simulations: int = MC_SIMULATIONS,
        n_trades: int = MC_TRADES,
        max_dd_limit: float = MC_MAX_DD_LIMIT,
        initial_capital: float = 100.0,
    ):
        self.n_simulations = n_simulations
        self.n_trades = n_trades
        self.max_dd_limit = max_dd_limit
        self.initial_capital = initial_capital

    def run(
        self,
        base_ev: float,
        base_win_rate: float,
        avg_position_fraction: float,
        partial_fill_rate: float = 0.85,
        slippage_std: float = 0.005,
        incomplete_structure_rate: float = 0.10,
        estimation_error_std: float = 0.02,
        cluster_prob: float = 0.05,
        cluster_length: int = 5,
    ) -> MonteCarloResult:
        """
        Run Monte Carlo simulation.

        base_ev              : expected value per trade (e.g. 0.03 = 3%)
        base_win_rate        : probability of winning a trade
        avg_position_fraction: fraction of capital per trade
        partial_fill_rate    : probability that the structure fills completely
        slippage_std         : std of random slippage per trade
        incomplete_structure_rate: fraction of trades where only 1 leg fills
        estimation_error_std : noise in probability estimation
        cluster_prob         : probability of entering a losing cluster
        cluster_length       : number of consecutive losses in a cluster
        """
        final_capitals = []
        max_drawdowns = []
        survivals = 0

        for _ in range(self.n_simulations):
            capital = self.initial_capital
            peak = capital
            max_dd = 0.0
            in_cluster = False
            cluster_remaining = 0

            for _ in range(self.n_trades):
                if capital <= 0:
                    break

                # Determine if we're in a losing cluster
                if not in_cluster and random.random() < cluster_prob:
                    in_cluster = True
                    cluster_remaining = cluster_length

                # Adjust win probability
                if in_cluster:
                    win_prob = base_win_rate * 0.3
                    cluster_remaining -= 1
                    if cluster_remaining <= 0:
                        in_cluster = False
                else:
                    # Add estimation error
                    noise = random.gauss(0, estimation_error_std)
                    win_prob = max(0.0, min(1.0, base_win_rate + noise))

                # Determine position size (fraction of current capital)
                f = avg_position_fraction * random.uniform(0.8, 1.2)
                f = min(f, 0.5)  # cap at 50%
                position = capital * f

                # Fill rate (partial fills)
                fill = 1.0 if random.random() < partial_fill_rate else random.uniform(0.3, 0.7)

                # Incomplete structure (only 1 leg fills = directional risk)
                if random.random() < incomplete_structure_rate:
                    # Only half the hedge fills → higher variance
                    fill *= 0.5
                    effective_ev = base_ev * 0.3  # much lower effective edge
                else:
                    effective_ev = base_ev

                # Slippage
                slippage = abs(random.gauss(0, slippage_std))

                # Trade outcome
                if random.random() < win_prob:
                    r = effective_ev - slippage
                else:
                    r = -(effective_ev + slippage) * 1.5  # losses tend to be larger

                # Apply return to filled portion
                capital_change = position * fill * r
                capital = capital * (1.0 + f * fill * r)
                capital = max(0.0, capital)

                # Track drawdown
                if capital > peak:
                    peak = capital
                if peak > 0:
                    dd = (peak - capital) / peak
                    max_dd = max(max_dd, dd)

            final_capitals.append(capital)
            max_drawdowns.append(max_dd)
            if capital > 0 and max_dd < self.max_dd_limit:
                survivals += 1

        final_capitals.sort()
        max_drawdowns.sort()

        n = len(final_capitals)
        p5 = final_capitals[int(0.05 * n)]
        p25 = final_capitals[int(0.25 * n)]
        median = final_capitals[int(0.50 * n)]
        p75 = final_capitals[int(0.75 * n)]
        p95 = final_capitals[int(0.95 * n)]

        median_dd = max_drawdowns[int(0.50 * n)]
        p95_dd = max_drawdowns[int(0.95 * n)]

        survival_rate = survivals / self.n_simulations
        growth_pct = (median / self.initial_capital - 1.0) * 100

        is_viable = survival_rate > 0.5 and p25 > self.initial_capital * 0.7

        description = (
            f"Survival rate: {survival_rate:.1%}, "
            f"Median growth: {growth_pct:+.1f}%, "
            f"Median max DD: {median_dd:.1%}, "
            f"P95 max DD: {p95_dd:.1%}, "
            f"Viable: {is_viable}"
        )

        return MonteCarloResult(
            median_final_capital=median,
            p5_final_capital=p5,
            p25_final_capital=p25,
            p75_final_capital=p75,
            p95_final_capital=p95,
            median_max_drawdown=median_dd,
            p95_max_drawdown=p95_dd,
            survival_rate=survival_rate,
            expected_growth_pct=growth_pct,
            is_viable=is_viable,
            description=description,
        )

    def validate_strategy(
        self,
        ev: float,
        win_rate: float,
        position_fraction: float,
    ) -> bool:
        """Quick check: run simulation and return True if strategy is viable."""
        result = self.run(
            base_ev=ev,
            base_win_rate=win_rate,
            avg_position_fraction=position_fraction,
        )
        return result.is_viable
