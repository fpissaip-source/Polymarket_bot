"""
Find Polymarket Proxy Wallet
============================
Computes the Polymarket proxy wallet address for your private key.
Run this on your VPS to find the proxy address and check balances.

Usage:
    python3 find_proxy.py
"""

import requests
import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from eth_account import Account
from eth_utils import to_checksum_address
from eth_hash.auto import keccak
from dotenv import load_dotenv

load_dotenv()


def eth_call(rpc_url: str, to: str, data: str) -> str | None:
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
        "id": 1,
    }
    try:
        r = requests.post(rpc_url, json=payload, timeout=10)
        result = r.json().get("result", "0x")
        return result
    except Exception as e:
        print(f"  RPC error: {e}")
        return None


def usdc_balance(rpc_url: str, address: str) -> float:
    usdc_e = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    selector = "70a08231"
    padded = "000000000000000000000000" + address[2:].lower()
    data = "0x" + selector + padded
    result = eth_call(rpc_url, usdc_e, data)
    if result and result != "0x":
        try:
            return int(result, 16) / 1_000_000
        except Exception:
            pass
    return 0.0


def main():
    pk = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
    if not pk:
        print("ERROR: POLYMARKET_PRIVATE_KEY not set in environment")
        return

    eoa = Account.from_key(pk).address
    print(f"\nEOA Address:  {eoa}")
    print("=" * 60)

    polygon_rpcs = [
        "https://polygon-bor-rpc.publicnode.com",
        "https://rpc-mainnet.matic.quiknode.pro",
        "https://1rpc.io/matic",
        "https://matic-mainnet.chainstacklabs.com",
    ]

    proxy_factory = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"

    func_sig = b"getPolyProxyWalletAddress(address)"
    selector = keccak(func_sig)[:4].hex()
    padded = "000000000000000000000000" + eoa[2:].lower()
    calldata = "0x" + selector + padded

    proxy = None
    working_rpc = None
    for rpc in polygon_rpcs:
        print(f"Trying RPC: {rpc}")
        result = eth_call(rpc, proxy_factory, calldata)
        if result and result not in ("0x", "0x" + "0" * 64, None):
            proxy = to_checksum_address("0x" + result[-40:])
            working_rpc = rpc
            print(f"  ✓ Proxy wallet found: {proxy}")
            break
        else:
            print(f"  ✗ No result or zero address")

    if not proxy:
        print("\nCould not compute proxy address from any RPC.")
        print("Set POLYMARKET_PROXY_ADDRESS manually in your .env file.")
        return

    print(f"\n{'='*60}")
    print(f"Results:")
    print(f"  EOA:   {eoa}")
    print(f"  Proxy: {proxy}")
    print()

    eoa_bal = usdc_balance(working_rpc, eoa)
    proxy_bal = usdc_balance(working_rpc, proxy)
    print(f"  EOA   USDC.e balance: ${eoa_bal:.6f}")
    print(f"  Proxy USDC.e balance: ${proxy_bal:.6f}")
    print()

    if proxy_bal > 0:
        print(f"✓ Proxy has ${proxy_bal:.2f} USDC.e — use proxy mode")
        print(f"\nAdd to your .env file:")
        print(f"  POLYMARKET_PROXY_ADDRESS={proxy}")
        print(f"\nOr set as environment variable before starting the bot:")
        print(f"  export POLYMARKET_PROXY_ADDRESS={proxy}")
    elif eoa_bal > 0:
        print(f"✓ EOA has ${eoa_bal:.2f} USDC.e — EOA mode works (no proxy needed)")
    else:
        print("✗ Both EOA and Proxy have $0 USDC.e!")
        print(f"\nFund one of these addresses on Polygon with USDC.e:")
        print(f"  Proxy (recommended): {proxy}")
        print(f"  EOA:                 {eoa}")
        print(f"\nIf you deposited via Polymarket website, funds are in the Proxy wallet.")
        print(f"The proxy just needs a 'refresh' — try depositing $0.01 via Polymarket")
        print(f"or use the Polymarket website to withdraw and re-deposit.")


if __name__ == "__main__":
    main()
