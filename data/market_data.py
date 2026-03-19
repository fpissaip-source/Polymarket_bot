"""
Polymarket Market Data
======================
Fetches market prices, order books, and related market info
from the Polymarket CLOB API and Gamma API.
"""

import time
import logging
import requests
from config import POLYMARKET_HOST, GAMMA_API_HOST, DATA_API_HOST

logger = logging.getLogger(__name__)

_SENTINEL = object()   # used to distinguish "not passed" from None/[]


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
        """Fetch order book for a token. Returns {} on 404 (token not on CLOB)."""
        try:
            r = self._session.get(f"{self.host}/book", params={"token_id": token_id}, timeout=10)
            if r.status_code == 404:
                logger.debug(f"Order book not found for token {token_id[:16]}... (404)")
                return {}
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Failed to fetch order book for {token_id}: {e}")
            return {}

    def get_book_data(self, token_id: str, levels: int = 5) -> dict:
        """
        Fetch the order book once and return mid_price, imbalance, and depth.
        Returns: {"mid_price": float|None, "imbalance": float, "depth": float}
        """
        book = self.get_order_book(token_id)
        if not book:
            return {"mid_price": None, "imbalance": 0.0, "depth": 0.0}

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        if best_bid and best_ask:
            mid_price = (best_bid + best_ask) / 2.0
        else:
            mid_price = best_bid or best_ask

        bid_vol = sum(float(b.get("size", 0)) for b in bids[:levels])
        ask_vol = sum(float(a.get("size", 0)) for a in asks[:levels])
        total = bid_vol + ask_vol
        imbalance = (bid_vol - ask_vol) / total if total > 1e-8 else 0.0
        depth = min(1.0, total / 1000.0)

        return {"mid_price": mid_price, "imbalance": imbalance, "depth": depth}

    def get_mid_price(self, token_id: str) -> float | None:
        """Compute mid price from order book."""
        return self.get_book_data(token_id)["mid_price"]

    def get_order_book_imbalance(self, token_id: str) -> float:
        return self.get_book_data(token_id)["imbalance"]

    def get_order_book_depth(self, token_id: str, levels: int = 5) -> float:
        return self.get_book_data(token_id)["depth"]

    def find_crypto_5min_markets(self, asset: str = "BTC") -> list[dict]:
        """
        Search for active crypto markets for the given asset via CLOB pagination.
        First tries 5-minute markets; falls back to any active market for the asset.
        """
        five_min_kws = ("5-MINUTE", "5 MINUTE", "5 MINUTES", "5-MINUTES", "UP OR DOWN", "5MIN")
        results_5m = []
        results_any = []
        cursor = ""
        seen = 0
        while seen < 800:  # limit search scope
            data = self.get_markets(next_cursor=cursor)
            markets = data.get("data", [])
            if not markets:
                break
            for m in markets:
                question = m.get("question", "").upper()
                if asset.upper() not in question:
                    continue
                if any(kw in question for kw in five_min_kws):
                    results_5m.append(m)
                else:
                    results_any.append(m)
            cursor = data.get("next_cursor", "")
            seen += len(markets)
            if not cursor or cursor == "LTE=":
                break

        if results_5m:
            logger.info(f"CLOB: found {len(results_5m)} 5-min markets for {asset}")
            return results_5m
        if results_any:
            logger.info(f"CLOB: no 5-min markets for {asset}, using {len(results_any)} broader matches")
        return results_any


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
        limit: int = 100,
        offset: int = 0,
        tag_slug: str = "",
        keyword: str = "",
    ) -> list[dict]:
        """
        Fetch markets from Gamma API.
        Uses 'tag_slug' for category filtering and 'keyword' for text search.
        No 'category' param — Gamma uses tag slugs (e.g. 'crypto').
        """
        params: dict = {"active": "true" if active else "false", "limit": limit, "offset": offset}
        if tag_slug:
            params["tag_slug"] = tag_slug
        if keyword:
            params["keyword"] = keyword
        data = _get_with_retry(self._session, f"{self.host}/markets", params=params)
        if isinstance(data, list):
            return data
        return data.get("data", data.get("markets", []))

    def find_crypto_markets(self, asset: str, keywords: list[str] | None = _SENTINEL) -> list[dict]:
        """
        Search active crypto markets matching an asset and optional keywords.

        keywords=None (default sentinel) → searches for 5-minute variants first,
                                           falls back to any match if none found.
        keywords=[]                      → no keyword filter (return all with asset name).
        keywords=[...]                   → filter by those keywords.
        """
        # Resolve keywords default
        no_keyword_filter = (keywords is None)
        if keywords is _SENTINEL:
            keywords = ["5 minutes", "5-minutes", "5 minute", "5-minute", "up or down", "5min"]

        matched = []
        fallback = []

        for offset in range(0, 600, 100):
            # Try crypto tag slug first, then unfiltered
            markets = self.get_markets(active=True, tag_slug="crypto", limit=100, offset=offset)
            if not markets:
                markets = self.get_markets(active=True, limit=100, offset=offset)
            if not markets:
                break

            for m in markets:
                question = m.get("question", "").upper()
                if asset.upper() not in question:
                    continue
                if no_keyword_filter or not keywords:
                    matched.append(m)
                elif any(kw.upper() in question for kw in keywords):
                    matched.append(m)
                else:
                    fallback.append(m)

            if len(markets) < 100:
                break

        if matched:
            logger.info(f"Gamma: found {len(matched)} markets for {asset}")
            return matched

        # Auto-fallback: return any result with the asset name
        if fallback:
            logger.info(f"Gamma: no keyword match for {asset}, using {len(fallback)} broader results")
        return fallback

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
