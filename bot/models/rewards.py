"""
Polymarket CLOB Rewards — fetches markets with active liquidity rewards.

Endpoint: GET https://clob.polymarket.com/rewards/markets/current
Returns condition_ids of markets where makers earn CLOB LP rewards.
Bot uses this to prioritize rewarded markets (extra income on top of trading PnL).
"""
import time
import logging
import requests

logger = logging.getLogger(__name__)

REWARDS_URL = "https://clob.polymarket.com/rewards/markets/current"
CACHE_TTL   = 300   # Refresh every 5 minutes

_cache:      set[str] = set()
_cache_time: float    = 0.0


def get_rewarded_condition_ids(force_refresh: bool = False) -> set[str]:
    """Return condition_ids of markets with active CLOB liquidity rewards."""
    global _cache, _cache_time
    now = time.time()
    if not force_refresh and _cache and (now - _cache_time) < CACHE_TTL:
        return _cache
    try:
        resp = requests.get(REWARDS_URL, timeout=5)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        cids = {str(item["condition_id"]) for item in data if item.get("condition_id")}
        _cache      = cids
        _cache_time = now
        logger.info(f"[REWARDS] {len(cids)} rewarded markets cached from CLOB")
        return cids
    except Exception as e:
        logger.warning(f"[REWARDS] Fetch failed — using stale cache ({len(_cache)} entries): {e}")
        return _cache


def is_rewarded(condition_id: str) -> bool:
    """True if this market's condition_id has active CLOB liquidity rewards."""
    if not condition_id:
        return False
    return condition_id in get_rewarded_condition_ids()
