"""
Gemini Sentiment Analyzer
=========================
Runs in a background thread — never blocks the trading loop.

For each asset (BTC, ETH, ...) it asks Gemini to estimate the probability
that the asset price will be HIGHER in the next 5 minutes, given current
price context and volatility.

The result is cached per asset and updated every REFRESH_INTERVAL seconds.
The bot reads from the cache (instant, no delay).
"""

import os
import re
import time
import logging
import threading
from dataclasses import dataclass, field

from google import genai

logger = logging.getLogger(__name__)

# How often to refresh sentiment per asset (seconds)
REFRESH_INTERVAL = 300  # 5 minutes

# How much Gemini can move the Bayesian prior (max boost/penalty)
MAX_BOOST = 0.08  # ±8%

MODEL = "gemini-2.5-flash-preview-04-17"


@dataclass
class SentimentResult:
    asset: str
    probability_up: float       # 0.0–1.0 from Gemini
    boost: float                # value added to Bayesian prior (-MAX_BOOST to +MAX_BOOST)
    reasoning: str              # short explanation from Gemini
    updated_at: float = field(default_factory=time.time)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.updated_at

    @property
    def is_fresh(self) -> bool:
        return self.age_seconds < REFRESH_INTERVAL * 1.5


class GeminiSentimentAnalyzer:
    """
    Background sentiment analyzer using Gemini.
    Call update_async() to trigger a non-blocking refresh.
    Call get_boost(asset) to read the cached result instantly.
    """

    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            logger.warning("GEMINI_API_KEY not set — sentiment analyzer disabled")
            self._enabled = False
            return

        self._client = genai.Client(api_key=api_key)
        self._enabled = True
        self._consecutive_failures = 0
        self._max_failures = 3  # Disable after 3 failures to avoid log spam

        self._cache: dict[str, SentimentResult] = {}
        self._lock = threading.Lock()
        self._running_assets: set[str] = set()

        logger.info(f"GeminiSentimentAnalyzer initialized (model: {MODEL})")

    def _analyze(self, asset: str, current_price: float, price_change_pct: float, volatility: float):
        """Call Gemini and update cache. Runs in background thread."""
        try:
            direction = "up" if price_change_pct >= 0 else "down"
            prompt = (
                f"You are a crypto trading signal generator. Answer with a single number only.\n\n"
                f"Asset: {asset}\n"
                f"Current price: ${current_price:,.2f}\n"
                f"Price change last 5 min: {price_change_pct:+.2f}%\n"
                f"Volatility: {volatility:.4f}\n"
                f"Recent trend: {direction}\n\n"
                f"Question: What is the probability (0.00 to 1.00) that {asset} will be HIGHER "
                f"in the next 5 minutes?\n\n"
                f"Rules:\n"
                f"- Answer with ONLY a decimal number between 0.00 and 1.00\n"
                f"- No text, no explanation, just the number\n"
                f"- 0.5 means uncertain, >0.5 means likely up, <0.5 means likely down"
            )

            response = self._client.models.generate_content(
                model=MODEL,
                contents=prompt,
            )
            raw = response.text.strip().replace(",", ".")

            match = re.search(r"0?\.\d+|[01]\.0*", raw)
            if not match:
                logger.warning(f"Gemini returned unexpected response for {asset}: {raw!r}")
                return

            prob = float(match.group())
            prob = max(0.0, min(1.0, prob))

            # Convert probability to boost: 0.5 → 0.0, 1.0 → +MAX_BOOST, 0.0 → -MAX_BOOST
            boost = (prob - 0.5) * 2 * MAX_BOOST

            result = SentimentResult(
                asset=asset,
                probability_up=prob,
                boost=boost,
                reasoning=f"p(up)={prob:.2f} → boost={boost:+.3f}",
            )

            with self._lock:
                self._cache[asset] = result

            logger.info(
                f"Gemini [{asset}]: p(up)={prob:.2f} | boost={boost:+.3f} | "
                f"price=${current_price:,.2f} ({price_change_pct:+.2f}%)"
            )
            self._consecutive_failures = 0  # reset on success

        except Exception as e:
            self._consecutive_failures += 1
            if self._consecutive_failures <= self._max_failures:
                logger.warning(f"Gemini analysis failed for {asset}: {e}")
            if self._consecutive_failures == self._max_failures:
                logger.warning("Gemini: too many failures, disabling sentiment analysis")
                self._enabled = False
        finally:
            with self._lock:
                self._running_assets.discard(asset)

    def update_async(self, asset: str, current_price: float, price_change_pct: float, volatility: float = 0.0):
        """
        Trigger a background refresh for this asset.
        Returns immediately — result will be cached when done.
        Skips if already running or cache is still fresh.
        """
        if not self._enabled:
            return

        with self._lock:
            cached = self._cache.get(asset)
            if cached and cached.is_fresh:
                return  # Cache still valid
            if asset in self._running_assets:
                return  # Already updating
            self._running_assets.add(asset)

        t = threading.Thread(
            target=self._analyze,
            args=(asset, current_price, price_change_pct, volatility),
            daemon=True,
            name=f"gemini-{asset}",
        )
        t.start()

    def get_boost(self, asset: str) -> float:
        """
        Return cached sentiment boost for this asset.
        Returns 0.0 if no data available (no delay, instant).
        """
        if not self._enabled:
            return 0.0
        with self._lock:
            result = self._cache.get(asset)
        if result and result.is_fresh:
            return result.boost
        return 0.0

    def summary(self) -> str:
        with self._lock:
            items = list(self._cache.values())
        if not items:
            return "Gemini: no data yet"
        parts = [f"{r.asset}={r.probability_up:.2f}({r.boost:+.3f})" for r in items if r.is_fresh]
        return f"Gemini sentiment: {', '.join(parts)}" if parts else "Gemini: cache stale"
