#!/usr/bin/env python3
"""L2 排班表（每边 2×、加成窗口 2 倍）的保证金可行性、回撤风险、积分与止损设计。

口径：spread = long MU+SKHY+SNDK(DRAM 权重) / short DRAM，单位是「单腿 $1 名义」的 bps。
      每边名义 n × 权益 E，总名义 = 2nE。
      保证金占用 = 总名义 × IMF / E。
        IMF 常规 10%，加成窗口 15%（官方 off-hours 加 50%）。
      权益损失 % = n × spread 位移(bps) / 10000。

输出 plan_L2.json + plan-L2.html。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import math
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from report import table
import sessions as S
import leverage_windows as LW
from stats_lib import quantile, stdev

HERE = Path(__file__).resolve().parent
OUTDIR = Path(os.environ.get("LAB_OUT", HERE / "out"))
ET = ZoneInfo("America/New_York")
IMF_REG, IMF_OFF, MMF = 0.10, 0.15, 0.066667
EQUITY = float(os.environ.get("LAB_EQUITY", "10000"))   # 演示用权益；换成本地值
DD = {"L1": 5.0, "L2": 10.0, "L3": 22.0}          # 权益回撤告警线 %
EFF = (1000.0, 4000.0)                            # 分 / $M-week


def usage(n, imf):
    return 2 * n * imf


def main():
    prep = S.build("1m")
    ts = prep["ts"]
    wt = LW.wtags(ts)
    xs = S.series_of(prep)
    x = xs[S.PRIMARY]
    days = prep["days"]
    weeks = days / 7.0

    blocks = {cls: S.block(ts, x, wt, {cls}) for cls in ("REG", "BOOST_EDGE", "BOOST_CORE")}
    hours = {c: b["minutes"] / 60.0 / weeks for c, b in blocks.items()}
    runs = {c: [r["dd"] for r in b["_runs"]] for c, b in blocks.items()}
    for c in blocks:
        dds = runs[c]
        blocks[c]["dd_p50"] = quantile(dds, 0.5)
        blocks[c]["dd_loss95"] = quantile(dds, 0.05)
        blocks[c]["dd_loss99"] = quantile(dds, 0.01)

    # whole-sample path risk
    peak, mdd = x[0], 0.0
    for v in x:
        peak = max(peak, v)
        mdd = min(mdd, v - peak)
    roll = {h: min(x[i] - max(x[i - h:i + 1]) for i in range(h, len(x)))
            for h in (1440, 7200)}          # 1 天 / 5 天滚动

    # ---- margin feasibility
    margin_rows = []
    for n, k in ((1.5, 1.0), (2.0, 1.0), (2.0, 1.17), (2.0, 1.33), (2.0, 1.67), (2.0, 2.0)):
        m_reg, m_boost = usage(n, IMF_REG), usage(n * k, IMF_OFF)
        verdict = ("可执行" if m_boost < 0.70 else
                   "超过 70% 自设守卫" if m_boost < 1.0 else "❌ 保证金超过权益，开不出来")
        margin_rows.append([f"每边 {n:g}×", f"{2*n:g}×", f"×{k:g}", f"{2*n*k:g}×",
                            f"{m_reg*100:.0f}%", f"{m_boost*100:.0f}%", verdict])
    n_cap = 1.0 / (2.0 * IMF_OFF)          # 每边名义 / 权益 的硬上限（保证金打满）
    k_cap_100 = n_cap / 2.0
    k_cap_70 = 0.70 / (2 * 2.0 * IMF_OFF)

    # ---- drawdown risk in % of equity, by leverage
    dd_rows = []
    for cls, lab in (("REG", "REG 常规 (16h 场)"), ("BOOST_EDGE", "BOOST_EDGE 加成边缘 (2.4h)"),
                     ("BOOST_CORE", "BOOST_CORE 加成核心 (7.9h)")):
        b = blocks[cls]
        row = [lab, f"{b['worst_dd_bps']:,.0f}", f"{b['dd_loss95']:,.0f}", f"{b['dd_loss99']:,.0f}"]
        for n in (1.5, 2.0, 3.0, 4.0):
            row.append(f"{abs(b['worst_dd_bps'])*n/1e4*100:.1f}%")
        dd_rows.append(row)
    dd_rows.append(["<b>全样本峰值→谷值</b>", f"<b>{mdd:,.0f}</b>", f"5 天滚动 {roll[7200]:,.0f}",
                    f"1 天滚动 {roll[1440]:,.0f}", "", "", "", ""])

    # ---- points: OI-time per week by plan
    base_hours = hours["REG"] + hours["BOOST_EDGE"]
    plans = [
        ("现状 每边1.5× / 加成不加", 1.5, 1.0, "可执行"),
        ("每边2× / 加成不加", 2.0, 1.0, "可执行（占用 60%）"),
        ("每边2× / 加成 1.17×", 2.0, 1.17, "等于 70% 自设守卫"),
        ("每边2× / 加成 1.33×", 2.0, 1.33, "超过守卫，需人工放行"),
        ("候选方案里的 每边2× / 加成 2×", 2.0, 2.0, "❌ 保证金 120%，开不出来"),
    ]
    point_rows = []
    for lab, n, k, note in plans:
        n_hours = base_hours + k * hours["BOOST_CORE"]
        m_week = EQUITY * n * n_hours / 1.68e8          # $M-week
        lo, hi = m_week * EFF[0], m_week * EFF[1]
        point_rows.append([lab, f"{n*k:g}×" if k != 1 else f"{n:g}×",
                           f"{EQUITY*n*n_hours/1e6:,.2f}", f"{m_week:,.4f}",
                           f"{lo:,.1f} – {hi:,.1f}", note])

    # ---- stop-loss design: equity-first, converted to spread bps per leverage
    def fired(lvl, cls):
        return 100.0 * sum(1 for v in runs[cls] if abs(v) > lvl) / len(runs[cls])
    stop_rows = []
    for pct, lab in ((2.0, "−2% 降杠杆"), (4.0, "−4% 平加成仓"), (5.0, "−5% = L1"),
                     (8.0, "−8% 熔断"), (10.0, "−10% = L2")):
        row = [lab]
        for n in (1.5, 2.0, 3.0, 4.0):
            bps = pct / 100 * 1e4 / n
            row.append(f"{bps:,.0f}")
        bps2 = pct / 100 * 1e4 / 2.0
        row += [f"{fired(bps2, 'BOOST_CORE'):.0f}%", f"{fired(bps2, 'REG'):.0f}%"]
        stop_rows.append(row)
    # liquidation distance (spread bps) at each leverage, gross = 2n
    liq_rows = []
    for n in (1.5, 2.0, 3.0, 4.0):
        room = (1 - 2 * n * MMF) / n * 1e4
        liq_rows.append([f"每边 {n:g}×（总 {2*n:g}×）", f"{max(room, 0):,.0f}"])

    out = dict(meta=dict(from_=time.strftime("%Y-%m-%d", time.gmtime(ts[0])),
                         to=time.strftime("%Y-%m-%d", time.gmtime(ts[-1])), days=days,
                         spread=S.SPREADS[S.PRIMARY]["full"]),
               hours=hours, blocks={c: {k: v for k, v in b.items() if not k.startswith("_")}
                                    for c, b in blocks.items()},
               sample_max_dd=mdd, roll_1d=roll[1440], roll_5d=roll[7200],
               k_cap_100=k_cap_100, k_cap_70=k_cap_70, n_cap=n_cap)
    (HERE / "plan_L2.json").write_text(json.dumps(out, indent=1, default=str))

    print(f"weeks {weeks:.2f} · hours/wk: " + ", ".join(f"{c} {v:.1f}" for c, v in hours.items()))
    print(f"sample max DD {mdd:,.0f} bps · 1d roll {roll[1440]:,.0f} · 5d roll {roll[7200]:,.0f}")
    for c in blocks:
        b = blocks[c]
        print(f"  {c:11s} worstDD {b['worst_dd_bps']:8.1f} · p95 {b['dd_loss95']:7.1f} · "
              f"p99 {b['dd_loss99']:7.1f} · median {b['dd_median_bps']:7.1f}")
    print(f"margin: n_cap(100%) {n_cap:.2f}x per side · k_cap(100%) {k_cap_100:.2f} · k_cap(70%) {k_cap_70:.2f}")
    render(out, blocks, runs, hours, margin_rows, dd_rows, point_rows, stop_rows, liq_rows,
           weeks, n_cap, k_cap_100, k_cap_70, mdd, roll)
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


def render(out, blocks, runs, hours, margin_rows, dd_rows, point_rows, stop_rows, liq_rows,
           weeks, n_cap, k_cap_100, k_cap_70, mdd, roll):
    f0 = lambda v: f"{v:,.0f}"
    f1 = lambda v: f"{v:,.1f}"
    f2 = lambda v: f"{v:,.2f}"
    core, reg = blocks["BOOST_CORE"], blocks["REG"]
    cards = [
        ("候选方案里的 8× 开不出来", f"加成窗口保证金 {2*2*2*IMF_OFF*100:.0f}% 权益",
         f"每边 2× × 加成 2× = 总名义 8×；off-hours IMF 15% → 需要 "
         f"{2*2*2*IMF_OFF*100:.0f}% 权益做保证金"),
        ("每边 2× 下加成窗口的余量", f"最多 ×{f2(k_cap_100)}（打满）· ×{f2(k_cap_70)}（70% 守卫）",
         f"每边 2× 不加杠杆时加成窗口占用已经是 {2*2*IMF_OFF*100:.0f}% 权益"),
        ("加成核心单场最差回撤", f"−{f0(abs(core['worst_dd_bps']))} bps（P95 −{f0(abs(core['dd_loss95']))}）",
         f"在每边 2× 下 = 权益的 {abs(core['worst_dd_bps'])*2/1e4*100:.1f}%"),
        ("止损设计", "软 −100 / 硬 −200 / 熔断 −8% 权益",
         f"每边 2× 下分别是权益的 {100*2/1e4*100:.0f}% / {200*2/1e4*100:.0f}% / 8%；"
         f"强平距离 {f0((1-2*2*MMF)/2*1e4)} bps"),
    ]
    cardhtml = "".join('<div class="card"><div class="ct">%s</div><div class="cv">%s</div>'
                       '<div class="cd">%s</div></div>' % c for c in cards)

    page = f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>L2 排班表：保证金、回撤、积分与止损</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>每边 2× / 加成窗口 2× 的可行性、风险、积分与止损</h1>
<div class="sub">spread = {out['meta']['spread']} · 1 分钟线 {out['meta']['from_']} → {out['meta']['to']}
（{weeks:.1f} 周）· 权益 E = ${EQUITY:,.0f} · 单位：单腿 $1 名义的 bps ·
生成 {time.strftime('%Y-%m-%d %H:%M')}</div>

<div class="cards">{cardhtml}</div>

<h2>1 · 先算保证金：候选方案里的 8× 开不出来</h2>
<p>换算关系：<code>保证金占用 = 总名义 × IMF ÷ 权益</code>，总名义 = 2 × 每边名义。
IMF 常规 10%、加成窗口 15%（官方 off-hours 上调 50%）。</p>
{table(["每边名义", "常规总名义", "加成窗口倍数", "加成总名义", "常规占用", "加成占用", "判定"], margin_rows)}
<p><b>结论：每边 2× 的基准下，加成窗口即使一点都不加杠杆，保证金占用已经是
{2*2*IMF_OFF*100:.0f}% 权益</b>（常规时段只有 {2*2*IMF_REG*100:.0f}%）。
要顶到 70% 的自设守卫，加成窗口最多 ×{f2(k_cap_70)}；把保证金打满（100%）也只有
×{f2(k_cap_100)} —— <b>候选方案里的 ×2（总 8×）需要 120% 权益，物理上开不出来</b>。</p>
<p>顺带一个反直觉的推论：因为 off-hours 的保证金贵 50%，<b>同样是 1 单位保证金，
放在常规时段能买到 1.5 倍的 OI-time</b>。所以「用杠杆换积分」这件事，
除非官方加成倍数 &gt; 1.5×，否则在保证金效率上是不划算的 —— 加成窗口的优势来自
时间（可以挂更久）和官方乘数，不是来自杠杆。</p>

<h2>2 · 风险：按回撤比例算</h2>
<p>把每个窗口的「单场最差回撤」换算成权益百分比：<code>权益损失% = 每边倍数 × 回撤bps ÷ 10000</code>。</p>
{table(["窗口", "最差回撤", "P95 回撤", "P99 回撤", "每边1.5×", "每边2×", "每边3×", "每边4×"], dd_rows)}
<p>对照告警阶梯：L1 = 5%、L2 = 10%、L3 = 22% 权益回撤。</p>
<ul>
<li>每边 2× 时，<b>常规时段</b>一场 16 小时的最差回撤是权益的
{abs(reg['worst_dd_bps'])*2/1e4*100:.1f}% —— 已经越过 L1；加成核心窗口是
{abs(core['worst_dd_bps'])*2/1e4*100:.1f}%，还在 L1 之内。</li>
<li>每边 4×（= 候选方案里加成窗口的总名义）时，加成核心窗口最差回撤 = 权益的
{abs(core['worst_dd_bps'])*4/1e4*100:.1f}%，直接越过 L1 逼近 L2。</li>
<li>全样本峰值→谷值 {f0(mdd)} bps、5 天滚动最差 {f0(roll[7200])} bps —— 这是「永不止损」的代价，
在每边 2× 下分别是权益的 {abs(mdd)*2/1e4*100:.0f}% 和 {abs(roll[7200])*2/1e4*100:.0f}%。</li>
</ul>

<h2>3 · 积分：OI-time 与预估积分</h2>
<p>积分 ≈ OI-time × 效率（1,000–4,000 分/$M-week）。OI-time 按
<code>Σ 各窗口 每边名义 × 小时数</code> 算，权益 ${EQUITY:,.0f}。</p>
{table(["方案", "加成窗口倍数", "每周 OI-time ($M·h)", "$M-week", "预估积分/周", "备注"], point_rows)}
<p>关键读数：<b>候选方案里的方案（每边 2× + 加成 2×）能把 OI-time 从
{point_rows[1][2]} 抬到 {point_rows[4][2]} E·h（+{(float(point_rows[4][2].replace(',',''))/float(point_rows[1][2].replace(',',''))-1)*100:.0f}%），
但它开不出来</b>。在能执行的范围内（加成 ×1.17），OI-time 是 {point_rows[2][2]} E·h，
比「每边 2× 不加杠杆」多 {f1((float(point_rows[2][2].replace(',',''))/float(point_rows[1][2].replace(',',''))-1)*100)}%。
换句话说：<b>把基准从 1.5× 提到 2× 拿到的 OI-time（+{(float(point_rows[1][2].replace(',',''))/float(point_rows[0][2].replace(',',''))-1)*100:.0f}%）远大于加成窗口再加杠杆能拿到的（+{f1((float(point_rows[2][2].replace(',',''))/float(point_rows[1][2].replace(',',''))-1)*100)}%）。</b></p>

<h2>4 · 止损设计</h2>
<p>止损先按<b>权益百分比</b>定义（这是真正保护账户的量），再换算成 spread 位移 bps
（这是执行时能盯的量）。换算：<code>bps = 权益% ÷ 100 × 10000 ÷ 每边倍数</code>。</p>
{table(["止损档（权益）", "每边1.5×", "每边2×", "每边3×", "每边4×",
        "核心窗口历史触发率（每边2×）", "常规窗口历史触发率（每边2×）"], stop_rows)}
<p>历史触发率那一列很关键，它决定了止损该放哪一层：</p>
<ul>
<li><b>加成核心窗口</b>：回撤中位数只 −{f0(abs(core['dd_median_bps']))} bps、P95 −{f0(abs(core['dd_loss95']))} bps、
历史最差 −{f0(abs(core['worst_dd_bps']))} bps。所以 −2% 权益（每边2× 下 = 100 bps）
只有 {f0(fired_pct := 100.0*sum(1 for v in runs['BOOST_CORE'] if abs(v)>100)/len(runs['BOOST_CORE']))}% 的场次会触发，
放在这里刚好「贴着噪声之外」。</li>
<li><b>常规时段</b>：中位数 −{f0(abs(reg['dd_median_bps']))} bps、P95 −{f0(abs(reg['dd_loss95']))} bps，
同样 100 bps 的止损会打掉 {f0(100.0*sum(1 for v in runs['REG'] if abs(v)>100)/len(runs['REG']))}% 的场次 ——
<b>在常规时段用 −2% 软止损是错的，会被正常噪声反复打掉</b>。常规时段只保留 −4% 硬止损
（200 bps，触发率 {f0(100.0*sum(1 for v in runs['REG'] if abs(v)>200)/len(runs['REG']))}%）。</li>
<li><b>硬止损 −4% 权益</b>：每边 2× 下 = 200 bps，在核心窗口触发率
{f0(100.0*sum(1 for v in runs['BOOST_CORE'] if abs(v)>200)/len(runs['BOOST_CORE']))}%、
常规窗口 {f0(100.0*sum(1 for v in runs['REG'] if abs(v)>200)/len(runs['REG']))}%，两次都是真事件。</li>
</ul>
<h3>4.1 动作设计：先降杠杆，不是市价清仓</h3>
<table>
<thead><tr><th>触发</th><th>适用窗口</th><th>动作</th><th>成本考虑</th></tr></thead>
<tbody>
<tr><td>−2% 权益</td><td>只在加成窗口（有额外杠杆时）</td>
<td>把加成窗口倍数降回 1.0×，常规仓位不动</td>
<td>只卖最上面那部分，成交额最小</td></tr>
<tr><td>−4% 权益</td><td>全部窗口</td>
<td>平掉加成窗口的全部仓位，只留常规基准仓</td>
<td>两步走：先降倍数，再平剩余</td></tr>
<tr><td>−8% 权益</td><td>账户级</td>
<td>全部平仓 + 停机 + 撤单</td>
<td>在 L1 (5%) 与 L2 (10%) 之间，先于交易所强平</td></tr>
</tbody></table>
<p><b>不要用市价单止损</b>：两条腿在最薄的窗口（DRAM 只有 8–10% 的分钟有成交）吃单，
9 bps/轮的 taker 成本加上滑点会放大损失。用「撤单 + 分批 maker 减仓」，
并接受减仓可能要花十几分钟。这也是为什么止损要留出足够距离：
每边 2× 下强平需要 spread 走 {f0((1-2*2*MMF)/2*1e4)} bps，
硬止损 −200 bps 只用到强平距离的 {200/((1-2*2*MMF)/2*1e4)*100:.0f}%，留了
{f0((1-2*2*MMF)/2*1e4/200)} 倍缓冲。</p>
{table(["每边倍数", "强平前可承受的 spread 位移"], liq_rows)}

<h2>5 · 建议</h2>
<table>
<thead><tr><th>项目</th><th>建议</th><th>理由</th></tr></thead>
<tbody>
<tr><td>基准</td><td>每边 2×（总 4×），常规时段保持</td><td>常规占用 40%，加杠杆后仍有空间</td></tr>
<tr><td>加成窗口倍数</td><td><b>×1.0–1.17（不是 ×2）</b></td>
<td>每边 2× 下，加成窗口基准占用已 60%，70% 守卫只允许 ×{f2(k_cap_70)}</td></tr>
<tr><td>想上更高加成倍数</td><td>先把常规时段基准降到 1.5× 以下腾保证金</td>
<td>但常规时段保证金效率高 1.5 倍，只在官方加成倍数 &gt; 1.5× 时才划算</td></tr>
<tr><td>止损</td><td>软 −100 / 硬 −200 / 熔断 −8% 权益</td><td>见第 4 节</td></tr>
<tr><td>告警阈值</td><td>加成窗口的保证金告警放宽到 75%，回撤告警保持 5%/10%</td>
<td>否则每晚会触发自己的 L1</td></tr>
</tbody></table>

<div class="foot">数据：<code>spread-lab/data/candles_*_1m.json</code>；保证金/维持保证金来自
<code>data/markets.json</code>。脚本：<code>python3 plan_L2.py</code> → <code>plan_L2.json</code> + 本页。
生成于 {time.strftime('%Y-%m-%d %H:%M')}。</div>
</div></body></html>"""
    OUTDIR.mkdir(parents=True, exist_ok=True)
    (OUTDIR / "plan-L2.html").write_text(page)
    (HERE / "report-plan-L2.html").write_text(page)
    print(f"wrote {OUTDIR / 'plan-L2.html'} ({len(page)/1024:.0f} KB)")


if __name__ == "__main__":
    main()
