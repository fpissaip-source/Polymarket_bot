"""
Event Sentiment Analyzer (Gemini-powered, with Google Search Grounding)
========================================================================
Analyzes prediction market questions: politics, elections, geopolitics,
sports, entertainment — markets where LLMs have genuine information advantage.

Uses Gemini as the PRIMARY probability estimator for event markets.
Gemini uses live Google Search to get up-to-date information before answering.
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

        # Try to set up Google Search Grounding tool
        self._search_tool = self._build_search_tool()
        if self._search_tool:
            logger.info("EventSentimentAnalyzer initialized (gemini-3-flash + Google Search Grounding)")
        else:
            logger.info("EventSentimentAnalyzer initialized (gemini-3-flash, no search grounding)")

    def _build_search_tool(self):
        """Build the Google Search Grounding tool. Returns None if unavailable."""
        if genai is None:
            return None
        try:
            from google.genai import types
            # Try newer SDK API first (google_search_retrieval)
            tool = types.Tool(
                google_search_retrieval=types.GoogleSearchRetrieval()
            )
            return tool
        except (AttributeError, TypeError):
            pass
        try:
            from google.genai import types
            # Fallback: newer SDK uses google_search
            tool = types.Tool(google_search=types.GoogleSearch())
            return tool
        except (AttributeError, TypeError):
            return None

    def _analyze(self, market_id: str, question: str, market_price: float,
                 weather_context: str = ""):
        """Call Gemini for an event market. Runs in background thread."""
        try:
            today = date.today().isoformat()
            weather_block = (
                f"\nVerified real-time weather sensor data:\n{weather_context}\n"
                if weather_context else ""
            )
            prompt = (
                f"Today is {today}. You are a professional prediction market analyst "
                f"with access to live internet search.\n\n"
                f"═══ MARKET QUESTION ═══\n"
                f"{question}\n"
                f"Current market price: YES = {market_price:.0%}\n"
                f"{weather_block}\n"
                f"═══ RESEARCH PROTOCOL ═══\n"
                f"Before answering, conduct multi-source research in this order:\n\n"
                f"1. OFFICIAL SOURCES — Search for statements from governments, "
                f"central banks, courts, scientific institutions, WHO, UN, NASA, etc. "
                f"These carry the highest credibility weight.\n\n"
                f"2. ESTABLISHED NEWS — Search major outlets: Reuters, AP, BBC, "
                f"Bloomberg, FT, NYT, Der Spiegel, Le Monde. Require multiple "
                f"independent sources confirming the same fact.\n\n"
                f"3. REDDIT SENTIMENT — Search reddit.com for discussion threads "
                f"about this topic (e.g. site:reddit.com {question[:60]}). "
                f"Note the dominant sentiment and volume of discussion, "
                f"but treat as soft signal only.\n\n"
                f"4. X / TWITTER SENTIMENT — Search twitter.com or x.com for "
                f"recent posts about this topic from verified accounts. "
                f"Note expert/analyst opinions vs. general public sentiment.\n\n"
                f"═══ FAKE NEWS FILTER (apply strictly) ═══\n"
                f"DISCARD any claim that:\n"
                f"- Comes from only ONE source with no independent corroboration\n"
                f"- Originates from anonymous accounts, tabloids, or known partisan outlets\n"
                f"- Contains extreme/sensational language without evidence\n"
                f"- Is older than 7 days for fast-moving events\n"
                f"- Shows 'echo chamber' pattern (many accounts repeating ONE original claim)\n"
                f"- Contradicts official data (e.g. government statistics, court documents)\n"
                f"If X/Reddit sentiment CONTRADICTS verified news → trust verified news.\n"
                f"If X/Reddit sentiment CONFIRMS verified news → slight confidence boost.\n\n"
                f"═══ OUTPUT FORMAT (4 lines, nothing else) ═══\n"
                f"PROBABILITY: 0.XX\n"
                f"CONFIDENCE: 0.XX\n"
                f"REASONING: <one sentence, cite your strongest verified source>\n"
                f"EDGE: BUY_YES / BUY_NO / NO_EDGE\n\n"
                f"Scoring rules:\n"
                f"- PROBABILITY: true probability that the question resolves YES (0.00–1.00)\n"
                f"- CONFIDENCE: how certain you are (0.10=no reliable data found, "
                f"0.90=multiple independent verified sources agree)\n"
                f"  • ≥0.75 only if: ≥2 independent credible sources confirm the key fact\n"
                f"  • 0.50–0.74: some evidence but conflicting signals or limited data\n"
                f"  • <0.50: speculation, fast-moving situation, or no recent data found\n"
                f"- REASONING: include the key fact + source type (e.g. 'Reuters reports…', "
                f"'Official govt. data shows…', 'Reddit/X consensus is…')\n"
                f"- EDGE: BUY_YES if prob > market+5%, BUY_NO if prob < market-5%, else NO_EDGE"
            )

            # Build config with Google Search Grounding when available
            gen_kwargs: dict = {"model": "gemini-3-flash", "contents": prompt}
            if self._search_tool is not None:
                try:
                    from google.genai import types
                    gen_kwargs["config"] = types.GenerateContentConfig(
                        tools=[self._search_tool]
                    )
                except Exception:
                    pass

            response = self._client.models.generate_content(**gen_kwargs)
            text = response.text.strip()

            # Log search grounding sources if present
            grounding_sources: list[str] = []
            try:
                chunks = (
                    response.candidates[0].grounding_metadata.grounding_chunks
                    if response.candidates else []
                )
                if chunks:
                    grounding_sources = [
                        c.web.uri for c in chunks if getattr(c, "web", None)
                    ]
                    if grounding_sources:
                        has_reddit = any("reddit" in u for u in grounding_sources)
                        has_x = any("x.com" in u or "twitter" in u for u in grounding_sources)
                        flags = []
                        if has_reddit:
                            flags.append("Reddit✓")
                        if has_x:
                            flags.append("X✓")
                        logger.debug(
                            f"[GEMINI] {len(grounding_sources)} sources "
                            f"{' '.join(flags)}: {grounding_sources[:4]}"
                        )
            except Exception:
                pass

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
            src_info = f" [{len(grounding_sources)} sources]" if grounding_sources else ""
            logger.info(
                f"[GEMINI] {market_id[:30]}: "
                f"p(YES)={prob:.2f} conf={conf:.2f} market={market_price:.2f} {edge_dir}"
                f"{src_info} | {reasoning}"
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

    def analyze_async(self, market_id: str, question: str, market_price: float = 0.5,
                      weather_context: str = ""):
        """
        Trigger background Gemini analysis for an event market.
        Skips if cache is fresh or already running.
        weather_context: optional real-time weather data string from WeatherFeed.
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
            args=(market_id, question, market_price, weather_context),
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
