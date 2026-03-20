"""
Setup API Keys
==============
Run ONCE to generate Polymarket API credentials from your private key.

Usage:
  python setup_api_keys.py

Requires in .env:
  POLYMARKET_PRIVATE_KEY=0x...
  POLYMARKET_PROXY_ADDRESS=0x...   (only for Magic/email wallets)
"""

import os
from dotenv import load_dotenv, set_key
from py_clob_client.client import ClobClient

load_dotenv()

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
private_key = "".join(private_key.split())
proxy_address = os.getenv("POLYMARKET_PROXY_ADDRESS", "").strip()

if not private_key:
    print("ERROR: POLYMARKET_PRIVATE_KEY not set in .env")
    exit(1)

print(f"Using proxy address: {proxy_address or 'none (EOA mode)'}")

# signature_type=1 for Magic/email wallets (proxy wallet)
# signature_type=0 for standard MetaMask EOA
if proxy_address:
    client = ClobClient(
        host=CLOB_HOST,
        key=private_key,
        chain_id=CHAIN_ID,
        signature_type=1,
        funder=proxy_address,
    )
else:
    client = ClobClient(
        host=CLOB_HOST,
        key=private_key,
        chain_id=CHAIN_ID,
        signature_type=0,
    )

print("Generating API credentials...")
try:
    creds = client.create_or_derive_api_creds()
except Exception as e:
    print(f"ERROR: {e}")
    exit(1)

print(f"\nAPI_KEY:     {creds.api_key}")
print(f"API_SECRET:  {creds.api_secret}")
print(f"PASSPHRASE:  {creds.api_passphrase}")

env_path = ".env"
set_key(env_path, "POLYMARKET_API_KEY", creds.api_key)
set_key(env_path, "POLYMARKET_API_SECRET", creds.api_secret)
set_key(env_path, "POLYMARKET_API_PASSPHRASE", creds.api_passphrase)

print("\nCredentials saved to .env - you can now start the bot.")
