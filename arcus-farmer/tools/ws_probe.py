#!/usr/bin/env python3
"""Dump raw WebSocket frames so the subscribe envelopes can be confirmed.

The published docs do not spell out how account-scoped channels take the
address, so run this against testnet first and paste the working frame into
``arcus/ws.py`` if it differs.

    .venv/bin/python tools/ws_probe.py --market QQQ-USD --address 0x...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arcus.ws import ArcusWS


async def main_async(args: argparse.Namespace) -> int:
    url = "wss://api.testnet.arcus.xyz/v1/ws" if args.env == "testnet" else "wss://api.arcus.xyz/v1/ws"
    seen: dict[str, int] = {}

    def handler(channel: str, sub_id: str, contents: dict) -> None:
        seen[channel] = seen.get(channel, 0) + 1
        if seen[channel] <= args.samples:
            print(f"\n[{channel}] id={sub_id}")
            print(json.dumps(contents, indent=2)[: args.chars])

    ws = ArcusWS(url, handler=handler)
    task = asyncio.create_task(ws.run())
    await asyncio.sleep(1.5)

    if args.market:
        await ws.subscribe("l2Orderbook", market=args.market, n_levels=5)
        await ws.subscribe("l2OrderbookUpdates", market=args.market, n_levels=5, snapshot=False)
    await ws.subscribe("markets")
    if args.address:
        await ws.subscribe("orders", address=args.address)
        await ws.subscribe("userFills", address=args.address, n_fills=20)
        await ws.subscribe("account", address=args.address)
        await ws.subscribe("positions", address=args.address)

    if args.market and args.rpc:
        await asyncio.sleep(1)
        try:
            resp = await ws.orderbook_snapshot(args.market, n_levels=5)
            print("\n[l2orderbook RPC]")
            print(json.dumps(resp, indent=2)[: args.chars])
        except Exception as exc:  # noqa: BLE001
            print(f"l2orderbook RPC failed: {exc}")

    await asyncio.sleep(args.seconds)
    await ws.close()
    task.cancel()
    print(f"\nchannel counters: {seen}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--env", choices=["testnet", "mainnet"], default="testnet")
    p.add_argument("--market", default="QQQ-USD")
    p.add_argument("--address", default="", help="account address for account-scoped channels")
    p.add_argument("--seconds", type=int, default=20)
    p.add_argument("--samples", type=int, default=2, help="frames to print per channel")
    p.add_argument("--chars", type=int, default=1200)
    p.add_argument("--rpc", action="store_true", help="also exercise the l2orderbook RPC")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
