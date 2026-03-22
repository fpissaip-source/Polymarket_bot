"""
Weather Feed — Open-Meteo Integration
======================================
Provides real-time and forecast weather data for weather-related
prediction markets (hurricanes, temperature records, rainfall, etc.)

Uses Open-Meteo (https://open-meteo.com) — fully free, no API key needed,
backed by DWD / NOAA / Météo-France / ECMWF model data.

Flow:
  1. Detect if a market question is weather-related (keyword match)
  2. Extract location from the question text
  3. Geocode via Open-Meteo geocoding API
  4. Fetch 7-day forecast + current conditions
  5. Return a compact summary string for the Gemini prompt

Example output fed into Gemini:
  "Weather at Miami, FL: now 31°C, humidity 82%, wind 24 km/h SE.
   Forecast: tomorrow 33°C / 26°C, thunderstorms likely (precip 18mm).
   Hurricane risk: NHC reports no active tropical cyclone within 500km."
"""

import re
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

# Cached results TTL (weather changes slowly; 30min is fine)
_CACHE_TTL = 1800

# Keywords that indicate a market is weather-related
WEATHER_KEYWORDS = [
    "hurricane", "tropical storm", "cyclone", "typhoon",
    "tornado", "flood", "drought", "wildfire", "blizzard",
    "temperature", "heatwave", "heat wave", "snowfall", "rainfall",
    "precipitation", "wind speed", "storm", "weather", "celsius",
    "fahrenheit", "degrees", "record high", "record low",
    "el nino", "la nina",
]

# Regex to extract city/country-style locations from question text
_LOCATION_PATTERN = re.compile(
    r"\b(?:in|at|near|for|over|across)\s+"
    r"([A-Z][a-zA-Z\s\-]{2,30}(?:,\s*[A-Z]{2,3})?)",
)


@dataclass
class WeatherResult:
    location: str
    latitude: float
    longitude: float
    summary: str            # Compact string for Gemini prompt
    fetched_at: float = field(default_factory=time.time)

    @property
    def is_fresh(self) -> bool:
        return time.time() - self.fetched_at < _CACHE_TTL


class WeatherFeed:
    """
    Fetches weather data from Open-Meteo for weather-related markets.
    No API key required.
    """

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "polymarket-bot/1.0"})
        self._cache: dict[str, WeatherResult] = {}   # location_key → WeatherResult

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def is_weather_market(self, question: str) -> bool:
        """Returns True if the market question is weather-related."""
        q_lower = question.lower()
        return any(kw in q_lower for kw in WEATHER_KEYWORDS)

    def get_context(self, question: str) -> str:
        """
        Return a compact weather context string for the Gemini prompt.
        Returns "" if the market is not weather-related or location unknown.
        """
        if not self.is_weather_market(question):
            return ""

        location = self._extract_location(question)
        if not location:
            # Fall back to generic global weather hint
            return (
                "[Weather context: this is a weather market. "
                "Use your Google Search to check current meteorological data.]"
            )

        cache_key = location.lower().strip()
        cached = self._cache.get(cache_key)
        if cached and cached.is_fresh:
            return cached.summary

        result = self._fetch(location)
        if result:
            self._cache[cache_key] = result
            return result.summary
        return ""

    # ------------------------------------------------------------------
    # Location extraction
    # ------------------------------------------------------------------

    def _extract_location(self, question: str) -> str:
        """Try to extract a location from the question text."""
        # Try explicit location pattern first
        match = _LOCATION_PATTERN.search(question)
        if match:
            return match.group(1).strip()

        # Try named entities: capitalized words in the question
        # (simple heuristic — Gemini will handle the rest via search)
        words = question.split()
        caps = []
        for w in words:
            clean = re.sub(r"[^\w]", "", w)
            if clean and clean[0].isupper() and len(clean) > 2:
                caps.append(clean)
        if caps:
            return " ".join(caps[:3])

        return ""

    # ------------------------------------------------------------------
    # Geocoding
    # ------------------------------------------------------------------

    def _geocode(self, location: str) -> tuple[float, float, str] | None:
        """Return (lat, lon, canonical_name) or None on failure."""
        try:
            r = self._session.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": location, "count": 1, "language": "en", "format": "json"},
                timeout=8,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            results = data.get("results", [])
            if not results:
                return None
            top = results[0]
            name = top.get("name", location)
            country = top.get("country", "")
            admin1 = top.get("admin1", "")
            canonical = f"{name}, {admin1}, {country}".strip(", ")
            return top["latitude"], top["longitude"], canonical
        except Exception as e:
            logger.debug(f"WeatherFeed: geocode failed for '{location}': {e}")
            return None

    # ------------------------------------------------------------------
    # Weather fetch
    # ------------------------------------------------------------------

    def _fetch(self, location: str) -> WeatherResult | None:
        """Fetch current + forecast weather for a location string."""
        geo = self._geocode(location)
        if not geo:
            logger.debug(f"WeatherFeed: could not geocode '{location}'")
            return None

        lat, lon, canonical = geo
        try:
            r = self._session.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": [
                        "temperature_2m",
                        "relative_humidity_2m",
                        "wind_speed_10m",
                        "wind_direction_10m",
                        "weather_code",
                        "precipitation",
                    ],
                    "daily": [
                        "temperature_2m_max",
                        "temperature_2m_min",
                        "precipitation_sum",
                        "weather_code",
                    ],
                    "forecast_days": 7,
                    "wind_speed_unit": "kmh",
                    "timezone": "auto",
                },
                timeout=10,
            )
            if r.status_code != 200:
                logger.debug(f"WeatherFeed: API returned {r.status_code} for {canonical}")
                return None

            data = r.json()
            summary = self._build_summary(canonical, data)
            return WeatherResult(
                location=canonical,
                latitude=lat,
                longitude=lon,
                summary=summary,
            )
        except Exception as e:
            logger.debug(f"WeatherFeed: fetch failed for {canonical}: {e}")
            return None

    def _build_summary(self, location: str, data: dict) -> str:
        """Convert Open-Meteo response into a compact Gemini-friendly string."""
        lines = [f"[Weather data for {location}]"]

        current = data.get("current", {})
        if current:
            temp = current.get("temperature_2m")
            humidity = current.get("relative_humidity_2m")
            wind_spd = current.get("wind_speed_10m")
            wind_dir = current.get("wind_direction_10m")
            precip = current.get("precipitation", 0)
            wcode = current.get("weather_code", 0)

            cond = _wmo_description(wcode)
            dir_str = _wind_direction(wind_dir) if wind_dir is not None else ""
            lines.append(
                f"Current: {temp}°C, humidity {humidity}%, "
                f"wind {wind_spd} km/h {dir_str}, {cond}"
                + (f", precip {precip}mm" if precip else "")
            )

        daily = data.get("daily", {})
        dates = daily.get("time", [])
        t_max = daily.get("temperature_2m_max", [])
        t_min = daily.get("temperature_2m_min", [])
        precip_sum = daily.get("precipitation_sum", [])
        wcodes = daily.get("weather_code", [])

        if dates:
            forecast_parts = []
            for i, day in enumerate(dates[:5]):
                hi = t_max[i] if i < len(t_max) else "?"
                lo = t_min[i] if i < len(t_min) else "?"
                rain = precip_sum[i] if i < len(precip_sum) else 0
                wc = wcodes[i] if i < len(wcodes) else 0
                cond = _wmo_description(wc)
                part = f"{day}: {hi}/{lo}°C {cond}"
                if rain:
                    part += f" ({rain}mm)"
                forecast_parts.append(part)
            lines.append("Forecast: " + "; ".join(forecast_parts))

        return " | ".join(lines)


# ------------------------------------------------------------------
# WMO weather code → description
# ------------------------------------------------------------------

def _wmo_description(code: int) -> str:
    """Convert WMO weather code to a short description."""
    if code == 0:
        return "clear sky"
    if code in (1, 2, 3):
        return "partly cloudy"
    if code in (45, 48):
        return "fog"
    if code in (51, 53, 55):
        return "drizzle"
    if code in (61, 63, 65):
        return "rain"
    if code in (71, 73, 75, 77):
        return "snow"
    if code in (80, 81, 82):
        return "rain showers"
    if code in (85, 86):
        return "snow showers"
    if code in (95,):
        return "thunderstorm"
    if code in (96, 99):
        return "thunderstorm with hail"
    return f"code{code}"


def _wind_direction(degrees: float) -> str:
    """Convert wind direction degrees to compass point."""
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    idx = round(degrees / 45) % 8
    return dirs[idx]
