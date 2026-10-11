#!/usr/bin/env python3
"""Operator tool for the memory-basket spread.

Structure (ETF weights, normalised): long MU + SKHY + SNDK at their weights
inside the Roundhill Memory ETF, and short the same dollar amount of DRAM.
That makes the net position "long the Arcus-tradeable names / short the rest
of the ETF" (Samsung, CXMT, STX, WDC, Kioxia, Nanya, Winbond).

    .venv/bin/python tools/spread_basket.py --config config/mainnet.yaml status
    .venv/bin/python tools/spread_basket.py --config config/mainnet.yaml open --lev 1.5 --yes
    .venv/bin/python tools/spread_basket.py --config config/mainnet.yaml close --yes

Only the operator runs this: it is not part of the grid engine, and nothing
here places resting orders - the position is simply held.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arcus import pricing, signing
from arcus.config import load_config
from arcus.errors import ArcusError
from arcus.models import Market, Position
from arcus.rest import ArcusRest

# Weights inside DRAM (Roundhill Memory ETF, 2026-10-07) for the tradeable legs.
ETF_WEIGHTS = {"MU-USD": Decimal("0.2736"), "SKHY-USD": Decimal("0.2213"),
               "SNDK-USD": Decimal("0.0465")}
SHORT_LEG = "DRAM-USD"
TRADEABLE_SUM = sum(ETF_WEIGHTS.values(), Decimal(0))

# How far through the touch the marketable-limit entry is priced.  40 bps is
# wide enough to cross on these books and still caps a broken-book fill.
THROUGH_BPS = Decimal("40")
STATE = Path(__file__).resolve().parents[1] / "state" / "spread_basket.json"


def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


async def context(cfg):
    priv = signing.load_private_key(cfg.account.api_key_path)
    public_key = cfg.account.api_key_public or signing.public_key_hex(priv)
    rest = ArcusRest(
        base_url=cfg.account.base_url,
        address=cfg.account.address,
        account_index=cfg.account.account_index,
        public_key=public_key,
        private_key=priv,
    )
    await rest.start()
    raw = (await rest.markets()).get("markets", [])
    markets = {}
    for m in raw:
        mk = Market.from_api(m)
        if mk.symbol in list(ETF_WEIGHTS) + [SHORT_LEG]:
            markets[mk.symbol] = mk
    missing = [s for s in list(ETF_WEIGHTS) + [SHORT_LEG] if s not in markets]
    if missing:
        raise SystemExit(f"!! markets not found: {missing}")
    mids = {k: Decimal(str(v)) for k, v in (await rest.mids()).get("mids", {}).items()}
    return rest, markets, mids


async def positions_by_symbol(rest, markets):
    raw = (await rest.positions()).get("positions") or {}
    if isinstance(raw, dict):
        raw = list(raw.values())
    out = {}
    for p in raw:
        if not isinstance(p, dict):
            continue
        pos = Position.from_api(p)
        if pos.size == 0:
            continue
        sym = next((s for s, m in markets.items() if m.market_id == pos.market_id), str(pos.market_id))
        out[sym] = pos
    return out


def plan(equity: Decimal, lev: Decimal, markets, mids):
    per_side = equity * lev
    legs = []
    for sym, w in ETF_WEIGHTS.items():
        notional = per_side * w / TRADEABLE_SUM
        mk = markets[sym]
        px = mids.get(sym) or mk.mark_price or mk.oracle_price
        size = pricing.size_for_notional(notional, px, mk.step_size)
        legs.append({"symbol": sym, "side": "BUY", "notional": notional, "price": px,
                     "size": size, "market_id": mk.market_id})
    mk = markets[SHORT_LEG]
    px = mids.get(SHORT_LEG) or mk.mark_price or mk.oracle_price
    size = pricing.size_for_notional(per_side, px, mk.step_size)
    legs.append({"symbol": SHORT_LEG, "side": "SELL", "notional": per_side, "price": px,
                 "size": size, "market_id": mk.market_id})
    return per_side, legs


def print_plan(equity, lev, per_side, legs):
    print(f"equity ${equity:,.2f}  L={lev}  ->  per-side notional ${per_side:,.2f} "
          f"(gross ${2*per_side:,.2f})")
    print(f"{'leg':10s} {'side':4s} {'target $':>10s} {'mid':>10s} {'size':>14s} {'actual $':>10s}")
    for l in legs:
        actual = l["size"] * l["price"]
        print(f"{l['symbol']:10s} {l['side']:4s} {l['notional']:10,.2f} {l['price']:10,.4f} "
              f"{l['size']:>14} {actual:10,.2f}")


async def cmd_status(cfg, args):
    rest, markets, mids = await context(cfg)
    try:
        acct = await rest.account()
        equity = Decimal(str(acct.get("equity") or 0))
        free = Decimal(str(acct.get("freeCollateral") or acct.get("free_collateral") or 0))
        pos = await positions_by_symbol(rest, markets)
        state = json.loads(STATE.read_text()) if STATE.exists() else None
        print(f"account : {cfg.account.address}  equity ${equity:,.2f}  free ${free:,.2f}")
        print(f"state   : {STATE}" + ("" if state else "  (flat: no basket on record)"))
        print(f"{'leg':10s} {'size':>14s} {'entry':>10s} {'mark':>10s} {'notional':>11s} {'uPnL':>9s}")
        total = Decimal(0)
        for sym in list(ETF_WEIGHTS) + [SHORT_LEG]:
            mk = markets[sym]
            mark = mids.get(sym) or mk.mark_price or mk.oracle_price
            p = pos.get(sym)
            if p is None:
                print(f"{sym:10s} {'-':>14s} {'-':>10s} {mark:10,.4f} {'-':>11s} {'-':>9s}")
                continue
            notional = abs(p.size) * mark
            upnl = (mark - p.entry_price) * p.size
            total += upnl
            print(f"{sym:10s} {p.size:>14} {p.entry_price:10,.4f} {mark:10,.4f} "
                  f"{notional:11,.2f} {upnl:+9.2f}")
        print(f"unrealised total: {total:+,.2f}")
        if state:
            print(f"opened {state['opened_at']}  L={state['leverage']}  "
                  f"equity at entry ${Decimal(str(state['equity_at_entry'])):,.2f}  "
                  f"notional at entry ${Decimal(str(state['per_side_notional'])):,.2f}/side")
    finally:
        await rest.close()


async def cmd_open(cfg, args):
    rest, markets, mids = await context(cfg)
    try:
        if STATE.exists() and not args.force:
            raise SystemExit(f"!! {STATE} exists - basket already on record (use --force to re-open)")
        acct = await rest.account()
        equity = Decimal(str(acct.get("equity") or 0))
        existing = await positions_by_symbol(rest, markets)
        if existing:
            raise SystemExit(f"!! refusing to open with existing positions: {sorted(existing)}")
        lev = Decimal(str(args.lev))
        per_side, legs = plan(equity, lev, markets, mids)
        print_plan(equity, lev, per_side, legs)
        if not args.yes:
            print("\n(dry run - pass --yes to place the orders)")
            return
        for l in legs:
            mk = markets[l["symbol"]]
            px = l["price"]
            price = (pricing.round_up_to_tick(px * (1 + THROUGH_BPS / 10000), mk.tick_size)
                     if l["side"] == "BUY"
                     else pricing.round_down_to_tick(px * (1 - THROUGH_BPS / 10000), mk.tick_size))
            try:
                resp = await rest.place_order(
                    market_id=mk.market_id, side=l["side"], price=price, quantity=l["size"],
                    tif="IOC", reduce_only=False,
                    client_id=f"spread-{l['symbol']}-{signing.now_us()}",
                    good_til_us=signing.now_us() + 45 * 86_400 * 1_000_000,
                    market_tick=mk.tick_size, market_step=mk.step_size,
                )
                print(f"  {l['symbol']:10s} {l['side']:4s} {l['size']} @ {price} -> {resp.get('status')}")
            except ArcusError as exc:
                print(f"  !! {l['symbol']} failed: {exc}")
            await asyncio.sleep(0.4)
        await asyncio.sleep(3)
        pos = await positions_by_symbol(rest, markets)
        print("\nresulting positions:")
        filled = {}
        for sym in list(ETF_WEIGHTS) + [SHORT_LEG]:
            p = pos.get(sym)
            if p is None:
                print(f"  {sym:10s} (none)")
                continue
            mk = markets[sym]
            mark = mids.get(sym) or mk.mark_price or mk.oracle_price
            filled[sym] = {"size": str(p.size), "entry_price": str(p.entry_price),
                           "side": "BUY" if p.size > 0 else "SELL"}
            print(f"  {sym:10s} {p.size} @ {p.entry_price}  mark {mark}")
        STATE.write_text(json.dumps({
            "opened_at": now_iso(), "timestamp": int(time.time()),
            "leverage": str(lev), "equity_at_entry": str(equity),
            "per_side_notional": str(per_side),
            "weights": {k: str(v) for k, v in ETF_WEIGHTS.items()},
            "legs": filled,
            "fills": {l["symbol"]: {"side": l["side"], "size": str(l["size"]),
                                    "ref_price": str(l["price"])} for l in legs},
        }, indent=1))
        print(f"\nstate written to {STATE}")
    finally:
        await rest.close()


async def cmd_close(cfg, args):
    rest, markets, mids = await context(cfg)
    try:
        orders = (await rest.open_orders()).get("orders", [])
        ids = {m.market_id for m in markets.values()}
        scoped = [o for o in orders if int(o.get("marketId", 0)) in ids]
        for o in scoped:
            try:
                await rest.cancel_order(market_id=int(o["marketId"]), order_id=o.get("orderId"))
            except ArcusError as exc:
                print(f"  !! cancel failed: {exc}")
        if scoped:
            print(f"cancelled {len(scoped)} resting order(s)")
            await asyncio.sleep(2)
        pos = await positions_by_symbol(rest, markets)
        if not pos:
            print("nothing to close")
            return
        for sym, p in pos.items():
            mk = markets[sym]
            mid = mids.get(sym) or mk.mark_price or mk.oracle_price
            side = "SELL" if p.size > 0 else "BUY"
            price = (pricing.round_down_to_tick(mid * (1 - THROUGH_BPS / 10000), mk.tick_size)
                     if side == "SELL"
                     else pricing.round_up_to_tick(mid * (1 + THROUGH_BPS / 10000), mk.tick_size))
            size = pricing.round_down_to_tick(abs(p.size), mk.step_size)
            try:
                resp = await rest.place_order(
                    market_id=mk.market_id, side=side, price=price, quantity=size,
                    tif="IOC", reduce_only=True,
                    client_id=f"spreadclose-{sym}-{signing.now_us()}",
                    good_til_us=signing.now_us() + 45 * 86_400 * 1_000_000,
                    market_tick=mk.tick_size, market_step=mk.step_size,
                )
                print(f"  {sym:10s} {side:4s} {size} @ {price} -> {resp.get('status')}")
            except ArcusError as exc:
                print(f"  !! {sym} close failed: {exc}")
            await asyncio.sleep(1.5)
        left = await positions_by_symbol(rest, markets)
        print("remaining:", {k: str(v.size) for k, v in left.items()} or "flat")
    finally:
        await rest.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    op = sub.add_parser("open")
    op.add_argument("--lev", default="1.5")
    op.add_argument("--yes", action="store_true")
    op.add_argument("--force", action="store_true")
    sub.add_parser("close")
    args = ap.parse_args()
    cfg = load_config(args.config)
    fn = {"status": cmd_status, "open": cmd_open, "close": cmd_close}[args.cmd]
    return asyncio.run(fn(cfg, args)) or 0


if __name__ == "__main__":
    raise SystemExit(main())
