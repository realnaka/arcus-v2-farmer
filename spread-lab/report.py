#!/usr/bin/env python3
"""Render the DRAM / MU+SKHY spread study into one self-contained HTML page."""

from __future__ import annotations

import html
import json
import math
import time
from pathlib import Path

from stats_lib import ewma, mean, stdev
import grid2

HERE = Path(__file__).resolve().parent
OUT = HERE / "out" / "spread-dram-mu-skhy.html"


def _scale(v, v0, v1, p0, p1):
    if v1 == v0:
        return (p0 + p1) / 2
    return p0 + (v - v0) / (v1 - v0) * (p1 - p0)


def chart(series, *, width=1000, height=300, pad=(60, 20, 34, 62), xlab=None,
          ylab="", yfmt="{:,.0f}", bands=(), zero=True, legend=True,
          xticks=6, yticks=5, colors=None):
    left, right, top, bottom = pad
    colors = colors or ["#3b82f6", "#ef4444", "#10b981", "#f59e0b", "#8b5cf6", "#64748b"]
    n = max(len(s["ys"]) for s in series)
    ys_all = [v for s in series for v in s["ys"] if v is not None]
    y0, y1 = min(ys_all), max(ys_all)
    span = (y1 - y0) or 1.0
    y0, y1 = y0 - span * 0.08, y1 + span * 0.08
    for b in bands:
        y0, y1 = min(y0, b["lo"]), max(y1, b["hi"])
    px = lambda i: _scale(i, 0, n - 1, left, width - right)
    py = lambda v: _scale(v, y0, y1, height - bottom, top)
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">']
    for b in bands:
        y_hi, y_lo = py(b["hi"]), py(b["lo"])
        parts.append(f'<rect x="{left}" y="{y_hi:.1f}" width="{width-left-right}" '
                     f'height="{y_lo-y_hi:.1f}" fill="{b.get("fill","rgba(59,130,246,.07)")}"/>')
        if b.get("label"):
            parts.append(f'<text x="{width-right-6}" y="{y_hi+13:.1f}" class="bl" '
                         f'text-anchor="end">{html.escape(b["label"])}</text>')
    for k in range(yticks + 1):
        v = y0 + (y1 - y0) * k / yticks
        y = py(v)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{left-8}" y="{y+4:.1f}" class="ax" text-anchor="end">{yfmt.format(v)}</text>')
    if zero and y0 < 0 < y1:
        parts.append(f'<line x1="{left}" y1="{py(0):.1f}" x2="{width-right}" y2="{py(0):.1f}" class="zero"/>')
    if xlab:
        for k in range(xticks + 1):
            i = int((n - 1) * k / xticks)
            parts.append(f'<text x="{px(i):.1f}" y="{height-bottom+20:.1f}" class="ax" '
                         f'text-anchor="middle">{html.escape(xlab[min(i, len(xlab)-1)])}</text>')
    for j, s in enumerate(series):
        c = s.get("color", colors[j % len(colors)])
        d, pen = [], False
        for i, v in enumerate(s["ys"]):
            if v is None:
                pen = False
                continue
            d.append(("L" if pen else "M") + f"{px(i):.1f} {py(v):.1f}")
            pen = True
        dash = f' stroke-dasharray="{s["dash"]}"' if s.get("dash") else ""
        parts.append(f'<path d="{" ".join(d)}" fill="none" stroke="{c}" '
                     f'stroke-width="{s.get("width",2)}"{dash}/>')
    parts.append(f'<text x="14" y="{top+4}" class="ax">{html.escape(ylab)}</text>')
    if legend:
        x = left + 4
        for j, s in enumerate(series):
            c = s.get("color", colors[j % len(colors)])
            parts.append(f'<line x1="{x}" y1="{top+13}" x2="{x+18}" y2="{top+13}" stroke="{c}" stroke-width="3"'
                         + (f' stroke-dasharray="{s["dash"]}"' if s.get("dash") else "") + "/>")
            lab = s["name"]
            parts.append(f'<text x="{x+23}" y="{top+17}" class="lg">{html.escape(lab)}</text>')
            x += 26 + 7.4 * len(lab)
    parts.append("</svg>")
    return "\n".join(parts)


def table(headers, rows, cls=""):
    h = "".join(f"<th>{c}</th>" for c in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table class="{cls}"><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table>'


def bar_row(label, value, *, pct, color="#3b82f6", note=""):
    return (f'<div class="barrow"><span class="bl2">{html.escape(label)}</span>'
            f'<span class="track"><span class="fill" style="width:{pct:.1f}%;background:{color}"></span></span>'
            f'<span class="bv">{html.escape(value)}</span>'
            f'<span class="bn">{html.escape(note)}</span></div>')


def main():
    res = json.loads((HERE / "results.json").read_text())
    grid = json.loads((HERE / "grid_results.json").read_text())
    ex = json.loads((HERE / "extras.json").read_text())
    prep = grid2.prepare("1h")
    prep1 = grid2.prepare("1m")
    ts, x = prep["ts"], prep["x"]
    days = prep["days"]
    day1 = prep1["days"]

    xlab = [time.strftime("%m-%d", time.gmtime(t)) for t in ts]
    i1lab = [time.strftime("%m-%d", time.gmtime(t)) for t in prep1["ts"]]

    closes = {sym: grid2.load(sym, "1h") for sym in
              ("DRAM-USD", "MU-USD", "SKHY-USD", "SNDK-USD")}
    series = {sym: [(closes[sym][t][2] / closes[sym][ts[0]][2] - 1) * 100 for t in ts]
              for sym in closes}
    trend_chart = chart(
        [{"name": "DRAM (Roundhill Memory ETF)", "ys": series["DRAM-USD"], "color": "#0284c7"},
         {"name": "MU (Micron)", "ys": series["MU-USD"], "color": "#dc2626"},
         {"name": "SKHY (SK hynix)", "ys": series["SKHY-USD"], "color": "#059669"},
         {"name": "SNDK (SanDisk)", "ys": series["SNDK-USD"], "color": "#94a3b8"}],
        xlab=xlab, ylab="累计收益 %", yfmt="{:,.0f}%")

    a2 = ewma(x, 1 - math.exp(-math.log(2) / 48))
    a7 = ewma(x, 1 - math.exp(-math.log(2) / 168))
    dev7 = [x[i] - a7[i] for i in range(len(x))]
    dev2 = [x[i] - a2[i] for i in range(len(x))]
    sd7, sd2 = stdev(dev7), stdev(dev2)
    spread_chart = chart(
        [{"name": "spread 指数（bps，按单腿 $1 名义）", "ys": x, "color": "#0f172a", "width": 2.3},
         {"name": "2 天 EWMA 锚", "ys": a2, "color": "#f59e0b", "dash": "6 4"},
         {"name": "7 天 EWMA 锚", "ys": a7, "color": "#2563eb", "dash": "2 4"}],
        xlab=xlab, ylab="bps", yfmt="{:,.0f}")

    W = 168
    zs = [None] * len(x)
    for i in range(W - 1, len(x)):
        seg = x[i - W + 1:i + 1]
        zs[i] = (x[i] - mean(seg)) / stdev(seg)
    z_chart = chart(
        [{"name": "z =（spread − 7 天均值）/ 7 天 σ", "ys": zs, "color": "#7c3aed"}],
        xlab=xlab, ylab="σ", yfmt="{:,.1f}",
        bands=[{"lo": -1, "hi": 1, "label": "±1σ"}, {"lo": -2, "hi": 2, "fill": "rgba(239,68,68,.07)"}])

    rec = grid2.simulate(prep1, step=20, rungs=24, anchor=grid2.anchor_of(prep1, 2880),
                         cost_bps_round=2.0, mode="close")
    rec_opt = grid2.simulate(prep1, step=20, rungs=24, anchor=grid2.anchor_of(prep1, 2880),
                             cost_bps_round=2.0, mode="natural")
    eq, eq_opt = rec["equity_curve"], rec_opt["equity_curve"]
    gs = max(1, len(eq) // 900)
    grid_chart = chart(
        [{"name": "保守：只在 1 分钟收盘价成交", "ys": eq[::gs], "color": "#0f766e", "width": 2.3},
         {"name": "乐观上界：每根 K 线走满 高→低", "ys": eq_opt[::gs], "color": "#f97316", "dash": "6 4"}],
        xlab=i1lab[::gs], ylab="累计毛益 bps（单腿 $1 名义）", yfmt="{:,.0f}")

    # ---- tables
    lv = res["levels"]
    corr_h = res["corr_hourly"]
    # SNDK is in the window but not in analyze.py's core trio: fill in its stats
    _lr = {}
    for sym in ("DRAM-USD", "MU-USD", "SKHY-USD", "SNDK-USD"):
        p = [closes[sym][t][2] for t in ts]
        _lr[sym] = [math.log(p[i] / p[i - 1]) for i in range(1, len(p))]
    corr_h["DRAM-USD~SNDK-USD"] = (
        sum((a - mean(_lr["DRAM-USD"])) * (b - mean(_lr["SNDK-USD"]))
            for a, b in zip(_lr["DRAM-USD"], _lr["SNDK-USD"]))
        / math.sqrt(sum((a - mean(_lr["DRAM-USD"])) ** 2 for a in _lr["DRAM-USD"])
                    * sum((b - mean(_lr["SNDK-USD"])) ** 2 for b in _lr["SNDK-USD"])))
    res["vol_hourly_bps"]["SNDK-USD"] = stdev(_lr["SNDK-USD"]) * 1e4
    trend_rows = []
    for sym, col in (("MU-USD", "MU"), ("SKHY-USD", "SKHY"), ("SNDK-USD", "SNDK")):
        c = corr_h.get(f"DRAM-USD~{sym}") or corr_h.get(f"{sym}~DRAM-USD")
        trend_rows.append([sym.replace("-USD", ""), f"{lv[sym]['cum_return_pct']:+.1f}%",
                           f"{res['vol_hourly_bps'][sym]:.0f} bps", f"{c:.2f}",
                           lv[sym]["own_history_from"][:10]])
    trend_rows.insert(0, ["DRAM", f"{lv['DRAM-USD']['cum_return_pct']:+.1f}%",
                          f"{res['vol_hourly_bps']['DRAM-USD']:.0f} bps", "1.00",
                          lv["DRAM-USD"]["own_history_from"][:10]])

    sp_rows = []
    for key, s in res["spreads"].items():
        sp_rows.append([
            {"equal_dollar": "等美元 50/50", "beta_weighted": "回归权重 0.71/0.29",
             "dram_vs_mu": "DRAM vs MU", "dram_vs_skhy": "DRAM vs SKHY",
             "dram_vs_sndk": "DRAM vs SNDK"}[key],
            f"{s['bar_ret_std_bps']:.0f}",
            f"{s['range_bps']:,.0f}",
            f"{s['adf']['t_stat']:.2f}",
            f"{s['half_life']['half_life_bars']:.0f}",
            f"{s['hurst']:.2f}",
            f"{s['z_occupancy']['pct_within_2']:.0f}%",
            f"{s['z_occupancy']['zero_crossings_per_day']:.2f}",
        ])

    pr = res["spreads"]["beta_weighted"]["predictive"]
    mr_rows = [
        ["重叠窗口 · 全样本", "24 h", f"{pr['24bar']['slope_bps_per_sigma']:.1f}",
         f"{pr['24bar']['t_stat']:.1f}", f"{pr['24bar']['r2']:.3f}", f"{pr['24bar']['n']}"],
        ["重叠窗口 · 全样本", "72 h", f"{pr['72bar']['slope_bps_per_sigma']:.1f}",
         f"{pr['72bar']['t_stat']:.1f}", f"{pr['72bar']['r2']:.3f}", f"{pr['72bar']['n']}"],
        ["不重叠 · 样本内 60%", "24 h", "-22.1", "-1.5", "0.052", "44"],
        ["不重叠 · 样本内 60%", "72 h", "-43.1", "-1.0", "0.075", "14"],
        ["不重叠 · 样本外 40%", "24 h", "-38.2", "-3.3", "0.255", "34"],
        ["不重叠 · 样本外 40%", "72 h", "-64.0", "-2.7", "0.445", "11"],
    ]

    b1 = grid["1m"]
    sim_rows = []
    for key in ("hl2880_step10_rungs24", "hl2880_step20_rungs12",
                "hl2880_step30_rungs12", "hl2880_step50_rungs12",
                "hl10080_step10_rungs24", "hl10080_step20_rungs24",
                "static_step20_rungs12", "static_step20_rungs24"):
        co = b1["sims"][key]["close_only"]
        lab = key.replace("_", " ").replace("hl2880", "锚 2 天").replace(
            "hl10080", "锚 7 天").replace("static", "不重置")
        sim_rows.append([lab, f"{co['rounds_per_day']:.1f}",
                         f"{co['gross_bps_per_day']:.0f}", f"{co['worst_open_mtm_bps']:,.0f}",
                         f"{co['max_open_units']:.0f}", f"{co['final_equity_bps']:,.0f}",
                         f"{co['open_units_at_end']:.0f}"])

    mk = ex["markets"]
    tick_rows = []
    for sym in ("DRAM-USD", "MU-USD", "SKHY-USD"):
        m = mk[sym]
        tick_rows.append([sym.replace("-USD", ""), f"${m['mark']:,.2f}", f"${m['tickSize']:.2f}",
                          f"{m['tick_bps']:.2f}", f"${m['volume24h_notional']/1000:,.0f}k",
                          f"{m['trades24h']:,}", f"${m['oi_notional']/1000:,.0f}k"])
    cap_rows = []
    for c in ex["capacity"]:
        if c["unit_usd_per_leg"] in (100, 250, 500) and c["rounds_per_day"] in (4, 8, 16):
            cap_rows.append([f"${c['unit_usd_per_leg']}", f"{c['rounds_per_day']}",
                             f"${c['turnover_usd_per_day']:,}",
                             f"{c['share_of_DRAM_volume']:.2f}%",
                             f"{c['share_of_SKHY_volume']:.2f}%",
                             f"${c['margin_at_10pct_usd']:,.0f}"])

    tl = [
        ("走势差异", f"87 天里 MU {lv['MU-USD']['cum_return_pct']:+.1f}%、SKHY "
                     f"{lv['SKHY-USD']['cum_return_pct']:+.1f}%，DRAM 只有 "
                     f"{lv['DRAM-USD']['cum_return_pct']:+.1f}%",
         "所以 long DRAM / short MU+SKHY 这条 spread 天生带负漂移 ≈ −10 bps/天。"),
        ("是不是箱体", "对固定中枢：不是。对移动锚：是。",
         "ADF t=−2.2（5% 临界 −2.86，不能拒绝单位根）；但对 2–7 天 EWMA 锚的偏离 "
         "Hurst 0.32–0.36、半衰期 48–64 h、|z|>1 回到 |z|<0.25 中位 0.7 天。"),
        ("网格可行吗", "可以，但必须锚在移动均值上、全 maker、库存上限写死。",
         "保守成交口径（1 分钟收盘价）下 1m 数据 20 bps 档距 ≈ 8 轮/天、毛 167 bps/天；"
         "同期最大浮亏 −4,400 bps，这才是真正的风险项。"),
        ("最大的坑", "对冲比例不稳定 + 样本外单位根不拒绝。",
         "7 天滚动 β_MU 在 0.31–0.82 之间漂；样本外（冻结 β）spread 的 ADF t=−1.06、"
         "Hurst 0.49 —— 一旦停止重置锚，网格就是往趋势里加仓。"),
    ]
    cards = "".join(
        f'<div class="card"><div class="ct">{html.escape(t)}</div>'
        f'<div class="cv">{html.escape(v)}</div><div class="cd">{html.escape(d)}</div></div>'
        for t, v, d in tl)

    page = f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DRAM / MU+SKHY 价差研究</title>
<style>
:root{{--ink:#0f172a;--mut:#64748b;--line:#e2e8f0;--bg:#f8fafc}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",Arial,sans-serif}}
.wrap{{max-width:1120px;margin:0 auto;padding:36px 24px 80px}}
h1{{font-size:27px;margin:0 0 6px}}
h2{{font-size:20px;margin:44px 0 6px;padding-bottom:8px;border-bottom:2px solid var(--line)}}
h3{{font-size:16px;margin:26px 0 8px}}
.sub{{color:var(--mut);font-size:13.5px;margin-bottom:26px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(238px,1fr));gap:14px;margin:22px 0 8px}}
.card{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:15px 16px}}
.ct{{font-size:12px;letter-spacing:.08em;color:var(--mut);text-transform:uppercase}}
.cv{{font-size:17px;font-weight:650;margin:6px 0 6px;line-height:1.35}}
.cd{{font-size:13px;color:#475569}}
.panel{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:14px 0}}
.chart{{width:100%;height:auto;display:block}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);
border-radius:10px;overflow:hidden;font-size:13.5px;margin:12px 0}}
th{{text-align:left;padding:9px 11px;background:#f1f5f9;font-weight:600;color:#334155;
font-size:12.5px;white-space:nowrap}}
td{{padding:8px 11px;border-top:1px solid var(--line);white-space:nowrap}}
tr:hover td{{background:#fafcff}}
.grid{{stroke:#eef2f7;stroke-width:1}}
.zero{{stroke:#94a3b8;stroke-width:1.2;stroke-dasharray:4 4}}
.ax{{fill:#94a3b8;font-size:11px}}
.lg{{fill:#475569;font-size:11.5px}}
.bl{{fill:#94a3b8;font-size:10.5px}}
.bx{{fill:#475569;font-size:11px}}
code{{background:#f1f5f9;padding:1px 5px;border-radius:4px;font-size:13px}}
.k{{color:#0f766e;font-weight:600}}
.b{{color:#b91c1c;font-weight:600}}
ul{{padding-left:20px}}li{{margin:4px 0}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
@media(max-width:760px){{.two{{grid-template-columns:1fr}}}}
.foot{{color:var(--mut);font-size:12.5px;margin-top:36px;border-top:1px solid var(--line);padding-top:14px}}
</style></head><body><div class="wrap">
<h1>DRAM × MU × SKHY：价差交易与网格可行性</h1>
<div class="sub">数据：Arcus 公开 <code>/v1/candles</code> · 小时线 {len(ts):,} 根
（{res['meta']['from'][:10]} → {res['meta']['to'][:10]}，{days:.1f} 天）·
分钟线 {len(prep1['ts']):,} 根（{time.strftime('%Y-%m-%d', time.gmtime(prep1['ts'][0]))} →
{time.strftime('%Y-%m-%d', time.gmtime(prep1['ts'][-1]))}，{day1:.1f} 天）·
生成 {time.strftime('%Y-%m-%d %H:%M')}</div>

<div class="cards">{cards}</div>

<h2>1 · 三条腿的走势差异</h2>
<div class="panel">{trend_chart}</div>
{table(["标的", "87 天累计", "小时波动率 1σ", "与 DRAM 相关", "本平台上市"], trend_rows)}
<p>DRAM 是 <b>Roundhill Memory ETF</b>（一篮子），MU / SKHY 是成分股。窗口内两条腿
MU、SKHY 几乎同步（+15.2% / +14.1%），而 ETF 只涨 2.2% —— 差距主要来自篮子里的其他
成分（SNDK 同期 −4.1%）和 ETF 自身跟踪/申赎价差。这意味着 <b>long DRAM / short MU+SKHY
并不是一个中性组合</b>，它等于在做多桶里剩下的部分。</p>
<p><b>区间背景（很重要）</b>：这 87 天不是一段平静行情。7 月中到 7 月底存储板块整体暴跌
（DRAM −25%、MU −24%、SKHY −24%、SNDK −44%），随后 8–9 月又强势反弹。两条腿在暴跌段
跌幅接近，但 ETF 的反弹幅度明显更小 —— spread 的「漂移」大部分产生于这段 regime 切换。
换句话说，样本里只有一次大级别的下跌 + 一次反弹，<b>「区间」的统计证据只来自这一个周期</b>。</p>

<h2>2 · spread 的定义与统计特征</h2>
<p>spread 在 log 空间构造：<code>s = log(DRAM) − w_MU·log(MU) − w_SKHY·log(SKHY)</code>。
单位是「单腿 $1 名义」的 bps，也就是每 $1 多头的相对盈亏。权重用小时收益回归
β<sub>MU</sub>={res['ret_regression_hourly']['beta_MU']:.2f}、
β<sub>SKHY</sub>={res['ret_regression_hourly']['beta_SKHY']:.2f}（R²={res['ret_regression_hourly']['r2']:.2f}），
归一成美元中性（0.71 / 0.29）。</p>
<div class="panel">{spread_chart}</div>
{table(["构造", "小时波动 σ", "1–99% 带宽", "ADF t", "半衰期 h", "Hurst", "|z|&lt;2 占比", "穿越 0 次/天"], sp_rows)}
<p>读法：<span class="b">ADF 不能拒绝单位根</span> —— 这个 spread 的<b>水平</b>没有固定中枢，
它随锚一起漂（87 天 −810 bps）。但 <b>Hurst &lt; 0.5、方差比随期数递减</b>说明
「相对自身移动均值的偏离」是均值回归的。可交易的正是后者。</p>

<h2>3 · 均值回归到底有多强</h2>
<div class="panel">{z_chart}</div>
{table(["样本", "预测窗口", "斜率 bps / 1σ", "t", "R²", "n"], mr_rows)}
<p>把 <b>未来 h 小时收益</b> 对 <b>当前 z</b> 回归：斜率显著为负 = 偏离会被收回。
重叠窗口（每小时采样）会高估显著性，所以同时给出<b>不重叠</b>的版本：样本外 24 h
斜率 −38 bps/σ（t=−3.3）、72 h −64 bps/σ（t=−2.7）。样本内不重叠只有 t=−1.5 / −1.0，
样本量本来就小（n=44 / 14），说明这个信号<b>方向可信、幅度不可信</b>。</p>
{table(["统计量", "值", "含义"],
       [["|z|&gt;1 → |z|&lt;0.25", "中位 0.7 天 / 均值 1.7 天 / P90 5.2 天", "偏离的典型修复时间"],
        ["|z| &lt; 1 的时间占比", f"{res['spreads']['beta_weighted']['z_occupancy']['pct_within_1']:.0f}%", "大部分时间在窄带内"],
        ["实测最大 |z|", f"{res['spreads']['beta_weighted']['z_occupancy']['max_abs_z']:.1f}σ", "尾部会到 4σ 以上"]])}

<h2>4 · 在 spread 上跑网格</h2>
<p>规则：以 EWMA 锚为中心挂 ±k×step 的梯子，买单只在价格<b>向下穿过</b>档位时成交，
成交后高一个 step 止盈；空侧对称。一轮 = 开仓 + 平仓，各腿合计 4 笔腿成交，
<b>maker 0 费率</b>下成本约 4×0.5 bps（滑点），taker 口径则是 4×2.25 = 9 bps/轮。</p>
<div class="panel">{grid_chart}</div>
{table(["档距 / 锚", "轮/天", "毛 bps/天", "最差浮亏 bps", "最大持仓档", "期末毛益 bps", "期末未平档"], sim_rows)}
<p>同一份数据两种成交口径差 30 倍，这正是 1 分钟 K 线藏掉路径的代价：</p>
{table(["口径", "轮/天", "毛 bps/天", "说明"],
       [["只按收盘价（保守）", f"{rec['rounds_per_day']:.1f}", f"{rec['gross_bps_per_day']:.0f}",
         "只有跨分钟的真实价格位移才计一轮"],
        ["每根 K 线走满 高→低（乐观）", f"{rec_opt['rounds_per_day']:.1f}",
         f"{rec_opt['gross_bps_per_day']:,.0f}", "等于假设网格吃到整根 K 线的振幅，不可能"],
        ["路径长度上限（TV/2）", "—", f"{ex['total_variation']['1m']['grid_ceiling_bps_per_day']:,.0f}",
         "1m 路径总变差的 1/2，任何网格都不可能超过"]])}
<p>结论：<b>乐观口径本身就超过理论上限</b>（↑ 表最后一行），所以只能拿保守口径当基准，
再用实盘 fill 校准（参考 arcus-farmer 对 SOL/CRCL 的做法）。</p>

<h2>5 · 成本、容量与执行约束</h2>
<div class="two"><div>
{table(["标的", "价格", "tick", "tick =", "24h 名义", "24h 笔数", "未平仓"], tick_rows)}
<p>DRAM 的 tick 是 <b>1.68 bps</b>（$0.01 / $59.58），所以 spread 的档距不可能小于
~2 bps；10 bps 档距 ≈ DRAM 走 6 个 tick。MU、SKHY 的 tick 只有 0.09 / 0.56 bps，
量化粒度完全由 ETF 腿决定。</p>
</div><div>
{table(["费率", "maker", "taker", "一轮往返成本"], [
    ["Arcus Base", "0 ppm", "225 ppm", "taker 9 bps / maker ~2 bps"],
    ["Beta 结束后", "150 ppm", "450 ppm", "maker 6 bps / taker 18 bps"]])}
<p>funding：所有 RWA 永续同向收 <b>≈4.5% 年化（0.00051%/小时）</b>。美元中性时两腿
对消，但腿的比例漂了就会漏出净 funding。<br>
DRAM 只有 <b>10% 的分钟有成交</b>（SKHY 8%、MU 17%），所以「报价走到档位」≠
「有人在这个价位成交」，这是保守口径之外还要再打折的原因。</p>
</div></div>
{table(["单腿规模", "轮/天", "日换手（DRAM 腿）", "占 DRAM 24h 量", "占 SKHY 24h 量", "10% 保证金"], cap_rows)}
<p>容量是硬约束：单腿 $250、8 轮/天 就已经是 DRAM 日成交额的 0.49%。
按 arcus-farmer 的 0.5% 诚实线，<b>单腿 $250 左右就是这条策略在这个场所的上限量级</b>。</p>

<h2>6 · 落地建议</h2>
{table(["参数", "建议", "理由"], [
    ["标的组合", "DRAM 多头 $1 : MU 空头 $0.71 : SKHY 空头 $0.29", "美元中性 + 回归 β；每周重估一次 β"],
    ["锚 / 重置", "7 天 EWMA 锚，每日 0 点（UTC）重挂一次梯子", "锚太短会追价、太长跟不上漂移"],
    ["档距", "20 bps（10–30 bps 区间可调）", "≥ DRAM tick 的 6 倍；maker 成本下才有正期望"],
    ["档数 / 库存", "上下各 12 档，硬上限 12–16 档（单腿 ≤ $250 × 档数）", "最差浮亏出现在满仓时，必须写死"],
    ["成交方式", "两条腿都 post-only；禁止 taker 平仓", "taker 9 bps/轮 直接吃掉 20 bps 档距的一半"],
    ["风控", "spread 相对锚偏离 &gt; 4σ 停止加档；β 漂移 &gt; 20% 重新对冲", "样本外单位根不拒绝，趋势是会发生的"],
    ["容量", "单腿 ≤ $250，日轮次 ≤ 8–16", "占 DRAM 24h 成交 ≤ 0.5%"]])}
<h3>上线前要补的三件事</h3>
<ul>
<li><b>用盘口验证成交</b>：现在的成交模型是 K 线路径推的，必须用 WebSocket l2 采样
DRAM 腿的真实买卖价差和挂单深度，确认 20 bps 档位有被动成交可能。</li>
<li><b>换更长的样本</b>：MU 有 2021 年以来数据，但 SKHY 只上市 87 天、DRAM 只上市
189 天。建议补 Samsung（如有其他场所报价）、并用 <code>MU vs SNDK</code> 这类老腿做
同结构的历史检验，看均值回归是否只是这段行情的特征。</li>
<li><b>小规模 live 校准</b>：照 arcus-farmer 的方式跑 1–2 周 dry-run + 小额实盘，
把 fill 率、实际 pick-up、滑点回填进模型，再决定是否放量。</li>
</ul>
<div class="foot">数据源：Arcus 公开行情接口（candles / markets / feeTiers）。所有统计量为
纯 Python 实现（ADF 采用 MacKinnon 渐近临界值 −3.43 / −2.86 / −2.57）。
本页由 <code>spread-lab/report.py</code> 生成，可重复运行。<br>
反方向（long MU+SNDK+SKHY / short DRAM，即做空 ETF 里的非 Arcus 成分）见
<a href="dram-basket-short.html">dram-basket-short.html</a>。</div>
</div></body></html>"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page)
    print(f"wrote {OUT} ({len(page)/1024:.0f} KB)")
    (HERE / "report.html").write_text(page)


if __name__ == "__main__":
    main()
