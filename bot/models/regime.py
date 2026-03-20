"""
Regime Detection Model — "The Weather Frog"
===========================================
Uses rolling volatility and trend analysis to classify the current
market into one of three regimes:

  LOW_VOL_SIDEWAYS   → Mean-reversion strategies work. Kelly multiplier = 1.0
  TRENDING           → Moderate vol, clear direction. Kelly multiplier = 0.75
  HIGH_VOL_BREAKOUT  → Chaotic, unpredictable. Kelly multiplier = 0.50

The regime multiplier is applied to Kelly position sizing:
  final_size = kelly_size * regime.kelly_multiplier

Uses pure numpy (no external ML libs required).
Rolling window of spot price returns → vol + trend classification.
"""

import logging
import numpy as np
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)

REGIME_LOW_VOL       = "LOW_VOL_SIDEWAYS"
REGIME_TRENDING      = "TRENDING"
REGIME_HIGH_VOL      = "HIGH_VOL_BREAKOUT"

# Per-asset windows of log-returns (up to 60 ticks ~= 60 seconds)
_WINDOW_SIZE = 60

# Thresholds (annualised-style for 1s tick data; tuned empirically)
VOL_LOW_THRESH  = 0.0008   # Below this → sideways
VOL_HIGH_THRESH = 0.0025   # Above this → breakout
TREND_THRESH    = 0.0004   # |mean return| above this → trending


@dataclass
class RegimeState:
    regime: str
    volatility: float          # rolling std of log-returns
    trend_strength: float      # |mean return|
    kelly_multiplier: float    # 0.50 / 0.75 / 1.00
    description: str


class RegimeModel:
    """
    One RegimeModel instance per asset (BTC, ETH, SOL …).
    Call `update(price)` each tick; read `.state` for current regime.
    """

    def __init__(self, asset: str, window: int = _WINDOW_SIZE):
        self.asset = asset
        self.window = window
        self._prices: deque = deque(maxlen=window + 1)
        self.state = RegimeState(
            regime=REGIME_LOW_VOL,
            volatility=0.0,
            trend_strength=0.0,
            kelly_multiplier=1.0,
            description="Initialising — defaulting to LOW_VOL",
        )
        # Simple state-machine: require N consecutive detections before switching
        self._regime_buffer: deque = deque(maxlen=3)

    def update(self, price: float) -> RegimeState:
        """Feed the latest spot price. Returns the current RegimeState."""
        if price <= 0:
            return self.state

        self._prices.append(price)
        if len(self._prices) < 10:
            return self.state

        prices = np.array(self._prices)
        log_returns = np.diff(np.log(prices))

        vol = float(np.std(log_returns))
        trend = float(np.mean(log_returns))
        trend_strength = abs(trend)

        # Classify this tick
        if vol > VOL_HIGH_THRESH:
            tick_regime = REGIME_HIGH_VOL
        elif trend_strength > TREND_THRESH and vol > VOL_LOW_THRESH:
            tick_regime = REGIME_TRENDING
        else:
            tick_regime = REGIME_LOW_VOL

        self._regime_buffer.append(tick_regime)

        # Only switch if the last 3 ticks agree (hysteresis)
        if len(self._regime_buffer) == 3 and len(set(self._regime_buffer)) == 1:
            confirmed_regime = tick_regime
        else:
            confirmed_regime = self.state.regime  # hold previous

        kelly_mult = {
            REGIME_LOW_VOL:      1.00,
            REGIME_TRENDING:     0.75,
            REGIME_HIGH_VOL:     0.50,
        }[confirmed_regime]

        desc = (
            f"vol={vol:.5f}, trend={trend:+.5f} → "
            f"{confirmed_regime} (Kelly×{kelly_mult:.2f})"
        )

        self.state = RegimeState(
            regime=confirmed_regime,
            volatility=vol,
            trend_strength=trend_strength,
            kelly_multiplier=kelly_mult,
            description=desc,
        )
        return self.state


class RegimeDetector:
    """
    Manages one RegimeModel per asset.
    Entry point: `update(asset, price)` → `get_multiplier(asset)`.
    """

    def __init__(self):
        self._models: dict[str, RegimeModel] = {}

    def _get_model(self, asset: str) -> RegimeModel:
        key = asset.upper()
        if key not in self._models:
            self._models[key] = RegimeModel(key)
        return self._models[key]

    def update(self, asset: str, price: float) -> RegimeState:
        model = self._get_model(asset)
        state = model.update(price)
        if state.regime == REGIME_HIGH_VOL:
            logger.info(
                f"[REGIME:{asset}] HIGH-VOL BREAKOUT detected — "
                f"Kelly reduced by 50%  |  {state.description}"
            )
        return state

    def get_multiplier(self, asset: str) -> float:
        """Return Kelly multiplier (0.5 / 0.75 / 1.0) for this asset."""
        return self._get_model(asset).state.kelly_multiplier

    def get_state(self, asset: str) -> RegimeState:
        return self._get_model(asset).state

    def summary(self) -> dict:
        return {
            asset: {
                "regime": m.state.regime,
                "vol": round(m.state.volatility, 6),
                "kelly_mult": m.state.kelly_multiplier,
            }
            for asset, m in self._models.items()
        }
