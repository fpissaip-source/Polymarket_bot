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

API credentials are auto-derived from the private key if not set.
"""

import time
import logging

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        ApiCreds, OrderArgs, OrderType, PartialCreateOrderOptions,
        BalanceAllowanceParams, AssetType,
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
    POLYMARKET_PROXY_ADDRESS as _PROXY_ADDRESS_CFG,
    CHAIN_ID,
)

import os
import requests as _requests

logger = logging.getLogger("polymarket_bot.executor")

PROXY_ADDRESS = os.getenv("POLYMARKET_PROXY_ADDRESS", _PROXY_ADDRESS_CFG).strip()

_POLYGON_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc-mainnet.matic.quiknode.pro",
    "https://1rpc.io/matic",
    "https://matic-mainnet.chainstacklabs.com",
]
_POLY_PROXY_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
_USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


def _eth_call(to: str, data: str) -> str | None:
    payload = {"jsonrpc": "2.0", "method": "eth_call",
               "params": [{"to": to, "data": data}, "latest"], "id": 1}
    for rpc in _POLYGON_RPCS:
        try:
            r = _requests.post(rpc, json=payload, timeout=8)
            result = r.json().get("result", "0x")
            if result and result not in ("0x", "0x" + "0" * 64):
                return result
        except Exception:
            continue
    return None


def _get_usdc_balance(address: str) -> float:
    data = "0x70a08231000000000000000000000000" + address[2:].lower()
    result = _eth_call(_USDC_E, data)
    if result:
        try:
            return int(result, 16) / 1_000_000
        except Exception:
            pass
    return 0.0


def _find_proxy_address(eoa: str) -> str | None:
    try:
        from eth_hash.auto import keccak as _keccak
        from eth_utils import to_checksum_address
        selector = _keccak(b"getPolyProxyWalletAddress(address)")[:4].hex()
        padded = "000000000000000000000000" + eoa[2:].lower()
        calldata = "0x" + selector + padded
        result = _eth_call(_POLY_PROXY_FACTORY, calldata)
        if result:
            return to_checksum_address("0x" + result[-40:])
    except Exception as e:
        logger.debug(f"[PROXY] proxy address lookup failed: {e}")
    return None

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

        creds = self._resolve_credentials()

        proxy = self._resolve_proxy(POLYMARKET_PRIVATE_KEY)
        sig_type = 1 if proxy else 0
        self.client = ClobClient(
            host=POLYMARKET_HOST,
            key=POLYMARKET_PRIVATE_KEY,
            chain_id=CHAIN_ID,
            creds=creds,
            signature_type=sig_type,
            funder=proxy if proxy else None,
        )
        logger.info(
            f"OrderExecutor initialized (sig_type={sig_type}, "
            f"signer={self.client.builder.signer.address()}, "
            f"funder={self.client.builder.funder})"
        )
        self._tick_cache: dict[str, str] = {}
        self._neg_risk_cache: dict[str, bool] = {}
        self._check_balance_and_allowance()

    def _resolve_proxy(self, private_key: str) -> str | None:
        if PROXY_ADDRESS:
            logger.info(f"[PROXY] Using proxy from env: {PROXY_ADDRESS}")
            return PROXY_ADDRESS

        try:
            from eth_account import Account
            eoa = Account.from_key(private_key).address

            eoa_bal = _get_usdc_balance(eoa)
            if eoa_bal >= 1.0:
                logger.info(f"[PROXY] EOA {eoa} has ${eoa_bal:.2f} USDC.e — using EOA mode (sig_type=0)")
                return None

            logger.info("[PROXY] Auto-detecting Polymarket proxy wallet from on-chain factory...")
            proxy = _find_proxy_address(eoa)
            if proxy:
                proxy_bal = _get_usdc_balance(proxy)
                logger.info(f"[PROXY] Proxy={proxy} | balance=${proxy_bal:.2f} USDC.e")
                if proxy_bal >= 1.0:
                    logger.info(f"[PROXY] Using proxy wallet {proxy} (sig_type=1)")
                    return proxy
                else:
                    logger.warning(
                        f"[PROXY] Proxy found ({proxy}) but balance=${proxy_bal:.2f}. "
                        f"Deposit USDC.e on Polygon to: {proxy} (or EOA: {eoa})"
                    )
            else:
                logger.warning(f"[PROXY] Could not detect proxy — trying EOA mode. EOA: {eoa}")
        except Exception as e:
            logger.debug(f"[PROXY] _resolve_proxy error: {e}")

        return None

    def _resolve_credentials(self) -> ApiCreds:
        if POLYMARKET_API_KEY and POLYMARKET_API_SECRET and POLYMARKET_API_PASSPHRASE:
            logger.info("[AUTH] Using API credentials from environment")
            return ApiCreds(
                api_key=POLYMARKET_API_KEY,
                api_secret=POLYMARKET_API_SECRET,
                api_passphrase=POLYMARKET_API_PASSPHRASE,
            )

        logger.warning("[AUTH] No API credentials in environment — auto-deriving from private key...")
        try:
            temp_client = ClobClient(
                host=POLYMARKET_HOST,
                key=POLYMARKET_PRIVATE_KEY,
                chain_id=CHAIN_ID,
            )
            creds = temp_client.derive_api_key()
            if creds and creds.api_key:
                logger.info(f"[AUTH] Successfully derived API key: {creds.api_key[:12]}...")
                return creds
        except Exception as e:
            logger.error(f"[AUTH] derive_api_key failed: {e}")

        try:
            temp_client = ClobClient(
                host=POLYMARKET_HOST,
                key=POLYMARKET_PRIVATE_KEY,
                chain_id=CHAIN_ID,
            )
            creds = temp_client.create_api_key()
            if creds and creds.api_key:
                logger.info(f"[AUTH] Created new API key: {creds.api_key[:12]}...")
                return creds
        except Exception as e:
            logger.error(f"[AUTH] create_api_key failed: {e}")

        logger.error(
            "[AUTH] FATAL: No API credentials available. "
            "Set POLYMARKET_API_KEY, POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE"
        )
        return ApiCreds(api_key="", api_secret="", api_passphrase="")

    def _check_balance_and_allowance(self):
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            self.client.update_balance_allowance(params)
            logger.info("[ALLOWANCE] Called update_balance_allowance (COLLATERAL)")
        except Exception as e:
            logger.warning(f"[ALLOWANCE] update_balance_allowance failed: {e}")

        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            bal = self.client.get_balance_allowance(params)
            if bal:
                balance = bal.get("balance", "0")
                allowances = bal.get("allowances", {})
                logger.info(f"[BALANCE] USDC.e balance: {balance}")
                for addr, val in allowances.items():
                    logger.info(f"[ALLOWANCE] {addr}: {val}")
                if balance == "0" or float(balance) < 1_000_000:
                    balance_usd = float(balance) / 1_000_000 if balance != "0" else 0
                    logger.warning(
                        f"[BALANCE] USDC.e balance is ${balance_usd:.2f}! "
                        f"Fund wallet {self.client.builder.funder} with USDC.e on Polygon. "
                        f"Orders will fail without balance."
                    )
        except Exception as e:
            logger.warning(f"[BALANCE] Could not check balance: {e}")

    def _fetch_tick_size(self, token_id: str) -> str:
        if token_id in self._tick_cache:
            return self._tick_cache[token_id]
        try:
            ts = self.client.get_tick_size(token_id)
            ts_str = str(ts)
            if ts_str in VALID_TICK_SIZES:
                self._tick_cache[token_id] = ts_str
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

        if size < 5.0:
            logger.warning(f"Order size ${size:.2f} below $5 minimum — skipping")
            return None

        shares = size / price if price > 0 else 0
        if shares < 5.0:
            logger.warning(f"Order too small: ${size:.2f} / {price:.4f} = {shares:.2f} shares (min 5)")
            return None

        rounded_price = round(price, decimals)

        order_args = OrderArgs(
            token_id=token_id,
            price=rounded_price,
            size=round(shares, 2),
            side=clob_side,
            expiration=expiration if expiration else 0,
        )

        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)

        logger.info(
            f"[{order_type}] Placing: {clob_side} {round(shares,2)} shares @ {rounded_price} "
            f"| tick={tick_size} neg_risk={neg_risk} | maker={self.client.builder.funder}"
        )

        try:
            signed = self.client.create_order(order_args, options)
            logger.debug(f"[{order_type}] Signed order: {signed}")
            resp = _post_with_retry(self.client.post_order, signed, ot)
            order_id = resp.get("orderID") or resp.get("id")
            status = resp.get("status", "unknown")
            error_msg = resp.get("errorMsg", "")
            if error_msg:
                logger.warning(
                    f"[{order_type}] Order error: {error_msg} "
                    f"| {side} {shares:.2f}@{rounded_price} tick={tick_size} neg={neg_risk}"
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
                f"| {side} {shares:.2f}@{rounded_price} tick={tick_size} neg={neg_risk}"
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
