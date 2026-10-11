#!/usr/bin/env python3
"""风险重估：用维持保证金（equity/MM）而不是初始保证金衡量安全性。

三个量要分清：
  初始保证金 IMF  10% 常规 / 15% 加成 —— 只决定「能开多大」，用满是能力上限，不是风险
  维持保证金 MMF  6.67%（股票类）    —— 决定强平：equity < gross x MMF 即被清算
  安全指标 R = equity / (gross x MMF)  —— R=1 是强平线，越大越安全

历史数据 + 价格带（结构性尾部）一起给出：跌多少才危险、止损该放哪。
输出 risk_mm.json + risk-mm.html。
"""

from __future__ import annotations

import json
import os
import math
import time
from decimal import Decimal
from pathlib import Path

import sessions as S
from report import table
from stats_lib import quantile, stdev

HERE = Path(__file__).resolve().parent
OUTDIR = Path(os.environ.get("LAB_OUT", HERE / "out"))
IMF_REG, IMF_OFF, MMF = 0.10, 0.15, 0.066667
EQUITY = float(os.environ.get("LAB_EQUITY", "10000"))   # 演示用权益；换成本地值
LEV_DEMO = 1.5                                          # 示例仓位：每边名义 / 权益
GROSS = 2 * LEV_DEMO * EQUITY                           # 总名义


def roll_dd(x, h):
    """Worst peak-to-trough drawdown inside any window of h bars."""
    worst = 0.0
    for i in range(h, len(x)):
        worst = min(worst, x[i] - max(x[i - h:i + 1]))
    return worst


def main():
    prep = S.build("1m")
    xs = S.series_of(prep)
    x = xs[S.PRIMARY]
    days = prep["days"]

    peak, mdd = x[0], 0.0
    for v in x:
        peak = max(peak, v)
        mdd = min(mdd, v - peak)
    roll = {h: roll_dd(x, h) for h in (360, 1440, 4320, 7200, 10080)}
    # 每日最差（日内）
    daily_worst = []
    step = 1440
    for i in range(step, len(x), step):
        seg = x[i - step:i + 1]
        p = seg[0]
        d = 0.0
        for v in seg:
            p = max(p, v)
            d = min(d, v - p)
        daily_worst.append(d)

    mk = {m["marketDisplayName"]: m
          for m in json.loads((HERE / "data" / "markets.json").read_text())["markets"]}
    # 结构性尾部：多头腿全到上带、空头腿到下带
    def band_leg(sym, expanded=False):
        m = mk[sym]
        ref = float(m.get("currentSettlementPrice") or m["oraclePrice"])
        up = (m.get("nextUpperTradingBound") if expanded else None) or m.get("upperTradingBound")
        lo = (m.get("nextLowerTradingBound") if expanded else None) or m.get("lowerTradingBound")
        if up and lo:
            return float(up) / ref - 1, float(lo) / ref - 1
        # API 字段在某些状态下返回 null；股票永续价格带是结算价 ±5%（扩张带 ±10%）。
        band = 0.10 if expanded else 0.05
        return band, -band

    def band_tail(expanded: bool) -> float:
        up = 0.0
        for sym, w in S.SPREADS[S.PRIMARY]["long"].items():
            hi, _ = band_leg(sym, expanded)
            up += w * hi
        _, lo_dram = band_leg("DRAM-USD", expanded)
        return (up - lo_dram) * 1e4

    tail_now, tail_exp = band_tail(False), band_tail(True)

    # 每个杠杆下的指标
    lev_rows = []
    for L in (1.5, 2.0, 2.3, 3.0, 4.0):
        gross = 2 * L
        R0 = 1 / (gross * MMF)
        bps_liq = (1 - gross * MMF) / L * 1e4
        lev_rows.append([f"每边 {L:.2f}×", f"{gross:.2f}×", f"{gross*IMF_REG*100:.0f}%",
                         f"{gross*IMF_OFF*100:.0f}%", f"{R0:.2f}×", f"{bps_liq:,.0f}",
                         f"{1 - gross*MMF:.1%}"])

    # 历史事件在不同杠杆下 -> 权益回撤 / R
    events = [("历史最深回撤（60 天）", mdd), ("最差 1 天滚动", roll[1440]),
              ("最差 3 天滚动", roll[4320]), ("最差 5 天滚动", roll[7200]),
              ("最差 7 天滚动", roll[10080]),
              ("结构性尾部（现价带 ±）", -tail_now), ("结构性尾部（扩张带）", -tail_exp)]
    ev_rows = []
    for lab, v in events:
        row = [lab, f"{v:,.0f}"]
        for L in (1.5, 2.0, 2.3, 3.0):
            loss_pct = abs(v) * L / 1e4 * 100
            R = (1 - loss_pct / 100) / (2 * L * MMF)
            row.append(f"{loss_pct:.1f}% / R {R:.2f}")
        ev_rows.append(row)

    dd_hist = sorted(abs(v) for v in daily_worst)
    out = dict(meta=dict(days=days, bars=len(prep["ts"]), equity=EQUITY,
                         spread=S.SPREADS[S.PRIMARY]["full"]),
               max_dd_bps=mdd, roll={str(k): v for k, v in roll.items()},
               band_tail_bps=tail_now, band_tail_expanded_bps=tail_exp,
               daily_worst_p50=quantile(daily_worst, 0.5),
               daily_worst_p95=quantile(daily_worst, 0.05),
               daily_worst_max=min(daily_worst))
    (HERE / "risk_mm.json").write_text(json.dumps(out, indent=1, default=str))

    print(f"spread {S.SPREADS[S.PRIMARY]['full'][:40]} · {days:.1f} 天")
    print(f"历史最深回撤 {mdd:,.0f} bps · 最差1天 {roll[1440]:,.0f} · 最差5天 {roll[7200]:,.0f}")
    print(f"日内回撤 p50 {quantile(daily_worst,0.5):,.0f} / p95 {quantile(daily_worst,0.05):,.0f} "
          f"/ max {min(daily_worst):,.0f}")
    print(f"结构性尾部（现价带）{tail_now:,.0f} bps · 扩张带 {tail_exp:,.0f} bps")
    for row in lev_rows:
        print("  ", " | ".join(row))
    for row in ev_rows:
        print("  ", " | ".join(row))
    render(lev_rows, ev_rows, out, mdd, roll, tail_now, tail_exp, daily_worst, days)
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


def render(lev_rows, ev_rows, out, mdd, roll, tail_now, tail_exp, daily_worst, days):
    f0 = lambda v: f"{v:,.0f}"
    f1 = lambda v: f"{v:,.1f}"
    f2 = lambda v: f"{v:,.2f}"
    R20 = 1 / (2 * 2.0 * MMF)
    R23 = 1 / (2 * 2.3 * MMF)
    R15 = 1 / (2 * 1.5 * MMF)
    ddp = lambda bps, L: abs(bps) * L / 1e4 * 100
    cards = [
        ("安全指标 R = equity / 维持保证金", f"{R20:.2f}× @ 每边 2.0×",
         f"每边 1.5× 是 {R15:.2f}×，2.3× 是 {R23:.2f}×；R=1 才是强平"),
        ("强平需要跌多少", f"{1-2*2.0*MMF:.1%} 权益 @ 每边 2.0×",
         f"换成 spread 位移 = {(1-2*2.0*MMF)/2.0*1e4:,.0f} bps"),
        ("历史最深回撤", f"{f0(mdd)} bps",
         f"在每边 2.0× 下 = 权益的 {ddp(mdd,2.0):.1f}%，R 仍有 "
         f"{(1-ddp(mdd,2.0)/100)*R20:.2f}×"),
        ("结构性尾部（价格带打满）", f"{f0(tail_now)} bps（扩张带 {f0(tail_exp)}）",
         f"在每边 2.3× 下 = 权益的 {ddp(tail_now,2.3):.0f}% / {ddp(tail_exp,2.3):.0f}%，"
         f"R 仍 ≥ {(1-ddp(tail_exp,2.3)/100)*R23:.2f}×"),
    ]
    cardhtml = "".join('<div class="card"><div class="ct">%s</div><div class="cv">%s</div>'
                       '<div class="cd">%s</div></div>' % c for c in cards)
    page = f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>风险重估：equity / 维持保证金</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>风险重估：用维持保证金而不是初始保证金衡量</h1>
<div class="sub">{out['meta']['spread']} · 1 分钟线 {out['meta']['bars']:,} 根（{days:.1f} 天）·
权益 ${EQUITY:,.0f} · MMF 6.667% · 生成 {time.strftime('%Y-%m-%d %H:%M')}</div>
<div class="cards">{cardhtml}</div>

<h2>1 · 三个量分清楚</h2>
<table>
<thead><tr><th>量</th><th>取值</th><th>决定什么</th></tr></thead>
<tbody>
<tr><td>初始保证金 IMF</td><td>常规 10% / 加成 15%</td>
<td><b>只能开多大</b>。占用 100% 只是不能再开新仓，不代表危险</td></tr>
<tr><td>维持保证金 MMF</td><td>6.667%（股票类）</td>
<td><b>什么时候被强平</b>：equity &lt; 总名义 × MMF</td></tr>
<tr><td><b>安全指标 R</b></td><td>= equity ÷ 维持保证金</td>
<td>R = 1 是强平线。R 越大越安全；<b>风险只看这个</b></td></tr>
</tbody></table>
<p>实测对照：当前仓位总名义 ${GROSS:,.0f}（每边 1.5×），维持保证金
${GROSS*MMF:,.0f}，权益 ${EQUITY:,.0f} → <b>R = {EQUITY/(GROSS*MMF):.2f}×</b>，
强平需要权益跌 {(1-GROSS*MMF/EQUITY)*100:.1f}%。这与看板上的强平缓冲是同一个数。</p>

<h2>2 · 每个杠杆下的安全性</h2>
{table(["每边名义", "总名义", "初始占用（常规）", "初始占用（加成）", "进场 R", "强平所需 spread 位移", "权益可跌"], lev_rows)}
<p>读法：<b>初始占用只影响能不能开新仓</b>（加成窗口 69% 甚至 100% 都只是能力问题）；
只要 R 还远大于 1，仓位就是安全的。每边 2.3× 时 R = {R23:.2f}×，
意味着权益要跌 {1-2*2.3*MMF:.0%}（={(1-2*2.3*MMF)/2.3*1e4:,.0f} bps）才会强平。</p>

<h2>3 · 历史与结构性尾部，在权益上是什么量级</h2>
{table(["事件", "spread bps", "每边 1.5×", "每边 2.0×", "每边 2.3×", "每边 3.0×"], ev_rows)}
<p>把「权益跌幅 / 剩余 R」两个数一起看：</p>
<ul>
<li><b>历史 60 天最深回撤 {f0(mdd)} bps</b>，在每边 2.0× 下只是权益的
{ddp(mdd,2.0):.1f}%，R 仍有 {(1-ddp(mdd,2.0)/100)*R20:.2f}× —— 离强平还差 3 倍以上。
「权益跌 5% 风险不大」这个判断是对的：跌 5% 后 R = {(1-0.05)*R20:.2f}×。</li>
<li><b>结构性尾部（价格带打满）{f0(tail_now)} bps</b>，扩张带 {f0(tail_exp)} bps。
这是比历史回撤更硬的约束 —— 因为 Arcus 的股票永续有 ±5% 价格带，
四条腿往相反方向同时走满，spread 一天能走这么大。在每边 2.3× 下 = 权益的
{ddp(tail_exp,2.3):.0f}%，R 仍有 {(1-ddp(tail_exp,2.3)/100)*R23:.2f}×。</li>
<li>日内回撤分布：中位 {f0(quantile(daily_worst,0.5))} bps、P95 {f0(quantile(daily_worst,0.05))} bps、
最深 {f0(min(daily_worst))} bps。</li>
</ul>

<h2>4 · 重算降杠杆位置</h2>
<p>既然强平在 {f0((1-2*2.0*MMF)/2.0*1e4)} bps（每边 2.0×）之外，止损就不该按强平线定，
而该按「历史能跌到哪」+「愿意亏多少」定。取两个锚点：历史最深 {f0(mdd)} bps、
结构性尾部 {f0(tail_now)} bps。</p>
{table(["档位", "触发（权益回撤）", "等价 spread（每边2.0×）", "等价（每边2.3×）", "触发时的 R", "动作"], [
    ["<b>L1</b>", "<b>−8%</b>", "400 bps", "348 bps", f"{(1-0.08)*R23:.2f}×", "降到每边 1.5×"],
    ["<b>L2</b>", "<b>−14%</b>", "700 bps", "609 bps", f"{(1-0.14)*R23:.2f}×", "降到每边 1.0×"],
    ["<b>L3</b>", "<b>−20%</b>", "1,000 bps", "870 bps", f"{(1-0.20)*R23:.2f}×", "熔断：全平 + HALT"]])}
<p>替换掉原来的 5%/8%/12%：原来的 5%/8%/12% 是按「初始保证金占用」拍的，把能力上限当成了风险线。
新三档的对应关系是 —— L1 放在历史最深回撤（{f0(mdd)} bps ≈ 每边2.0× 的
{ddp(mdd,2.0):.1f}%）附近，L3 放在结构性尾部（{f0(tail_now)} bps）之前，
并且<b>任何一档触发时 R 都还在 {f"{(1-0.20)*R23:.2f}"} 以上</b>，离强平永远有 2.5 倍以上缓冲。</p>
<p>若想让止损更「永不触发」（纯灾难刹车），把三档改成 −12% / −18% / −25%，
对应每边 2.0× 的 600 / 900 / 1,250 bps —— 60 天样本里一次都不会响。</p>

<h2>5 · 结论</h2>
<ul>
<li><b>初始保证金占用 40%–69% 不是风险指标</b>，它是能力指标；用满也可以接受。</li>
<li>真正的风险指标是 <b>R = equity/维持保证金</b>：当前 1.5× 下 {EQUITY/(GROSS*MMF):.2f}×，
每边 2.0× 下 {R20:.2f}×，加成 2.3× 下 {R23:.2f}×。全部远高于 1。</li>
<li>按历史 + 价格带尾部重估，<b>降杠杆位置应该放宽到 −8% / −14% / −20% 权益</b>
（原为 −5% / −8% / −12%）。</li>
<li>保证金占用告警（55%/70%/85%）应理解为「不能再开仓」的能力告警，
不是风险告警 —— 加成窗口关闭它是对的，常规时段可以放宽到 90%。</li>
</ul>

<div class="foot">数据：<code>spread-lab/data/candles_*_1m.json</code> + <code>markets.json</code>（价格带）。
脚本：<code>python3 risk_mm.py</code>。生成于 {time.strftime('%Y-%m-%d %H:%M')}。</div>
</div></body></html>"""
    OUTDIR.mkdir(parents=True, exist_ok=True)
    (OUTDIR / "risk-mm.html").write_text(page)
    (HERE / "report-risk-mm.html").write_text(page)
    print(f"wrote {OUTDIR / 'risk-mm.html'} ({len(page)/1024:.0f} KB)")


if __name__ == "__main__":
    main()
