#!/usr/bin/env python3
"""Generate an Ed25519 API key and register it with Arcus.

    export ARCUS_ETH_PRIVATE_KEY=0x...
    .venv/bin/python tools/setup_key.py --env testnet --name main --account-index 0

The Ethereum key only signs the one-time registration; it is never stored by
the bot.  If the eth key is omitted the tool stops after writing the key pair,
so you can register separately.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric import ed25519
from eth_account import Account
from eth_account.messages import encode_defunct

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arcus import signing


def canonical(obj: object) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--env", choices=["testnet", "mainnet"], default="testnet")
    p.add_argument("--name", default="main")
    p.add_argument("--account-index", type=int, default=0, help="0-9, or 255 for all subaccounts")
    p.add_argument("--key-path", default=None)
    p.add_argument("--valid-days", type=int, default=170, help="must be within [1, 180] days")
    p.add_argument("--force", action="store_true", help="overwrite an existing key file")
    args = p.parse_args()

    key_path = Path(args.key_path or f"~/.arcus-farmer/keys/{args.name}.pem").expanduser()
    if key_path.exists() and not args.force:
        print(f"key already exists: {key_path} (use --force to replace)")
        return 1

    priv: ed25519.Ed25519PrivateKey
    if key_path.exists():
        priv = signing.load_private_key(key_path)
        public_key = signing.public_key_hex(priv)
        print(f"reusing existing key {key_path}")
    else:
        priv, public_key = signing.generate_keypair()
        signing.save_private_key(priv, key_path)
        print(f"wrote {key_path} (mode 600)")

    print(f"api key (public): {public_key}")

    eth_key = os.getenv("ARCUS_ETH_PRIVATE_KEY")
    if not eth_key:
        print("\nARCUS_ETH_PRIVATE_KEY not set - skipping registration.")
        print("Register later with the same environment variable, re-running this command.")
        return 0

    wallet = Account.from_key(eth_key)
    address = wallet.address
    valid_until = int(time.time() * 1000) + args.valid_days * 86_400_000
    message = canonical(
        {"apiWalletName": args.name, "apiWalletPublicKey": public_key, "validUntil": valid_until}
    )
    sig = wallet.sign_message(encode_defunct(primitive=message))
    body = {
        "address": address,
        "publicKey": public_key,
        "apiWalletName": args.name,
        "validUntil": valid_until,
        "accountIndex": args.account_index,
        "signature": {"r": hex(sig.r), "s": hex(sig.s), "v": hex(sig.v)},
    }
    base = "https://api.testnet.arcus.xyz" if args.env == "testnet" else "https://api.arcus.xyz"
    with httpx.Client(base_url=base, timeout=30) as client:
        resp = client.post("/v1/createApiKey", json=body)
        print(f"createApiKey -> {resp.status_code} {resp.text[:300]}")
        if resp.status_code >= 300:
            return 1
        created = resp.json()
        print(f"accountIndex: {created.get('accountIndex')}")
        print("\nThe key needs a moment to start authenticating. Polling...")
        for _ in range(30):
            keys = client.get("/v1/apiKeys", params={"address": address}).json().get("apiKeys", [])
            if any(k.get("apiKey") == public_key for k in keys):
                print("key is live.")
                break
            time.sleep(1)

    print("\nNext steps:")
    print(f"  1. fund the account (testnet: Testnet Deposit button; mainnet: on-chain USDG)")
    print(f"  2. put the address + key path in config/<name>.yaml")
    print(f"  3. check the connection: python tools/ws_probe.py --env {args.env}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
