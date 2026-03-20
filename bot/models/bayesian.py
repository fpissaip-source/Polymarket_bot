"""
Bayesian Model
==============
Builds an internal estimate of the true probability of an outcome
using Bayes' theorem: P(H|D) = P(D|H) * P(H) / P(D)

Instead of blindly following the market price, the bot updates
its own probability as new data arrives (spot price changes,
volatility, volume, order book imbalance, reprice speed).
"""

import math
from collections import deque
from config import BAYESIAN_PRIOR, BAYESIAN_ALPHA, BAYESIAN_MIN_SAMPLES


class BayesianModel:
    def __init__(self, market_id: str, prior: float = BAYESIAN_PRIOR):
        self.market_id = market_id
        self.prior = prior          # P(H) - initial probability estimate
        self.posterior = prior      # P(H|D) - updated after each data point
        self.samples: deque = deque(maxlen=100)
        self.update_count = 0

    def update(self, data: dict) -> float:
        """
        Update posterior probability given new market data.

        data keys:
          - spot_return: float   # % change in underlying crypto spot price
          - speed: float         # speed of price movement (abs change / time)
          - volatility: float    # short-term realized volatility
          - volume: float        # normalized trading volume
          - ob_imbalance: float  # order book imbalance [-1, 1]
          - reprice_speed: float # how fast nearby markets repriced [0, 1]
        """
        likelihood = self._compute_likelihood(data)
        self.posterior = self._bayes_update(self.posterior, likelihood)
        self.posterior = max(0.01, min(0.99, self.posterior))
        self.samples.append(self.posterior)
        self.update_count += 1
        return self.posterior

    def _compute_likelihood(self, data: dict) -> float:
        """
        P(D|H): probability of observing this data if H is true.
        Each data signal pushes the likelihood above or below 0.5.
        Returns a value in (0, 1).
        """
        score = 0.5  # neutral baseline

        spot_return = data.get("spot_return", 0.0)
        speed = data.get("speed", 0.0)
        volatility = data.get("volatility", 0.0)
        volume = data.get("volume", 0.5)
        ob_imbalance = data.get("ob_imbalance", 0.0)
        reprice_speed = data.get("reprice_speed", 0.0)

        # Strong positive spot return → higher likelihood
        score += 0.20 * math.tanh(spot_return * 10)

        # Fast price move → stronger signal
        score += 0.10 * math.tanh(speed * 5)

        # High volatility → more uncertainty, pull toward 0.5
        score -= 0.05 * min(volatility, 1.0)

        # High volume → confirm signal direction
        score += 0.10 * (volume - 0.5) * 2

        # Order book imbalance: positive = more buy pressure
        score += 0.15 * ob_imbalance

        # Fast reprice of nearby markets → our market likely lags
        score += 0.10 * reprice_speed

        return max(0.05, min(0.95, score))

    def set_alpha(self, alpha: float):
        self._alpha_override = max(0.05, min(0.5, alpha))

    def _bayes_update(self, prior: float, likelihood: float) -> float:
        """
        P(H|D) = P(D|H) * P(H) / P(D)
        P(D) = P(D|H)*P(H) + P(D|~H)*(1-P(H))
        Uses exponential smoothing with BAYESIAN_ALPHA to avoid overreacting.
        """
        alpha = getattr(self, '_alpha_override', BAYESIAN_ALPHA)
        p_d_given_h = likelihood
        p_d_given_not_h = 1.0 - likelihood
        p_d = p_d_given_h * prior + p_d_given_not_h * (1.0 - prior)

        if p_d < 1e-10:
            return prior

        raw_posterior = (p_d_given_h * prior) / p_d
        smoothed = (1 - alpha) * prior + alpha * raw_posterior
        return smoothed

    @property
    def is_ready(self) -> bool:
        """Returns True once we have enough data points to trust the estimate."""
        return self.update_count >= BAYESIAN_MIN_SAMPLES

    @property
    def confidence(self) -> float:
        """Confidence: distance from 0.5, scaled to [0, 1]."""
        return abs(self.posterior - 0.5) * 2

    def reset(self, prior: float = BAYESIAN_PRIOR):
        self.prior = prior
        self.posterior = prior
        self.samples.clear()
        self.update_count = 0
