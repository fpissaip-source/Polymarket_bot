"""
Polymarket Market Data
======================
Fetches market prices, order books, and related market info
from the Polymarket CLOB API.
"""

import time
import logging
import requests
from config import POLYMARKET_HOST, GAMMA_API_HOST, DATA_API_HOST

logger = logging.getLogger(__name__)


class PolymarketDataClient:
    """
    Lightweight client for Polymarket's CLOB REST API.
    Handles market discovery and price fetching.
    """

    def __init__(self, host: str = POLYMARKET_HOST):
        self.host = host.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    def get_markets(self, next_cursor: str = "") -> dict:
        """Fetch list of active markets."""
        params = {"next_cursor": next_cursor} if next_cursor else {}
        try:
            r = self._session.get(f"{self.host}/markets", params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Failed to fetch markets: {e}")
            return {}

    def get_market(self, condition_id: str) -> dict:
        """Fetch a single market by condition ID."""
        try:
            r = self._session.get(f"{self.host}/markets/{condition_id}", timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Failed to fetch market {condition_id}: {e}")
            return {}

    def get_order_book(self, token_id: str) -> dict:
        """Fetch order book for a token."""
        try:
            r = self._session.get(f"{self.host}/book", params={"token_id": token_id}, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Failed to fetch order book for {token_id}: {e}")
            return {}

    def get_mid_price(self, token_id: str) -> float | None:
        """Compute mid price from order book."""
        book = self.get_order_book(token_id)
        if not book:
            return None
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if not bids or not asks:
            return None
        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        if best_bid and best_ask:
            return (best_bid + best_ask) / 2.0
        return best_bid or best_ask

    def get_order_book_imbalance(self, token_id: str) -> float:
        """
        Compute order book imbalance in [-1, 1].
        Positive = more buy pressure, Negative = more sell pressure.
        """
        book = self.get_order_book(token_id)
        if not book:
            return 0.0
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        bid_vol = sum(float(b.get("size", 0)) for b in bids[:5])
        ask_vol = sum(float(a.get("size", 0)) for a in asks[:5])
        total = bid_vol + ask_vol
        if total < 1e-8:
            return 0.0
        return (bid_vol - ask_vol) / total

    def get_order_book_depth(self, token_id: str, levels: int = 5) -> float:
        """
        Estimate order book depth as a normalized score [0, 1].
        Higher = deeper book = more liquidity.
        """
        book = self.get_order_book(token_id)
        if not book:
            return 0.0
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        depth = sum(float(b.get("size", 0)) for b in bids[:levels])
        depth += sum(float(a.get("size", 0)) for a in asks[:levels])
        # Normalize: assume 1000 USDC depth = 1.0
        return min(1.0, depth / 1000.0)

    def find_crypto_5min_markets(self, asset: str = "BTC") -> list[dict]:
        """
        Search for active 5-minute crypto markets for the given asset.
        Returns a list of market dicts with condition_id and token_ids.
        """
        results = []
        cursor = ""
        seen = 0
        while seen < 500:  # limit search scope
            data = self.get_markets(next_cursor=cursor)
            markets = data.get("data", [])
            if not markets:
                break
            for m in markets:
                question = m.get("question", "").upper()
                if asset.upper() not in question:
                    continue
                if any(kw in question for kw in ("5-MINUTE", "5 MINUTE", "5 MINUTES", "5-MINUTES", "UP OR DOWN")):
                    results.append(m)
            cursor = data.get("next_cursor", "")
            seen += len(markets)
            if not cursor or cursor == "LTE=":
                break
        return results


def _get_with_retry(session: requests.Session, url: str, params: dict = None, timeout: int = 10) -> dict:
    """GET with automatic 429 backoff."""
    for attempt in range(4):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                wait = 2 ** attempt
                logger.warning(f"Rate limited (429), retrying in {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Request failed ({url}): {e}")
            return {}
    return {}


class GammaClient:
    """
    Client for the Gamma API (Market Discovery & Metadata).
    Level 0 – no authentication required.
    """

    def __init__(self, host: str = GAMMA_API_HOST):
        self.host = host.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    def get_markets(
        self,
        active: bool = True,
        category: str = "crypto",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Fetch markets from Gamma API with optional category filter."""
        params = {"active": str(active).lower(), "limit": limit, "offset": offset}
        if category:
            params["category"] = category
        data = _get_with_retry(self._session, f"{self.host}/markets", params=params)
        if isinstance(data, list):
            return data
        return data.get("data", data.get("markets", []))

    def find_crypto_markets(self, asset: str, keywords: list[str] | None = None) -> list[dict]:
        """
        Search active crypto markets matching an asset and optional keywords.
        Falls back to broad search if category filter returns nothing.
        """
        # All known variants of 5-minute market naming on Polymarket
        keywords = keywords or ["5 minutes", "5-minutes", "5 minute", "5-minute", "up or down"]
        results = []
        for offset in range(0, 500, 100):
            markets = self.get_markets(active=True, category="crypto", limit=100, offset=offset)
            if not markets:
                break
            for m in markets:
                question = m.get("question", "").upper()
                if asset.upper() not in question:
                    continue
                if keywords and not any(kw.upper() in question for kw in keywords):
                    continue
                results.append(m)
            if len(markets) < 100:
                break
        return results

    def get_events(self, limit: int = 50) -> list[dict]:
        """Fetch event groups (e.g. 'US Election')."""
        data = _get_with_retry(self._session, f"{self.host}/events", params={"limit": limit})
        if isinstance(data, list):
            return data
        return data.get("data", [])


class DataApiClient:
    """
    Client for the Data API (Portfolio, Positions, Activity).
    Level 0 for public endpoints; user address required for positions.
    """

    def __init__(self, host: str = DATA_API_HOST):
        self.host = host.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    def get_positions(self, user_address: str) -> list[dict]:
        """Fetch open positions for a wallet address."""
        data = _get_with_retry(
            self._session,
            f"{self.host}/positions",
            params={"user": user_address},
        )
        if isinstance(data, list):
            return data
        return data.get("data", [])

    def get_activity(self, user_address: str, limit: int = 100) -> list[dict]:
        """Fetch trade history / PnL for a wallet address."""
        data = _get_with_retry(
            self._session,
            f"{self.host}/activity",
            params={"user": user_address, "limit": limit},
        )
        if isinstance(data, list):
            return data
        return data.get("data", [])
