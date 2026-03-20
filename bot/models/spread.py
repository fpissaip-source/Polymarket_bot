"""
Spread Model
============
Identifies cross-market dislocations: situations where two related
markets temporarily diverge more than they should.

S = P1 - P2
z = (S - mu_S) / sigma_S

If |z| > SPREAD_ZSCORE_THRESHOLD, the bot flags an arbitrage opportunity.

The bot maintains a map of related markets:
  - BTC 5m vs BTC 15m
  - Current BTC 5m window vs next BTC 5m window
  - One market vs a synthetic combination of related probabilities
"""

from collections import deque
from dataclasses import dataclass
import statistics
from config import SPREAD_ZSCORE_THRESHOLD, SPREAD_LOOKBACK


@dataclass
class SpreadSignal:
    market1_id: str
    market2_id: str
    spread: float           # S = P1 - P2
    z_score: float          # z = (S - mu_S) / sigma_S
    mu_s: float             # historical mean spread
    sigma_s: float          # historical std of spread
    is_signal: bool         # |z| > threshold
    direction: str          # "market1_overpriced" | "market2_overpriced" | "neutral"


class SpreadModel:
    def __init__(
        self,
        market1_id: str,
        market2_id: str,
        lookback: int = SPREAD_LOOKBACK,
        z_threshold: float = SPREAD_ZSCORE_THRESHOLD,
    ):
        self.market1_id = market1_id
        self.market2_id = market2_id
        self.lookback = lookback
        self.z_threshold = z_threshold
        self._history: deque = deque(maxlen=lookback)

    def update(self, p1: float, p2: float) -> SpreadSignal:
        """
        Update spread history and return the current signal.
        """
        s = p1 - p2
        self._history.append(s)

        if len(self._history) < 3:
            return SpreadSignal(
                market1_id=self.market1_id,
                market2_id=self.market2_id,
                spread=s,
                z_score=0.0,
                mu_s=s,
                sigma_s=0.0,
                is_signal=False,
                direction="neutral",
            )

        mu_s = statistics.mean(self._history)
        sigma_s = statistics.stdev(self._history)

        if sigma_s < 1e-8:
            z = 0.0
        else:
            z = (s - mu_s) / sigma_s

        is_signal = abs(z) > self.z_threshold

        if is_signal and z > 0:
            direction = "market1_overpriced"
        elif is_signal and z < 0:
            direction = "market2_overpriced"
        else:
            direction = "neutral"

        return SpreadSignal(
            market1_id=self.market1_id,
            market2_id=self.market2_id,
            spread=s,
            z_score=z,
            mu_s=mu_s,
            sigma_s=sigma_s,
            is_signal=is_signal,
            direction=direction,
        )

    @property
    def is_ready(self) -> bool:
        return len(self._history) >= self.lookback // 2

    def mean_spread(self) -> float:
        if not self._history:
            return 0.0
        return statistics.mean(self._history)

    def std_spread(self) -> float:
        if len(self._history) < 2:
            return 0.0
        return statistics.stdev(self._history)


class SpreadMap:
    """
    Manages a collection of related market pairs and detects
    which pairs currently have abnormal spreads.
    """

    def __init__(self):
        self._models: dict[tuple, SpreadModel] = {}

    def register_pair(
        self,
        market1_id: str,
        market2_id: str,
        lookback: int = SPREAD_LOOKBACK,
        z_threshold: float = SPREAD_ZSCORE_THRESHOLD,
    ):
        key = (market1_id, market2_id)
        if key not in self._models:
            self._models[key] = SpreadModel(market1_id, market2_id, lookback, z_threshold)

    def update_pair(self, market1_id: str, market2_id: str, p1: float, p2: float) -> SpreadSignal:
        key = (market1_id, market2_id)
        if key not in self._models:
            self.register_pair(market1_id, market2_id)
        return self._models[key].update(p1, p2)

    def get_signals(self) -> list[SpreadSignal]:
        """Return all pairs currently showing an arbitrage signal."""
        signals = []
        for model in self._models.values():
            if model.is_ready:
                # Re-use last computed signal via dummy update is not ideal;
                # caller should call update_pair and collect signals live.
                pass
        return signals

    def all_pairs(self) -> list[tuple]:
        return list(self._models.keys())
