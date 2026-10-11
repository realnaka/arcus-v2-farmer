#!/usr/bin/env python3
"""非交易时段加杠杆排班：按回撤预算定倍数，避开美股/韩股的开收盘窗口。

Arcus 官方口径（blog "Introducing Arcus Points", 2026-10-01）：
    "regular hours include US extended trading: 4AM to 8PM ET on US trading days.
     The boost window is everything else: 8PM to 4AM ET overnight, plus weekends
     and US market holidays for the markets that quote then."
所以盘前(04:00-09:30)与盘后(16:00-20:00) ET 都算常规时段，没有加成。

窗口（ET，样本期内全程 EDT = UTC-4）：
    REG        04:00-20:00 周一至周五        常规，积分×1
    US_OPEN    08:00-11:00                  美股开盘 ±1.5h（仍在常规时段内）
    US_CLOSE   14:30-17:30                  美股收盘 ±1.5h（仍在常规时段内）
    BOOST      20:00-04:00 + 周末            加成窗口，共 88 h/周
    BOOST_EDGE 加成窗口 ∩ 韩股开/收盘±1.5h   加成但避让 → 不加杠杆
    BOOST_CORE 加成窗口 − BOOST_EDGE          加杠杆窗口

韩股 09:00-15:30 KST = 20:00-02:30 ET（KST = ET + 13h）：
    开盘 ±1.5h → 18:30-21:30 ET，周日-周四
    收盘 ±1.5h → 01:00-04:00 ET，周一-周五

输出 leverage_windows.json + leverage-windows.html。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import math
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from report import chart, table
import sessions as S
from stats_lib import mean, stdev

HERE = Path(__file__).resolve().parent
OUTDIR = Path(os.environ.get("LAB_OUT", HERE / "out"))
ET = ZoneInfo("America/New_York")
SPREAD = S.PRIMARY                     # "BASKET"
LEV_BOOK = 1.5                         # 每边名义 / 权益
IMF_REG, IMF_OFF = 0.10, 0.15
MARGIN_GUARD = 0.70
DD_LEVELS = {"L1": 0.05, "L2": 0.10, "L3": 0.22}


def is_boost(e):
    """Arcus boost window: 20:00-04:00 ET on any day, plus Sat/Sun."""
    return e.weekday() >= 5 or e.hour >= 20 or e.hour < 4


def korea_edge(e):
    """±1.5h around the KOSPI open (09:00 KST) and close (15:30 KST)."""
    wd, h = e.weekday(), e.hour + e.minute / 60
    if wd in (6, 0, 1, 2, 3) and 18.5 <= h < 21.5:      # 开盘 20:00 ET ±1.5h
        return "KR_OPEN"
    if wd in (0, 1, 2, 3, 4) and 1.0 <= h < 4.0:        # 收盘 02:30 ET ±1.5h
        return "KR_CLOSE"
    return None


def us_edge(e):
    """±1.5h around the US open (09:30 ET) and close (16:00 ET)."""
    wd, h = e.weekday(), e.hour + e.minute / 60
    if wd >= 5:
        return None
    if 8.0 <= h < 11.0:
        return "US_OPEN"
    if 14.5 <= h < 17.5:
        return "US_CLOSE"
    return None


def window_of(e):
    """One operational window per timestamp."""
    wd, h = e.weekday(), e.hour + e.minute / 60
    if is_boost(e):
        edge = korea_edge(e)
        if edge:
            return "BOOST_EDGE"
        return "BOOST_CORE"
    return "REG"


def wsession(e, cls):
    """Session key so a night's window is one occurrence even across midnight."""
    d = e.date()
    if cls == "BOOST_CORE" and e.hour < 12:
        d -= dt.timedelta(days=1)
    if cls in ("KR_OPEN", "KR_CLOSE") and e.hour < 12:
        d -= dt.timedelta(days=1)
    return f"{cls}:{d.isoformat()}"


def wtags(ts):
    out = []
    for t in ts:
        e = dt.datetime.fromtimestamp(t, ET)
        cls = window_of(e)
        out.append((cls, wsession(e, cls)))
    return out


def main():
    prep = S.build("1m")
    ts = prep["ts"]
    tags = S.tag(ts)
    wt = wtags(ts)
    xs = S.series_of(prep)
    x = xs[SPREAD]
    days = prep["days"]
    weeks = days / 7.0

    # hours per week per window class
    counts = {}
    for cls, _ in wt:
        counts[cls] = counts.get(cls, 0) + 1
    for cls in ("BOOST_EDGE", "BOOST_CORE"):
        for i, (c, _) in enumerate(wt):
            pass
    edge_detail = {}
    for t in ts:
        e = dt.datetime.fromtimestamp(t, ET)
        k = korea_edge(e)
        if k and is_boost(e):
            edge_detail[k] = edge_detail.get(k, 0) + 1
    us_edge_counts = {}
    for t in ts:
        e = dt.datetime.fromtimestamp(t, ET)
        k = us_edge(e)
        if k:
            us_edge_counts[k] = us_edge_counts.get(k, 0) + 1

    hours = {c: n / 60.0 / weeks for c, n in counts.items()}
    blocks = {}
    for cls in ("REG", "BOOST_EDGE", "BOOST_CORE"):
        blocks[cls] = S.block(ts, x, wt, {cls})
        blocks[cls]["hours_per_week"] = hours.get(cls, 0.0)
        blocks[cls]["hours_per_session"] = blocks[cls]["minutes"] / 60.0 / max(1, blocks[cls]["sessions"])
    blocks["RTH"] = S.block(ts, x, tags, {"RTH"})
    blocks["RTH"]["hours_per_week"] = 32.5
    blocks["RTH"]["hours_per_session"] = 6.5

    # ---- leverage from three constraints
    ref = blocks["RTH"]["sigma_1h_bps"] * math.sqrt(6.5)     # one RTH session of risk
    budget = {k: 10000.0 * v / LEV_BOOK for k, v in DD_LEVELS.items()}
    guard = MARGIN_GUARD / (2 * LEV_BOOK * IMF_OFF)          # gross 2*L*k*E*IMF/E <= 70%
    cap100 = 1.0 / (2 * LEV_BOOK * IMF_OFF)                  # margin fully used
    plan = {}
    for cls in ("RTH", "REG", "BOOST_EDGE", "BOOST_CORE"):
        b = blocks[cls]
        h = b["hours_per_session"]
        cum = b["sigma_1h_bps"] * math.sqrt(h)
        plan[cls] = dict(
            hours_per_week=b["hours_per_week"], hours_per_session=h,
            sigma_1m=b["sigma_1m_bps"], sigma_1h=b["sigma_1h_bps"], cum_sigma=cum,
            k_equal_risk=ref / cum,
            worst_dd=b["worst_dd_bps"], dd_p05=b["dd_p05_bps"], dd_median=b["dd_median_bps"],
            k_L1=budget["L1"] / abs(b["worst_dd_bps"]),
            k_L1_p95=budget["L1"] / abs(b["dd_p05_bps"]),
            k_L2=budget["L2"] / abs(b["worst_dd_bps"]),
            path=b["path_per_session_bps"], sessions=b["sessions"],
            path_per_risk=b["path_per_session_bps"] / abs(b["worst_dd_bps"]))
    core = plan["BOOST_CORE"]
    edge = plan["BOOST_EDGE"]
    k_rec = round(min(core["k_equal_risk"], core["k_L1"], guard) - 0.05, 1)

    # ---- weekly OI-time under plans
    h_reg, h_edge, h_core = (plan["REG"]["hours_per_week"], plan["BOOST_EDGE"]["hours_per_week"],
                             plan["BOOST_CORE"]["hours_per_week"])
    oi = lambda k: h_reg + h_edge + h_core * k
    ks = sorted({1.0, 1.2, float(k_rec), 2.0})
    plans = [(k, oi(k)) for k in ks]

    out = dict(meta=dict(from_=time.strftime("%Y-%m-%d", time.gmtime(ts[0])),
                         to=time.strftime("%Y-%m-%d", time.gmtime(ts[-1])),
                         days=days, bars=len(ts), spread=S.SPREADS[SPREAD]["full"]),
               hours=hours, edge_detail={k: v / 60 / weeks for k, v in edge_detail.items()},
               us_edge={k: v / 60 / weeks for k, v in us_edge_counts.items()},
               plan=plan, budget=budget, margin_guard_k=guard, k_recommend=k_rec,
               margin_cap_k=cap100,
               oi_time_weekly=[{"k": k, "oi": v, "rel": v / oi(1.0)} for k, v in plans])
    (HERE / "leverage_windows.json").write_text(json.dumps(out, indent=1, default=str))

    print(f"weeks {weeks:.2f} · windows (h/week): "
          + ", ".join(f"{k} {v:.1f}" for k, v in sorted(hours.items())))
    print("korea edge (h/week):", {k: round(v, 1) for k, v in out["edge_detail"].items()})
    print("us edge   (h/week):", {k: round(v, 1) for k, v in out["us_edge"].items()})
    for cls in ("RTH", "REG", "BOOST_EDGE", "BOOST_CORE"):
        p = plan[cls]
        print(f"{cls:11s} {p['hours_per_week']:5.1f}h/wk · {p['hours_per_session']:4.1f}h/session · "
              f"σ1h {p['sigma_1h']:5.1f} · cum {p['cum_sigma']:5.1f} · worstDD {p['worst_dd']:7.1f} · "
              f"k_eq {p['k_equal_risk']:4.2f} · k_L1 {p['k_L1']:4.2f} · k_p95 {p['k_L1_p95']:4.2f} · "
              f"path/risk {p['path_per_risk']:5.1f}")
    print(f"margin guard k <= {guard:.2f} (gross 2·L·k·15% <= 70% at L={LEV_BOOK})")
    print(f"margin cap   k <= {cap100:.2f} (margin fully used)")
    print(f"recommended k = {k_rec:.2f}")
    for k, v in plans:
        print(f"  k={k:.2f} -> weekly OI-time {v:6.1f} N·h ({v/oi(1.0):.3f}x)")
    for k in (1.0, 1.2, 1.5, round(guard, 2), round(cap100, 2)):
        print(f"  k={k:5.2f} -> core worst DD {core['worst_dd']*k:7.1f} bps "
              f"({abs(core['worst_dd']*k)/budget['L1']*100:4.0f}% of L1) · "
              f"margin {2*LEV_BOOK*k*IMF_OFF*100:5.1f}% · OI-time {oi(k):6.1f} ({oi(k)/oi(1.0):.3f}x)")
    render(out, blocks, plan, guard, cap100, k_rec, oi, plans, weeks)
    return out


CSS = """
:root{--ink:#0f172a;--mut:#64748b;--line:#e2e8f0;--bg:#f8fafc}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",Arial,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:36px 24px 80px}
h1{font-size:25px;margin:0 0 6px}
h2{font-size:19px;margin:40px 0 6px;padding-bottom:8px;border-bottom:2px solid var(--line)}
h3{font-size:15.5px;margin:24px 0 8px}
.sub{color:var(--mut);font-size:13.5px;margin-bottom:22px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin:20px 0 8px}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:15px 16px}
.ct{font-size:12px;letter-spacing:.06em;color:var(--mut)}
.cv{font-size:17px;font-weight:650;margin:6px 0 6px;line-height:1.35}
.cd{font-size:13px;color:#475569}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);
border-radius:10px;overflow:hidden;font-size:13.5px;margin:12px 0}
th{text-align:left;padding:9px 11px;background:#f1f5f9;font-weight:600;color:#334155;
font-size:12.5px;white-space:nowrap}
td{padding:8px 11px;border-top:1px solid var(--line);white-space:nowrap}
tr:hover td{background:#fafcff}
code{background:#f1f5f9;padding:1px 5px;border-radius:4px;font-size:13px}
.k{color:#0f766e;font-weight:600}.b{color:#b91c1c;font-weight:600}
ul{padding-left:20px}li{margin:4px 0}
.foot{color:var(--mut);font-size:12.5px;margin-top:34px;border-top:1px solid var(--line);padding-top:14px}
"""


def render(out, blocks, plan, guard, cap100, k_rec, oi, plans, weeks):
    f0 = lambda v: f"{v:,.0f}"
    f1 = lambda v: f"{v:,.1f}"
    f2 = lambda v: f"{v:,.2f}"
    core, edge, reg = plan["BOOST_CORE"], plan["BOOST_EDGE"], plan["REG"]
    cards = [
        ("加成核心窗口的小时数", f"{f1(core['hours_per_week'])} h/周", 
         f"加成窗口共 {f1(core['hours_per_week']+edge['hours_per_week'])} h/周，"
         f"避让韩股开收盘 ±1.5h 后剩 {f1(core['hours_per_week'])} h"),
        ("建议倍数", f"{f1(k_rec)}×",
         f"回撤预算允许 {f2(core['k_L1'])}×、等波动率 {f2(core['k_equal_risk'])}×，"
         f"但保证金 70% 线只给到 {f2(guard)}× → 取 1.5×"),
        ("每周 OI-time 增益", f"{f2(oi(k_rec)/oi(1.0))}×",
         f"{f0(oi(1.0))} → {f0(oi(k_rec))} N·h（只靠加成核心窗口加 {f2(k_rec)}×）"),
        ("避让韩股窗口的收益", f"核心 σ/小时 {f1(core['sigma_1h'])} vs 边缘 {f1(edge['sigma_1h'])} bps",
         "被排除的韩股开/收盘窗口波动率是核心窗口的 "
         + f"{f2(edge['sigma_1h']/core['sigma_1h'])}×"),
    ]
    cardhtml = "".join('<div class="card"><div class="ct">%s</div><div class="cv">%s</div>'
                       '<div class="cd">%s</div></div>' % c for c in cards)

    win_rows = [
        ["REG 常规", "04:00-20:00 周一-周五", f"{f1(reg['hours_per_week'])}", f"{f1(reg['sigma_1h'])}",
         f"{f1(reg['cum_sigma'])}", f"{f0(reg['worst_dd'])}", "1.0×（基准）"],
        ["BOOST_EDGE 加成边缘", "18:30-21:30 + 01:00-04:00 ET", f"{f1(edge['hours_per_week'])}",
         f"{f1(edge['sigma_1h'])}", f"{f1(edge['cum_sigma'])}", f"{f0(edge['worst_dd'])}", "1.0×（避让）"],
        ["BOOST_CORE 加成核心", "21:30-01:00 ET + 周末", f"{f1(core['hours_per_week'])}",
         f"{f1(core['sigma_1h'])}", f"{f1(core['cum_sigma'])}", f"{f0(core['worst_dd'])}",
         f"<b>{f2(k_rec)}×</b>"],
    ]
    k_rows = []
    for cls, lab in (("REG", "REG 常规"), ("BOOST_EDGE", "BOOST_EDGE 加成边缘"),
                     ("BOOST_CORE", "BOOST_CORE 加成核心")):
        p = plan[cls]
        k_rows.append([lab, f"{f1(p['hours_per_session'])} h", f"{f1(p['cum_sigma'])}",
                       f"{f2(p['k_equal_risk'])}×", f0(p["worst_dd"]), f0(p["dd_p05"]),
                       f"{f2(p['k_L1'])}×", f"{f2(p['k_L1_p95'])}×"])
    oi_rows = [[f"{f2(k)}×", f"{f0(v)}", f"{f2(v/oi(1.0))}×",
                f"{f1(hours_per_week_of_plan := 0) if False else ''}"] for k, v in []]
    oi_rows = [[f"{f2(k)}×", f"{f0(v)}", f"{f2(v/oi(1.0))}×"] for k, v in plans]
    sweep = []
    for k in (1.0, 1.2, float(k_rec), round(guard, 2), round(cap100, 2)):
        dd = core["worst_dd"] * k
        marg = 2 * LEV_BOOK * k * IMF_OFF
        flag = ("不触发" if marg < 0.55 else
                "触发 L1 保证金线 (55%)" if marg < 0.70 else
                "超过 70% 守卫，需人工放行" if marg < 1.0 else "保证金打满，不可执行")
        sweep.append([f"{f1(k)}×", f"{f0(dd)}", f"{abs(dd)/out['budget']['L1']*100:.0f}%",
                      f"{marg*100:.1f}%", flag, f"{f0(oi(k))}", f"{oi(k)/oi(1.0):.2f}×"])
    sched = [
        ["04:00-08:00", "常规（盘前）", "×1", "1.0×", "—"],
        ["08:00-11:00", "常规（US 开盘 ±1.5h）", "×1", "1.0×", "避开美股开盘"],
        ["11:00-14:30", "常规（盘中）", "×1", "1.0×", "—"],
        ["14:30-17:30", "常规（US 收盘 ±1.5h）", "×1", "1.0×", "避开美股收盘"],
        ["17:30-20:00", "常规（盘后）", "×1", "1.0×", "—"],
        ["20:00-21:30", "加成（韩股开盘 ±1.5h）", "更高", "1.0×", "避开韩股开盘"],
        ["<b>21:30-01:00</b>", "<b>加成</b>", "更高", f"<b>{f2(k_rec)}×</b>", "唯一的加杠杆窗口"],
        ["01:00-04:00", "加成（韩股收盘 ±1.5h）", "更高", "1.0×", "避开韩股收盘"],
        ["周末（除上述韩股窗口）", "加成", "更高", f"{f2(k_rec)}×", "周五 21:30 至周一 01:00"],
    ]

    page = f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>非交易时段加杠杆排班</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>非交易时段加杠杆：排班、倍数与 OI-time 增益</h1>
<div class="sub">spread = {out['meta']['spread']} · 1 分钟线 {out['meta']['bars']:,} 根
（{out['meta']['from_']} → {out['meta']['to']}，{weeks:.1f} 周）· 每边基准名义 L = {LEV_BOOK}× 权益 ·
生成 {time.strftime('%Y-%m-%d %H:%M')}</div>

<div class="cards">{cardhtml}</div>

<h2>1 · Arcus 的加成窗口（官方原文）</h2>
<p>「Introducing Arcus Points」(2026-10-01) 原文：<i>"Outside regular trading hours: trading RWA
perpetuals outside US market hours currently earns you more. On Arcus, <b>regular hours include US
extended trading: 4AM to 8PM ET on US trading days. The boost window is everything else: 8PM to 4AM
ET overnight, plus weekends and US market holidays</b> for the markets that quote then."</i></p>
<p>结论：<b>盘前 04:00-09:30 与盘后 16:00-20:00 ET 都属于「常规时段」，没有加成</b>，
只有 20:00-04:00 ET + 周末/假期才有。API 的 <code>regularTradingHours</code>
（14400-72000 秒 = 04:00-20:00 ET）和 <code>offHoursInitialMarginFraction</code>（15%）都指向同一条线。
旁证：官方 Week 1 周报说 RWA 成交 $727.6M 里「隔夜/周末」$258.8M = 35.6%；
实测 DRAM 在 20:00-04:00+周末的成交占比 {f1(36.9)}% —— 吻合。
若规则是「不在美股 RTH 就给」，这个占比应该接近 67%（DRAM 只有 33% 成交落在 RTH）。</p>

<h2>2 · 窗口划分与每周小时数</h2>
{table(["窗口", "ET 时段", "h/周", "σ/小时 (bps)", "单场累计 σ", "单场最差回撤", "杠杆"], win_rows)}
<p>加成窗口共 {f1(core['hours_per_week']+edge['hours_per_week'])} h/周；按既定规则避让韩股开收盘 ±1.5h 后，
还剩 <b>{f1(core['hours_per_week'])} h/周（占全周 {f1(core['hours_per_week']/168*100)}%）</b>可以加杠杆。
美股开/收盘 ±1.5h 落在 08:00-11:00 与 14:30-17:30 ET，这两段本来就在 Arcus 常规时段里，
所以避让它们<b>不额外损失加成时间</b>。</p>

<h2>3 · 倍数从哪来：三个约束取最小</h2>
{table(["窗口", "单场时长", "单场累计 σ", "等波动率 k", "最差回撤", "回撤 P95", "L1 预算 k", "按 P95 的 k"], k_rows)}
<ul>
<li><b>等波动率</b>：让一整场的累计风险等于一个 RTH 时段（{f1(blocks['RTH']['sigma_1h_bps']*math.sqrt(6.5))} bps）。</li>
<li><b>L1 回撤预算</b>：L1 = 权益回撤 5%（arcus-farmer 的告警分级第一档）。每边 L={f1(LEV_BOOK)}× 时，
5% 权益 = {f0(out['budget']['L1'])} bps 的 spread 位移；用「单场最差回撤」而不是 σ 来定倍数。</li>
<li><b>保证金 70% 线</b>：加成窗口初始保证金 15%，总名义 2·L·k·权益 →
占用 = 2×{f1(LEV_BOOK)}×k×15%；顶到 70% 的 k 上限 = <b>{f2(guard)}×</b>。</li>
</ul>
<p>三条线分别给出 {f2(core['k_equal_risk'])}×（等波动率）/ {f2(core['k_L1'])}×（L1 回撤预算）/
{f2(guard)}×（保证金 70% 线）。<b>先撞墙的是保证金，不是波动率</b> —— 这正是 Arcus 把非交易时段
初始保证金上调 50% 想要达到的效果（官方 off-hours 说明原文：<i>"Opening a new position takes more
margin… putting on fresh risk costs more, which discourages overexposure in exactly the conditions
where it's most dangerous"</i>）。</p>
{table(["加成核心倍数 k", "核心窗口单场最差回撤", "占 L1 回撤预算", "保证金占用", "L1 保证金线 (55%)", "每周 OI-time (N·h)", "相对 1.0×"], sweep)}
<p>注意一个操作细节：arcus-farmer 的 L1 告警里还有一条「保证金占用 ≥55%」。
在 L={f1(LEV_BOOK)}× 下，k = {f2(0.55/(2*LEV_BOOK*IMF_OFF))}× 就会碰到这条线，
所以 <b>1.5× 会每晚触发本策略的告警</b>。两个选择：先用 {f1(1.2)}×（保证金
{f1(2*LEV_BOOK*1.2*IMF_OFF*100)}%，不触发任何现有告警），或者把加成窗口的保证金阈值放宽到 75% 再上 1.5×。</p>
<p>按后者，<b>建议 1.5×</b>：保证金占用 {f1(2*LEV_BOOK*1.5*IMF_OFF*100)}%、核心窗口最差回撤
{f0(abs(core['worst_dd'])*1.5)} bps（L1 回撤预算的 {f0(abs(core['worst_dd'])*1.5/out['budget']['L1']*100)}%），
每周 OI-time 从 {f0(oi(1.0))} 提到 {f0(oi(1.5))}。再往上（打满保证金的 {f2(cap100)}×）
会把 L1 余量吃到 {f0(abs(core['worst_dd'])*cap100/out['budget']['L1']*100)}%，而 OI-time 只再多
{f1((oi(cap100)-oi(1.5))/oi(1.0)*100)}% —— 不划算。</p>

<h2>4 · 排班表（ET）</h2>
{table(["ET 窗口", "Arcus 归类", "积分", "杠杆", "备注"], sched)}
<p>韩股窗口按 KST = ET + 13h 换算：开盘 09:00 KST = 20:00 ET，收盘 15:30 KST = 02:30 ET。
周末的加成核心从周五 21:30 ET 一直连到周一 01:00 ET（周日的韩股开盘窗口和周一凌晨的收盘窗口仍避让）。</p>

<h2>5 · 每周 OI-time 增益</h2>
{table(["加成核心窗口倍数 k", "每周 OI-time (N·h)", "相对 1.0×"], oi_rows)}
<p>1.0× 基准 = {f0(oi(1.0))} N·h。建议的 {f2(k_rec)}× 把它抬到 {f0(oi(k_rec))} N·h
（{f2(oi(k_rec)/oi(1.0))}×）。积分按「名义 × 时间 × 加成系数」计，所以
<b>OI-time 的这一段增长，是纯靠加杠杆拿到的额外积分基数</b>；把库存搬进加成窗口（时间维度）
则是另一条独立的增益路径。</p>

<div class="foot">数据：<code>spread-lab/data/candles_*_1m.json</code>；窗口时长按实际分钟数统计（{weeks:.2f} 周样本）。
脚本：<code>python3 leverage_windows.py</code> → <code>leverage_windows.json</code> + 本页。
生成于 {time.strftime('%Y-%m-%d %H:%M')}。</div>
</div></body></html>"""
    OUTDIR.mkdir(parents=True, exist_ok=True)
    (OUTDIR / "leverage-windows.html").write_text(page)
    (HERE / "report-leverage-windows.html").write_text(page)
    print(f"wrote {OUTDIR / 'leverage-windows.html'} ({len(page)/1024:.0f} KB)")


if __name__ == "__main__":
    main()
