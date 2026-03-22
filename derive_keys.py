#!/usr/bin/env python3
"""
Derives CLOB API credentials (apiKey, secret, passphrase) from the private key
and updates the .env file automatically.
"""
import os
import sys

from dotenv import dotenv_values, set_key

ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
env = dotenv_values(ENV_PATH)

PRIVATE_KEY = env.get("POLYMARKET_PRIVATE_KEY")
WALLET_ADDRESS = env.get("WALLET_ADDRESS", "0x024558B703f59Bff6BBA21919697163E96E2353B")

if not PRIVATE_KEY:
    print("ERROR: POLYMARKET_PRIVATE_KEY nicht in .env gefunden!")
    sys.exit(1)

print(f"Private Key gefunden: {PRIVATE_KEY[:8]}...{PRIVATE_KEY[-4:]}")
print(f"Wallet Address: {WALLET_ADDRESS}")
print("Verbinde mit Polymarket CLOB API...")

try:
    from py_clob_client.client import ClobClient

    client = ClobClient(
        host="https://clob.polymarket.com",
        key=PRIVATE_KEY,
        chain_id=137,
        funder=WALLET_ADDRESS,
    )

    print("Leite API Credentials ab (derive)...")
    try:
        creds = client.derive_api_key()
    except Exception:
        print("Derive fehlgeschlagen, erstelle neue Credentials...")
        creds = client.create_api_key()

    api_key = creds.api_key
    api_secret = creds.api_secret
    api_passphrase = creds.api_passphrase

    print("\n Credentials erfolgreich abgeleitet!")
    print(f"   API Key:    {api_key}")
    print(f"   Secret:     {api_secret[:8]}...")
    print(f"   Passphrase: {api_passphrase[:8]}...")

    # Write to .env
    set_key(ENV_PATH, "WALLET_ADDRESS", WALLET_ADDRESS)
    set_key(ENV_PATH, "CLOB_API_KEY", api_key)
    set_key(ENV_PATH, "CLOB_SECRET", api_secret)
    set_key(ENV_PATH, "CLOB_PASS_PHRASE", api_passphrase)

    print("\n .env wurde aktualisiert mit:")
    print("   WALLET_ADDRESS")
    print("   CLOB_API_KEY")
    print("   CLOB_SECRET")
    print("   CLOB_PASS_PHRASE")

except Exception as e:
    print(f"\n Fehler: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
