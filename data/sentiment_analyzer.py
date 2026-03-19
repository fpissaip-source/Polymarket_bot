"""
Event Sentiment Analyzer (Gemini-powered)
==========================================
Analyzes NON-crypto event markets: politics, elections, geopolitics,
sports, entertainment — markets where LLMs have genuine information advantage.

NOT used for 5-minute crypto price markets (Gemini cannot predict short-term
price movements better than math).

Only activates when portfolio >= EVENT_SENTIMENT_MIN_BANKROLL ($100).
Updates each market every 30 minutes in background threads.
"""

import os
import re
import time
import logging
import threading
from dataclasses import dataclass, field

try:
    from google import genai
except ImportError:
    genai = None
    logging.getLogger(__name__).warning("google-genai not installed — EventSentiment disabled")

from config import EVENT_SENTIMENT_MIN_BANKROLL, EVENT_SENTIMENT_REFRESH

logger = logging.getLogger(__name__)


@dataclass
class EventSentimentResult:
    market_id: str
    question: str
    probability_yes: float      # 0.0–1.0 Gemini estimate
    confidence: float           # 0.0–1.0 how confident Gemini is
    reasoning: str
    updated_at: float = field(default_factory=time.time)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.updated_at

    @property
    def is_fresh(self) -> bool:
        return self.age_seconds < EVENT_SENTIMENT_REFRESH * 1.5

    @property
    def boost(self) -> float:
        """Probability adjustment: distance from 0.5, scaled by confidence."""
        return (self.probability_yes - 0.5) * self.confidence * 0.15


class EventSentimentAnalyzer:
    """
    Uses Gemini to estimate probabilities for event/political/sports markets.

    Usage:
        analyzer.analyze_async(market_id, question, bankroll)
        boost = analyzer.get_boost(market_id)       # instant, cached
        prob  = analyzer.get_probability(market_id) # None if no data
    """

    def __init__(self):
        self._cache: dict[str, EventSentimentResult] = {}
        self._lock = threading.Lock()
        self._running: set[str] = set()

        if genai is None:
            logger.warning("google-genai not installed — event sentiment disabled")
            self._enabled = False
            return

        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            logger.warning("GEMINI_API_KEY not set — event sentiment disabled")
            self._enabled = False
            return

        self._client = genai.Client(api_key=api_key)
        self._enabled = True
        self._consecutive_failures = 0
        self._max_failures = 5

        logger.info("EventSentimentAnalyzer initialized (gemini-1.5-flash, event markets only)")

    def _analyze(self, market_id: str, question: str):
        """Call Gemini for an event market. Runs in background thread."""
        try:
            prompt = (
                f"You are a prediction market analyst. Estimate the probability of the following event.\n\n"
                f"Market question: {question}\n\n"
                f"Answer in exactly this format (two lines):\n"
                f"PROBABILITY: 0.XX\n"
                f"CONFIDENCE: 0.XX\n\n"
                f"Rules:\n"
                f"- PROBABILITY: your best estimate that the answer is YES (0.00 to 1.00)\n"
                f"- CONFIDENCE: how confident you are in this estimate (0.00=guess, 1.00=certain)\n"
                f"- Use only your training knowledge, no real-time data\n"
                f"- If you have no relevant knowledge, set CONFIDENCE to 0.10\n"
                f"- No other text"
            )

            response = self._client.models.generate_content(
                model="gemini-1.5-flash",
                contents=prompt,
            )
            text = response.text.strip()

            prob_match = re.search(r"PROBABILITY:\s*(0?\.\d+|[01]\.0*)", text)
            conf_match = re.search(r"CONFIDENCE:\s*(0?\.\d+|[01]\.0*)", text)

            if not prob_match or not conf_match:
                logger.warning(f"Gemini event response unparseable for {market_id}: {text!r}")
                return

            prob = max(0.0, min(1.0, float(prob_match.group(1))))
            conf = max(0.0, min(1.0, float(conf_match.group(1))))

            result = EventSentimentResult(
                market_id=market_id,
                question=question,
                probability_yes=prob,
                confidence=conf,
                reasoning=f"p(YES)={prob:.2f} conf={conf:.2f} boost={((prob-0.5)*conf*0.15):+.3f}",
            )
            with self._lock:
                self._cache[market_id] = result

            logger.info(
                f"EventSentiment [{market_id[:20]}...]: "
                f"p(YES)={prob:.2f} conf={conf:.2f} → boost={result.boost:+.3f}"
            )
            self._consecutive_failures = 0

        except Exception as e:
            self._consecutive_failures += 1
            if self._consecutive_failures <= self._max_failures:
                logger.warning(f"EventSentiment failed for {market_id}: {e}")
            if self._consecutive_failures >= self._max_failures:
                logger.warning("EventSentiment: too many failures, disabling")
                self._enabled = False
        finally:
            with self._lock:
                self._running.discard(market_id)

    def analyze_async(self, market_id: str, question: str, bankroll: float):
        """
        Trigger background analysis for an event market.
        Skips if bankroll < $100, cache is fresh, or already running.
        """
        if not self._enabled:
            return
        if bankroll < EVENT_SENTIMENT_MIN_BANKROLL:
            return  # Not active until $100

        with self._lock:
            cached = self._cache.get(market_id)
            if cached and cached.is_fresh:
                return
            if market_id in self._running:
                return
            self._running.add(market_id)

        t = threading.Thread(
            target=self._analyze,
            args=(market_id, question),
            daemon=True,
            name=f"eventsent-{market_id[:12]}",
        )
        t.start()

    def get_boost(self, market_id: str) -> float:
        """Cached probability boost. Returns 0.0 if no data or stale."""
        if not self._enabled:
            return 0.0
        with self._lock:
            result = self._cache.get(market_id)
        if result and result.is_fresh:
            return result.boost
        return 0.0

    def get_probability(self, market_id: str) -> float | None:
        """Cached Gemini probability estimate. None if not available."""
        with self._lock:
            result = self._cache.get(market_id)
        if result and result.is_fresh:
            return result.probability_yes
        return None

    def summary(self) -> str:
        with self._lock:
            items = [r for r in self._cache.values() if r.is_fresh]
        if not items:
            return "EventSentiment: no data"
        parts = [f"{r.market_id[:15]}={r.probability_yes:.2f}(conf={r.confidence:.1f})" for r in items]
        return f"EventSentiment: {', '.join(parts)}"
