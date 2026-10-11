#!/usr/bin/env python3
"""Second report: what long MU+SNDK+SKHY / short DRAM actually bets on."""

from __future__ import annotations

import html
import json
import math
import time
from pathlib import Path

from report import chart, table
import grid2

HERE = Path(__file__).resolve().parent
OUT = HERE / "out" / "dram-basket-short.html"


def stacked(rows, *, width=1000, height=150, pad=(20, 20, 46, 60)):
    """rows: list of dicts {name, pct, color}"""
    left, right, top, bottom = pad
    total = sum(r["pct"] for r in rows)
    x = left
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart">']
    inner = width - left - right
    for r in rows:
        w = inner * r["pct"] / total
        parts.append(f'<rect x="{x:.1f}" y="{top}" width="{w:.1f}" height="46" fill="{r["color"]}" rx="2"/>')
        if r["pct"] > 2.6:
            parts.append(f'<text x="{x + w/2:.1f}" y="{top+28}" class="lg" text-anchor="middle" '
                         f'fill="#fff">{r["pct"]:.1f}%</text>')
        parts.append(f'<text x="{x + w/2:.1f}" y="{top+62}" class="ax" text-anchor="middle">{html.escape(r["short"])}</text>')
        if r.get("full"):
            parts.append(f'<text x="{x + w/2:.1f}" y="{top+78}" class="ax" text-anchor="middle" '
                         f'fill="#0f172a">{html.escape(r["full"])}</text>')
        x += w
    parts.append("</svg>")
    return "\n".join(parts)


def main():
    b = json.loads((HERE / "basket_results.json").read_text())
    pts = json.loads((HERE / "points_stats.json").read_text())
    H = b["holdings"]
    prep = grid2.prepare_family("1h", ["DRAM-USD", "MU-USD", "SKHY-USD", "SNDK-USD"])
    xlab = [time.strftime("%m-%d", time.gmtime(t)) for t in prep["ts"]]

    # ---- holdings split
    trad, untrad = [], []
    for h in H["top10"]:
        (trad if h["arcus"] else untrad).append(h)
    holds_rows = []
    for h in H["top10"]:
        holds_rows.append([
            h["name"] + ("" if h["arcus"] else " <span class='b'>·</span>"),
            h["ticker"], f"{h['weight_pct']:.2f}%",
            "✅ " + h["arcus"] if h["arcus"] else "❌ 不可交易",
        ])
    holds_rows.append(["其余 12 只（合计）", "—", f"{H['others_est_pct']:.2f}%", "❌ 不可交易"])
    bar_rows = [{"short": "MU", "full": "Micron", "pct": 27.36, "color": "#0284c7"},
                {"short": "SKHY", "full": "SK hynix", "pct": 22.13, "color": "#0ea5e9"},
                {"short": "SNDK", "full": "SanDisk", "pct": 4.65, "color": "#7dd3fc"},
                {"short": "Samsung", "full": "Electronics", "pct": 25.30, "color": "#b91c1c"},
                {"short": "CXMT", "full": "TRS 5.27%", "pct": 5.27, "color": "#dc2626"},
                {"short": "STX", "full": "Seagate", "pct": 4.22, "color": "#ef4444"},
                {"short": "WDC", "full": "West.Digital", "pct": 3.28, "color": "#f87171"},
                {"short": "Kioxia", "full": "285A JP", "pct": 3.19, "color": "#fca5a5"},
                {"short": "Nanya", "full": "2408 TT", "pct": 2.45, "color": "#fecaca"},
                {"short": "Winbond", "full": "2344 TT", "pct": 1.14, "color": "#fee2e2"},
                {"short": "其他", "full": "", "pct": 1.01, "color": "#f1f5f9"}]
    hold_chart = stacked(bar_rows)

    # ---- net exposure table (per $1 short DRAM, unnormalised)
    net_rows = []
    for h in H["top10"]:
        w = h["weight_pct"] / 100
        net = -w if not h["arcus"] else 0.0
        net_rows.append([h["name"], f"+{h['weight_pct']:.2f}%" if h["arcus"] else "—",
                         f"{net*100:+.2f}%"])
    net_rows.append(["<b>合计净敞口</b>", f"+{b['tradeable_sum']*100:.2f}%",
                     f"−{(1-b['tradeable_sum'])*100:.2f}%"])

    # ---- P&L chart
    keys = [("etf_weights", "ETF 权重不归一（净空 45.9%）", "#b91c1c"),
            ("etf_weights_norm", "ETF 权重归一（多 $1 / 空 $1）", "#0f766e"),
            ("equal_three", "三条腿等权", "#f59e0b"),
            ("mv_weights", "最小方差权重（回归）", "#7c3aed")]
    pnl = chart([{"name": lab, "ys": b["spreads"][k]["curve"], "color": c,
                  "width": 2.2 if k == "etf_weights_norm" else 1.7}
                 for k, lab, c in keys],
                xlab=xlab, ylab="累计 bps（对 $1 DRAM 空头名义）", yfmt="{:,.0f}")

    # ---- implied basket path
    bk = b["untradeable_basket"]
    basket_ts = [time.strftime("%m-%d", time.gmtime(t)) for t in prep["ts"][1:]]
    basket_chart = chart([{"name": "隐含「非 Arcus 篮子」累计走势（等权缩放后 %）",
                           "ys": bk["curve"], "color": "#b91c1c", "width": 2.2}],
                         xlab=basket_ts[::max(1, len(basket_ts) // 500)],
                         ylab="%", yfmt="{:,.0f}%")

    st = b["spreads"]
    stat_rows = [[lab, f"{st[k]['cum_bps']:+,.0f}", f"{st[k]['drift_bps_per_day']:+.1f}",
                  f"{st[k]['bar_std_bps']:.1f}", f"{st[k]['ann_vol_pct']:.1f}%",
                  f"{st[k]['sharpe']:.2f}", f"{st[k]['max_dd_bps']:+,.0f}",
                  f"{st[k]['adf_t']:.2f}", f"{st[k]['half_life_h']:.0f}h"]
                 for k, lab, _ in keys]
    sub_rows = [[f"{s['from']}→{s['to']}", f"{s['dram_pct']:+.1f}%", f"{s['mu_pct']:+.1f}%",
                 f"{s['skhy_pct']:+.1f}%", f"{s['sndk_pct']:+.1f}%",
                 f"{s['basket_pct']:+.1f}%"] for s in bk["sub"]]
    grid_rows = []
    for k, v in b["grid"]["1m"].items():
        c = v["close"]
        step = k.split("_")[0].replace("step", "")
        rungs = k.split("_")[1].replace("rungs", "")
        grid_rows.append([f"{step} bps × {rungs} 档", f"{c['rounds_per_day']:.1f}",
                          f"{c['gross_bps_per_day']:.0f}", f"{c['net_bps_per_day']:.0f}",
                          f"{c['worst_open_mtm_bps']:,.0f}", f"{c['max_open_units']:.0f}",
                          f"{c['final_equity_bps']:,.0f}"])

    # ---- statistics block
    st1 = pts["stats_1h"]
    corr = pts["corr_with_legs"]
    stat2 = [[
        "累计 / 漂移", f"{st1['cum_bps']:+,.0f} bps / {st1['drift_bps_per_day']:+.1f} bps·天",
        f"日均收益的 t = {st1['t_stat_of_mean']:.2f}（不显著）"],
        ["波动", f"{st1['hour_std_bps']:.1f} bps/小时 · {st1['daily_std_bps']:.0f} bps/天",
         f"年化 {st1['ann_vol_pct']:.1f}% · Sharpe {st1['sharpe']:.2f}（样本内）"],
        ["分布形状", f"偏度 {st1['skew']:+.2f} · 超额峰度 {st1['excess_kurtosis']:.1f}",
         "极端跳变很常见，正态假设不成立"],
        ["平稳性", f"ADF t = {st1['adf_full']['t_stat']:.2f}（5% 临界 −2.86）",
         "不能拒绝单位根 → 水平不回到固定中枢"],
        ["长记忆", f"Hurst {st1['hurst']:.2f} · 半衰期 {st1['half_life_h']:.0f} 小时",
         "低于 0.5 = 有均值回归成分，但只在多日尺度"],
        ["方差比", "2h 0.91 / 8h 0.66 / 24h 0.41 / 72h 0.35 / 168h 0.14",
         "比值随期数单调下降 = 多日尺度上明显回补"],
        ["自相关", f"1h {st1['autocorr']['1h']:+.3f} · 2h {st1['autocorr']['2h']:+.3f} · 24h {st1['autocorr']['24h']:+.3f}",
         "小时级负自相关（−0.10）是唯一可交易的短周期信号"],
        ["市场中性", f"与 SPY {corr.get('SPY-USD', 0):+.2f} · QQQ {corr.get('QQQ-USD', 0):+.2f}",
         "与大盘几乎无关，适合当 OI 蓄水池"],
        ["腿的相关", f"MU {corr['MU-USD']:+.2f} · SKHY {corr['SKHY-USD']:+.2f} · SNDK {corr['SNDK-USD']:+.2f} · DRAM {corr['DRAM-USD']:+.2f}",
         "净多可交易组、净空其余，正是设计意图"],
        ["最大回撤", f"{st1['max_dd_bps']:,.0f} bps（区间 {st1['min_bps']:,.0f} ~ {st1['max_bps']:,.0f}）",
         "回撤 ≈ 累计收益的 85%，不能只看收益"],
    ]
    sub2_rows = [[s["from"] + "→" + s["to"], f"{s['ret_bps']:+,.0f}", f"{s['std_bps']:.0f}",
                  f"{s['ann_vol_pct']:.0f}%"] for s in pts["sub_periods"]]

    # ---- points block
    P = pts["points"]
    hold = P["hold"]
    rows_pts = [["<b>直接持有 24/7</b>", f"${2*P['S_side_usd']:,.0f}", "100%",
                 f"{hold['points_oi_low']:.1f} – {hold['points_oi_high']:.1f}",
                 "—", f"{hold['income_usd_per_day']:.2f}",
                 f"−${-pts['stats_1h']['max_dd_bps']/1e4*P['S_side_usd']:,.0f}"]]
    for name, label in (("step10", "网格 · 10 bps × 12 档"), ("step20", "网格 · 20 bps × 12 档"),
                        ("step30", "网格 · 30 bps × 12 档"), ("step50", "网格 · 50 bps × 12 档")):
        g = P["grids"][name]
        n_rung = P["S_side_usd"] / (2 * P["rungs"])
        rows_pts.append([label, f"${2*g['avg_deployed_side_usd']:,.0f}",
                         f"{g['deployed_pct_of_ladder']:.0f}%",
                         f"{g['points_oi_low']:.2f} – {g['points_oi_high']:.2f}",
                         f"{g['points_volume_low']:.2f} – {g['points_volume_high']:.2f}",
                         f"{g['income_usd_per_day']:.2f}",
                         f"−${-g['worst_open_mtm_bps']/1e4*n_rung:,.0f}"])
    cap_rows2 = [[f"${r['equity']:,}", f"{r['leverage']:.0f}×", f"${r['side_notional']:,.0f}",
                  f"${r['gross_notional']:,.0f}",
                  f"−${r['max_dd_usd']:,.0f}", f"{r['max_dd_pct_of_equity']:.0f}%",
                  f"{r['hold_pts_low']:.1f} – {r['hold_pts_high']:.1f}"]
                 for r in pts["capital_table"]]
    ch = pts["chart"]
    inv_chart = chart(
        [{"name": "spread 指数（% · 右轴同尺度）", "ys": [v / 100 for v in ch["spread"]],
          "color": "#0f766e", "width": 2.2},
         {"name": "网格持仓占阶梯上限 %（20 bps 档距）", "ys": ch["inventory_pct"],
          "color": "#f59e0b", "width": 1.8}],
        xlab=ch["ts"], ylab="%", yfmt="{:,.0f}%")

    # ---- volatility & leverage block
    rk = json.loads((HERE / "risk.json").read_text())
    vp = rk["vol_profile"]
    vol_rows = [[k, f"{v['hour_std_bps']:.1f}", f"{v['daily_std_bps']:.0f}",
                 f"{v['ann_vol_pct']:.0f}%", f"{v['mean_abs_daily_bps']:.0f}",
                 f"{v['max_daily_up_bps']:+,.0f}", f"{v['max_daily_down_bps']:,.0f}",
                 f"{v['max_dd_bps']:,.0f}"] for k, v in vp.items()]
    lev_rows = [[f"{r['leverage_side_per_equity']:g}×", f"{r['gross_per_equity']:g}×",
                 f"{r['worst_day_pct_of_equity']:.1f}%", f"{r['three_sigma_day_pct_of_equity']:.1f}%",
                 f"{r['max_dd_pct_of_equity']:.1f}%", f"{r['max_dd_intrabar_pct_of_equity']:.1f}%",
                 f"{r['margin_used_pct_offhours']:.0f}%"] for r in rk["leverage"]["rows"]]
    zc, cur = rk["current"], rk["z_predictive"]

    # ---- product-category exposure (estimates from segment revenue mix)
    seg_rows = [
        ["高端 DRAM / HBM", "+0.32（MU 0.17 + SKHY 0.15）", "−0.24（Samsung 0.15 + CXMT 0.05 + Nanya 0.02 + Winbond 0.01）", "+0.09"],
        ["NAND", "+0.14（MU 0.06 + SKHY 0.04 + SNDK 0.04）", "−0.13（Samsung 0.10 + Kioxia 0.03）", "≈0"],
        ["HDD", "—", "−0.08（STX 0.04 + WDC 0.03）", "−0.08"],
        ["Samsung 的非存储业务", "—", "−0.13（手机/代工/面板，按收入占比折算）", "−0.13"],
    ]

    cards = "".join(
        f'<div class="card"><div class="ct">{t}</div><div class="cv">{v}</div><div class="cd">{d}</div></div>'
        for t, v, d in [
            ("ETF 里能交易的只有 54.1%",
             "MU 27.4% + SKHY 22.1% + SNDK 4.7%",
             "剩下的 45.9% 在 Arcus 上没有对应标的：Samsung 25.3%、CXMT 5.3%、STX 4.2%、WDC 3.3%、Kioxia 3.2%、Nanya 2.5%、Winbond 1.1%。"),
            ("所以这个判断是对的",
             "这个组合 = 做空那 45.9%",
             "按 ETF 权重做多三条腿、做空 DRAM，净敞口就是「空 Samsung 25.3% + 空 CXMT 5.3% + …」，其中 Samsung 占空头腿的 55.2%。"),
            ("这段样本里它赚钱",
             f"非 Arcus 篮子 87 天 {bk['cum_pct_full']:+.1f}%",
             f"策略毛收益 +446 bps（不归一，+5.2 bps/天）；归一成「多 $1 空 $1」后 +1,006 bps、Sharpe 1.35，但最大回撤 −851 bps。"),
            ("真正的风险长这样",
             "空头腿里 55% 是一只股票",
             "Samsung 之外还有 19.5% 是 Kioxia/Nanya/Winbond/CXMT/STX/WDC —— 没有对冲工具、没有替代品，只能靠 DRAM 空头本身做对冲。"),
            ("如果目标是积分",
             "持有 > 网格",
             "积分 ≈ OI-time × 效率。同样资金下持有的 OI-time 是 20 bps 网格的 3.6 倍，"
             "网格的量路径最多补 0.9 分/周。而且 24/7 持仓自动命中 52.5% 的加成窗。"),
        ])

    page = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>long MU+SNDK+SKHY / short DRAM 到底在赌什么</title>
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
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px;margin:22px 0 8px}}
.card{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:15px 16px}}
.ct{{font-size:12px;letter-spacing:.06em;color:var(--mut)}}
.cv{{font-size:17px;font-weight:650;margin:6px 0}}
.cd{{font-size:13px;color:#475569}}
.panel{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:14px 0}}
.chart{{width:100%;height:auto;display:block}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);
border-radius:10px;overflow:hidden;font-size:13.5px;margin:12px 0}}
th{{text-align:left;padding:9px 11px;background:#f1f5f9;font-weight:600;color:#334155;font-size:12.5px;white-space:nowrap}}
td{{padding:8px 11px;border-top:1px solid var(--line);white-space:nowrap}}
.grid{{stroke:#eef2f7;stroke-width:1}}
.zero{{stroke:#94a3b8;stroke-width:1.2;stroke-dasharray:4 4}}
.ax{{fill:#94a3b8;font-size:11px}}
.lg{{fill:#475569;font-size:11.5px}}
.b{{color:#b91c1c;font-weight:600}}
.g{{color:#0f766e;font-weight:600}}
code{{background:#f1f5f9;padding:1px 5px;border-radius:4px;font-size:13px}}
ul{{padding-left:20px}}li{{margin:4px 0}}
.foot{{color:var(--mut);font-size:12.5px;margin-top:36px;border-top:1px solid var(--line);padding-top:14px}}
</style></head><body><div class="wrap">
<h1>long MU + SNDK + SKHY / short DRAM：本质上在做空什么？</h1>
<div class="sub">持仓来自 Roundhill 官网 Top Holdings（as of {H['as_of']}，共 {H['holdings_count']} 只）·
行情来自 Arcus 公开 <code>/v1/candles</code>（1 小时 {len(prep['ts']):,} 根，2026-07-13 → 2026-10-08，{prep['days']:.0f} 天）·
生成 {time.strftime('%Y-%m-%d %H:%M')}</div>
<div class="cards">{cards}</div>

<h2>1 · DRAM 的持仓拆解</h2>
<div class="panel">{hold_chart}</div>
<p>蓝色 = Arcus 上有对应永续、可以交易；红色 = Arcus 上没有的标的。ETF 是
<b>主动管理 + 季度再平衡</b>，还会用总收益互换（CXMT 那 5.27% 就是一笔 TRS，因为它没上市）。
基金基本资料：2026-04-02 上市、AUM $25.13B、费率 0.65%、22 只持仓、
30 天中位买卖价差 0.02%、成立以来 +113.74%（截至 9/30/26）。</p>
{table(["成分", "代码", "权重", "Arcus 可交易"], holds_rows)}

<h2>2 · 两种「按 DRAM 权重缩放」口径的净头寸</h2>
<p>每做空 $1 DRAM 的净敞口（正 = 多头，负 = 空头）：</p>
{table(["成分", "多头（按 ETF 权重买入）", "净敞口 / $1 DRAM 空头"], net_rows)}
<p><b>口径 A（严格按 ETF 权重，不归一）</b>：多 $0.274 MU + $0.221 SKHY + $0.047 SNDK，
空 $1 DRAM。可交易三条腿的净敞口正好抵消，剩下的就是<b>净空 45.86%</b> 的非 Arcus 篮子
—— 这才是真正的风险敞口，也是收益率来源。<br>
<b>口径 B（把可交易权重归一）</b>：多 $1（MU 50.5% / SKHY 40.9% / SNDK 8.6%）、空 $1 DRAM。
净头寸变成「多 $0.459 可交易 vs 空 $0.459 不可交易」的相对价值组合，波动比口径 A 低、
Sharpe 更高，但已经不是纯做空。</p>
<p>换成产品维度看（数字 = 每做空 $1 DRAM 的净名义，按各公司存储业务收入结构估算）：</p>
{table(["产品线", "多头", "空头", "净敞口"], seg_rows)}
<p>所以更准确的说法是：<b>多 HBM/高端 DRAM 的赢家（MU、SKHY）＋一点 NAND，空 HBM 掉队的
Samsung ＋ 商品化 DRAM ＋ HDD</b>。「空 NAND」只对一半 —— SNDK 的多头正好抵消 Kioxia 与
Samsung 的 NAND；「空 HDD」倒是干净的（STX+WDC 全额空，没有对应多头）。
最大的单一头寸其实是 <b>多 MU+SKHY / 空 Samsung</b>：一笔韩国内存同业之间的强弱对冲，
不是「DDR vs NAND」。</p>

<h2>3 · 这段样本里的表现</h2>
<div class="panel">{pnl}</div>
{table(["构造", "累计 bps", "漂移/天", "小时 σ", "年化 σ", "Sharpe", "最大回撤", "ADF t", "半衰期"], stat_rows)}
<p>注意口径 A 的 ADF t = −4.08 主要来自那 +5 bps/天的<b>确定性漂移</b>，不是均值回归；
口径 B 的 ADF t = −1.12 说明它更像随机游走。</p>
{table(["区间", "DRAM", "MU", "SKHY", "SNDK", "隐含篮子"], sub_rows)}
<p>驱动收益的不是单调的趋势，而是每一段里「不可交易组 vs 可交易组」的相对强弱：
7 月暴跌时段篮子跌得更狠（−18.2% vs DRAM −13.7%，做空赚钱），8 月反弹时篮子又涨得少
（+9.4% vs SKHY +16%），9 月下旬之后篮子转跌（−2.6%、−4.5%）。</p>

<h2>4 · 隐含的「非 Arcus 篮子」</h2>
<div class="panel">{basket_chart}</div>
<p>把 DRAM 的收益扣掉三条可交易腿的加权贡献，剩下的就是那个 45.9% 的篮子 ——
它的合成走势是 <b>{bk['cum_pct_full']:+.1f}%（{bk['drift_bps_per_day']:+.1f} bps/天）</b>，
年化波动 {bk['ann_vol_pct']:.0f}%（和 SKHY 同量级），Hurst {bk['hurst']:.2f}。
这与「Samsung 等名字在这轮行情里跑输 Micron / SanDisk」一致。</p>
<div class="panel"><b>用另一个窗口交叉验证</b><br>
4/2 → 7/13（101 天，那时 SKHY 还没上 Arcus）：DRAM +122.9%，而 MU +171.5%、SNDK +171.8%。
只扣掉 MU+SNDK 两条腿后，隐含篮子的累计收益仍达 <b>+100%</b> —— 也就是说
<b>「非可交易成分跑输 MU/SNDK」这个现象在两个窗口都成立</b>，不是单次行情的巧合。<br>
<span class="b">必须注意</span>：这段是 2026 年 AI/内存超级周期，两个窗口都是单边行情，
「篮子跑输」也可能只是「赢家（HBM 受益者）继续赢」的动量效应。</p>

<h2>5 · spread 的统计分析（口径 B：多 $1 三条腿 / 空 $1 DRAM）</h2>
{table(["维度", "实测", "解读"], stat2)}
<div class="panel">{inv_chart}</div>
<p>逐段拆开看（每段约 2 周）：</p>
{table(["区间", "spread 收益 bps", "小时 σ bps", "年化 σ"], sub2_rows)}
<p><b>一句话总结</b>：这是一个「近似随机游走 + 很肥的尾巴 + 小到统计上说不清的正漂移」的价差。
均值回归确实存在（Hurst 0.34、方差比随期数掉到 0.14、小时负自相关 −0.10），但它出现在
<b>多日尺度</b>上，而且必须靠移动锚才能捕捉；固定水平上没有箱体。</p>

<h2>6 · 波动率、杠杆与入场时机</h2>
<p>口径 B 的 spread 波动率约为 DRAM 本身的 <b>1/4</b>：</p>
{table(["序列", "小时 σ (bps)", "日 σ (bps)", "年化波动", "平均单日波动", "单日最大涨", "单日最大跌", "最大回撤"],
       vol_rows)}
<p>spread 的日 σ = <b>{vp['口径 B spread']['daily_std_bps']:.0f} bps（{vp['口径 B spread']['daily_std_bps']/100:.2f}%）</b>，
年化 <b>{vp['口径 B spread']['ann_vol_pct']:.1f}%</b>；平均单日波动
{vp['口径 B spread']['mean_abs_daily_bps']:.0f} bps，单日最大 {vp['口径 B spread']['max_daily_up_bps']:+,.0f} /
{vp['口径 B spread']['max_daily_down_bps']:,.0f} bps。DRAM 单腿年化
{vp['DRAM（ETF 本身）']['ann_vol_pct']:.0f}%、SKHY {vp['SKHY']['ann_vol_pct']:.0f}%、SNDK {vp['SNDK']['ann_vol_pct']:.0f}%。
用分时高低点算的<b>盘中最大不利偏移</b>是 {rk['intrabar']['max_excursion_bps']:,.0f} bps
（单日 {rk['intrabar']['worst_intraday_excursion_bps']:,.0f} bps）—— 这是「同一根 K 线里
最差进场、最差出场」的上界，比收盘口径严苛得多，用来做压力测试。</p>
<p>设 <b>L = 每边名义 / 权益</b>（总名义 = 2L×权益），按本样本的历史极值换算损失：</p>
{table(["L（每边/权益）", "总名义/权益", "历史最差单日", "3σ 单日", "最大回撤(收盘)",
        "最大回撤(盘中上界)", "15% 保证金占用"], lev_rows)}
<p><b>杠杆上限由三件事同时约束</b>：①保证金 —— 隔夜 IMF 15%、两条腿即 30%×L，所以
L ≤ 3.3 就吃满保证金；②回撤容忍度 —— L=2 时历史最差回撤占权益 {rk['leverage']['rows'][3]['max_dd_pct_of_equity']:.0f}%，
盘中上界口径 {rk['leverage']['rows'][3]['max_dd_intrabar_pct_of_equity']:.0f}%；③流动性/滑点。
<b>建议 L = 1.5–2</b>（即总名义 3–4 倍权益）：L=2 时最差单日 −11.6% 权益、最大回撤约 −17%，
留出 40% 保证金钱。L=3 时保证金占用 90%，任何一次补保证金或腿的滑点都会很危险。</p>
<p><b>入场时机</b>：spread 现在就在样本高位 —— 7 天 z = <b>{zc['z_7d']:+.2f}</b>，
近 7 天 {zc['trailing_7d_bps']:+,.0f} bps、近 30 天 {zc['trailing_30d_bps']:+,.0f} bps，
而偏离是会回补的：z 每高 1σ，未来 24 小时 {zc and zc and rk['z_predictive']['24h']['slope_bps_per_sigma']:.0f} bps、
72 小时 {rk['z_predictive']['72h']['slope_bps_per_sigma']:.0f} bps（t =
{rk['z_predictive']['24h']['t']:.1f} / {rk['z_predictive']['72h']['t']:.1f}）。
所以一次性满仓进场等于把「漂移」和「短期回补」对冲掉；合理的做法是<b>按 z 分批建仓</b>
（z&gt;1 只上 1/3，z&lt;0.5 补到 2/3，z&lt;−1 打满），这也顺便让 OI-time 均匀分布。</p>

<h2>7 · 积分：直接持有 vs 跑网格</h2>
<p>Arcus Season 1 的规则（依据官方周报 + 社区样本倒推，详见
<code>docs/points-model.md</code>）：
<b>积分 ≈ OI-time × 效率</b>，效率约 1,000–4,000 分 / $1M 名义·周；
RWA 在美股常规时段外（美东 20:00–04:00 + 周末 + 假期）有加成；
纯交易量是弱路径（$5.49M 成交的账户只拿 20 分）。</p>
<p>下面用同一份资金预算对比：<b>每边 $1,000 名义</b>（总名义 $2,000）。网格阶梯按 12 档/方向，
使「满仓」等于同样 $2,000 总名义，因此两者资金预算完全相同，差别只在资金的实际占用率。</p>
{table(["模式", "平均占用名义", "预算利用率", "积分（OI 路径）/周", "积分（量路径）/周",
        "价差收入 $/天", "历史最差浮亏"], rows_pts)}
<p><b>结论：直接持有赢。</b>同样的资金，持有的 OI-time 是 20 bps 网格的
<b>{hold['oi_dollar_hours_per_week']/P['grids']['step20']['oi_dollar_hours_per_week']:.1f} 倍</b>，
积分同理；网格的量路径即使按最乐观的 50 分/$1M 也只值
{P['grids']['step10']['points_volume_high']:.1f} 分/周，补不上缺口。原因是结构性的：
<b>阶梯的挂单在等待成交时是不计 OI 的</b>，所以网格的平均占用只有预算的
{P['grids']['step20']['deployed_pct_of_ladder']:.0f}%，而持有的占用是 100%。</p>
<p>关于「非美股时段加成」：加成是对<b>同一份 OI-time</b> 乘系数，不是独立路径。
一个 24/7 持有的仓位自动有 <b>{hold['bonus_share_pct']:.1f}%</b> 的 OI-time 落在加成窗里
（美东 20:00–04:00 = 40 小时 + 周末 48 小时 = 88/168），所以不必「在夜里交易」，
只需要「在夜里持仓」—— 这正好是持有比网格更占优的地方。唯一要留意的是：
加成窗同时是 off-hours 时段，初始保证金从 10% 提到 15%。</p>
<p>真正决定积分的变量是<b>名义规模</b>，不是手法。按上面同一套假设，持有 24/7 的积分预估：</p>
{table(["权益", "杠杆", "每边名义", "总名义", "历史最大浮亏", "占权益", "预估积分/周"], cap_rows2)}
<p>注：这里的「杠杆」是名义/权益倍数，不是保证金占用（RWA 永续初始保证金 10%，隔夜 15%）。
最大浮亏来自本样本 87 天的 −851 bps，按每边名义换算。</p>

<h2>8 · 这条 spread 能做网格吗</h2>
<p>用口径 B 的 spread（多 $1 可交易 / 空 $1 DRAM），1 分钟数据 60 天，锚 2 天 EWMA，
成交只按 1 分钟收盘价计（保守口径），成本按 maker 4×0.5 bps：</p>
{table(["档距 × 档数", "轮/天", "毛 bps/天", "净 bps/天", "最差浮亏 bps", "最大持仓档", "期末毛益 bps"], grid_rows)}
<p>表里的 bps 是「每档名义」口径。换算成上一节的资金口径（每边 $1,000 预算、24 档满仓）
之后：20 bps 档距的网格每天赚 $0.47、占用 28% 预算；直接持有每天赚 $1.16、占用 100%。
网格的唯一优势是<b>赚的是震荡而不是漂移</b>（如果 +11.6 bps/天的漂移消失，网格仍然有收入），
以及绝对浮亏更小（因为它没把钱全押上）。但在<b>积分和收入两项上都不如直接持有</b>。
如果要做，档距 10–30 bps、上下各 12 档、库存上限硬约束、全 maker。</p>

<h2>9 · 上线前必须确认的六件事</h2>
<ul>
<li><b>Samsung 的替代性</b>：空头腿 55% 是 Samsung。虽然它是韩国内存同业、与 SKHY 高度相关，
但它本身在 Arcus 上<b>无法交易</b>，只能靠 SKHY 腿间接获得部分对冲 —— 现有结构并没有对冲它。</li>
<li><b>权重会变</b>：ETF 是主动管理 + 季度再平衡，今天的 27.36% 不是三个月前的权重。
每季（或每次公告后）必须重算，否则敞口会在不知不觉中改变。</li>
<li><b>CXMT 是 TRS 而不是股票</b>：5.27% 的敞口通过总收益互换持有，定价、流动性、对手方都与股票不同。</li>
<li><b>ETF 溢价/折价 + 分红</b>：ETF 30 天中位买卖价差只有 0.02%，但 perp 跟踪的是场外价格；
另外 DRAM 分年度分红，除息日会让 perp 与篮子出现一次性错位。</li>
<li><b>perp 基差</b>：Arcus DRAM 永续自上市以来上涨 125.7%，而官方公布的成立以来收益为 113.74%
（截至 9/30）。约 12 个百分点的口径差异必须先解释清楚 —— 它可能来自上市首日定价，
也可能来自 perp 与 ETF 的持续基差，而这正好是这条策略收益的一部分。</li>
<li><b>容量</b>：DRAM 24h 名义成交仅 $821k、SKHY $713k、MU $4.09M。
单腿 $250 × 8 轮/天就已占 DRAM 日成交的 0.49%，这是这个场所的硬上限。</li>
</ul>
<div class="foot">数据与脚本：<code>spread-lab/holdings.json</code>（持仓快照）、
<code>basket.py</code>（拆解与回测）、<code>report2.py</code>（本页）。
第一份报告（long DRAM / short MU+SKHY 方向的价差与网格）见
<a href="spread-dram-mu-skhy.html">spread-dram-mu-skhy.html</a>。</div>
</div></body></html>"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page)
    (HERE / "report-basket.html").write_text(page)
    print(f"wrote {OUT} ({len(page)/1024:.0f} KB)")


if __name__ == "__main__":
    main()
