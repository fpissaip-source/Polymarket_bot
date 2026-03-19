"""
Order Executor
==============
Wraps py-clob-client to place, cancel, and track real orders on Polymarket.

Supported order types:
  GTC  – Good Till Cancelled (default, stays in book)
  GTD  – Good Till Date (expires at given timestamp)
  FOK  – Fill or Kill (immediate full fill or cancel)
  FAK  – Fill and Kill (immediate partial fill, rest cancelled)
"""

import time
import logging
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType

# py-clob-client >= 0.15 removed BUY/SELL from constants – use string literals
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
}


def _post_with_retry(fn, *args, retries: int = 4):
    """Call fn(*args) with exponential backoff on rate-limit errors."""
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
    """Places and manages orders via py-clob-client."""

    def __init__(self):
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

    def place_order(
        self,
        token_id: str,
        side: str,                      # "BUY" or "SELL"
        price: float,                   # limit price [0, 1]
        size: float,                    # USDC amount
        order_type: str = "GTC",        # GTC | GTD | FOK | FAK
        expiration: int | None = None,  # Unix timestamp (GTD only)
    ) -> str | None:
        """
        Place a limit order. Returns order_id or None on failure.

        - GTC: stays in book until filled or cancelled
        - GTD: expires at `expiration` Unix timestamp
        - FOK: fill entire size immediately or cancel
        - FAK: fill what's available now, cancel remainder
        """
        clob_side = BUY if side.upper() == "BUY" else SELL
        ot = _ORDER_TYPE_MAP.get(order_type.upper(), OrderType.GTC)

        order_args = OrderArgs(
            token_id=token_id,
            price=round(price, 4),
            size=round(size, 2),
            side=clob_side,
            expiration=expiration,
        )

        try:
            signed = self.client.create_order(order_args)
            resp = _post_with_retry(self.client.post_order, signed, ot)
            order_id = resp.get("orderID") or resp.get("id")
            logger.info(
                f"[{order_type}] {side} {size:.2f} USDC @ {price:.4f} "
                f"| token={token_id[:12]}... | id={order_id}"
            )
            return order_id
        except Exception as e:
            logger.error(f"Failed to place {order_type} order: {e}")
            return None

    # Convenience wrappers
    def place_limit_order(self, token_id: str, side: str, price: float, size: float) -> str | None:
        return self.place_order(token_id, side, price, size, order_type="GTC")

    def place_fok_order(self, token_id: str, side: str, price: float, size: float) -> str | None:
        return self.place_order(token_id, side, price, size, order_type="FOK")

    def place_gtd_order(
        self, token_id: str, side: str, price: float, size: float, expiration: int
    ) -> str | None:
        return self.place_order(token_id, side, price, size, order_type="GTD", expiration=expiration)

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
