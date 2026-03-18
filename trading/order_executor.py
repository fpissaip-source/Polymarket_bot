"""
Order Executor
==============
Wraps py-clob-client to place, cancel, and track real orders on Polymarket.
"""

import logging
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.constants import BUY, SELL

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

    def place_limit_order(
        self,
        token_id: str,
        side: str,          # "BUY" or "SELL"
        price: float,       # limit price [0, 1]
        size: float,        # USDC amount
    ) -> str | None:
        """
        Place a GTC limit order. Returns order_id or None on failure.
        Price is probability [0.01 - 0.99], size is in USDC.
        """
        clob_side = BUY if side.upper() == "BUY" else SELL

        order_args = OrderArgs(
            token_id=token_id,
            price=round(price, 4),
            size=round(size, 2),
            side=clob_side,
        )

        try:
            signed = self.client.create_order(order_args)
            resp = self.client.post_order(signed, OrderType.GTC)
            order_id = resp.get("orderID") or resp.get("id")
            logger.info(
                f"Order placed: {side} {size:.2f} USDC @ {price:.4f} "
                f"| token={token_id[:12]}... | id={order_id}"
            )
            return order_id
        except Exception as e:
            logger.error(f"Failed to place order: {e}")
            return None

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
