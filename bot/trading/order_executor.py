"""
Order Executor
==============
Wraps py-clob-client to place, cancel, and track real orders on Polymarket.

Supported order types:
  GTC  – Good Till Cancelled (default, stays in book)
  GTD  – Good Till Date (expires at given timestamp)
  FOK  – Fill or Kill (immediate full fill or cancel)
  FAK  – Fill and Kill (immediate partial fill, rest cancelled)

Per Polymarket docs, every order requires:
  - tickSize  (string: "0.1", "0.01", "0.001", "0.0001")
  - negRisk   (bool: True for multi-outcome 3+ markets)
These are fetched dynamically from the CLOB API before each order.
"""

import time
import logging

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        ApiCreds, OrderArgs, OrderType, PartialCreateOrderOptions,
    )
    _PY_CLOB_AVAILABLE = True
except ImportError:
    _PY_CLOB_AVAILABLE = False
    logging.getLogger("polymarket_bot.executor").warning(
        "py_clob_client not installed – OrderExecutor disabled (dry-run only)"
    )

BUY = "BUY"
SELL = "SELL"

from config import (
    POLYMARKET_HOST,
    POLYMARKET_PRIVATE_KEY,
    POLYMARKET_API_KEY,
    POLYMARKET_API_SECRET,
    POLYMARKET_API_PASSPHRASE,
    CHAIN_ID,
)

import os

logger = logging.getLogger("polymarket_bot.executor")

PROXY_ADDRESS = os.getenv("POLYMARKET_PROXY_ADDRESS", "").strip()

_ORDER_TYPE_MAP = {
    "GTC": OrderType.GTC,
    "GTD": OrderType.GTD,
    "FOK": OrderType.FOK,
    "FAK": OrderType.FAK,
} if _PY_CLOB_AVAILABLE else {}

VALID_TICK_SIZES = {"0.1", "0.01", "0.001", "0.0001"}
TICK_DECIMALS = {"0.1": 1, "0.01": 2, "0.001": 3, "0.0001": 4}


def _post_with_retry(fn, *args, retries: int = 4):
    for attempt in range(retries):
        try:
            return fn(*args)
        except Exception as e:
            msg = str(e)
            if "429" in msg or "rate limit" in msg.lower():
                wait = 2 ** attempt
                logger.warning(f"Rate limited, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Exceeded retry limit due to rate limiting")


class OrderExecutor:

    def __init__(self):
        if not _PY_CLOB_AVAILABLE:
            raise RuntimeError(
                "py_clob_client is not installed. Run: pip install poly-market-maker"
            )
        creds = ApiCreds(
            api_key=POLYMARKET_API_KEY,
            api_secret=POLYMARKET_API_SECRET,
            api_passphrase=POLYMARKET_API_PASSPHRASE,
        )
        sig_type = 1 if PROXY_ADDRESS else 0
        self.client = ClobClient(
            host=POLYMARKET_HOST,
            key=POLYMARKET_PRIVATE_KEY,
            chain_id=CHAIN_ID,
            creds=creds,
            signature_type=sig_type,
            funder=PROXY_ADDRESS if PROXY_ADDRESS else None,
        )
        logger.info(f"OrderExecutor initialized (sig_type={sig_type})")
        self._tick_cache: dict[str, str] = {}
        self._neg_risk_cache: dict[str, bool] = {}
        self._ensure_allowance()

    def _ensure_allowance(self):
        try:
            self.client.update_balance_allowance()
            logger.info("[ALLOWANCE] Called update_balance_allowance to ensure approval")
        except Exception as e:
            logger.warning(f"[ALLOWANCE] update_balance_allowance failed: {e}")
        try:
            bal = self.client.get_balance_allowance()
            if bal:
                allowance = bal.get("allowance") or bal.get("balance_allowance", {}).get("allowance")
                balance = bal.get("balance") or bal.get("balance_allowance", {}).get("balance")
                logger.info(f"[ALLOWANCE] balance={balance}, allowance={allowance}")
                if allowance is not None and float(allowance) < 1.0:
                    logger.warning(
                        "[ALLOWANCE] USDC.e allowance is zero or too low! "
                        "Orders may fail. Try running update_balance_allowance again."
                    )
        except Exception as e:
            logger.debug(f"[ALLOWANCE] Could not check balance/allowance: {e}")

    def _fetch_tick_size(self, token_id: str) -> str:
        if token_id in self._tick_cache:
            return self._tick_cache[token_id]
        try:
            ts = self.client.get_tick_size(token_id)
            ts_str = str(ts)
            if ts_str in VALID_TICK_SIZES:
                self._tick_cache[token_id] = ts_str
                logger.debug(f"[TICK] {token_id[:16]}... = {ts_str}")
                return ts_str
        except Exception as e:
            logger.warning(f"[TICK] get_tick_size failed for {token_id[:16]}...: {e}")
        return "0.01"

    def _fetch_neg_risk(self, token_id: str) -> bool:
        if token_id in self._neg_risk_cache:
            return self._neg_risk_cache[token_id]
        try:
            nr = self.client.get_neg_risk(token_id)
            val = bool(nr)
            self._neg_risk_cache[token_id] = val
            logger.debug(f"[NEG_RISK] {token_id[:16]}... = {val}")
            return val
        except Exception as e:
            logger.warning(f"[NEG_RISK] get_neg_risk failed for {token_id[:16]}...: {e}")
        return False

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "GTC",
        expiration: int | None = None,
        tick_size: str = "0.01",
        neg_risk: bool = False,
    ) -> str | None:
        clob_side = BUY if side.upper() == "BUY" else SELL
        ot = _ORDER_TYPE_MAP.get(order_type.upper(), OrderType.GTC)

        real_tick = self._fetch_tick_size(token_id)
        real_neg = self._fetch_neg_risk(token_id)

        if real_tick != tick_size:
            logger.info(f"[TICK OVERRIDE] {tick_size} -> {real_tick} (from CLOB)")
        if real_neg != neg_risk:
            logger.info(f"[NEG_RISK OVERRIDE] {neg_risk} -> {real_neg} (from CLOB)")

        tick_size = real_tick
        neg_risk = real_neg
        decimals = TICK_DECIMALS[tick_size]

        shares = size / price if price > 0 else 0
        if shares < 1.0:
            logger.warning(f"Order too small: ${size:.2f} / {price:.4f} = {shares:.2f} shares (min 1)")
            return None

        rounded_price = round(price, decimals)

        order_args = OrderArgs(
            token_id=token_id,
            price=rounded_price,
            size=round(shares, 2),
            side=clob_side,
            expiration=expiration,
        )

        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)

        logger.info(
            f"[{order_type}] Placing order: {clob_side} {round(shares,2)} shares @ {rounded_price} "
            f"| tick={tick_size} neg_risk={neg_risk} | token={token_id[:16]}..."
        )

        try:
            signed = self.client.create_order(order_args, options)
            resp = _post_with_retry(self.client.post_order, signed, ot)
            order_id = resp.get("orderID") or resp.get("id")
            status = resp.get("status", "unknown")
            error_msg = resp.get("errorMsg", "")
            if error_msg:
                logger.warning(
                    f"[{order_type}] Order response error: {error_msg} "
                    f"| {side} {shares:.2f} shares @ {rounded_price} "
                    f"| tick={tick_size} neg_risk={neg_risk}"
                )
                return None
            logger.info(
                f"[{order_type}] SUCCESS {side} ${size:.2f} ({shares:.2f} shares) @ {rounded_price} "
                f"| tick={tick_size} neg_risk={neg_risk} "
                f"| status={status} | id={order_id}"
            )
            return order_id
        except Exception as e:
            logger.error(
                f"Failed to place {order_type} order: {e} "
                f"| {side} {shares:.2f} shares @ {rounded_price} "
                f"| tick={tick_size} neg_risk={neg_risk}"
            )
            return None

    def place_limit_order(self, token_id: str, side: str, price: float, size: float,
                          tick_size: str = "0.01", neg_risk: bool = False) -> str | None:
        return self.place_order(token_id, side, price, size, order_type="GTC",
                                tick_size=tick_size, neg_risk=neg_risk)

    def place_fok_order(self, token_id: str, side: str, price: float, size: float,
                        tick_size: str = "0.01", neg_risk: bool = False) -> str | None:
        return self.place_order(token_id, side, price, size, order_type="FOK",
                                tick_size=tick_size, neg_risk=neg_risk)

    def place_gtd_order(
        self, token_id: str, side: str, price: float, size: float, expiration: int,
        tick_size: str = "0.01", neg_risk: bool = False,
    ) -> str | None:
        return self.place_order(token_id, side, price, size, order_type="GTD",
                                expiration=expiration, tick_size=tick_size, neg_risk=neg_risk)

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel(order_id)
            logger.info(f"Cancelled order {order_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    def get_open_orders(self) -> list[dict]:
        try:
            return self.client.get_orders() or []
        except Exception as e:
            logger.error(f"Failed to fetch open orders: {e}")
            return []
