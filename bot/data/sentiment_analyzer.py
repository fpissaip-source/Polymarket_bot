"""
Event Sentiment Analyzer (Gemini-powered)
==========================================
Analyzes prediction market questions: politics, elections, geopolitics,
sports, entertainment — markets where LLMs have genuine information advantage.

Uses Gemini as the PRIMARY probability estimator for event markets.
If Gemini thinks YES=70% and market price is 60%, that is a 10% edge → BUY.

Updates each market every 5 minutes in background threads.
Active from bankroll $1+.
"""

import os
import re
import time
import logging
import threading
from dataclasses import dataclass, field
from datetime import date

try:
    from google import genai
except ImportError:
    genai = None
    logging.getLogger(__name__).warning("google-genai not installed — EventSentiment disabled")

from config import EVENT_SENTIMENT_REFRESH

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


class EventSentimentAnalyzer:
    """
    Uses Gemini to estimate probabilities for event/political/sports markets.
    Gemini's probability IS the primary q — not just a boost.

    Usage:
        analyzer.analyze_async(market_id, question, market_price)
        prob = analyzer.get_probability(market_id)   # None if no data yet
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

        logger.info("EventSentimentAnalyzer initialized (gemini-2.0-flash)")

    def _analyze(self, market_id: str, question: str, market_price: float):
        """Call Gemini for an event market. Runs in background thread."""
        try:
            today = date.today().isoformat()
            prompt = (
                f"Today is {today}. You are an expert prediction market analyst.\n\n"
                f"Market question: {question}\n"
                f"Current market consensus price: YES = {market_price:.0%}\n\n"
                f"Your task: estimate the TRUE probability that the answer resolves YES.\n"
                f"Compare your estimate to the market price — divergence = edge.\n\n"
                f"Respond in EXACTLY this format (4 lines, nothing else):\n"
                f"PROBABILITY: 0.XX\n"
                f"CONFIDENCE: 0.XX\n"
                f"REASONING: one sentence explaining your estimate\n"
                f"EDGE: BUY_YES / BUY_NO / NO_EDGE\n\n"
                f"Rules:\n"
                f"- PROBABILITY: your best estimate that the answer resolves YES (0.00–1.00)\n"
                f"- CONFIDENCE: how reliable your estimate is (0.10=pure guess, 0.90=well-informed)\n"
                f"  Set low (0.10–0.20) if the question is about a very recent event you lack data on\n"
                f"- REASONING: key fact or logic behind your estimate (max 20 words)\n"
                f"- EDGE: BUY_YES if your prob > market+5%, BUY_NO if < market-5%, else NO_EDGE"
            )

            response = self._client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
            )
            text = response.text.strip()

            prob_match = re.search(r"PROBABILITY:\s*(0?\.\d+|[01]\.0*)", text)
            conf_match = re.search(r"CONFIDENCE:\s*(0?\.\d+|[01]\.0*)", text)
            reason_match = re.search(r"REASONING:\s*(.+)", text)

            if not prob_match or not conf_match:
                logger.warning(f"Gemini response unparseable for {market_id}: {text!r}")
                return

            prob = max(0.0, min(1.0, float(prob_match.group(1))))
            conf = max(0.0, min(1.0, float(conf_match.group(1))))
            reasoning = reason_match.group(1).strip() if reason_match else ""

            result = EventSentimentResult(
                market_id=market_id,
                question=question,
                probability_yes=prob,
                confidence=conf,
                reasoning=reasoning,
            )
            with self._lock:
                self._cache[market_id] = result

            edge_dir = "→ BUY YES" if prob > market_price + 0.05 else ("→ BUY NO" if prob < market_price - 0.05 else "→ no edge")
            logger.info(
                f"[GEMINI] {market_id[:30]}: "
                f"p(YES)={prob:.2f} conf={conf:.2f} market={market_price:.2f} {edge_dir} | {reasoning}"
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

    def analyze_async(self, market_id: str, question: str, market_price: float = 0.5):
        """
        Trigger background Gemini analysis for an event market.
        Skips if cache is fresh or already running.
        """
        if not self._enabled:
            return

        with self._lock:
            cached = self._cache.get(market_id)
            if cached and cached.is_fresh:
                return
            if market_id in self._running:
                return
            self._running.add(market_id)

        t = threading.Thread(
            target=self._analyze,
            args=(market_id, question, market_price),
            daemon=True,
            name=f"gemini-{market_id[:12]}",
        )
        t.start()

    def get_probability(self, market_id: str) -> float | None:
        """
        Gemini's probability estimate for YES. None if not available yet.
        Callers should fall back to market price (→ no edge) when None.
        """
        with self._lock:
            result = self._cache.get(market_id)
        if result and result.is_fresh:
            return result.probability_yes
        return None

    def get_confidence(self, market_id: str) -> float:
        """Gemini's confidence for cached result. 0.0 if no data."""
        with self._lock:
            result = self._cache.get(market_id)
        if result and result.is_fresh:
            return result.confidence
        return 0.0

    def summary(self) -> str:
        with self._lock:
            items = [r for r in self._cache.values() if r.is_fresh]
        if not items:
            return "Gemini: no data yet"
        parts = [
            f"{r.market_id[:20]}=p{r.probability_yes:.2f}(c{r.confidence:.1f})"
            for r in items
        ]
        return f"Gemini[{len(items)}]: {', '.join(parts)}"
