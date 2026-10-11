#!/usr/bin/env python3
"""ADL / forced-closure monitor for the Arcus legs (report-only).

Arcus force-closes a position twice: ``LIQUIDATION`` when the account itself
busts, and ``ADL`` when the engine deleverages a *counterparty* instead of
letting the book or the insurance fund absorb the loss.  The second one is
dangerous for a spread book because it removes a leg without any order of ours,
and Arcus publishes neither the trigger rule nor the ranking it uses to pick the
counterparty.

So this tool reports three layers, and never trades:

* **Fact** - our own fills carrying a ``liquidation`` marker.  An ``ADL`` here
  is a real event on our account and alerts immediately.
* **Pressure** - forced-close orders the engine left in the public tape
  (``liq:`` order ids), counted per market per hour.
* **Exposure** - a 0-100 proxy for "how attractive are we as the counterparty",
  built from our profit/leverage vs same-side holders sampled from the public
  leaderboard, plus our share of open interest.

    .venv/bin/python tools/adl_watch.py --config config/mainnet.yaml --once
    .venv/bin/python tools/adl_watch.py --config config/mainnet.yaml --once --json
    .venv/bin/python tools/adl_watch.py --config config/mainnet.yaml --test
    .venv/bin/python tools/adl_watch.py --config config/mainnet.yaml

Nothing here is a calibrated probability; see engine/adl.py for the model and
README ("ADL 监控") for how the shadow period is graded.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arcus.config import load_config
from arcus.models import Account, Market, Position
from arcus.rest import ArcusRest
from engine import adl

from tools.margin_watch import send_feishu

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state" / "adl_watch_state.json"
HISTORY = ROOT / "state" / "adl_pressure.jsonl"

TRADE_PAGE = 1000          # /v1/trades page size we scan per market
PEER_CACHE_S = 900         # re-sample peer ranks at most this often
COOLDOWN_S = 3600          # score alerts per (market, level)
#: Levels we push to Feishu.  L1 ("中") is deliberately report-only: it fires on
#: a proxy score, and paging on a proxy every hour trains the operator to ignore
#: the channel.  ``--alert-l1`` opts in while the model is being calibrated.
ALERT_FROM = {"L2", "L3"}


def load_state() -> dict[str, Any]:
    if STATE.exists():
        try:
            st = json.loads(STATE.read_text())
            if isinstance(st, dict):
                return st
        except Exception:  # noqa: BLE001
            pass
    return {"last_sent": {}, "peer_cache": {}}


def save_state(st: dict[str, Any]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))


def append_history(row: dict[str, Any]) -> None:
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def history_row(rep: dict[str, Any]) -> dict[str, Any]:
    """The shadow-period audit line: one compact row per pass."""
    return {
        "ts": rep["ts"],
        "legs": [
            {
                "symbol": leg["symbol"],
                "side": leg["side"],
                "score": round(leg["score"], 2),
                "level": leg["level"],
                "attract": round(leg["attract"], 4),
                "pressure": round(leg["pressure"], 4),
                "crowding": round(leg["crowding"], 4),
                "peer_percentile": leg["peer_percentile"],
                "peer_n": leg["peer_n"],
                "forced_1h": (leg.get("tape") or {}).get("forced"),
                "forced_notional_1h": (leg.get("tape") or {}).get("forced_notional"),
                "our_hits_1h": (leg.get("tape") or {}).get("our_hits"),
                "adverse_1h": (leg.get("adverse") or {}).get("count"),
                "adverse_notional_1h": (leg.get("adverse") or {}).get("notional"),
            }
            for leg in rep["legs"]
        ],
        "forced_on_us": len(rep["forced_on_us"]),
    }


# ------------------------------------------------------------------ collect


def index_markets(raw_markets: list[Any], *, symbols: set[str]) -> dict[str, Market]:
    """Markets we care about: everything we hold, plus the configured legs.

    Reading open positions rather than trusting the config matters here - a
    spread basket or an adopted leftover position is an Arcus leg too, and it
    would otherwise be scored as if it did not exist.
    """
    out: dict[str, Market] = {}
    for raw in raw_markets:
        mk = Market.from_api(raw)
        if mk.symbol in symbols:
            out[mk.symbol] = mk
    return out


def leg_metrics(*, pos: Position, market: Market, equity: float) -> dict[str, Any]:
    """Our own numbers for one position: the ADL-attractiveness inputs."""
    mark = float(market.mark_price or 0)
    notional = float(pos.notional) if pos.notional is not None else abs(float(pos.size)) * mark
    margin = float(pos.margin_used or 0) or notional * float(market.active_initial_margin_fraction or 0)
    upnl = float(pos.unrealized_pnl)
    oi_notional = float(market.open_interest) * mark
    liq_distance_bps = None
    if pos.liquidation_price:
        liq = float(pos.liquidation_price)
        if mark:
            liq_distance_bps = (mark - liq) / mark * 1e4 * (1 if pos.size > 0 else -1)
    return {
        "symbol": market.symbol,
        "side": "多" if pos.size > 0 else "空",
        "notional": notional,
        "margin": margin,
        "upnl": upnl,
        "upnl_margin_pct": (100.0 * upnl / margin) if margin else 0.0,
        "leverage_eff": (notional / margin) if margin else 0.0,
        "margin_mode": pos.margin_mode or "CROSS",
        "mark": mark,
        "liq_price": float(pos.liquidation_price) if pos.liquidation_price else None,
        "liq_distance_bps": liq_distance_bps,
        "oi_notional": oi_notional,
        "oi_share": (notional / oi_notional) if oi_notional else 0.0,
        "equity": equity,
    }


async def scan_tape(
    rest: ArcusRest,
    *,
    markets: dict[str, Market],
    our_address: str,
    window_s: int,
) -> tuple[dict[str, Any], dict[str, list[adl.ForcedClosure]], int]:
    """Count engine forced closes in each market's recent public tape."""
    now_us = int(time.time() * 1_000_000)
    out: dict[str, Any] = {}
    forced_by_market: dict[str, list[adl.ForcedClosure]] = {}
    for sym, market in markets.items():
        rows = (await rest.trades(sym, limit=TRADE_PAGE)).get("trades", [])
        forced = [f for f in (adl.forced_from_trade(r, our_address=our_address) for r in rows) if f]
        window = adl.pressure_window(forced, now_us=now_us, window_s=window_s)
        # The accounts actually trading this market are the realistic ADL cohort
        # - a global volume leaderboard barely overlaps an exotic RWA perp.
        seen: dict[str, int] = {}
        unusual: dict[str, int] = {}
        for r in rows:
            for side in ("makerAddress", "takerAddress"):
                addr = str(r.get(side) or "").lower()
                if addr and addr != our_address.lower():
                    seen[addr] = seen.get(addr, 0) + 1
            for key in ("makerOrderId", "takerOrderId"):
                oid = str(r.get(key) or "")
                if adl.is_unrecognised_order_id(oid):
                    unusual[oid.split(":")[0]] = unusual.get(oid.split(":")[0], 0) + 1
        span_s = 0.0
        if len(rows) >= 2:
            ts = [int(r.get("timestamp") or 0) for r in rows if r.get("timestamp")]
            if ts:
                span_s = (max(ts) - min(ts)) / 1e6
        last = forced[0] if forced else None
        out[sym] = {
            "forced": window["count"],
            "forced_notional": float(window["notional"]),
            "our_hits": window["our_hits"],
            "rows": len(rows),
            "span_s": span_s,
            "truncated": adl.page_may_be_truncated(rows, limit=TRADE_PAGE),
            "participants": [a for a, _ in sorted(seen.items(), key=lambda kv: -kv[1])],
            "unrecognised_order_ids": dict(sorted(unusual.items(), key=lambda kv: -kv[1])[:5]),
            "last": (
                {
                    "order_id": last.order_id,
                    "price": float(last.price),
                    "size": float(last.size),
                    "notional": float(last.notional),
                    "ts_us": last.ts_us,
                }
                if last
                else None
            ),
        }
        forced_by_market[sym] = forced
    return out, forced_by_market, now_us


async def sample_peers(
    rest: ArcusRest,
    *,
    legs: list[dict[str, Any]],
    our_address: str,
    top_n: int,
    tape_participants: dict[int, list[str]] | None = None,
) -> dict[str, list[float]]:
    """Rank metric for same-side holders in our markets.

    The pool is the accounts we actually see trading each market plus the
    global volume leaderboard, because a global leaderboard alone barely
    overlaps an exotic RWA perp and would leave the percentile empty.
    """
    wanted: dict[int, str] = {}
    for leg in legs:
        wanted[leg["market_id"]] = leg["side"]
    out: dict[int, list[float]] = {mid: [] for mid in wanted}
    if not wanted or top_n <= 0:
        return out

    candidates: list[str] = []
    for mid in wanted:
        candidates += (tape_participants or {}).get(mid, [])[:top_n]
    try:
        board = await rest.leaderboard(sort_by="volume", window="24h", limit=top_n)
    except Exception:  # noqa: BLE001 - peer sampling is optional colour, not a control
        board = {}
    candidates += [str(e.get("address") or "") for e in board.get("entries", [])]

    unique: list[str] = []
    for addr in candidates:
        low = addr.lower()
        if low and low != our_address.lower() and low not in unique:
            unique.append(low)
    for addr in unique[: max(top_n, 15) * 2]:
        try:
            raw = (await rest.positions(address=addr)).get("positions") or {}
        except Exception:  # noqa: BLE001
            continue
        rows = list(raw.values()) if isinstance(raw, dict) else list(raw)
        for row in rows:
            if not isinstance(row, dict):
                continue
            mid = int(row.get("marketId") or 0)
            if mid not in wanted:
                continue
            size = float(row.get("size") or 0)
            if size == 0:
                continue
            side = "多" if size > 0 else "空"
            if side != wanted[mid]:
                continue
            notional = abs(float(row.get("positionValueNotional") or 0))
            margin = float(row.get("marginUsed") or 0)
            if notional <= 0 or margin <= 0:
                continue
            upnl = float(row.get("unrealizedPnl") or 0)
            out[mid].append(
                adl.profit_leverage_rank(
                    upnl_margin_pct=100.0 * upnl / margin,
                    leverage_eff=notional / margin,
                )
            )
    return out


async def collect(rest: ArcusRest, *, cfg, args) -> dict[str, Any]:
    our_address = cfg.account.address
    raw_markets = (await rest.markets()).get("markets", [])
    all_markets = {m.symbol: m for m in (Market.from_api(r) for r in raw_markets)}
    configured = {s.symbol for s in cfg.strategies}

    acct_raw = await rest.account()
    account = Account.from_api(acct_raw)
    raw_positions = (await rest.positions()).get("positions") or {}
    if isinstance(raw_positions, dict):
        raw_positions = list(raw_positions.values())

    equity = float(account.equity)
    legs: list[dict[str, Any]] = []
    maintenance = 0.0
    held: set[str] = set()
    for row in raw_positions:
        if not isinstance(row, dict):
            continue
        pos = Position.from_api(row)
        if pos.size == 0:
            continue
        market = next((m for m in all_markets.values() if m.market_id == pos.market_id), None)
        if market is None:
            continue
        leg = leg_metrics(pos=pos, market=market, equity=equity)
        leg["market_id"] = pos.market_id
        leg["liquidation_fee_ppm"] = market.liquidation_fee_ppm
        maintenance += leg["notional"] * float(market.maintenance_margin_fraction or 0)
        legs.append(leg)
        held.add(market.symbol)

    # Pressure is reported for the legs themselves and for the configured
    # markets, so the tape is still read while the book is flat.
    watched = {s: all_markets[s] for s in (held | configured) if s in all_markets}
    window_s = args.window_min * 60
    tape, forced_by_market, now_us = await scan_tape(
        rest, markets=watched, our_address=our_address, window_s=window_s
    )

    # Forced closures on our own account: the hard signal.
    fills = (await rest.fills(limit=200)).get("fills", [])
    forced_on_us = [f for f in (adl.forced_from_fill(r) for r in fills) if f]

    # Peer ranks come from the leaderboard; cache them so a 5-minute loop does
    # not re-sample 15 accounts every pass.
    st = load_state()
    cache = st.get("peer_cache") or {}
    cache_fresh = (time.time() - float(cache.get("ts") or 0)) < PEER_CACHE_S
    if cache_fresh and cache.get("legs") == [f"{l['market_id']}:{l['side']}" for l in legs]:
        peers = {int(k): v for k, v in (cache.get("samples") or {}).items()}
        peer_age_s = time.time() - float(cache.get("ts") or 0)
    else:
        participants = {
            leg["market_id"]: ((tape.get(leg["symbol"]) or {}).get("participants") or [])
            for leg in legs
        }
        peers = await sample_peers(
            rest,
            legs=legs,
            our_address=our_address,
            top_n=args.peers,
            tape_participants=participants,
        )
        st["peer_cache"] = {
            "ts": time.time(),
            "legs": [f"{l['market_id']}:{l['side']}" for l in legs],
            "samples": {str(k): v for k, v in peers.items()},
        }
        peer_age_s = 0.0

    for leg in legs:
        samples = peers.get(leg["market_id"], [])
        our_metric = adl.profit_leverage_rank(
            upnl_margin_pct=leg["upnl_margin_pct"], leverage_eff=leg["leverage_eff"]
        )
        leg["attract_raw"] = our_metric
        leg["peer_n"] = len(samples)
        pct = adl.peer_percentile(our_metric, samples)
        leg["peer_percentile"] = pct
        leg["attract"] = pct if pct is not None else our_metric
        tape_row = tape.get(leg["symbol"], {})
        # Only a failed position on the other side of the book makes us the ADL
        # counterparty; forced closes on our own side are market news.
        adverse = adl.adverse_pressure(
            forced_by_market.get(leg["symbol"], []),
            our_side=("LONG" if leg["side"] == "多" else "SHORT"),
            now_us=now_us,
            window_s=window_s,
        )
        leg["adverse"] = {
            "side": adverse["side"],
            "count": adverse["count"],
            "notional": float(adverse["notional"]),
            "our_hits": adverse["our_hits"],
        }
        leg["pressure"] = adl.pressure_component(
            forced_notional=Decimal(str(adverse["notional"])),
            oi_notional=Decimal(str(leg["oi_notional"] or 0)),
        )
        leg["crowding"] = adl.crowding_component(
            our_notional=Decimal(str(leg["notional"] or 0)),
            oi_notional=Decimal(str(leg["oi_notional"] or 0)),
        )
        leg["score"] = adl.exposure_score(
            attract=leg["attract"], pressure=leg["pressure"], crowding=leg["crowding"]
        )
        leg["level"] = adl.level(leg["score"])
        leg["tape"] = dict(tape_row)

    nonzero_fee = sum(1 for m in all_markets.values() if m.liquidation_fee_ppm)
    return {
        "ts": int(time.time()),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "address": our_address,
        "equity": equity,
        "free": float(account.free_collateral),
        "maintenance": maintenance,
        "R": (equity / maintenance) if maintenance else None,
        "legs": legs,
        "tape": {k: v for k, v in tape.items()},
        "forced_on_us": [
            {
                "method": f.method,
                "market": f.market,
                "notional": float(f.notional),
                "ts_us": f.ts_us,
                "trade_id": f.trade_id,
                "liquidated_user": f.forced_party,
            }
            for f in forced_on_us
        ],
        "peer_age_s": peer_age_s,
        "insurance": {"markets": len(raw_markets), "nonzero_fee_markets": nonzero_fee},
    }


# ------------------------------------------------------------------ report


def summary_line(rep: dict[str, Any]) -> str:
    bits = [f"[{rep['time']}] equity ${rep['equity']:,.0f}"]
    if rep.get("R"):
        bits.append(f"R {rep['R']:.2f}")
    if not rep["legs"]:
        bits.append("无持仓")
    for sym, tape in (rep.get("tape") or {}).items():
        if any(l["symbol"] == sym for l in rep["legs"]):
            continue
        bits.append(f"{sym} 盘口强平 {tape.get('forced', 0)}/{tape.get('our_hits', 0)}")
    for leg in rep["legs"]:
        tape = leg.get("tape") or {}
        pct = leg.get("peer_percentile")
        bits.append(
            f"{leg['symbol']} {leg['side']} ${leg['notional']:,.0f} "
            f"upnl {leg['upnl_margin_pct']:+.1f}%margin lev {leg['leverage_eff']:.1f}x "
            f"peer {'n/a' if pct is None else f'{pct*100:.0f}%'} "
            f"tape {tape.get('forced', 0)}强平/逆向{leg['adverse']['count']} "
            f"score {leg['score']:.0f} {leg['level']}"
        )
    if rep["forced_on_us"]:
        bits.append(f"!! 本账户被强制平仓 {len(rep['forced_on_us'])} 笔")
    return "  ".join(bits)


def message(rep: dict[str, Any], *, level: str, reason: str) -> str:
    icon = {"L1": "🟡", "L2": "🟠", "L3": "🔴"}.get(level, "⚪")
    lines = [
        f"{icon} ADL 监控 {level} · {reason}",
        f"时间 {rep['time']}",
        "",
        f"权益 ${rep['equity']:,.2f} · 维持保证金 ${rep['maintenance']:,.2f}"
        + (f" · R = {rep['R']:.2f}（R=1 强平）" if rep.get("R") else ""),
    ]
    for leg in rep["legs"]:
        tape = leg.get("tape") or {}
        pct = leg.get("peer_percentile")
        score_line = f"  风险指数 {leg['score']:.0f}/100（{adl.LEVEL_LABEL[leg['level']]}）"
        if leg["liq_distance_bps"] is not None:
            score_line += f" · 距强平 {leg['liq_distance_bps']:,.0f} bps"
        lines += [
            "",
            f"{leg['symbol']} {leg['side']} ${leg['notional']:,.0f}（OI 的 {leg['oi_share']*100:.2f}%）",
            f"  未实现 {leg['upnl']:+,.2f} = 保证金 {leg['upnl_margin_pct']:+.1f}%"
            f" · 有效杠杆 {leg['leverage_eff']:.1f}x",
            f"  同侧对手方分位 {('n/a' if pct is None else f'{pct*100:.0f}%')}"
            f"（样本 {leg['peer_n']}）→ 吸引力 {leg['attract']*100:.0f}%",
            f"  近 1h 全场强平 {tape.get('forced', 0)} 笔；其中砍到我们对手方（{leg['adverse']['side']}）"
            f"{leg['adverse']['count']} 笔 / ${leg['adverse']['notional']:,.0f}"
            f"，我们对家 {leg['adverse']['our_hits']} 笔 → 压力 {leg['pressure']*100:.0f}%",
            score_line,
        ]
    if rep["forced_on_us"]:
        lines += ["", "本账户强制平仓记录："]
        for f in rep["forced_on_us"][:5]:
            lines.append(f"  {f['method']} {f['market']} ${f['notional']:,.0f} @ {f['ts_us']}")
    ins = rep.get("insurance") or {}
    lines += [
        "",
        f"保险池：余额未公开（无 REST 端点 / 无公开链上合约）；"
        f"强平罚金非零市场 {ins.get('nonzero_fee_markets', 0)}/{ins.get('markets', 0)}",
        "",
        "（代理指标，非概率。参考：Arcus 未公布 ADL 触发规则与排序。）",
    ]
    return "\n".join(lines)


async def run_once(cfg, *, args) -> int:
    rest = ArcusRest(
        base_url=cfg.account.base_url,
        address=cfg.account.address,
        account_index=cfg.account.account_index,
        public_key=cfg.account.api_key_public or "",
        private_key=None,
    )
    async with rest:
        rep = await collect(rest, cfg=cfg, args=args)

    # A test or a dry run must not enter the shadow-period audit trail.
    recording = not (args.test or args.dry_run)

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, default=str))
    else:
        print(summary_line(rep))
        for leg in rep["legs"]:
            tape = leg.get("tape") or {}
            if tape.get("truncated"):
                print(f"  note: {leg['symbol']} 的 trade 页已满，强平计数是下限（覆盖 {tape['span_s']:.0f}s）")
            if tape.get("span_s") and tape["span_s"] + 1 < args.window_min * 60:
                print(f"  note: {leg['symbol']} tape 只覆盖 {tape['span_s']:.0f}s < 窗口 {args.window_min*60}s")
            if tape.get("unrecognised_order_ids"):
                print(
                    f"  note: {leg['symbol']} 出现非 16-hex 且非 liq: 的 orderId 前缀 "
                    f"{tape['unrecognised_order_ids']} —— 强制平仓的 id 形状可能变了，压力计数要复核"
                )

    if recording:
        append_history(history_row(rep))

    st = load_state()
    st["last"] = rep
    now = time.time()
    sends: list[tuple[str, str, str, bool]] = []

    # 1. Hard signal: free of cooldown, one alert per forced fill.
    for f in rep["forced_on_us"]:
        key = f"forced-{f['method']}-{f['trade_id'] or f['ts_us']}"
        if key in st["last_sent"]:
            continue
        sends.append((key, "L3", f"本账户被 {f['method']}（{f['market']}）", True))

    # 2. Score alerts, per market and level, behind a cooldown.
    for leg in rep["legs"]:
        lvl = leg["level"]
        if lvl not in ALERT_FROM and not args.alert_l1:
            continue
        if lvl == "OK":
            continue
        key = f"{leg['symbol']}-{lvl}"
        if now - float(st["last_sent"].get(key) or 0) < COOLDOWN_S:
            continue
        sends.append((key, lvl, f"{leg['symbol']} ADL 风险指数 {leg['score']:.0f}/100", False))

    if args.test:
        text = "🧪 测试消息——ADL 监控通道\n\n" + message(rep, level="L2", reason="测试")
        if args.dry_run:
            print(text)
            return 0
        ok, info = send_feishu(text, "adl-test")
        print("feishu:", "OK" if ok else f"FAIL {info}")
        return 0

    for key, lvl, reason, _urgent in sends:
        text = message(rep, level=lvl, reason=reason)
        if args.dry_run:
            print("--- would send ---")
            print(text)
        else:
            ok, info = send_feishu(text, key)
            print("feishu:", "OK" if ok else f"FAIL {info}", f"({reason})")
            # Recorded whether or not the webhook accepted it: a failing webhook
            # must not turn into a send loop on every pass.
            st["last_sent"][key] = now
    if not sends:
        print("(no alert)")
    save_state(st)
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description="Arcus ADL / forced-closure monitor (report-only)")
    ap.add_argument("--config", required=True)
    ap.add_argument("--once", action="store_true", help="collect and report once")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    ap.add_argument("--test", action="store_true", help="send a labelled test message")
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending")
    ap.add_argument("--interval", type=int, default=300, help="loop interval (s)")
    ap.add_argument("--window-min", type=int, default=60, help="forced-close window (min)")
    ap.add_argument("--peers", type=int, default=15, help="leaderboard accounts to sample")
    ap.add_argument("--alert-l1", action="store_true", help="also push the '中' level to Feishu")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.once or args.test:
        return await run_once(cfg, args=args)
    while True:
        try:
            await run_once(cfg, args=args)
        except Exception as exc:  # noqa: BLE001 - a monitor must not die on one bad pass
            print(f"!! {exc}")
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
