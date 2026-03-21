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
        ApiCreds, OrderArgs, MarketOrderArgs, OrderType, PartialCreateOrderOptions,
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
    MIN_BET_SIZE,
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
        funder = getattr(self.client.builder, "funder", "?")
        sig = getattr(self.client.builder, "sig_type", "?")
        logger.info(f"[ALLOWANCE] Approving exchange contracts for funder={funder} sig_type={sig}")

        # Approve USDC.e (collateral) → Exchange contract (required for BUY orders)
        for asset_type, label in [
            (AssetType.COLLATERAL, "COLLATERAL/USDC.e"),
            (AssetType.CONDITIONAL, "CONDITIONAL/CTF"),
        ]:
            try:
                params = BalanceAllowanceParams(asset_type=asset_type)
                self.client.update_balance_allowance(params)
                logger.info(f"[ALLOWANCE] ✓ Approved {label}")
            except Exception as e:
                logger.warning(f"[ALLOWANCE] Could not approve {label}: {e}")

        # Check current USDC.e balance
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            bal = self.client.get_balance_allowance(params)
            if bal:
                balance_raw = bal.get("balance", "0") or "0"
                balance_usd = float(balance_raw) / 1_000_000
                allowances = bal.get("allowances", {})
                logger.info(f"[BALANCE] USDC.e balance: ${balance_usd:.2f} (raw={balance_raw})")
                for addr, val in (allowances or {}).items():
                    logger.info(f"[ALLOWANCE]   {addr}: {val}")
                if balance_usd < 1.0:
                    logger.warning(
                        f"[BALANCE] ⚠ Only ${balance_usd:.2f} USDC.e on {funder}. "
                        f"Orders need at least $5. If you deposited via Polymarket website, "
                        f"the funds are in the proxy wallet — ensure proxy={funder} is correct."
                    )
                else:
                    logger.info(f"[BALANCE] ✓ ${balance_usd:.2f} USDC.e ready for trading")
        except Exception as e:
            logger.warning(f"[BALANCE] Could not fetch balance: {e}")

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
        min_size_usd: float | None = None,
    ) -> str | None:
        clob_side = BUY if side.upper() == "BUY" else SELL
        ot = _ORDER_TYPE_MAP.get(order_type.upper(), OrderType.GTC)

        # ── Early guards (no API calls yet) ─────────────────────────────────
        # CLOB enforces a hard minimum of 5 shares — check before hitting API
        # to avoid burning rate-limit quota on doomed orders.
        shares_preview = size / price if price > 0 else 0
        clob_min_shares = 5.0
        if shares_preview < clob_min_shares:
            clob_min_usd = clob_min_shares * price
            logger.warning(
                f"[SKIP] ${size:.2f}/{price:.4f}={shares_preview:.2f} shares "
                f"< CLOB min {clob_min_shares:.0f} (need ${clob_min_usd:.2f}) — skipping"
            )
            return None

        # min_size_usd=0 skips the dollar-floor (used for SL bracket sells where
        # the USD value may be small but the share count already cleared the 5-share gate above)
        effective_min = min_size_usd if min_size_usd is not None else max(MIN_BET_SIZE, 0.01)
        if effective_min > 0 and size < effective_min - 0.01:
            logger.warning(f"Order size ${size:.2f} below ${effective_min:.2f} minimum — skipping")
            return None

        # ── Fetch live tick_size / neg_risk from CLOB ────────────────────────
        real_tick = self._fetch_tick_size(token_id)
        real_neg = self._fetch_neg_risk(token_id)

        if real_tick != tick_size:
            logger.info(f"[TICK OVERRIDE] {tick_size} -> {real_tick} (from CLOB)")
        if real_neg != neg_risk:
            logger.info(f"[NEG_RISK OVERRIDE] {neg_risk} -> {real_neg} (from CLOB)")

        tick_size = real_tick
        neg_risk = real_neg
        decimals = TICK_DECIMALS[tick_size]

        shares = shares_preview

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

    def close_position(self, token_id: str, shares: float, price: float,
                       tick_size: str = "0.01", neg_risk: bool = False) -> str | None:
        """
        Sell `shares` outcome tokens at `price` (worst-price limit) to close a position.
        Uses create_market_order(SELL, amount=shares, price=worst_price) per Polymarket docs.

        Execution strategy (immediate fills only — no GTC resting orders):
          1. FOK  — fill all-or-nothing immediately
          2. FAK  — fill what's available immediately, cancel rest (partial sell OK)
          3. None — if both fail, return None so _check_tp_sl retries next cycle

        GTC fallback is intentionally NOT used: a resting sell order would appear
        as "success" but might never fill, causing undetected open exposure.
        """
        try:
            real_tick = self._fetch_tick_size(token_id)
            real_neg = self._fetch_neg_risk(token_id)
            decimals = TICK_DECIMALS[real_tick]
            rounded_price = round(price, decimals)
            rounded_shares = round(shares, 2)

            if rounded_shares <= 0:
                logger.warning(f"[CLOSE] Zero shares — nothing to sell")
                return None

            # Verify CTF (outcome) token balance before selling
            # "Fehlende Token-Balance" = proxy wallet doesn't hold the ERC1155 tokens
            try:
                bal_params = BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=token_id,
                )
                bal_data = self.client.get_balance_allowance(bal_params)
                raw_bal = bal_data.get("balance", "0") or "0"
                ctf_balance = float(raw_bal) / 1_000_000
                logger.info(
                    f"[CLOSE] CTF token balance: {ctf_balance:.4f} shares "
                    f"| needed: {rounded_shares:.2f} | token={token_id[:16]}..."
                )
                if ctf_balance < rounded_shares * 0.95:
                    logger.warning(
                        f"[CLOSE] ⚠ CTF balance ({ctf_balance:.4f}) < needed ({rounded_shares:.2f}) "
                        f"— CLOB fill may not have delivered tokens yet. Attempting anyway."
                    )
            except Exception as be:
                logger.warning(f"[CLOSE] Could not check CTF balance: {be}")

            options = PartialCreateOrderOptions(tick_size=real_tick, neg_risk=real_neg)
            logger.info(
                f"[CLOSE] SELL {rounded_shares} shares @ {rounded_price} (worst-price) "
                f"| tick={real_tick} neg_risk={real_neg} | maker={self.client.builder.funder}"
            )

            market_args = MarketOrderArgs(
                token_id=token_id,
                side=SELL,
                amount=rounded_shares,
                price=rounded_price,
            )

            signed = self.client.create_market_order(market_args, options)
            resp = _post_with_retry(self.client.post_order, signed, OrderType.FOK)
            order_id = resp.get("orderID") or resp.get("id")
            status = resp.get("status", "unknown")
            error_msg = resp.get("errorMsg", "")

            if error_msg:
                logger.warning(f"[CLOSE] FOK rejected: {error_msg} — retrying as FAK (partial fill)")
                signed2 = self.client.create_market_order(market_args, options)
                resp2 = _post_with_retry(self.client.post_order, signed2, OrderType.FAK)
                order_id = resp2.get("orderID") or resp2.get("id")
                error_msg2 = resp2.get("errorMsg", "")
                if error_msg2:
                    logger.error(f"[CLOSE] FAK also rejected: {error_msg2} — will retry next cycle")
                    return None
                status = resp2.get("status", "unknown")

            logger.info(f"[CLOSE] SUCCESS SELL {rounded_shares} shares @ {rounded_price} | status={status} | id={order_id}")
            return order_id
        except Exception as e:
            err_str = str(e).lower()
            if "not enough balance" in err_str or "allowance" in err_str:
                logger.error(f"[CLOSE] FATAL balance/allowance error (market likely expired): {e}")
                return "BALANCE_ERROR"
            logger.error(f"[CLOSE] Failed to close position: {e}")
            return None

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

    def place_sl_sell_order(
        self, token_id: str, shares: float, sl_price: float,
        tick_size: str = "0.01", neg_risk: bool = False,
    ) -> str | None:
        """Place a GTC SELL limit order at the stop-loss price.
        This acts as an automatic bracket order — the CLOB executes it even if the bot crashes.
        sl_price: the price at which to sell (stop-loss level)
        shares:   number of shares to sell (must be >= 5 for CLOB acceptance)
        """
        if shares < 5.0:
            logger.warning(
                f"[SL_ORDER] Skipping bracket: {shares:.2f} shares < 5 (CLOB min) — "
                f"position will be managed by bot TP/SL loop only"
            )
            return None
        size_usd = shares * sl_price
        logger.info(
            f"[SL_ORDER] Placing GTC SELL bracket: {shares:.2f} shares @ {sl_price:.4f} "
            f"(size=${size_usd:.2f})"
        )
        # min_size_usd=0: skip dollar-floor — shares already >= 5 (CLOB min cleared above).
        # Low-price tokens naturally produce small USD values (e.g. 7 shares × 0.12 = $0.84)
        # which would otherwise be blocked by MIN_BET_SIZE even though the order is valid.
        return self.place_order(
            token_id, "SELL", sl_price, size_usd,
            order_type="GTC", tick_size=tick_size, neg_risk=neg_risk,
            min_size_usd=0,
        )

    def cancel_order(self, order_id: str) -> str:
        """Cancel order. Returns status string:
        'CANCELED'       — order was successfully cancelled (unfilled/partial)
        'ALREADY_DONE'   — order not in canceled list (already filled or already cancelled)
        'API_ERROR'      — transient failure, order may still be open
        """
        try:
            resp = self.client.cancel(order_id)
            canceled_list = resp.get("canceled", []) if isinstance(resp, dict) else []
            not_canceled = resp.get("not_canceled", {}) if isinstance(resp, dict) else {}
            if order_id in canceled_list:
                logger.info(f"[CANCEL] Order {order_id[:8]} cancelled successfully")
                return "CANCELED"
            else:
                reason = not_canceled.get(order_id, "unknown")
                logger.info(f"[CANCEL] Order {order_id[:8]} not cancelled (filled/done): {reason}")
                return "ALREADY_DONE"
        except Exception as e:
            logger.error(f"[CANCEL] API error for {order_id[:8]}: {e}")
            return "API_ERROR"

    def get_order_fills(self, order_id: str) -> float:
        """Query CLOB API for actual filled share count.
        Returns filled shares (>=0) on success, -1.0 on API failure.

        The GET /order endpoint returns size_matched and original_size as
        human-readable decimals (e.g. "15.62" = 15.62 shares). Do NOT
        divide by 1e6 — that would give near-zero values.
        """
        try:
            order = self.client.get_order(order_id)
            size_matched_raw = order.get("size_matched", "0")
            original_size_raw = order.get("original_size", "0")
            price_raw = order.get("price", "0")
            filled_shares = float(size_matched_raw)
            total_shares = float(original_size_raw)
            logger.info(
                f"[ORDER_CHECK] {order_id[:8]} filled={filled_shares:.2f}/{total_shares:.2f} shares "
                f"(price={price_raw})"
            )
            return filled_shares
        except Exception as e:
            logger.warning(f"[ORDER_CHECK] Failed to get order {order_id}: {e}")
            return -1.0

    def get_open_orders(self) -> list[dict]:
        try:
            return self.client.get_orders() or []
        except Exception as e:
            logger.error(f"Failed to fetch open orders: {e}")
            return []

    def cancel_all_open_orders(self) -> int:
        """Cancel all open GTC orders on startup. Returns number cancelled."""
        try:
            orders = self.get_open_orders()
            if not orders:
                logger.info("[STARTUP] No open orders to cancel")
                return 0
            logger.info(f"[STARTUP] Found {len(orders)} open order(s) — cancelling all to free balance")
            cancelled = 0
            for o in orders:
                oid = o.get("id") or o.get("orderID") or o.get("order_id")
                if oid:
                    try:
                        self.client.cancel(oid)
                        logger.info(f"[STARTUP] Cancelled order {oid[:12]}...")
                        cancelled += 1
                    except Exception as e:
                        logger.warning(f"[STARTUP] Could not cancel {oid}: {e}")
            logger.info(f"[STARTUP] Cancelled {cancelled}/{len(orders)} orders. Balance now freed.")
            return cancelled
        except Exception as e:
            logger.error(f"[STARTUP] cancel_all_open_orders failed: {e}")
            return 0

    def get_available_balance_usd(self) -> float:
        """Return USDC.e balance available for new orders (from CLOB API)."""
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            bal = self.client.get_balance_allowance(params)
            if bal:
                raw = bal.get("balance", "0") or "0"
                return float(raw) / 1_000_000
        except Exception as e:
            logger.debug(f"get_available_balance_usd: {e}")
        return 0.0
