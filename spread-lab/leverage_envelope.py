#!/usr/bin/env python3
"""杠杆可行域：加成窗口的 15% 初始保证金 + 维持保证金 R，能撑到每边几倍？

一个候选方案：平时每边 3.5x，加成核心再 x2（= 每边 7x，总 14x）。
这里把「能不能开」和「亏得起吗」两条线算清楚。

输出 leverage_envelope.json + leverage-envelope.html。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from report import table

HERE = Path(__file__).resolve().parent
OUTDIR = Path(os.environ.get("LAB_OUT", HERE / "out"))
IMF_REG, IMF_OFF, MMF = 0.10, 0.15, 0.066667
TAIL_BAND, TAIL_EXP = 1000.0, 2000.0      # ±5% / ±10% 价格带打满的 spread 位移（bps）


def row(L: float) -> dict:
    gross = 2 * L
    R0 = 1 / (gross * MMF)
    liq = (1 - gross * MMF) / L * 1e4
    loss_band = L * TAIL_BAND / 1e4
    loss_exp = L * TAIL_EXP / 1e4
    return dict(L=L, gross=gross, im_reg=gross * IMF_REG, im_off=gross * IMF_OFF,
                R0=R0, liq_bps=liq, loss_band=loss_band, R_band=max(R0 * (1 - loss_band), 0),
                loss_exp=loss_exp, R_exp=max(R0 * (1 - loss_exp), 0),
                open_off=(gross * IMF_OFF) <= 1.0)


def max_L(floor: float) -> float:
    """Largest per-side x that survives a ±5% band-tail hit with R >= floor."""
    lo, hi = 0.1, 10.0
    for _ in range(60):
        mid = (lo + hi) / 2
        r = row(mid)
        if r["R_band"] >= floor and r["open_off"]:
            lo = mid
        else:
            hi = mid
    return lo


def main():
    Ls = [2.0, 2.5, 3.0, 3.33, 3.5, 4.0, 5.0, 7.0]
    rows = [row(L) for L in Ls]
    caps = {f"R>={f}": max_L(f) for f in (2.5, 2.0, 1.5)}
    im_cap = 1.0 / (2 * IMF_OFF)                       # 加成窗口 IM 100%
    im_cap_90 = 0.90 / (2 * IMF_OFF)                   # 加成窗口 IM 90%
    out = dict(rows=rows, caps=caps, im_cap=im_cap, im_cap_90=im_cap_90,
               tail_band=TAIL_BAND, tail_expanded=TAIL_EXP)
    (HERE / "leverage_envelope.json").write_text(json.dumps(out, indent=1))
    print(f"加成窗口 IM 上限: 每边 {im_cap:.2f}x (100%) / {im_cap_90:.2f}x (90%)")
    print("三角约束下最大每边倍数:", {k: round(v, 2) for k, v in caps.items()})
    for r in rows:
        print(f"  每边 {r['L']:.2f}x | 总 {r['gross']:.2f}x | IM 常规 {r['im_reg']*100:5.0f}% "
              f"/ 加成 {r['im_off']*100:5.0f}%{' ✗' if not r['open_off'] else '  '} | "
              f"R {r['R0']:.2f} | 强平 {r['liq_bps']:6.0f} bps | "
              f"±5%带后 R {r['R_band']:.2f} | 扩张带后 R {r['R_exp']:.2f}")
    render(rows, caps, im_cap, im_cap_90)
    return out


CSS = """
body{margin:0;background:#f8fafc;color:#0f172a;font:15px/1.65 -apple-system,
BlinkMacSystemFont,"PingFang SC","Helvetica Neue",Arial,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:36px 24px 80px}
h1{font-size:25px;margin:0 0 6px}
h2{font-size:19px;margin:40px 0 6px;padding-bottom:8px;border-bottom:2px solid #e2e8f0}
.sub{color:#64748b;font-size:13.5px;margin-bottom:22px}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e2e8f0;
border-radius:10px;overflow:hidden;font-size:13.5px;margin:12px 0}
th{text-align:left;padding:9px 11px;background:#f1f5f9;font-weight:600;color:#334155;font-size:12.5px}
td{padding:8px 11px;border-top:1px solid #e2e8f0;white-space:nowrap}
tr:hover td{background:#fafcff}
code{background:#f1f5f9;padding:1px 5px;border-radius:4px;font-size:13px}
.bad{color:#b91c1c;font-weight:600}.ok{color:#0f766e;font-weight:600}
ul{padding-left:20px}li{margin:4px 0}
.foot{color:#64748b;font-size:12.5px;margin-top:34px;border-top:1px solid #e2e8f0;padding-top:14px}
"""


def render(rows, caps, im_cap, im_cap_90):
    f0 = lambda v: f"{v:,.0f}"
    def verdict(r):
        if not r["open_off"]:
            return "<span class='bad'>加成窗口开不出来</span>"
        if r["R_exp"] < 1.0:
            return "<span class='bad'>扩张带打满就强平</span>"
        if r["R_band"] < 1.5:
            return "<span class='bad'>±5% 带打满就接近强平</span>"
        if r["R_band"] < 2.0:
            return "偏高（±5% 带后 R &lt; 2）"
        return "<span class='ok'>安全</span>"

    t_rows = [[f"每边 {r['L']:.2f}×", f"{r['gross']:.2f}×", f"{r['im_reg']*100:.0f}%",
               (f"<span class='bad'>{r['im_off']*100:.0f}%</span>" if not r["open_off"]
                else f"{r['im_off']*100:.0f}%"),
               f"{r['R0']:.2f}×", f0(r["liq_bps"]), f"{r['loss_band']*100:.0f}%",
               f"{r['R_band']:.2f}×", f"{r['loss_exp']*100:.0f}%", f"{r['R_exp']:.2f}×",
               verdict(r)] for r in rows]

    page = f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>杠杆可行域</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>杠杆可行域：每边能开到几倍？</h1>
<div class="sub">两条线：<b>能不能开</b>（加成窗口初始保证金 15%）和
<b>亏得起吗</b>（维持保证金 R = equity / (总名义 × 6.667%)，R=1 强平）·
结构性尾部取 ±5%（1,000 bps）与扩张带 ±10%（2,000 bps）·
生成 {time.strftime('%Y-%m-%d %H:%M')}</div>

<h2>1 · 候选方案：平时 3.5× + 加成 ×2</h2>
<p>加成核心 = 每边 7×（总 14×）：<b>进场 R 只有 {rows[-1]['R0']:.2f}×</b> ——
维持保证金就要吃掉权益的 {rows[-1]['gross']*MMF*100:.0f}%，<b>spread 走
{f0(rows[-1]['liq_bps'])} bps 就强平</b>（1 分钟 σ 是 3.6 bps，这个量级几小时就能走到）。
初始保证金 {rows[-1]['im_off']*100:.0f}%，开都开不出来。这不是策略，是掷硬币。</p>

<h2>2 · 每个杠杆的两条线</h2>
{table(["每边", "总名义", "IM 常规", "IM 加成", "进场 R", "强平位移", "±5%带亏", "带后 R",
        "扩张带亏", "扩张带后 R", "判定"], t_rows)}
<p>读法：<b>IM 加成 &gt; 100% 就是开不出来</b>（加成窗口的硬上限是每边
{im_cap:.2f}×）；<b>带后 R</b> 是「价格带被四条腿反向打满」之后还剩多少缓冲，
小于 1.5 就说明一次极端行情就能把仓位推到强平附近。</p>

<h2>3 · 三条约束给出的上限</h2>
{table(["约束", "每边上限", "含义"], [
    ["加成窗口初始保证金 100%", f"{im_cap:.2f}×", "15% × 总名义 ≤ 权益，超过就开不出仓"],
    ["加成窗口初始保证金 90%（留余量）", f"{im_cap_90:.2f}×", "推荐上限"],
    ["±5% 带打满后 R ≥ 2.0", f"{caps['R>=2.0']:.2f}×", "极端行情后仍留 2 倍缓冲"],
    ["±5% 带打满后 R ≥ 1.5", f"{caps['R>=1.5']:.2f}×", "底线"],
])}
<p><b>结论：加成窗口能开的上限是每边 {im_cap:.2f}×（IM 打满）。
所以「加成核心 ×2」只有在平时每边 ≤ {im_cap/2:.2f}× 时才成立</b> ——
平时 1.67× 加成 3.33×，或者平时 2.5× 加成 3.33× 已经到顶。</p>

<h2>4 · 可选组合（基准 / 加成核心）</h2>
{table(["方案", "常规时段", "加成核心", "加成 IM", "进场 R", "±5%带后 R", "每周 OI-time 相对"], [
    ["A · 现状", "2.00×", "2.30×", "69%", "3.26×", "2.51×", "1.00×"],
    ["B · 保守加档", "2.50×", "2.75×", "83%", "2.73×", "2.10×", "1.22×"],
    ["C · 推荐上限", "3.00×", "3.00×", "90%", "2.50×", "1.75×", "1.43×"],
    ["D · 硬上限", f"{im_cap:.2f}×", f"{im_cap:.2f}×", "100%", "2.25×", "1.50×", "1.57×"],
    ["<span class='bad'>候选方案</span>", "<span class='bad'>3.50×</span>",
     "<span class='bad'>7.00×</span>", f"<span class='bad'>{rows[-1]['im_off']*100:.0f}%</span>",
     f"<span class='bad'>{rows[-1]['R0']:.2f}×</span>",
     f"<span class='bad'>{rows[-1]['R_exp']:.2f}×</span>", "—"]])}
<p>方案 C（每边 3.0×）是「加成窗口留 10% 保证金余量」下的上限：加成 IM 90%、
进场 R 2.50、±5% 带打满后 R 1.75、扩张带打满后 R 1.00（刚好不爆）。
方案 D 是 IM 打满，任何权益波动都会让账户连新单都开不出来。</p>

<div class="foot">脚本：<code>spread-lab/leverage_envelope.py</code>；
维持保证金/价格带来自 <code>arcus-farmer/data/markets.json</code>。
生成于 {time.strftime('%Y-%m-%d %H:%M')}。</div>
</div></body></html>"""
    OUTDIR.mkdir(parents=True, exist_ok=True)
    (OUTDIR / "leverage-envelope.html").write_text(page)
    (HERE / "report-leverage-envelope.html").write_text(page)
    print(f"wrote {OUTDIR / 'leverage-envelope.html'} ({len(page)/1024:.0f} KB)")


if __name__ == "__main__":
    main()
