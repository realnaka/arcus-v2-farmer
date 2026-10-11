#!/usr/bin/env python3
"""Window-aware leverage scheduler + equity stop ladder for the memory spread.

Reads config/spread-v2.yaml (schedule / stops / execution) and the live account,
then either reports (`plan`, `status`) or moves the basket to the target size.

    .venv/bin/python tools/spread_schedule.py --config config/mainnet.yaml plan
    .venv/bin/python tools/spread_schedule.py --config config/mainnet.yaml sync --apply
    .venv/bin/python tools/spread_schedule.py --config config/mainnet.yaml stops --apply
    .venv/bin/python tools/spread_schedule.py --config config/mainnet.yaml tick --apply

`tick` = stops first, then sync.  That is the launchd cadence (every 5 min).
Nothing is sent without --apply.  state/STOP blocks all new orders.

Design notes
------------
* Base leverage applies in every window; only the boost core (21:30-01:00 ET and
  the weekend, minus the Korea open/close +-1.5h windows) gets the multiplier.
* ``mode.hold_rebalance: true`` freezes the book: no window switch, no equity
  drift and no price drift may trade.  Only a *new* stop-ladder rung still cuts
  the position (ratchet, recorded in ``state/spread_ladder.json``), and a rung
  that only partly filled gets finished against the frozen target sizes.
* If the stop ladder has fired, its target *overrides* the window target - the
  book stays de-levered until the recovery rule allows stepping back up.
* Increases are quoted maker-first at mid -+ 1 tick.  If an opening quote is
  still unfilled after fill_timeout_s it is cancelled and the remainder is taken
  (operator rule: "5 分钟不成交，改 taker").
* Reductions and every stop-ladder action are taken immediately: de-risking must
  not wait for a maker fill.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arcus import pricing, signing
from arcus.config import load_config
from arcus.errors import ArcusError
from tools.spread_basket import (ETF_WEIGHTS, SHORT_LEG, TRADEABLE_SUM,
                                 context, plan, positions_by_symbol)
from tools.margin_watch import send_feishu

ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "config" / "spread-v2.yaml"
PEAK_STATE = ROOT / "state" / "spread_peak.json"
PENDING = ROOT / "state" / "spread_pending.json"
HALT = ROOT / "state" / "spread_halt"
LADDER_STATE = ROOT / "state" / "spread_ladder.json"
ADL_STATE = ROOT / "state" / "spread_adl_reset.json"
ADL_LOG = ROOT / "state" / "adl_reset_log.jsonl"
ET = ZoneInfo("America/New_York")


def load_v2(path: Path = CFG_PATH) -> dict:
    return yaml.safe_load(path.read_text())


def read_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:                                   # noqa: BLE001
            return default
    return default


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=1, default=str))


# --------------------------------------------------------------------- schedule

def window_of(e: dt.datetime) -> str:
    """REG | BOOST_EDGE | BOOST_CORE  (ET clock)."""
    wd, h = e.weekday(), e.hour + e.minute / 60
    boost = wd >= 5 or h >= 20 or h < 4
    if not boost:
        return "REG"
    korea_open = wd in (6, 0, 1, 2, 3) and 18.5 <= h < 21.5
    korea_close = wd in (0, 1, 2, 3, 4) and 1.0 <= h < 4.0
    return "BOOST_EDGE" if (korea_open or korea_close) else "BOOST_CORE"


def hold_mode(v2: dict) -> bool:
    """True = never rebalance; only the stop ladder may still cut the book."""
    return bool((v2.get("mode") or {}).get("hold_rebalance", False))


def read_ladder_state(path: Path = LADDER_STATE) -> dict:
    st = read_json(path, {})
    return st if isinstance(st, dict) else {}


def read_ladder_level(path: Path = LADDER_STATE) -> int:
    """Highest stop-ladder step already executed (ratchet for hold mode)."""
    try:
        return int(read_ladder_state(path).get("level") or 0)
    except (TypeError, ValueError):
        return 0


def hold_action(hold: bool, level: int, state: dict, cycle: int) -> str:
    """What this cycle is allowed to do.

    ``rebalance`` - not in hold mode, caller keeps its normal behaviour
    ``cancel``    - hold, no rung armed: drop leftover quotes, trade nothing
    ``fire``      - hold, a *new* rung armed: cut the book to the rung target
    ``residual``  - hold, same rung: top up a de-lever that did not fully fill
    ``idle``      - hold, nothing to do
    """
    if not hold:
        return "rebalance"
    if level == 0:
        return "cancel"
    try:
        applied = int(state.get("level") or 0)
    except (TypeError, ValueError):
        applied = 0
    if level > applied:
        return "fire"
    if (state.get("targets") or {}) and cycle % 4 == 0:
        return "residual"
    return "idle"


def target_leverage(v2: dict, equity: Decimal, peak: Decimal) -> tuple[Decimal, str, int]:
    """Window target, capped by the stop ladder.

    Returns ``(per-side x, reason, level)``.  ``level`` is the 1-based ladder rung
    that capped the target, ``0`` when none is active and ``len(ladder) + 1`` for
    the flatten step.  The daemon treats it as a ratchet so one rung fires once
    instead of re-firing every 15 s.
    """
    lev = Decimal(str(v2["leverage"]["base_per_side"]))
    win = window_of(dt.datetime.now(ET))
    note = f"窗口 {win}"
    level = 0
    if win == "BOOST_CORE":
        lev *= Decimal(str(v2["leverage"]["boost_multiplier"]))
        note += " 加成核心"
    elif win == "BOOST_EDGE":
        note += " 加成避让（韩股窗口，不加杠杆）"
    dd = Decimal(0) if not peak else (peak - equity) / peak * 100
    ladder = sorted(v2["stops"]["ladder"], key=lambda s: float(s["trigger_pct"]))
    for idx, step in enumerate(ladder, start=1):
        if dd >= Decimal(str(step["trigger_pct"])):
            if step["action"] == "flatten_and_halt":
                return (Decimal(0),
                        f"回撤 {dd:.2f}% >= {step['trigger_pct']}% -> 熔断空仓",
                        len(ladder) + 1)
            cap = Decimal(str(step["target_per_side"]))
            if cap < lev:
                lev = cap
                note = f"回撤 {dd:.2f}% >= {step['trigger_pct']}% -> 降到每边 {cap}x"
                level = idx
    return lev, note, level


# ------------------------------------------------------------------ order maths

def target_sizes(equity: Decimal, lev: Decimal, markets, mids) -> dict:
    """Signed target size per leg for a given per-side leverage."""
    _, legs = plan(equity, lev, markets, mids)
    out = {}
    for l in legs:
        out[l["symbol"]] = l["size"] if l["side"] == "BUY" else -l["size"]
    return out


def diff_legs(targets: dict, pos: dict, markets, mids, min_notional: Decimal) -> list[dict]:
    """Signed deltas, rounded to each market's step, dust dropped."""
    out = []
    for sym, want in targets.items():
        mk = markets[sym]
        px = mids.get(sym) or mk.mark_price or mk.oracle_price
        have = pos[sym].size if sym in pos else Decimal(0)
        delta = want - have
        qty = pricing.round_down_to_tick(abs(delta), mk.step_size)
        if qty <= 0 or qty * px < min_notional:
            continue
        out.append({"symbol": sym, "market_id": mk.market_id, "side": "BUY" if delta > 0 else "SELL",
                    "size": qty, "price": px, "want": want, "have": have, "delta": delta,
                    "reducing": abs(want) < abs(have)})
    return out


def maker_price(side: str, mid: Decimal, tick: Decimal) -> Decimal:
    raw = mid - tick if side == "BUY" else mid + tick
    return pricing.round_down_to_tick(raw, tick) if side == "BUY" else pricing.round_up_to_tick(raw, tick)


def taker_price(side: str, mid: Decimal, tick: Decimal, through_bps: Decimal) -> Decimal:
    raw = mid * (1 + through_bps / 10000) if side == "BUY" else mid * (1 - through_bps / 10000)
    return pricing.round_up_to_tick(raw, tick) if side == "BUY" else pricing.round_down_to_tick(raw, tick)


async def send(rest, leg, price: Decimal, tif: str, *, reduce_only=False) -> str | None:
    mk_tick, mk_step = leg["_tick"], leg["_step"]
    try:
        resp = await rest.place_order(
            market_id=leg["market_id"], side=leg["side"], price=price, quantity=leg["size"],
            tif=tif, reduce_only=reduce_only,
            client_id=f"sched-{leg['symbol']}-{signing.now_us()}",
            good_til_us=signing.now_us() + 45 * 86_400 * 1_000_000,
            market_tick=mk_tick, market_step=mk_step,
        )
        oid = resp.get("orderId") or (resp.get("order") or {}).get("orderId")
        print(f"    {leg['symbol']:10s} {leg['side']:4s} {leg['size']} @ {price} {tif} -> "
              f"{resp.get('status')} {oid or ''}")
        status = str(resp.get("status") or "").upper()
        if tif == "ALO" and status in ("REJECTED", "CANCELED", "CANCELLED", "EXPIRED"):
            return None            # 被拒/撤销的 ALO 不能算 pending，否则会白等 5 分钟
        return oid
    except ArcusError as exc:
        print(f"    !! {leg['symbol']} {tif} failed: {exc}")
        return None


# ------------------------------------------------------------------ ADL 复位
#
# 2026-10-11 实盘决定。ADL 按「盈利 × 杠杆」挑对手方，所以浮盈越大越危险；
# 把仓位平掉再开回来，入场价被重置到当前价，浮盈与吸引力一起归零。
# 触发：任一腿未实现盈亏 >= 该腿保证金的 +leg_pnl_pct%（默认 10%）。
# 执行沿用既有方式：先挂 maker，fill_timeout_s 不成交就撤单改 taker。
#
# 阶段机（closing -> opening -> 空闲）写在 state/spread_adl_reset.json，
# 挂单写在 state/spread_pending.json，所以 daemon 重启后会接着跑，
# 不会「平完忘记开」，也不会开出两倍。


def adl_reset_cfg(v2: dict) -> dict:
    """ADL 复位参数，缺省对齐既有的 maker-first 执行约定。"""
    raw = v2.get("adl_reset") or {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "leg_pnl_pct": Decimal(str(raw.get("leg_pnl_pct", 10.0))),
        "fill_timeout_s": float(raw.get("fill_timeout_s",
                                        v2.get("execution", {}).get("fill_timeout_s", 300))),
        "cooldown_s": float(raw.get("cooldown_s", 1800)),
        "close_method": str(raw.get("close_method", "maker_first")),
        "notify": bool(raw.get("notify", True)),
    }


def leg_margin_notional(pos_row, market) -> tuple[Decimal, Decimal] | None:
    """``(保证金, 名义)``。保证金优先 ``marginUsed``，缺失时退回「名义 × 初始保证金率」。"""
    try:
        size = Decimal(str(pos_row.size))
        mark = Decimal(str(market.mark_price or market.oracle_price or 0))
        notional = (Decimal(str(pos_row.notional)) if getattr(pos_row, "notional", None) is not None
                    else abs(size) * mark)
        margin = Decimal(str(getattr(pos_row, "margin_used", None) or 0))
        if margin <= 0:
            margin = notional * Decimal(str(market.active_initial_margin_fraction or 0))
    except (AttributeError, InvalidOperation, TypeError, ValueError):
        return None
    if margin <= 0 or notional <= 0:
        return None
    return margin, notional


def leg_pnl_margin_pct(pos_row, market) -> float | None:
    """一条腿的未实现盈亏 = 该腿保证金的百分之多少。

    口径与 ``tools/adl_watch.py`` 的 ``upnl_margin_pct`` 一致：ADL 排序用的就是
    「盈利 ÷ 保证金」，所以触发器与监控报告用同一个数，不会各说各话。
    """
    got = leg_margin_notional(pos_row, market)
    if got is None:
        return None
    margin, _notional = got
    try:
        upnl = Decimal(str(pos_row.unrealized_pnl))
    except (AttributeError, InvalidOperation, TypeError, ValueError):
        return None
    return float(100 * upnl / margin)


def adl_trigger_legs(v2: dict, markets: dict, pos: dict) -> list[dict]:
    """哪条腿的浮盈已经达到「该腿保证金 +leg_pnl_pct%」。按浮盈比例降序。"""
    cfg = adl_reset_cfg(v2)
    if not cfg["enabled"]:
        return []
    floor = Decimal(str(v2["execution"]["min_order_notional"]))
    hits: list[dict] = []
    for sym, p in sorted(pos.items()):
        mk = markets.get(sym)
        if mk is None or p.size == 0:
            continue
        got = leg_margin_notional(p, mk)
        pct = leg_pnl_margin_pct(p, mk)
        if got is None or pct is None:
            continue
        margin, notional = got
        if notional < floor:              # 灰尘仓位：平了也开不回来，忽略
            continue
        if pct >= float(cfg["leg_pnl_pct"]):
            hits.append({
                "symbol": sym,
                "side": "多" if p.size > 0 else "空",
                "pnl_margin_pct": round(pct, 2),
                "upnl": round(float(p.unrealized_pnl), 2),
                "margin": round(float(margin), 2),
                "notional": round(float(notional), 2),
            })
    hits.sort(key=lambda h: -h["pnl_margin_pct"])
    return hits


def append_adl_log(row: dict) -> None:
    """复位事件流水：影子期与事后复核都靠这一行行 JSONL。"""
    ADL_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ADL_LOG.open("a") as fh:
        fh.write(json.dumps({"ts": time.time(),
                             "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                             **row}, default=str) + "\n")


def notify_adl(text: str, idem: str) -> None:
    try:
        ok, info = send_feishu(text, idem)
        print(f"[{time.strftime('%H:%M:%S')}] feishu: {'OK' if ok else 'FAIL ' + info}", flush=True)
    except Exception as exc:                                        # noqa: BLE001
        print(f"[{time.strftime('%H:%M:%S')}] feishu 异常: {exc}", flush=True)


def _finish_adl_state(st: dict, *, now: float, note: str) -> None:
    write_json(ADL_STATE, {"stage": "", "done_ts": now,
                           "done_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "trigger": st.get("trigger"), "targets": st.get("targets"),
                           "note": note})


async def adl_reset_step(rest, v2, markets, mids, pos, equity, *, dry: bool = False,
                        blocked: bool = False) -> bool:
    """ADL 复位状态机。返回 True = 本轮已被复位占用，调用者不要再做常规调仓。

    ``blocked`` 为真（熔断 / HALT）时放弃复位并交回常规风控：这两种状态只允许
    减仓，重新开仓是对规则的反向操作。
    """
    cfg = adl_reset_cfg(v2)
    if not cfg["enabled"]:
        return False
    min_notional = Decimal(str(v2["execution"]["min_order_notional"]))
    st = read_json(ADL_STATE, {})
    if not isinstance(st, dict):
        st = {}
    stage = str(st.get("stage") or "")
    now = time.time()

    if blocked:
        if stage:
            await cancel_resting(rest)
            _finish_adl_state(st, now=now, note="熔断/HALT 生效")
            append_adl_log({"event": "abandon", "stage": stage,
                            "reason": "熔断/HALT 生效", "trigger": st.get("trigger")})
            print(f"[{time.strftime('%H:%M:%S')}] ADL 复位放弃：熔断/HALT 生效", flush=True)
        return False

    if not stage:
        if now - float(st.get("done_ts") or 0) < cfg["cooldown_s"]:
            return False
        hits = adl_trigger_legs(v2, markets, pos)
        if not hits:
            return False
        line = " ".join(f"{h['symbol']}({h['side']}) {h['pnl_margin_pct']:+.1f}%保证金"
                        f"/${h['upnl']:+,.2f}" for h in hits)
        print(f"[{time.strftime('%H:%M:%S')}] ADL 复位触发：{line} -> 全平后按原大小重开",
              flush=True)
        if dry:
            return True
        await cancel_resting(rest)          # 先清掉遗留挂单，别和复位单打架
        targets = {sym: str(p.size) for sym, p in pos.items() if p.size}
        st = {"stage": "closing", "started_ts": now,
              "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "equity_at_start": str(equity), "trigger": hits, "targets": targets}
        write_json(ADL_STATE, st)
        append_adl_log({"event": "trigger", "trigger": hits, "targets": targets,
                        "equity": str(equity)})
        if cfg["notify"]:
            notify_adl(
                f"🔁 ADL 复位触发（每腿浮盈 >= 保证金 {cfg['leg_pnl_pct']}%）\n"
                f"时间 {time.strftime('%Y-%m-%d %H:%M:%S')}  权益 ${equity:,.2f}\n"
                f"{line}\n"
                f"动作：平掉整个篮子后按原大小重开；先 maker，"
                f"{int(cfg['fill_timeout_s'])}s 不成交改 taker。",
                f"adl-reset-start-{int(now)}",
            )

    if st.get("stage") == "closing":
        zero = {sym: Decimal(0) for sym in list(ETF_WEIGHTS) + [SHORT_LEG]}
        legs = [l for l in diff_legs(zero, pos, markets, mids, min_notional) if l["reducing"]]
        if legs:
            # close_method: maker_first（默认，按实盘决定）| taker（立刻降风险，
            # 不等 5 分钟）。薄盘周末 maker 大概率不成交，最后还是要吃单；
            # 想先脱身再慢慢重开时把它改成 taker。
            maker_first = cfg["close_method"] != "taker"
            print(f"[{time.strftime('%H:%M:%S')}] ADL 复位 1/2 平仓 {len(legs)} 腿"
                  + (f"（先 maker，{int(cfg['fill_timeout_s'])}s 不成交改 taker）"
                     if maker_first else "（直接 taker）"), flush=True)
            await run_sync(rest, v2, legs, markets, mids, maker_first_reduce=maker_first,
                           timeout=cfg["fill_timeout_s"])
            return True
        print(f"[{time.strftime('%H:%M:%S')}] ADL 复位 1/2 完成：篮子已平，转入重开",
              flush=True)
        await cancel_resting(rest)
        st["stage"] = "opening"
        st["flat_ts"] = now
        write_json(ADL_STATE, st)
        append_adl_log({"event": "flat", "trigger": st.get("trigger")})

    if st.get("stage") == "opening":
        targets = {s: Decimal(str(v)) for s, v in (st.get("targets") or {}).items()}
        if not targets:
            _finish_adl_state(st, now=now, note="没有记录重开目标")
            append_adl_log({"event": "abandon", "stage": "opening",
                            "reason": "没有记录重开目标"})
            print(f"[{time.strftime('%H:%M:%S')}] ADL 复位放弃：没有记录重开目标", flush=True)
            return False
        legs = [l for l in diff_legs(targets, pos, markets, mids, min_notional)
                if not l["reducing"]]
        if not legs:
            started = float(st.get("started_ts") or now)
            elapsed = round(now - started, 1)
            _finish_adl_state(st, now=now, note=f"完成 {elapsed}s")
            append_adl_log({"event": "done", "elapsed_s": elapsed,
                            "trigger": st.get("trigger"), "targets": st.get("targets")})
            print(f"[{time.strftime('%H:%M:%S')}] ADL 复位完成：已按原大小重开（{elapsed:.0f}s）",
                  flush=True)
            if cfg["notify"]:
                notify_adl(f"✅ ADL 复位完成（{elapsed:.0f}s）\n"
                           f"篮子已按原大小重开，浮盈口径归零。",
                           f"adl-reset-done-{int(now)}")
            return False
        print(f"[{time.strftime('%H:%M:%S')}] ADL 复位 2/2 重开 {len(legs)} 腿"
              f"（先 maker，{int(cfg['fill_timeout_s'])}s 不成交改 taker）", flush=True)
        await run_sync(rest, v2, legs, markets, mids, timeout=cfg["fill_timeout_s"])
        return True
    return False


# -------------------------------------------------------------------- commands

def peak_roll(equity: Decimal, write: bool = True) -> dict:
    st = read_json(PEAK_STATE, {})
    peak = Decimal(str(st.get("peak_equity") or equity))
    if equity > peak:
        peak = equity
    if write:
        write_json(PEAK_STATE, {"peak_equity": str(peak), "equity": str(equity),
                                "updated": time.strftime("%Y-%m-%d %H:%M:%S")})
    return {"peak": peak, "equity": equity,
            "dd_pct": (peak - equity) / peak * 100 if peak else Decimal(0)}


def halted() -> dict | None:
    return read_json(HALT, None) if HALT.exists() else None


async def cmd_report(cfg, v2, apply_changes: bool, do_stops: bool, do_sync: bool) -> None:
    rest, markets, mids, equity, free, pos = await gather(cfg, v2)
    try:
        st = peak_roll(equity, write=apply_changes)
        lev, why, level = target_leverage(v2, equity, st["peak"])
        hold = hold_mode(v2)
        print(f"equity ${equity:,.2f}  free ${free:,.2f}  peak ${st['peak']:,.2f}  "
              f"回撤 {st['dd_pct']:.2f}%")
        print(f"ET {dt.datetime.now(ET):%Y-%m-%d %H:%M} ({window_of(dt.datetime.now(ET))})  "
              f"目标每边 {lev}x  <- {why}")
        halt = halted()
        if halt:
            print(f"HALT 生效中（{halt.get('reason')} @ {halt.get('at')}）—— 只允许减仓")
        stop_file = ROOT / v2["limits"]["stop_file"]
        if stop_file.exists():
            apply_changes = False
            print(f"{stop_file} 存在 -> 本次只报告，不下单")
        adl_st = read_json(ADL_STATE, {})
        if isinstance(adl_st, dict) and adl_st.get("stage"):
            print(f"ADL 复位进行中：{adl_st['stage']}（{adl_st.get('started_at')} 起，"
                  f"触发 {adl_st.get('trigger')}）")
        else:
            hits = adl_trigger_legs(v2, markets, pos)
            cfg_adl = adl_reset_cfg(v2)
            if hits:
                print("ADL 复位已就绪（本轮 daemon 会执行）："
                      + " ".join(f"{h['symbol']} {h['pnl_margin_pct']:+.1f}%保证金"
                                 f"/${h['upnl']:+,.2f}" for h in hits))
            elif cfg_adl["enabled"]:
                print(f"ADL 复位待命：触发线 = 任一腿浮盈 >= 保证金 {cfg_adl['leg_pnl_pct']}%"
                      f"（冷却 {int(cfg_adl['cooldown_s'])}s）")

        if lev == 0 and do_stops:
            print("熔断：平掉全部 spread 腿")
            if apply_changes:
                for sym in list(ETF_WEIGHTS) + [SHORT_LEG]:
                    p = pos.get(sym)
                    if not p or p.size == 0:
                        continue
                    mk = markets[sym]
                    mid = mids.get(sym) or mk.mark_price or mk.oracle_price
                    leg = {"symbol": sym, "market_id": mk.market_id,
                           "side": "SELL" if p.size > 0 else "BUY",
                           "size": pricing.round_down_to_tick(abs(p.size), mk.step_size),
                           "price": mid, "_tick": mk.tick_size, "_step": mk.step_size}
                    leg["price"] = taker_price(leg["side"], mid, mk.tick_size,
                                               Decimal(str(v2["execution"]["through_bps"])))
                    await send(rest, leg, leg["price"], "IOC", reduce_only=True)
                    await asyncio.sleep(1.5)
            if apply_changes and not HALT.exists():
                write_json(HALT, {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                  "reason": f"回撤 {st['dd_pct']:.2f}% >= 12%"})
                print("HALT 已写入 state/spread_halt")
            return

        if halt and lev > 0:
            print("HALT 生效 -> 本次不增加仓位（如需恢复请删除 state/spread_halt）")
            return

        if hold and level == 0:
            print(f"hold 模式：跳过调仓（只跑止损阶梯；已执行档位 {read_ladder_level()}）")
            return
        if hold and level <= read_ladder_level():
            print(f"hold 模式：止损档位 {read_ladder_level()} 已执行过，跳过")
            return
        if hold and level > read_ladder_level():
            print(f"hold 模式：止损阶梯新档位 {read_ladder_level()} -> {level}，执行降杠杆")
            if apply_changes:
                write_json(LADDER_STATE, {"level": level, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                          "equity": str(equity), "reason": why})

        targets = target_sizes(equity, lev, markets, mids)
        min_notional = Decimal(str(v2["execution"]["min_order_notional"]))
        legs = diff_legs(targets, pos, markets, mids, min_notional)
        if not legs:
            print("已在目标仓位，无需动作")
            return
        print(f"{'leg':10s} {'now':>14s} {'target':>14s} {'delta':>14s}  action")
        for l in legs:
            act = "减仓(taker)" if l["reducing"] else "加仓(先 maker)"
            print(f"{l['symbol']:10s} {l['have']:>14} {l['want']:>14} {l['delta']:>+14}  {act}")
        if not (do_sync and apply_changes):
            print("(dry run - 加 --apply 才会下单)")
            return
        await run_sync(rest, v2, legs, markets, mids)
    finally:
        await rest.close()


async def run_sync(rest, v2, legs, markets, mids, *, timeout: float | None = None,
                   maker_first_reduce: bool = False) -> None:
    """把给定腿送向目标。

    加仓腿一律 maker 优先，超时改 taker。减仓腿默认立即吃单（``reduce_is_always_taker``），
    只有 ``maker_first_reduce`` 打开时才走同一条 maker -> 超时 taker 的路 —— ADL 复位
    按实盘决定用后者：平仓也要先挂 maker，5 分钟不成交才转 taker。
    """
    timeout = float(timeout if timeout is not None
                    else v2["execution"]["fill_timeout_s"])
    through = Decimal(str(v2["execution"]["through_bps"]))
    pending = read_json(PENDING, {})
    # 对冲腿先走：多腿的名义按「实际成交的空头」重算，避免部分成交时留下裸多头。
    legs = sorted(legs, key=lambda l: 0 if l["symbol"] == SHORT_LEG else 1)
    if legs and legs[0]["symbol"] == SHORT_LEG and not legs[0]["reducing"]:
        l = legs[0]
        mk = markets[l["symbol"]]
        l["_tick"], l["_step"] = mk.tick_size, mk.step_size
        ent = pending.get(l["symbol"])
        if ent and time.time() - float(ent.get("ts", 0)) < timeout:
            print(f"    {SHORT_LEG} 对冲腿挂单未满 {int(time.time()-float(ent['ts']))}s -> 本轮不碰多腿")
            write_json(PENDING, pending)
            return
        if ent:
            try:
                await rest.cancel_order(market_id=ent["market_id"], order_id=ent.get("order_id"))
            except ArcusError:
                pass
            pending.pop(l["symbol"], None)
            await send(rest, l, taker_price(l["side"], l["price"], mk.tick_size, through), "IOC")
        else:
            oid = await send(rest, l, maker_price(l["side"], l["price"], mk.tick_size), "ALO")
            if not oid:
                # ALO 被拒 = 我们的报价已经可成交（盘口穿过）。对冲腿卡在这里，
                # 多腿就永远等不到那次「实际成交的空头」，整个开仓会死循环。
                # 先按 MM 习惯退一 tick 重挂，再被拒就直接吃：报价可成交时
                # taker 只是把本来会吃到的成交拿到手。
                px2 = maker_price(l["side"],
                                  l["price"] - mk.tick_size * (1 if l["side"] == "BUY" else -1),
                                  mk.tick_size)
                print(f"    {SHORT_LEG} 对冲腿 ALO 被拒 -> 退一 tick 重挂 {px2}")
                oid = await send(rest, l, px2, "ALO")
            if not oid:
                px3 = taker_price(l["side"], l["price"], mk.tick_size, through)
                print(f"    {SHORT_LEG} 对冲腿仍被拒 -> 直接吃单 {px3}")
                await send(rest, l, px3, "IOC")
            if oid:
                pending[l["symbol"]] = {"order_id": oid, "market_id": l["market_id"],
                                        "ts": time.time(), "side": l["side"], "size": str(l["size"])}
        write_json(PENDING, pending)
        await asyncio.sleep(4)
        pos = await positions_by_symbol(rest, markets)
        p = pos.get(SHORT_LEG)
        mkp = markets[SHORT_LEG]
        px = mids.get(SHORT_LEG) or mkp.mark_price or mkp.oracle_price
        hedge = abs(p.size) * px if p else Decimal(0)
        print(f"    对冲腿现有空头 ${hedge:,.0f} -> 多腿按此重算")
        legs = legs[1:]
        kept = []
        for lg in legs:
            mk2 = markets[lg["symbol"]]
            notional = hedge * ETF_WEIGHTS[lg["symbol"]] / TRADEABLE_SUM
            want = pricing.size_for_notional(notional, lg["price"], mk2.step_size)
            have = pos[lg["symbol"]].size if lg["symbol"] in pos else Decimal(0)
            qty = pricing.round_down_to_tick(abs(want - have), mk2.step_size)
            if qty <= 0 or qty * lg["price"] < Decimal(str(v2["execution"]["min_order_notional"])):
                continue
            lg.update(size=qty, want=want, have=have, delta=want - have,
                      reducing=abs(want) < abs(have))
            kept.append(lg)
        legs = kept
    for l in legs:
        mk = markets[l["symbol"]]
        l["_tick"], l["_step"] = mk.tick_size, mk.step_size
        # reduce-only 始终跟着腿走：减仓单在任何路径下都不允许反手开新仓。
        ro = bool(l["reducing"])
        if ro and not maker_first_reduce:                     # 缩小绝对仓位 -> 立即吃单
            px = taker_price(l["side"], l["price"], mk.tick_size, through)
            await send(rest, l, px, "IOC", reduce_only=True)
            pending.pop(l["symbol"], None)
            await asyncio.sleep(1.0)
            continue
        ent = pending.get(l["symbol"])
        if ent and time.time() - float(ent.get("ts", 0)) < timeout:
            print(f"    {l['symbol']:10s} 挂单未满 {int(time.time()-float(ent['ts']))}s "
                  f"(< {int(timeout)}s) -> 继续等")
            continue
        if ent:
            if ent.get("order_id"):
                try:
                    await rest.cancel_order(market_id=ent["market_id"], order_id=ent["order_id"])
                    print(f"    {l['symbol']:10s} 超时撤单 {ent['order_id']}")
                except ArcusError as exc:
                    print(f"    {l['symbol']:10s} 撤单跳过: {exc}")
            else:
                print(f"    {l['symbol']:10s} 上次 ALO 被拒（盘口上没有挂单）-> 直接吃")
            pending.pop(l["symbol"], None)
            px = taker_price(l["side"], l["price"], mk.tick_size, through)
            await send(rest, l, px, "IOC", reduce_only=ro)
            await asyncio.sleep(1.2)
            continue
        px = maker_price(l["side"], l["price"], mk.tick_size)
        oid = await send(rest, l, px, "ALO", reduce_only=ro)
        if not oid:                                    # ALO 被拒（价格可成交）-> 再退一 tick 重试
            px2 = maker_price(l["side"], l["price"] - mk.tick_size * (1 if l["side"] == "BUY" else -1),
                              mk.tick_size)
            print(f"    {l['symbol']:10s} ALO 被拒 -> 退一 tick 重挂 {px2}")
            oid = await send(rest, l, px2, "ALO", reduce_only=ro)
        if oid:
            pending[l["symbol"]] = {"order_id": oid, "market_id": l["market_id"],
                                    "ts": time.time(), "side": l["side"], "size": str(l["size"])}
        else:
            # 两次都被拒 = 盘口上仍然没有我们的挂单。记一条没有 order_id 的
            # pending，让 5 分钟计时跑起来 —— 否则这一腿会每轮重挂、永远走不到
            # 「超时改 taker」，复位就会卡在半平状态（2026-10-11 实测踩到过）。
            pending[l["symbol"]] = {"order_id": "", "market_id": l["market_id"],
                                    "ts": time.time(), "side": l["side"],
                                    "size": str(l["size"]), "refused": True}
            print(f"    {l['symbol']:10s} 两次 ALO 都被拒 -> 从此刻起计时，"
                  f"{int(timeout)}s 后改 taker")
        await asyncio.sleep(0.6)
    write_json(PENDING, pending)


async def cancel_resting(rest) -> int:
    """Cancel every tracked resting order.  Used when hold mode takes over.

    A maker quote placed by an earlier rebalance cycle would otherwise sit on the
    book with nobody left to time it out, and it could still fill into a position
    the operator has explicitly frozen.
    """
    pending = read_json(PENDING, {})
    if not pending:
        return 0
    cancelled = 0
    for sym, ent in list(pending.items()):
        if not ent.get("order_id"):
            # 只有「两次 ALO 都被拒」留下的计时记录，盘口上没有单可撤。
            print(f"    {sym:10s} 没有挂单（ALO 被拒过）-> 只清记录", flush=True)
            pending.pop(sym, None)
            continue
        try:
            await rest.cancel_order(market_id=ent["market_id"], order_id=ent.get("order_id"))
            cancelled += 1
            print(f"    {sym:10s} 撤掉挂单 {ent.get('order_id')}（hold 模式接管）", flush=True)
        except ArcusError as exc:
            print(f"    {sym:10s} 撤单失败: {exc}", flush=True)
        pending.pop(sym, None)
    write_json(PENDING, pending)
    return cancelled


async def gather(cfg, v2):
    rest, markets, mids = await context(cfg)
    acct = await rest.account()
    equity = Decimal(str(acct.get("equity") or 0))
    free = Decimal(str(acct.get("freeCollateral") or acct.get("free_collateral") or 0))
    pos = await positions_by_symbol(rest, markets)
    return rest, markets, mids, equity, free, pos


async def cmd_adl(cfg, v2, *, apply_changes: bool) -> int:
    """跑一轮 ADL 复位检查。daemon 每轮也在跑，这个入口用于人工核对与演练。

    不带 ``--apply`` 时只报告会不会触发；带 ``--apply`` 时和 daemon 走同一条
    状态机（触发 -> 平仓 -> 重开），中途可以 Ctrl-C，剩下的阶段 daemon 会接手。
    """
    rest, markets, mids, equity, free, pos = await gather(cfg, v2)
    try:
        st = read_json(ADL_STATE, {})
        cfg_adl = adl_reset_cfg(v2)
        print(f"ADL 复位：{'开启' if cfg_adl['enabled'] else '关闭'}  "
              f"触发线 任一腿浮盈 >= 保证金 {cfg_adl['leg_pnl_pct']}%  "
              f"maker 超时 {int(cfg_adl['fill_timeout_s'])}s -> taker  "
              f"冷却 {int(cfg_adl['cooldown_s'])}s")
        if isinstance(st, dict) and st.get("stage"):
            print(f"在途阶段 {st['stage']}（{st.get('started_at')} 起，"
                  f"目标 {st.get('targets')}）")
        else:
            print(f"上轮完成于 {st.get('done_at') or '—'}（{st.get('note') or '无记录'}）")
        print(f"{'leg':10s} {'side':4s} {'notional':>12s} {'margin':>10s} "
              f"{'upnl':>9s} {'%margin':>9s}")
        for sym in list(ETF_WEIGHTS) + [SHORT_LEG]:
            p = pos.get(sym)
            mk = markets[sym]
            if not p or p.size == 0:
                print(f"{sym:10s} {'-':>4s} {'-':>12s} {'-':>10s} {'-':>9s} {'-':>9s}")
                continue
            got = leg_margin_notional(p, mk)
            pct = leg_pnl_margin_pct(p, mk)
            margin, notional = got if got else (Decimal(0), Decimal(0))
            print(f"{sym:10s} {'多' if p.size > 0 else '空':>4s} {notional:>12,.2f} "
                  f"{margin:>10,.2f} {float(p.unrealized_pnl):>+9.2f} "
                  f"{(pct if pct is not None else float('nan')):>+9.2f}")
        hits = adl_trigger_legs(v2, markets, pos)
        print("触发腿：" + (" ".join(f"{h['symbol']} {h['pnl_margin_pct']:+.1f}%"
                                     for h in hits) if hits else "无"))
        blocked = bool(halted()) or (ROOT / v2["limits"]["stop_file"]).exists()
        acted = await adl_reset_step(rest, v2, markets, mids, pos, equity,
                                     dry=not apply_changes, blocked=blocked)
        print(f"本轮动作：{'已执行一步' if acted and apply_changes else '无'}"
              f"{'（只报告）' if not apply_changes else ''}"
              f"{'（HALT/STOP 挡着）' if blocked else ''}")
    finally:
        await rest.close()
    return 0


# ---------------------------------------------------------------- 常驻守护进程

async def flatten_all(rest, v2, markets, mids, pos) -> None:
    through = Decimal(str(v2["execution"]["through_bps"]))
    for sym in list(ETF_WEIGHTS) + [SHORT_LEG]:
        p = pos.get(sym)
        if not p or p.size == 0:
            continue
        mk = markets[sym]
        mid = mids.get(sym) or mk.mark_price or mk.oracle_price
        side = "SELL" if p.size > 0 else "BUY"
        leg = {"symbol": sym, "market_id": mk.market_id, "side": side,
               "size": pricing.round_down_to_tick(abs(p.size), mk.step_size),
               "price": taker_price(side, mid, mk.tick_size, through),
               "_tick": mk.tick_size, "_step": mk.step_size}
        await send(rest, leg, leg["price"], "IOC", reduce_only=True)
        await asyncio.sleep(1.5)


async def cmd_daemon(cfg, v2, *, interval: float, inc_cooldown: float, dry: bool = False) -> None:
    """15 秒一轮的常驻止损/调仓进程。

    * 降杠杆与熔断：每轮检查，命中立刻执行（taker，不等 maker）。
    * 加仓：受 inc_cooldown 限制（默认 300s），maker 优先，超时改 taker。
    * ADL 复位：任一腿浮盈 >= 该腿保证金 +10% 时，全平后按原大小重开，
      平／开都先挂 maker，超时改 taker（``adl_reset``）。
    * ``mode.hold_rebalance: true`` 时只做上面两条，不跟权益/价格调仓。
    * markets 每 10 分钟刷新一次，account/positions 每轮读。
    """
    rest, markets, mids = await context(cfg)
    last_markets = time.time()
    last_mids = time.time()
    last_inc = 0.0
    cycle = 0
    print(f"[{time.strftime('%H:%M:%S')}] daemon 启动 interval={interval}s "
          f"inc_cooldown={inc_cooldown}s", flush=True)
    try:
        while True:
            cycle += 1
            t0 = time.time()
            try:
                if time.time() - last_markets > 600:
                    await rest.close()
                    rest, markets, mids = await context(cfg)
                    last_markets = time.time()
                    last_mids = time.time()
                elif time.time() - last_mids > 60:
                    # 报价是按 mid 推出来的：market 快照可以 10 分钟一刷，
                    # 但 mid 不行。跑完 5 分钟的 maker 等待再重开时，旧 mid
                    # 会让 ALO 直接穿价被拒（2026-10-11 重开阶段实测踩到过）。
                    try:
                        fresh = (await rest.mids()).get("mids", {})
                        if fresh:
                            mids = {k: Decimal(str(v)) for k, v in fresh.items()}
                            last_mids = time.time()
                    except ArcusError as exc:
                        print(f"[{time.strftime('%H:%M:%S')}] mids 刷新失败: {exc}", flush=True)
                acct = await rest.account()
                equity = Decimal(str(acct.get("equity") or 0))
                pos = await positions_by_symbol(rest, markets)
                st = peak_roll(equity)
                lev, why, level = target_leverage(v2, equity, st["peak"])
                hold = hold_mode(v2)
                stop_file = ROOT / v2["limits"]["stop_file"]
                blocked = stop_file.exists()
                halt = halted()
                if cycle % 4 == 1:            # 每 4 轮（≈60s）一行心跳
                    print(f"[{time.strftime('%H:%M:%S')}] equity ${equity:,.2f} "
                          f"回撤 {st['dd_pct']:.2f}% 目标 {lev}x"
                          f"{'｜hold：只做风控，不调仓' if hold else ''}"
                          f"{' (STOP 挡着)' if blocked else ''}"
                          f"{' (HALT)' if halt else ''}", flush=True)
                if blocked:
                    await asyncio.sleep(interval)
                    continue
                if dry:
                    if cycle % 20 == 1:
                        print(f"[{time.strftime('%H:%M:%S')}] (dry) {why}", flush=True)
                    hits = adl_trigger_legs(v2, markets, pos)
                    if hits:
                        print(f"[{time.strftime('%H:%M:%S')}] (dry) ADL 复位会触发："
                              + " ".join(f"{h['symbol']} {h['pnl_margin_pct']:+.1f}%保证金"
                                         for h in hits), flush=True)
                    await asyncio.sleep(interval)
                    continue
                if lev == 0:
                    print(f"[{time.strftime('%H:%M:%S')}] 熔断 {why} -> 全平", flush=True)
                    await adl_reset_step(rest, v2, markets, mids, pos, equity,
                                         dry=dry, blocked=True)
                    await flatten_all(rest, v2, markets, mids, pos)
                    if not HALT.exists():
                        write_json(HALT, {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": why})
                    await asyncio.sleep(interval)
                    continue
                if halt:
                    # HALT 只允许减仓：任何在途的复位在这里放弃，重新开仓是对规则的反向操作。
                    await adl_reset_step(rest, v2, markets, mids, pos, equity,
                                         dry=dry, blocked=True)
                    await asyncio.sleep(interval)
                    continue
                if await adl_reset_step(rest, v2, markets, mids, pos, equity, dry=dry):
                    await asyncio.sleep(interval)
                    continue
                targets = target_sizes(equity, lev, markets, mids)
                legs = diff_legs(targets, pos, markets, mids,
                                 Decimal(str(v2["execution"]["min_order_notional"])))
                if hold:
                    # hold 模式：仓位冻结，只有止损阶梯能动，而且永远只减不加。
                    # 新档位触发时先把目标冻在 state/spread_ladder.json —— 后续几轮
                    # 只用来补完没成交的减仓，不会跟着权益/价格漂移继续下单。
                    state = read_ladder_state()
                    action = hold_action(hold, level, state, cycle)
                    if action == "cancel":
                        await cancel_resting(rest)
                        await asyncio.sleep(interval)
                        continue
                    if action == "fire":
                        applied = state.get("level") or 0
                        print(f"[{time.strftime('%H:%M:%S')}] 风控降档 {applied} -> {level}：{why}",
                              flush=True)
                        write_json(LADDER_STATE, {
                            "level": level, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "equity": str(equity), "reason": why,
                            "targets": {s: str(v) for s, v in targets.items()}})
                    elif action == "residual":
                        frozen = state.get("targets") or {}
                        floor = Decimal(str(v2["execution"].get("ladder_residual_notional", 50)))
                        legs = [l for l in diff_legs({s: Decimal(v) for s, v in frozen.items()},
                                                     pos, markets, mids, floor)
                                if l["reducing"]]
                        if not legs:
                            await asyncio.sleep(interval)
                            continue
                        print(f"[{time.strftime('%H:%M:%S')}] 档位 {level} 减仓未跑完 -> 补 "
                              f"{len(legs)} 腿", flush=True)
                    else:
                        await asyncio.sleep(interval)
                        continue
                red = [l for l in legs if l["reducing"]]
                inc = [] if hold else [l for l in legs if not l["reducing"]]
                # 不平衡闸门：两腿成交速度不同会留下裸敞口。
                # 只允许「缩小 多头-空头 差额」的那一侧加仓，差额上限 = 目标名义的 25%。
                long_n = sum((pos[s].size * (mids.get(s) or markets[s].mark_price
                                             or markets[s].oracle_price))
                             for s in ETF_WEIGHTS if s in pos)
                mk_d = markets[SHORT_LEG]
                px_d = mids.get(SHORT_LEG) or mk_d.mark_price or mk_d.oracle_price
                short_n = abs(pos[SHORT_LEG].size * px_d) if SHORT_LEG in pos else Decimal(0)
                imbalance = long_n - short_n
                cap = (equity * lev) * Decimal("0.25")
                if imbalance < -cap:
                    inc = [l for l in inc if l["symbol"] != SHORT_LEG]
                    if inc:
                        print(f"[{time.strftime('%H:%M:%S')}] 空头偏多 ${-imbalance:,.0f} "
                              f"(> 25% 上限 ${cap:,.0f}) -> 本轮只补多腿", flush=True)
                elif imbalance > cap:
                    inc = [l for l in inc if l["symbol"] == SHORT_LEG]
                    if inc:
                        print(f"[{time.strftime('%H:%M:%S')}] 多头偏多 ${imbalance:,.0f} "
                              f"(> 25% 上限 ${cap:,.0f}) -> 本轮只补空腿", flush=True)
                if red:
                    print(f"[{time.strftime('%H:%M:%S')}] {why} -> 减仓 {len(red)} 腿", flush=True)
                    await run_sync(rest, v2, red, markets, mids)
                if inc and time.time() - last_inc >= inc_cooldown:
                    print(f"[{time.strftime('%H:%M:%S')}] {why} -> 加仓 {len(inc)} 腿", flush=True)
                    await run_sync(rest, v2, inc, markets, mids)
                    last_inc = time.time()
            except ArcusError as exc:
                print(f"[{time.strftime('%H:%M:%S')}] API 错误: {exc}", flush=True)
            except Exception as exc:                                   # noqa: BLE001
                print(f"[{time.strftime('%H:%M:%S')}] 循环异常: {type(exc).__name__}: {exc}",
                      flush=True)
            await asyncio.sleep(max(0.5, interval - (time.time() - t0)))
    finally:
        await rest.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--spread-config", default=str(CFG_PATH))
    ap.add_argument("--apply", action="store_true", help="真的下单（默认只报告）")
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--increase-cooldown", type=float, default=300.0)
    ap.add_argument("cmd", choices=["plan", "status", "sync", "stops", "tick", "daemon",
                                    "adl", "clear-halt"])
    args = ap.parse_args()
    v2 = load_v2(Path(args.spread_config))
    if args.cmd == "clear-halt":
        HALT.unlink(missing_ok=True)
        print("HALT 已清除（恢复按窗口杠杆执行）")
        return 0
    cfg = load_config(args.config)
    if args.cmd == "adl":
        asyncio.run(cmd_adl(cfg, v2, apply_changes=args.apply))
        return 0
    if args.cmd == "daemon":
        asyncio.run(cmd_daemon(cfg, v2, interval=args.interval,
                               inc_cooldown=args.increase_cooldown, dry=not args.apply))
        return 0
    do_stops = args.cmd in ("stops", "tick")
    do_sync = args.cmd in ("sync", "tick")
    asyncio.run(cmd_report(cfg, v2, args.apply, do_stops, do_sync))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
