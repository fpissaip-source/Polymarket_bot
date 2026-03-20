"""
Polymarket Market Data
======================
- Discovery: Gamma API only (events → markets → clobTokenIds)
- Live data: CLOB API only (book, midpoint, price)

Per Polymarket docs:
  "Use the events endpoint and work backwards — events contain
   their associated markets, reducing API calls."
  "Save a token ID from clobTokenIds — the first ID is the Yes token,
   the second is the No token."
"""

import json
import time
import logging
import requests
from config import POLYMARKET_HOST, GAMMA_API_HOST, DATA_API_HOST

logger = logging.getLogger(__name__)


def _get_with_retry(session: requests.Session, url: str, params: dict = None, timeout: int = 10) -> dict | list:
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


class PolymarketDataClient:
    """
    CLOB API client — used ONLY for live market data.
    NOT for market discovery (use GammaClient for that).
    
    Endpoints used:
      GET /book?token_id=...    → order book
      GET /midpoint?token_id=... → midpoint price  
      GET /price?token_id=...   → last trade price
    """

    def __init__(self, host: str = POLYMARKET_HOST):
        self.host = host.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    def get_order_book(self, token_id: str) -> dict:
        try:
            r = self._session.get(f"{self.host}/book", params={"token_id": token_id}, timeout=10)
            if r.status_code == 404:
                return {}
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.debug(f"Order book fetch failed for {token_id[:16]}...: {e}")
            return {}

    def get_midpoint(self, token_id: str) -> float | None:
        try:
            r = self._session.get(f"{self.host}/midpoint", params={"token_id": token_id}, timeout=10)
            if r.status_code == 200:
                data = r.json()
                mid = data.get("mid") or data.get("midpoint") or data.get("price")
                if mid is not None:
                    return float(mid)
        except Exception as e:
            logger.debug(f"Midpoint fetch failed for {token_id[:16]}...: {e}")
        return None

    def get_price(self, token_id: str) -> float | None:
        try:
            r = self._session.get(f"{self.host}/price", params={"token_id": token_id}, timeout=10)
            if r.status_code == 200:
                data = r.json()
                price = data.get("price") or data.get("last")
                if price is not None:
                    return float(price)
        except Exception as e:
            logger.debug(f"Price fetch failed for {token_id[:16]}...: {e}")
        return None

    def get_book_data(self, token_id: str, levels: int = 5) -> dict:
        book = self.get_order_book(token_id)
        if not book:
            mid = self.get_midpoint(token_id)
            return {"mid_price": mid, "imbalance": 0.0, "depth": 0.0}

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
        return self.get_book_data(token_id)["mid_price"]

    def get_order_book_imbalance(self, token_id: str) -> float:
        return self.get_book_data(token_id)["imbalance"]

    def get_order_book_depth(self, token_id: str, levels: int = 5) -> float:
        return self.get_book_data(token_id)["depth"]


def _parse_json_field(val) -> list:
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    return []


def extract_clob_tokens(m: dict) -> tuple[str | None, str | None]:
    """
    Extract YES and NO clobTokenIds from a Gamma market dict.
    Per docs: "first ID is the Yes token, second is the No token."
    
    Priority:
      1. clobTokenIds field (Gamma's canonical source)
      2. tokens list with outcome field
      3. Direct token_id_yes / token_id_no fields
    """
    YES_OUTCOMES = {"YES", "1", "UP", "HOCH", "HIGH", "OVER", "ABOVE", "TRUE"}
    NO_OUTCOMES = {"NO", "0", "DOWN", "RUNTER", "LOW", "UNDER", "BELOW", "FALSE"}

    clob_ids = _parse_json_field(m.get("clobTokenIds", []))
    outcomes = _parse_json_field(m.get("outcomes", []))

    if clob_ids and len(clob_ids) >= 2:
        if outcomes and len(outcomes) >= 2:
            yes = no = None
            for i, outcome in enumerate(outcomes):
                if str(outcome).upper() in YES_OUTCOMES and i < len(clob_ids):
                    yes = clob_ids[i]
                elif str(outcome).upper() in NO_OUTCOMES and i < len(clob_ids):
                    no = clob_ids[i]
            if yes and no:
                return str(yes), str(no)
        return str(clob_ids[0]), str(clob_ids[1])

    tokens = _parse_json_field(m.get("tokens", []))
    if tokens and isinstance(tokens[0], dict):
        def _tid(t):
            return t.get("token_id") or t.get("tokenId")

        yes = next((_tid(t) for t in tokens
                    if t.get("outcome", "").upper() in YES_OUTCOMES), None)
        no = next((_tid(t) for t in tokens
                   if t.get("outcome", "").upper() in NO_OUTCOMES), None)
        if yes and no:
            return str(yes), str(no)
        if len(tokens) >= 2 and _tid(tokens[0]) and _tid(tokens[1]):
            return str(_tid(tokens[0])), str(_tid(tokens[1]))

    yes = m.get("token_id_yes") or m.get("tokenIdYes")
    no = m.get("token_id_no") or m.get("tokenIdNo")
    if yes and no:
        return str(yes), str(no)

    return None, None


def extract_gamma_prices(m: dict) -> tuple[float | None, float | None]:
    raw = _parse_json_field(m.get("outcomePrices", []))
    if len(raw) >= 2:
        try:
            return float(raw[0]), float(raw[1])
        except (ValueError, TypeError):
            pass
    return None, None


class GammaClient:
    """
    Gamma API client — used for ALL market discovery.
    
    Per docs, Gamma is the source of truth for:
      - Market metadata (question, outcomes, endDate)
      - clobTokenIds (YES/NO token IDs for CLOB endpoints)
      - outcomePrices (current prices)
      - Events (groups of related markets)
    """

    def __init__(self, host: str = GAMMA_API_HOST):
        self.host = host.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    def get_markets(
        self,
        active: bool = True,
        closed: bool = False,
        limit: int = 100,
        offset: int = 0,
        tag_slug: str = "",
        keyword: str = "",
        order: str = "",
        ascending: str = "",
    ) -> list[dict]:
        params: dict = {
            "active": "true" if active else "false",
            "closed": "true" if closed else "false",
            "limit": limit,
            "offset": offset,
        }
        if tag_slug:
            params["tag_slug"] = tag_slug
        if keyword:
            params["keyword"] = keyword
        if order:
            params["order"] = order
        if ascending:
            params["ascending"] = ascending

        data = _get_with_retry(self._session, f"{self.host}/markets", params=params)
        if isinstance(data, list):
            return data
        return data.get("data", data.get("markets", []))

    def get_events(
        self,
        active: bool = True,
        closed: bool = False,
        limit: int = 50,
        offset: int = 0,
        tag: str = "",
        keyword: str = "",
        order: str = "",
    ) -> list[dict]:
        """
        Fetch events from Gamma. Each event contains a 'markets' list.
        Per docs: "Use the events endpoint and work backwards."
        """
        params: dict = {
            "active": "true" if active else "false",
            "closed": "true" if closed else "false",
            "limit": limit,
            "offset": offset,
        }
        if tag:
            params["tag"] = tag
        if keyword:
            params["keyword"] = keyword
        if order:
            params["order"] = order

        data = _get_with_retry(self._session, f"{self.host}/events", params=params)
        if isinstance(data, list):
            return data
        return data.get("data", data.get("events", []))

    def discover_5min_markets(self, asset: str) -> list[dict]:
        """
        Discover active 5-minute Up/Down markets for a crypto asset.
        These are series-based events with slug: {asset}-updown-5m-{unix_timestamp}.
        New markets open every 5 minutes. We check the current and next window.
        """
        import time as _time

        slug_prefix = asset.lower() + "-updown-5m"
        now = int(_time.time())
        current_window = (now // 300) * 300
        windows = [current_window, current_window + 300, current_window - 300]

        matched = []
        seen_ids = set()

        for window_ts in windows:
            slug = f"{slug_prefix}-{window_ts}"
            event_data = self._fetch_event_by_slug(slug)
            if not event_data:
                continue

            for m in event_data.get("markets", []):
                if not m.get("active", True) or m.get("closed", False):
                    continue
                clob_ids = _parse_json_field(m.get("clobTokenIds", []))
                if len(clob_ids) < 2:
                    continue
                mid = m.get("conditionId") or m.get("id", "")
                if mid in seen_ids:
                    continue
                p_yes, _ = extract_gamma_prices(m)
                if p_yes is not None and not (0.05 <= p_yes <= 0.95):
                    continue
                seen_ids.add(mid)
                matched.append(m)

        if matched:
            logger.info(f"Gamma: found {len(matched)} active 5-min markets for {asset}")
        else:
            logger.debug(f"Gamma: no active 5-min markets for {asset}")
        return matched

    def _fetch_event_by_slug(self, slug: str) -> dict | None:
        """Fetch a single event by its exact slug."""
        try:
            data = _get_with_retry(
                self._session,
                f"{self.host}/events",
                params={"slug": slug},
            )
            events = data if isinstance(data, list) else []
            return events[0] if events else None
        except Exception as e:
            logger.debug(f"Event fetch failed for slug {slug}: {e}")
            return None

    def discover_crypto_markets(self, asset: str) -> list[dict]:
        """
        Discover active crypto markets for an asset.
        Strategy:
          1. Try 5-min Up/Down series events (slug-based, most common)
          2. Fall back to events endpoint keyword search
          3. Fall back to markets endpoint
        Returns list of Gamma market dicts with clobTokenIds.
        """
        five_min = self.discover_5min_markets(asset)
        if five_min:
            return five_min

        matched = []
        seen_ids = set()

        asset_variants = [asset.upper()]
        name_map = {
            "BTC": ["BTC", "BITCOIN"],
            "ETH": ["ETH", "ETHEREUM"],
            "SOL": ["SOL", "SOLANA"],
            "XRP": ["XRP", "RIPPLE"],
            "DOGE": ["DOGE", "DOGECOIN"],
            "BNB": ["BNB", "BINANCE"],
            "HYPE": ["HYPE", "HYPERLIQUID"],
        }
        if asset.upper() in name_map:
            asset_variants = name_map[asset.upper()]

        def _matches_asset(question: str) -> bool:
            q = question.upper()
            return any(v in q for v in asset_variants)

        def _try_add(m: dict):
            if not m.get("active", True) or m.get("closed", False):
                return
            question = m.get("question", "")
            if not _matches_asset(question):
                return
            clob_ids = _parse_json_field(m.get("clobTokenIds", []))
            if len(clob_ids) < 2:
                return
            mid = m.get("conditionId") or m.get("id", "")
            if mid in seen_ids:
                return
            p_yes, _ = extract_gamma_prices(m)
            if p_yes is not None and not (0.05 <= p_yes <= 0.95):
                return
            seen_ids.add(mid)
            matched.append(m)

        for variant in asset_variants:
            events = self.get_events(active=True, keyword=variant, tag="crypto", limit=50)
            for event in events:
                for m in event.get("markets", []):
                    _try_add(m)

        if not matched:
            for variant in asset_variants:
                for offset in range(0, 300, 100):
                    markets = self.get_markets(
                        active=True, keyword=variant, tag_slug="crypto",
                        limit=100, offset=offset,
                    )
                    if not markets:
                        break
                    for m in markets:
                        _try_add(m)
                    if len(markets) < 100:
                        break

        if matched:
            logger.info(f"Gamma: found {len(matched)} markets for {asset}")
        else:
            logger.info(f"Gamma: no active markets found for {asset}")
        return matched

    def discover_event_markets(self, limit: int = 50, exclude_assets: list[str] = None) -> list[dict]:
        """
        Discover high-volume non-crypto event markets (politics, sports, etc.)
        using the events endpoint.
        """
        exclude = [a.lower() for a in (exclude_assets or [])]
        crypto_words = ["bitcoin", "ethereum", "solana", "btc", "eth", "sol", "xrp", "doge", "bnb", "hype", "crypto"]

        matched = []
        events = self.get_events(active=True, limit=limit, order="volume")
        for event in events:
            event_markets = event.get("markets", [])
            for m in event_markets:
                question = m.get("question", "").lower()
                if any(cw in question for cw in crypto_words):
                    continue
                if not m.get("active", True) or m.get("closed", False):
                    continue
                clob_ids = _parse_json_field(m.get("clobTokenIds", []))
                if len(clob_ids) < 2:
                    continue
                p_yes, _ = extract_gamma_prices(m)
                if p_yes is not None and not (0.05 <= p_yes <= 0.95):
                    continue
                matched.append(m)

        logger.info(f"Gamma: found {len(matched)} event markets")
        return matched


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
        data = _get_with_retry(
            self._session,
            f"{self.host}/positions",
            params={"user": user_address},
        )
        if isinstance(data, list):
            return data
        return data.get("data", [])

    def get_activity(self, user_address: str, limit: int = 100) -> list[dict]:
        data = _get_with_retry(
            self._session,
            f"{self.host}/activity",
            params={"user": user_address, "limit": limit},
        )
        if isinstance(data, list):
            return data
        return data.get("data", [])
