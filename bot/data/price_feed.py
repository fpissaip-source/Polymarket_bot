"""
Crypto Price Feed
=================
Fetches real-time spot prices for BTC, ETH, SOL, XRP from Binance.
Used as external data (D) for the Bayesian model.
"""

import math
import time
import logging
import requests
from collections import deque
from config import PRICE_FEED_URL, CRYPTO_SYMBOLS

logger = logging.getLogger(__name__)


class PriceFeed:
    def __init__(self, symbols: list[str] = CRYPTO_SYMBOLS, volatility_window: int = 20):
        self.symbols = symbols
        self._last_prices: dict[str, float] = {}
        self._last_update: float = 0.0
        # Rolling return history for volatility calculation
        self._return_history: dict[str, deque] = {
            s: deque(maxlen=volatility_window) for s in symbols
        }

    def fetch(self) -> dict[str, float]:
        """Fetch current prices for all configured symbols."""
        prices = {}
        try:
            for symbol in self.symbols:
                response = requests.get(
                    PRICE_FEED_URL,
                    params={"symbol": symbol},
                    timeout=5,
                )
                response.raise_for_status()
                data = response.json()
                prices[symbol] = float(data["price"])
        except Exception as e:
            logger.warning(f"Price feed error: {e}, using last known prices")
            return dict(self._last_prices)

        # Update return history for volatility
        for symbol, price in prices.items():
            last = self._last_prices.get(symbol)
            if last and last > 0:
                ret = (price - last) / last
                if symbol not in self._return_history:
                    self._return_history[symbol] = deque(maxlen=20)
                self._return_history[symbol].append(ret)

        self._last_prices = dict(prices)
        self._last_update = time.time()
        return prices

    def get_returns(self, new_prices: dict[str, float]) -> dict[str, float]:
        """Compute % returns vs last known prices."""
        returns = {}
        for symbol, price in new_prices.items():
            last = self._last_prices.get(symbol)
            if last and last > 0:
                returns[symbol] = (price - last) / last
            else:
                returns[symbol] = 0.0
        return returns

    def get_speed(self, new_prices: dict[str, float], elapsed_seconds: float) -> dict[str, float]:
        """Compute absolute price change per second."""
        speed = {}
        for symbol, price in new_prices.items():
            last = self._last_prices.get(symbol)
            if last and last > 0 and elapsed_seconds > 0:
                speed[symbol] = abs(price - last) / (last * elapsed_seconds)
            else:
                speed[symbol] = 0.0
        return speed

    def get_volatility(self, symbol: str) -> float:
        """
        Compute realized volatility as std of recent returns, normalized to [0, 1].
        Returns 0.0 if insufficient history.
        """
        history = self._return_history.get(symbol)
        if not history or len(history) < 3:
            return 0.0
        mean = sum(history) / len(history)
        variance = sum((r - mean) ** 2 for r in history) / len(history)
        std = math.sqrt(variance)
        # Normalize: 1% std per tick ≈ 1.0 volatility score
        return min(1.0, std * 100)

    def build_bayesian_data(
        self,
        symbol: str | None,
        new_prices: dict[str, float],
        elapsed_seconds: float,
        volatility: float = 0.0,
        volume: float = 0.5,
        ob_imbalance: float = 0.0,
        reprice_speed: float = 0.0,
    ) -> dict:
        """Build the data dict expected by BayesianModel.update().
        symbol=None for assets without a Binance price feed (e.g. HYPE).
        """
        returns = self.get_returns(new_prices)
        speeds = self.get_speed(new_prices, elapsed_seconds)

        return {
            "spot_return": returns.get(symbol, 0.0) if symbol else 0.0,
            "speed": speeds.get(symbol, 0.0) if symbol else 0.0,
            "volatility": volatility,
            "volume": volume,
            "ob_imbalance": ob_imbalance,
            "reprice_speed": reprice_speed,
        }

    @property
    def stale(self) -> bool:
        return (time.time() - self._last_update) > 30
