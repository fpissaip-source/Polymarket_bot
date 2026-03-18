"""
Polymarket Market Data
======================
Fetches market prices, order books, and related market info
from the Polymarket CLOB API.
"""

import logging
import requests
from config import POLYMARKET_HOST

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
                if asset.upper() in question and ("5-MINUTE" in question or "5 MINUTE" in question):
                    results.append(m)
            cursor = data.get("next_cursor", "")
            seen += len(markets)
            if not cursor or cursor == "LTE=":
                break
        return results
