#!/usr/bin/env python3
"""Margin guard for the held basket: warn on Feishu *before* a top-up is needed.

Cross margin means the account is liquidated when equity falls to the
maintenance requirement, not when the initial margin is exhausted - but the
first thing an operator feels is free collateral draining away.  Both are
tracked here, with three levels and a cooldown so the channel stays readable.

    .venv/bin/python tools/margin_watch.py --config config/mainnet.yaml --once
    .venv/bin/python tools/margin_watch.py --config config/mainnet.yaml --test
    .venv/bin/python tools/margin_watch.py --config config/mainnet.yaml        # loop

Pushes go to a Feishu (Lark) custom-bot webhook taken from the
``ARCUS_ALERT_WEBHOOK`` environment variable.  Unset means report-only.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import time
import urllib.request
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arcus import signing
from arcus.config import load_config
from arcus.models import Market, Position
from arcus.rest import ArcusRest

from tools.spread_basket import ETF_WEIGHTS, SHORT_LEG

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state" / "margin_alert_state.json"
ET = ZoneInfo("America/New_York")
# Alert delivery is a Feishu (Lark) custom-bot webhook: one POST with
# {"msg_type": "text"} and the alert body.  The URL is a bearer secret - anyone
# holding it can post into the chat - so it is read from the environment and
# never committed.  Set ARCUS_ALERT_WEBHOOK to enable pushes; with it unset the
# guard still evaluates and prints, it just does not send.
WEBHOOK = os.environ.get("ARCUS_ALERT_WEBHOOK", "")
SYMS = list(ETF_WEIGHTS) + [SHORT_LEG]

# Triggers, from least to most severe.  Drawdown is measured against peak equity
# and is the trigger an operator actually feels; the margin ones back it up.
#
# 2026-10-08 (strategy v2, 重估)：全部改成「按权益比例」，加本金后阈值自动缩放。
#   free 24/15/6（% of equity）—— 这是容量告警：还能不能开新仓
#   buffer 55/50/43  ==  安全指标 R = equity/维持保证金 分别跌到 2.20 / 2.00 / 1.75
#                        （R=1 才是强平；这是真正的风险告警）
#                        注意：每边 3.0x 的进场 R 就是 2.50（buffer 60%），
#                        阈值必须落在这之下，否则目标仓位本身就会天天触发 L1。
#   dd 12/20/30 与降杠杆阶梯一一对应（12% -> 1.5x，20% -> 1.0x，30% -> 熔断）
#   margin_pct 只是容量提醒（初始保证金用满不会强平），只在常规时段生效，
#   加成窗口关闭：那里基准占用本来就是 69%（off-hours IMF 15%）。
LEVELS = [
    ("L1", "提示", dict(margin_pct=90, free_pct=24, buffer=55, dd=12)),
    ("L2", "警告", dict(margin_pct=95, free_pct=15, buffer=50, dd=20)),
    ("L3", "紧急", dict(margin_pct=98, free_pct=6, buffer=43, dd=30)),
]


def in_boost_window(now: dt.datetime | None = None) -> bool:
    """Arcus 加成窗口 = ET 20:00-04:00 任意日 + 周末/假期。"""
    e = now or dt.datetime.now(ET)
    return e.weekday() >= 5 or e.hour >= 20 or e.hour < 4
COOLDOWN_S = 6 * 3600
HIST_MDD_BPS = 851.0     # basket spread max drawdown in the 87-day study
TEST_DELAY = [0]         # set by --delay, test mode only
TEST_LABEL = [""]        # set by --label, test mode only


def alert_levels(equity: float, margin_used: float, free: float, maint: float,
                 per_side: float, peak: float) -> tuple[float, list[dict]]:
    """Where each alert level sits, expressed in equity and in spread bps.

    A level fires when *any* of its conditions is met, so the binding trigger is
    whichever of the four produces the highest equity threshold.
    """
    dd = 100 * (peak - equity) / peak if peak else 0.0
    rows = []
    for name, label, th in LEVELS:
        free_th = equity * th["free_pct"] / 100
        cands = [
            ("回撤", peak * (1 - th["dd"] / 100) if peak else float("inf")),
            ("可用抵押", margin_used + free_th),
            ("强平缓冲", maint / (1 - th["buffer"] / 100) if maint else float("inf")),
        ]
        if not in_boost_window():
            cands.append(("保证金占用",
                          margin_used / (th["margin_pct"] / 100) if margin_used else float("inf")))
        driver, trig = max(cands, key=lambda kv: kv[1])
        dist = equity - trig
        rows.append({
            "level": name, "label": label, "driver": driver,
            "trigger_equity": trig, "dist_usd": dist,
            "dist_bps": dist / per_side * 1e4 if per_side else 0.0,
            "thresholds": th,
        })
    return dd, rows


def load_state() -> dict:
    if STATE.exists():
        try:
            st = json.loads(STATE.read_text())
            if not st.get("peak_equity"):
                raise ValueError
            return st
        except Exception:  # noqa: BLE001
            pass
    # Seed the peak from the entry record so the drawdown is measured from funding,
    # not from whenever this guard first ran.
    peak = 0.0
    basket = ROOT / "state" / "spread_basket.json"
    if basket.exists():
        try:
            peak = float(json.loads(basket.read_text()).get("equity_at_entry") or 0)
        except Exception:  # noqa: BLE001
            peak = 0.0
    return {"peak_equity": peak, "last_sent": {}, "last_level": None}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))


async def collect(rest, markets) -> dict:
    acct = await rest.account()
    equity = float(acct.get("equity") or 0)
    free = float(acct.get("freeCollateral") or 0)
    raw = (await rest.positions()).get("positions") or {}
    if isinstance(raw, dict):
        raw = list(raw.values())
    margin = maint = 0.0
    gross = 0.0
    legs = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        pos = Position.from_api(p)
        if pos.size == 0:
            continue
        sym = next((s for s, m in markets.items() if m.market_id == pos.market_id), None)
        if sym is None:
            continue
        mk = markets[sym]
        mark = float(p.get("markPx") or mk.mark_price or 0)
        notional = abs(float(pos.size)) * mark
        margin += float(p.get("marginUsed") or 0)
        maint += notional * float(mk.maintenance_margin_fraction)
        gross += notional
        legs.append({"symbol": sym, "side": "多" if pos.size > 0 else "空",
                     "notional": notional, "entry": float(pos.entry_price), "mark": mark,
                     "upnl": float(pos.unrealized_pnl)})
    buffer_amt = equity - maint
    per_side = float(json.loads((ROOT / "state" / "spread_basket.json").read_text())["per_side_notional"]) \
        if (ROOT / "state" / "spread_basket.json").exists() else 0.0
    return {
        "ts": int(time.time()), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "equity": equity, "free": free, "margin": margin, "maint": maint,
        "gross": gross, "legs": legs,
        "margin_pct": 100 * margin / equity if equity else 0,
        "buffer_pct": 100 * buffer_amt / equity if equity else 0,
        "buffer_usd": buffer_amt,
        "upnl": sum(l["upnl"] for l in legs),
        "per_side": per_side,
        "spread_bps": (sum(l["upnl"] for l in legs) / per_side * 1e4) if per_side else 0.0,
    }


def level_of(snap: dict, dd: float):
    boost = in_boost_window()
    for name, label, th in reversed(LEVELS):
        hit = (snap["free"] < snap["equity"] * th["free_pct"] / 100
               or snap["buffer_pct"] < th["buffer"] or dd >= th["dd"])
        if not boost:
            hit = hit or snap["margin_pct"] >= th["margin_pct"]
        if hit:
            return name, label
    return None, None


def message(snap: dict, dd: float, level: str, label: str, topup: float) -> str:
    icon = {"L1": "🟡", "L2": "🟠", "L3": "🔴"}[level]
    lines = [
        f"{icon} 保证金{label} {level} · memory 篮子价差",
        f"时间 {snap['time']}",
        "",
        f"权益      ${snap['equity']:,.2f}（较峰值回撤 {dd:.1f}%）",
        f"已用保证金 ${snap['margin']:,.2f} = 权益的 {snap['margin_pct']:.1f}%",
        f"可用抵押   ${snap['free']:,.2f}",
        f"维持保证金 ${snap['maint']:,.2f}  距强平缓冲 {snap['buffer_pct']:.1f}% "
        f"(${snap['buffer_usd']:,.0f})",
        f"spread 自入场 {snap['spread_bps']:+,.0f} bps（相当于每边名义的 {snap['spread_bps']/100:+.2f}%）",
        "",
        f"仓位 总名义 ${snap['gross']:,.0f} · 未实现 {snap['upnl']:+,.2f}",
    ]
    for l in snap["legs"]:
        lines.append(f"  {l['symbol']:9s} {l['side']} ${l['notional']:,.0f} @ {l['entry']:.2f} → {l['mark']:.2f}")
    if topup > 0:
        lines += ["", f"建议：补充保证金 ≈ ${topup:,.0f}（把占用率拉回 40%），或按比例减仓。",
                  f"参考：本样本 spread 历史最大回撤 {HIST_MDD_BPS:,.0f} bps。"]
    else:
        lines += ["", "建议：目前无需补充，继续观察；再跌则减仓或补保证金。"]
    return "\n".join(lines)


def send_feishu(text: str, idem: str) -> tuple[bool, str]:
    body = json.dumps({"msg_type": "text", "content": {"text": text}}).encode()
    req = urllib.request.Request(WEBHOOK, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode() or "{}")
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    ok = payload.get("code") == 0
    return ok, json.dumps(payload, ensure_ascii=False)[:200]


async def run_once(cfg, *, dry: bool, test: bool = False) -> int:
    if test and TEST_DELAY[0]:
        print(f"sleeping {TEST_DELAY[0]}s before sending the test message…")
        await asyncio.sleep(TEST_DELAY[0])
    priv = signing.load_private_key(cfg.account.api_key_path)
    async with ArcusRest(base_url=cfg.account.base_url, address=cfg.account.address,
                         account_index=cfg.account.account_index,
                         public_key=cfg.account.api_key_public or signing.public_key_hex(priv),
                         private_key=priv) as rest:
        markets = {}
        for m in (await rest.markets()).get("markets", []):
            mk = Market.from_api(m)
            if mk.symbol in SYMS:
                markets[mk.symbol] = mk
        snap = await collect(rest, markets)

    st = load_state()
    st["peak_equity"] = max(float(st.get("peak_equity") or 0), snap["equity"])
    dd = 100 * (st["peak_equity"] - snap["equity"]) / st["peak_equity"] if st["peak_equity"] else 0.0
    level, label = level_of(snap, dd)
    topup = max(0.0, snap["margin"] / 0.40 - snap["equity"]) if level else 0.0
    summary = (f"[{snap['time']}] equity ${snap['equity']:,.2f} dd {dd:+.1f}%  "
               f"margin {snap['margin_pct']:.1f}%  free ${snap['free']:,.2f}  "
               f"buffer {snap['buffer_pct']:.1f}%  gross ${snap['gross']:,.0f}  "
               f"uPnL {snap['upnl']:+,.2f}  -> {level or 'normal'}")
    if test:
        text = message(snap, dd, "L2", "警告（测试）", topup or 250.0)
        text = f"🧪 测试消息——保证金告警通道{TEST_LABEL[0]}\n\n" + text
        if dry:
            print(text)
            return 0
        ok, info = send_feishu(text, f"margin-test")
        print("feishu:", "OK" if ok else f"FAIL {info}")
        return 0

    print(summary)
    st["last_level"] = level
    now = time.time()
    if level:
        last = float(st["last_sent"].get(level, 0))
        if now - last >= COOLDOWN_S:
            text = message(snap, dd, level, label, topup)
            idem = f"margin-{level}-{time.strftime('%Y%m%dT%H', time.localtime())}"
            if dry:
                print("--- would send ---")
                print(text)
            else:
                ok, info = send_feishu(text, idem)
                print("feishu:", "OK" if ok else f"FAIL {info}")
                if ok:
                    st["last_sent"][level] = now
        else:
            print(f"({level} alert in cooldown, {(COOLDOWN_S - (now - last)) / 3600:.1f}h left)")
    save_state(st)
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--once", action="store_true", help="evaluate once and exit")
    ap.add_argument("--test", action="store_true", help="send a labelled test message")
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending")
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--delay", type=int, default=0, help="test mode: wait N seconds before sending")
    ap.add_argument("--label", default="", help="test mode: extra label appended to the message")
    args = ap.parse_args()
    TEST_DELAY[0], TEST_LABEL[0] = args.delay, args.label
    cfg = load_config(args.config)
    if args.once or args.test:
        return await run_once(cfg, dry=args.dry_run, test=args.test)
    while True:
        try:
            await run_once(cfg, dry=args.dry_run)
        except Exception as exc:  # noqa: BLE001
            print(f"!! {exc}")
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
