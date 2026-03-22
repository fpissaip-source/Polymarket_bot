"""
On-Chain Approval Setup for Polymarket
=======================================
Sets ERC20 approve() for USDC.e and ERC1155 setApprovalForAll() for CTF tokens
on ALL 3 exchange contracts. Must be run ONCE per wallet.

Usage:
    python setup_approvals.py

Requires POLYMARKET_PRIVATE_KEY in .env (or environment).
"""

import os
import sys
import time
import logging

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

try:
    import requests
    from eth_account import Account
    from eth_utils import to_checksum_address
except ImportError:
    logger.error("Install: pip install eth-account eth-utils requests")
    sys.exit(1)

# ── Contract addresses (Polygon Mainnet) ──
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

SPENDERS = {
    "CTF Exchange":          "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "Neg Risk CTF Exchange": "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "Neg Risk Adapter":      "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
}

MAX_UINT256 = "0x" + "f" * 64
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
CHAIN_ID = 137


def _send_tx(account, to: str, data: str, label: str) -> str | None:
    """Send a transaction and wait for confirmation."""
    nonce = _get_nonce(account.address)
    if nonce is None:
        logger.error(f"  [{label}] Could not get nonce")
        return None

    # Get gas price
    gas_price = _get_gas_price()

    tx = {
        "to": to_checksum_address(to),
        "data": data,
        "gas": 100_000,
        "gasPrice": gas_price,
        "nonce": nonce,
        "chainId": CHAIN_ID,
    }

    signed = account.sign_transaction(tx)
    raw = signed.raw_transaction.hex()
    if not raw.startswith("0x"):
        raw = "0x" + raw

    # Send
    resp = requests.post(POLYGON_RPC, json={
        "jsonrpc": "2.0", "method": "eth_sendRawTransaction",
        "params": [raw], "id": 1
    }, timeout=30)
    result = resp.json()
    if "error" in result:
        logger.error(f"  [{label}] TX failed: {result['error']}")
        return None

    tx_hash = result.get("result", "")
    logger.info(f"  [{label}] TX sent: {tx_hash}")

    # Wait for confirmation (max 60s)
    for i in range(20):
        time.sleep(3)
        receipt = requests.post(POLYGON_RPC, json={
            "jsonrpc": "2.0", "method": "eth_getTransactionReceipt",
            "params": [tx_hash], "id": 1
        }, timeout=10).json().get("result")
        if receipt:
            status = int(receipt.get("status", "0x0"), 16)
            if status == 1:
                logger.info(f"  [{label}] ✓ Confirmed in block {int(receipt['blockNumber'], 16)}")
                return tx_hash
            else:
                logger.error(f"  [{label}] ✗ TX reverted")
                return None
    logger.warning(f"  [{label}] Timeout waiting for confirmation")
    return tx_hash


def _get_nonce(address: str) -> int | None:
    try:
        resp = requests.post(POLYGON_RPC, json={
            "jsonrpc": "2.0", "method": "eth_getTransactionCount",
            "params": [address, "latest"], "id": 1
        }, timeout=10)
        return int(resp.json()["result"], 16)
    except Exception:
        return None


def _get_gas_price() -> int:
    try:
        resp = requests.post(POLYGON_RPC, json={
            "jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 1
        }, timeout=10)
        base = int(resp.json()["result"], 16)
        return int(base * 1.2)  # 20% buffer
    except Exception:
        return 50_000_000_000  # 50 gwei fallback


def _check_erc20_allowance(owner: str, spender: str) -> int:
    """Check current USDC.e allowance."""
    # allowance(address,address) selector = 0xdd62ed3e
    data = ("0xdd62ed3e"
            + "000000000000000000000000" + owner[2:].lower()
            + "000000000000000000000000" + spender[2:].lower())
    try:
        resp = requests.post(POLYGON_RPC, json={
            "jsonrpc": "2.0", "method": "eth_call",
            "params": [{"to": USDC_E, "data": data}, "latest"], "id": 1
        }, timeout=10)
        return int(resp.json()["result"], 16)
    except Exception:
        return 0


def _check_erc1155_approval(owner: str, operator: str) -> bool:
    """Check if CTF contract has setApprovalForAll for operator."""
    # isApprovedForAll(address,address) selector = 0xe985e9c5
    data = ("0xe985e9c5"
            + "000000000000000000000000" + owner[2:].lower()
            + "000000000000000000000000" + operator[2:].lower())
    try:
        resp = requests.post(POLYGON_RPC, json={
            "jsonrpc": "2.0", "method": "eth_call",
            "params": [{"to": CTF_CONTRACT, "data": data}, "latest"], "id": 1
        }, timeout=10)
        result = resp.json()["result"]
        return int(result, 16) == 1
    except Exception:
        return False


def main():
    private_key = "".join(os.getenv("POLYMARKET_PRIVATE_KEY", "").split())
    if not private_key:
        logger.error("Set POLYMARKET_PRIVATE_KEY in .env")
        sys.exit(1)

    account = Account.from_key(private_key)
    wallet = account.address
    logger.info(f"Wallet (EOA): {wallet}")

    # Check if using proxy
    proxy = os.getenv("POLYMARKET_PROXY_ADDRESS", "").strip()
    if proxy:
        logger.info(f"Proxy wallet: {proxy}")
        logger.info("Note: On-chain approvals are set on the EOA. "
                     "The proxy delegates through the EOA's approvals.")

    approvals_needed = 0
    approvals_done = 0

    for name, spender in SPENDERS.items():
        logger.info(f"\n{'='*50}")
        logger.info(f"Contract: {name} ({spender})")
        logger.info(f"{'='*50}")

        # 1. Check & set USDC.e ERC20 approve
        current_allowance = _check_erc20_allowance(wallet, spender)
        if current_allowance > 10**18:  # Already approved with large allowance
            logger.info(f"  [USDC.e approve] Already approved ✓")
        else:
            approvals_needed += 1
            logger.info(f"  [USDC.e approve] Current allowance: {current_allowance / 1e6:.2f} — approving MAX...")
            # approve(address,uint256) selector = 0x095ea7b3
            data = ("0x095ea7b3"
                    + "000000000000000000000000" + spender[2:].lower()
                    + MAX_UINT256[2:])
            result = _send_tx(account, USDC_E, data, f"{name} USDC.e approve")
            if result:
                approvals_done += 1

        # 2. Check & set CTF ERC1155 setApprovalForAll
        is_approved = _check_erc1155_approval(wallet, spender)
        if is_approved:
            logger.info(f"  [CTF setApprovalForAll] Already approved ✓")
        else:
            approvals_needed += 1
            logger.info(f"  [CTF setApprovalForAll] Not approved — setting...")
            # setApprovalForAll(address,bool) selector = 0xa22cb465
            data = ("0xa22cb465"
                    + "000000000000000000000000" + spender[2:].lower()
                    + "0000000000000000000000000000000000000000000000000000000000000001")
            result = _send_tx(account, CTF_CONTRACT, data, f"{name} CTF setApprovalForAll")
            if result:
                approvals_done += 1

    logger.info(f"\n{'='*50}")
    if approvals_needed == 0:
        logger.info("All approvals already set ✓ — no transactions needed")
    else:
        logger.info(f"Done: {approvals_done}/{approvals_needed} approvals set")
    logger.info(f"{'='*50}")


if __name__ == "__main__":
    main()
