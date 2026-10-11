#!/usr/bin/env python3
"""Cancel every resting order and flatten the position.

An operator tool, not part of the strategy: the grid never closes by itself, so
without this there is no way to go flat short of hand-trading the account.

    .venv/bin/python tools/flatten.py --config config/mainnet.yaml
    .venv/bin/python tools/flatten.py --config config/mainnet.yaml --cancel-only
    .venv/bin/python tools/flatten.py --config config/mainnet.yaml --list
    .venv/bin/python tools/flatten.py --config config/mainnet.yaml --symbols SOL-USD

The closing leg is a reduce-only IOC order priced well through the touch, so it
behaves like a market order while keeping a hard bound on the worst price we
accept.  It pays taker fees (a few cents at these sizes).

``--symbols`` scopes both the cancel and the close to the named markets.  Use it
when a multi-leg account should lose one leg only: the unscoped default calls
``cancelAllOrders``, which would strip every other leg's closing ladder too.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arcus import pricing, signing
from arcus.config import load_config
from arcus.errors import ArcusError
from arcus.models import Market, Order, Position
from arcus.rest import ArcusRest

# How far through the touch to price the closing order.  50 bps is far enough
# to cross on any liquid book and still caps the damage if the book is broken.
THROUGH_BPS = Decimal("50")


async def flatten(config_path: str, *, cancel_only: bool) -> int:
    return await run(config_path, cancel_only=cancel_only, symbols=[], list_only=False)


async def run(config_path: str, *, cancel_only: bool, symbols: list[str], list_only: bool) -> int:
    cfg = load_config(config_path)
    priv = signing.load_private_key(cfg.account.api_key_path)
    public_key = cfg.account.api_key_public or signing.public_key_hex(priv)

    async with ArcusRest(
        base_url=cfg.account.base_url,
        address=cfg.account.address,
        account_index=cfg.account.account_index,
        public_key=public_key,
        private_key=priv,
    ) as rest:
        markets = {int(m["marketId"]): Market.from_api(m) for m in (await rest.markets()).get("markets", [])}
        # Scope by symbol when asked.  Resolve to market ids up front so a typo
        # fails loudly instead of silently flattening everything.
        wanted: set[int] = set()
        for name in symbols:
            match = [mid for mid, m in markets.items() if m.symbol.upper() == name.upper()]
            if not match:
                print(f"!! unknown symbol {name!r}; refusing to run")
                return 2
            wanted.update(match)

        def in_scope(market_id: int) -> bool:
            return not wanted or market_id in wanted

        raw_positions = (await rest.positions()).get("positions") or {}
        if isinstance(raw_positions, dict):
            raw_positions = list(raw_positions.values())
        positions: dict[int, Position] = {}
        for raw in raw_positions:
            if not isinstance(raw, dict):
                continue
            parsed = Position.from_api(raw)
            if parsed.size != 0 and in_scope(parsed.market_id):
                positions[parsed.market_id] = parsed

        orders_raw = await rest.open_orders() if not wanted else {"orders": [
            o for mid in sorted(wanted) for o in (await rest.open_orders(market_id=mid)).get("orders", [])
        ]}
        orders = [Order.from_api(o) for o in orders_raw.get("orders", [])]

        print(f"account   : {cfg.account.name} ({cfg.account.env}) {cfg.account.address}")
        print(f"scope     : {', '.join(symbols) if symbols else 'ALL markets'}")
        print(f"positions : {len(positions)}")
        for p in positions.values():
            m = markets.get(p.market_id)
            mark = (m.mark_price or m.oracle_price) if m else Decimal(0)
            unreal = (mark - p.entry_price) * p.size if mark else Decimal(0)
            print(f"  {m.symbol if m else p.market_id}: {p.size} @ entry {p.entry_price}"
                  f"  mark {mark}  unrealized {unreal:+.2f}")
        print(f"orders    : {len(orders)}")
        for o in orders:
            m = markets.get(o.market_id)
            print(f"  {m.symbol if m else o.market_id} {o.side} {o.price} x {o.remaining} reduceOnly={o.reduce_only}")

        if list_only:
            print("\n--list: nothing changed")
            return 0

        if orders and wanted:
            # Targeted cancel.  cancelAllOrders would also take the other legs'
            # closing ladders down, which is exactly what --symbols avoids.
            cancelled = failed = 0
            for o in orders:
                try:
                    await rest.cancel_order(market_id=o.market_id, order_id=o.order_id)
                    cancelled += 1
                except ArcusError as exc:
                    failed += 1
                    print(f"  !! cancel failed {o.order_id}: {exc}")
                await asyncio.sleep(0.05)
            print(f"\ncancelled {cancelled} order(s)" + (f", {failed} failed" if failed else ""))
            await asyncio.sleep(2)
        elif orders:
            resp = await rest.cancel_all_orders()
            print(f"\ncancelAllOrders -> {resp.get('status')} "
                  f"(cancel pool left {((resp.get('rateLimit') or {}).get('remaining'))})")
            await asyncio.sleep(2)

        if cancel_only:
            print("--cancel-only: leaving the position open")
            return 0

        if not positions:
            print("\nnothing to flatten.")
            return 0

        mids = (await rest.mids()).get("mids", {})
        for market_id, position in positions.items():
            market = markets.get(market_id)
            if market is None:
                print(f"  !! market {market_id} not found, skipping")
                continue
            mid = Decimal(str(mids.get(market.symbol) or market.mark_price or market.oracle_price))
            closing_side = "SELL" if position.size > 0 else "BUY"
            if closing_side == "SELL":
                price = pricing.round_down_to_tick(mid * (1 - THROUGH_BPS / 10000), market.tick_size)
            else:
                price = pricing.round_up_to_tick(mid * (1 + THROUGH_BPS / 10000), market.tick_size)
            size = pricing.round_down_to_tick(abs(position.size), market.step_size)
            if size <= 0:
                print(f"  !! {market.symbol}: position {position.size} is below one step, skipping")
                continue
            print(f"\nflattening {market.symbol}: {closing_side} {size} @ {price} (mid {mid}, IOC, reduce-only)")
            try:
                resp = await rest.place_order(
                    market_id=market_id,
                    side=closing_side,
                    price=price,
                    quantity=size,
                    tif="IOC",
                    reduce_only=True,
                    client_id=f"flatten-{market.symbol}-{signing.now_us()}",
                    good_til_us=signing.now_us() + 45 * 86_400 * 1_000_000,
                    market_tick=market.tick_size,
                    market_step=market.step_size,
                )
                print(f"  -> {resp.get('status')} orderId={resp.get('orderId')}")
            except ArcusError as exc:
                print(f"  !! failed: {exc}")
                continue
            await asyncio.sleep(2)
            after = (await rest.positions()).get("positions") or {}
            if isinstance(after, dict):
                after = list(after.values())
            remaining = [p for p in after if int(p.get("marketId", 0)) == market_id and Decimal(str(p.get("size", 0))) != 0]
            print(f"  position now: {remaining[0]['size'] if remaining else '0'}")

        fills = (await rest.fills()).get("fills", [])
        if fills:
            print("\nlast fills:")
            for f in fills[:4]:
                print(f"  {f.get('side')} {f.get('price')} x {f.get('size')} fee={f.get('fee')} "
                      f"role={f.get('role')} pnl={f.get('closedPnl')}")
        return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--cancel-only", action="store_true", help="cancel orders but keep the position")
    p.add_argument("--symbols", default="",
                   help="comma-separated markets to touch (e.g. SOL-USD).  Default: every market.")
    p.add_argument("--list", action="store_true", help="show positions/orders and exit; changes nothing")
    args = p.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if args.list and args.cancel_only:
        print("--list and --cancel-only are mutually exclusive")
        return 2
    return asyncio.run(
        run(args.config, cancel_only=args.cancel_only, symbols=symbols, list_only=args.list)
    )


if __name__ == "__main__":
    raise SystemExit(main())
