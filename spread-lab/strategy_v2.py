#!/usr/bin/env python3
"""策略 v2：每边 2× 基准 + 加成窗口 ×1.33，降杠杆阶梯按既定口径。

排班（ET）：
    常规 04:00-20:00（含盘前盘后）    每边 2.0×  总 4.00×
    加成避让 20:00-21:30 / 01:00-04:00 每边 2.0×  总 4.00×
    加成核心 21:30-01:00 + 周末        每边 2.66× 总 5.32×

降杠杆阶梯（既定口径，按权益回撤触发）：
    -5%  → 全账户降到每边 1.5×（不管有没有加成仓位）
    -8%  → 继续降到每边 1.0×
    -12% → 熔断：全平 + 停机

读 plan_L2.json 取窗口风险，输出 strategy_v2.json / strategy_v2.yaml / strategy-v2.html。
"""

from __future__ import annotations

import json
import os
import math
import time
from pathlib import Path

from report import table

HERE = Path(__file__).resolve().parent
OUTDIR = Path(os.environ.get("LAB_OUT", HERE / "out"))
IMF_REG, IMF_OFF, MMF = 0.10, 0.15, 0.066667
EQUITY = float(os.environ.get("LAB_EQUITY", "10000"))   # 演示用权益；换成本地值
EFF = (1000.0, 4000.0)
BOOST_MULT = 1.15                      # 加成核心窗口相对基准的倍数（交易者 2026-10-08 确认降到 1.15）
TAKER_BPS_PER_LEG = 2.25               # Arcus base tier
MAKER_BPS_PER_LEG = 0.5
FILL_TIMEOUT_S = 300                   # 5 分钟不成交改 taker


def main():
    src = json.loads((HERE / "plan_L2.json").read_text())
    hours = src["hours"]
    blk = src["blocks"]
    core, edge, reg = blk["BOOST_CORE"], blk["BOOST_EDGE"], blk["REG"]
    base_hours = hours["REG"] + hours["BOOST_EDGE"]

    levels = [
        ("L0 正常", 2.0, BOOST_MULT),
        ("L1 降一档（−5% 触发）", 1.5, BOOST_MULT),
        ("L2 降两档（−8% 触发）", 1.0, BOOST_MULT),
    ]
    lev_rows, risk_rows = [], []
    for lab, n, k in levels:
        lev_rows.append([lab, f"{n:.2f}×", f"{2*n:.2f}×",
                         f"{2*n*IMF_REG*100:.0f}%", f"{n*k:.2f}×", f"{2*n*k:.2f}×",
                         f"{2*n*k*IMF_OFF*100:.0f}%"])
        risk_rows.append([lab, f"{abs(reg['worst_dd_bps'])*n/1e4*100:.2f}%",
                          f"{abs(core['worst_dd_bps'])*n*k/1e4*100:.2f}%",
                          f"{abs(reg['dd_loss95'])*n/1e4*100:.2f}%",
                          f"{abs(core['dd_loss95'])*n*k/1e4*100:.2f}%"])

    # 保证金余量（L0 基准）
    n0, k0 = 2.0, BOOST_MULT
    usage0 = 2 * n0 * k0 * IMF_OFF
    free0 = 1 - usage0
    dd_to_full = free0                                  # 权益跌多少比例把可用抵押吃光
    bps_to_full = dd_to_full * 1e4 / (n0 * k0)
    dd_to_liq = 1 - 2 * n0 * k0 * MMF
    bps_to_liq = dd_to_liq * 1e4 / (n0 * k0)

    # 积分
    n_hours = base_hours + k0 * hours["BOOST_CORE"]
    oi_eh = n0 * n_hours
    oi_mh = EQUITY * oi_eh / 1e6
    m_week = oi_mh / 168.0
    pts = (m_week * EFF[0], m_week * EFF[1])
    base_hours_noboost = base_hours + hours["BOOST_CORE"]
    oi_no = n0 * base_hours_noboost
    oi_no_mh = EQUITY * oi_no / 1e6

    # 阶梯：每降一档还要再亏多少
    ladder = []
    for pct, prev_pct, n_eff, after, act in (
            (12.0, 0.0, n0, 2.0, "全账户降到每边 2.0× —— 含加成仓位一起降"),
            (20.0, 12.0, 2.0, 1.5, "继续降到每边 1.5×"),
            (30.0, 20.0, 1.5, 0.0, "熔断：全部平仓 + 停机 + 撤单")):
        bps_reg = (pct - prev_pct) / 100 * 1e4 / n_eff
        bps_bst = (pct - prev_pct) / 100 * 1e4 / (n_eff * BOOST_MULT)
        ladder.append([f"<b>{pct:.0f}%</b>", act,
                       f"{bps_reg:,.0f} bps（常规）<br>/ {bps_bst:,.0f} bps（加成）",
                       f"每边 {after:.2f}×" if after else "空仓"])

    sched = [
        ["04:00–08:00", "常规（盘前，无加成）", "×1", "2.00×", "4.00×", "40%"],
        ["08:00–11:00", "常规（避开美股开盘 ±1.5h）", "×1", "2.00×", "4.00×", "40%"],
        ["11:00–14:30", "常规（盘中）", "×1", "2.00×", "4.00×", "40%"],
        ["14:30–17:30", "常规（避开美股收盘 ±1.5h）", "×1", "2.00×", "4.00×", "40%"],
        ["17:30–20:00", "常规（盘后，无加成）", "×1", "2.00×", "4.00×", "40%"],
        ["20:00–21:30", "加成（避开韩股开盘 ±1.5h）", "更高", "2.00×", "4.00×", "60%"],
        ["<b>21:30–01:00</b>", "<b>加成核心</b>", "更高", f"<b>{n0*k0:.2f}×</b>",
         f"<b>{2*n0*k0:.2f}×</b>", f"<b>{usage0*100:.0f}%</b>"],
        ["01:00–04:00", "加成（避开韩股收盘 ±1.5h）", "更高", "2.00×", "4.00×", "60%"],
        ["<b>周末</b>", "<b>加成核心（除韩股窗口）</b>", "更高", f"<b>{n0*k0:.2f}×</b>",
         f"<b>{2*n0*k0:.2f}×</b>", f"<b>{usage0*100:.0f}%</b>"],
    ]
    # taker 兜底的代价：一轮 = 4 笔腿成交
    cost_rows = [
        ["全 maker（现状）", "4 × 0.5 = 2.0 bps", "10%", "网格最理想"],
        ["<b>进场 taker，出场 maker</b>", f"2 × {TAKER_BPS_PER_LEG} + 2 × {MAKER_BPS_PER_LEG} = 5.5 bps",
         "27.5%", "<b>5 分钟未成交规则下的典型情形</b>"],
        ["全 taker（含止损/降杠杆）", f"4 × {TAKER_BPS_PER_LEG} = 9.0 bps", "45%",
         "只在降杠杆、止损、熔断时用"],
    ]
    alert_rows = [
        ["L1 提示", "权益回撤 ≥ 5%", "回撤为主触发，与降杠杆阶梯第一档一致"],
        ["L1 提示", "常规时段：保证金占用 ≥ 55%（基准 40%）", "占用告警只在常规时段生效"],
        ["L1 提示", "加成窗口：可用抵押 < 24% 权益", "加成窗口基准占用已 69%，占用告警会天天触发 → 改用比例化的可用抵押"],
        ["L2 警告", "回撤 ≥ 8%；常规占用 ≥ 70%；加成窗口可用 < 15% 权益", "L2 = 第二档降杠杆"],
        ["L3 紧急", "回撤 ≥ 12%；常规占用 ≥ 85%；可用 < 6% 权益", "L3 = 熔断全平"],
        ["全部比例化", "可用抵押阈值用「% 权益」而不是绝对美元（24% / 15% / 6%）",
         "阈值按权益比例表达，加本金后自动缩放"],
    ]

    yaml_cfg = f"""# Arcus 记忆体 spread 策略 v2  —— 参数快照 {time.strftime('%Y-%m-%d %H:%M')}
spread:
  direction: long_basket_short_dram          # 多 MU/SKHY/SNDK(DRAM 权重归一) / 空 DRAM
  weights: {{MU-USD: 0.5054, SKHY-USD: 0.4088, SNDK-USD: 0.0859}}
  dollar_neutral: true

leverage:
  base_per_side: 2.0                         # 常规时段 04:00-20:00 ET
  boost_multiplier: {BOOST_MULT}              # 加成核心窗口 21:30-01:00 ET + 周末（2026-10-08 确认）
  boost_per_side: {n0*k0:.2f}

schedule:                                    # ET
  regular:      ["04:00-20:00"]
  boost_avoid:  ["20:00-21:30", "01:00-04:00"]   # 韩股开收盘 ±1.5h，仅加成窗口内
  boost_core:   ["21:30-01:00", "weekend"]

stops:                                       # 按权益回撤（自峰值），单位 %
  - trigger: 5.0                             # L1
    action: de_lever
    target_per_side: 1.5
    scope: account
  - trigger: 8.0
    action: de_lever
    target_per_side: 1.0
    scope: account
  - trigger: 12.0
    action: flatten_and_halt

execution:
  post_only: true                            # 两条腿默认 post-only maker
  fill_timeout_s: {FILL_TIMEOUT_S}           # 5 分钟未成交 → 撤单改 taker（吃 1 tick 以内）
  taker_bps_per_leg: {TAKER_BPS_PER_LEG}
  risk_actions_always_taker: true            # 降杠杆 / 止损 / 熔断不等 maker

alerts:                                      # arcus-farmer margin_watch 覆盖值
  # 所有阈值按权益比例，加本金后自动缩放
  boost_window:
    usage_alert: disabled                    # 基准占用 69%，占用告警在加成窗口关闭
    free_pct: [24, 15, 6]                    # 可用抵押 / 权益
  regular_window:
    margin_pct: [55, 70, 85]                 # 基准 40%
  equity_dd_pct: [5, 8, 12]                  # 三级回撤阶梯
  free_pct: [24, 15, 6]                      # ≈ 24% / 15% / 6% of equity
"""

    out = dict(meta=dict(generated=time.strftime("%Y-%m-%d %H:%M"), equity=EQUITY,
                         spread=src["meta"]["spread"], hours=hours),
               base_per_side=n0, boost_mult=BOOST_MULT, boost_per_side=n0 * k0,
               usage_regular=2 * n0 * IMF_REG, usage_boost=usage0,
               free_collateral_pct=free0, drawdown_to_full_margin_pct=dd_to_full * 100,
               bps_to_full_margin=bps_to_full,
               drawdown_to_liquidation_pct=dd_to_liq * 100, bps_to_liquidation=bps_to_liq,
               oi_time_mh_week=oi_mh, m_week=m_week, points_week=pts,
               oi_time_mh_week_noboost=oi_no_mh)
    (HERE / "strategy_v2.json").write_text(json.dumps(out, indent=1, default=str))
    (HERE / "strategy_v2.yaml").write_text(yaml_cfg)

    print(f"L0: 每边 {n0:.2f}× / 加成 {n0*k0:.2f}× (总 {2*n0*k0:.2f}×)")
    print(f"保证金: 常规 {2*n0*IMF_REG*100:.0f}% · 加成 {usage0*100:.0f}% · "
          f"可用抵押 {free0*100:.0f}%（${EQUITY*free0:,.0f}）")
    print(f"可用抵押吃光: 权益 −{dd_to_full*100:.1f}%（= {bps_to_full:,.0f} bps）")
    print(f"强平线:       权益 −{dd_to_liq*100:.1f}%（= {bps_to_liq:,.0f} bps）")
    print(f"OI-time {oi_mh:.3f} $M·h/周 = {m_week:.4f} $M-week → {pts[0]:.1f}–{pts[1]:.1f} 分/周")
    print(f"（不加成窗口加杠杆的话 {oi_no_mh:.3f} $M·h/周）")
    render(src, hours, blk, lev_rows, risk_rows, ladder, sched, yaml_cfg,
           cost_rows, alert_rows,
           n0, k0, usage0, free0, dd_to_full, bps_to_full, dd_to_liq, bps_to_liq,
           oi_mh, oi_no_mh, m_week, pts)
    return out


CSS = """
:root{--ink:#0f172a;--mut:#64748b;--line:#e2e8f0;--bg:#f8fafc}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",Arial,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:36px 24px 80px}
h1{font-size:26px;margin:0 0 6px}
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
pre{background:#0f172a;color:#e2e8f0;padding:16px;border-radius:10px;overflow:auto;font-size:12.5px;line-height:1.5}
.k{color:#0f766e;font-weight:600}.b{color:#b91c1c;font-weight:600}
ul{padding-left:20px}li{margin:4px 0}
.foot{color:var(--mut);font-size:12.5px;margin-top:34px;border-top:1px solid var(--line);padding-top:14px}
"""


def render(src, hours, blk, lev_rows, risk_rows, ladder, sched, yaml_cfg,
           cost_rows, alert_rows,
           n0, k0, usage0, free0, dd_to_full, bps_to_full, dd_to_liq, bps_to_liq,
           oi_mh, oi_no_mh, m_week, pts):
    f0 = lambda v: f"{v:,.0f}"
    f1 = lambda v: f"{v:,.1f}"
    f2 = lambda v: f"{v:,.2f}"
    core, reg = blk["BOOST_CORE"], blk["REG"]
    cards = [
        ("基准仓位", f"每边 {f2(n0)}×（总 {f2(2*n0)}×）",
         f"常规时段 04:00–20:00 ET；保证金占用 {2*n0*IMF_REG*100:.0f}%"),
        ("加成核心窗口", f"每边 {f2(n0*k0)}×（总 {f2(2*n0*k0)}×）",
         f"21:30–01:00 ET + 周末；保证金占用 {usage0*100:.0f}%"),
        ("加成窗口每周时长", f"{f1(hours['BOOST_CORE'])} h",
         f"加成窗口共 {f1(hours['BOOST_CORE']+hours['BOOST_EDGE'])} h，避让韩股开收盘 ±1.5h 后剩这么多"),
        ("每周 OI-time / 积分", f"{f2(oi_mh)} $M·h · {f1(pts[0])}–{f1(pts[1])} 分",
         f"按 1,000–4,000 分/$M-week；不加成窗口加杠杆是 {f2(oi_no_mh)} $M·h"),
    ]
    cardhtml = "".join('<div class="card"><div class="ct">%s</div><div class="cv">%s</div>'
                       '<div class="cd">%s</div></div>' % c for c in cards)

    page = f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>记忆体 spread 策略 v2</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>记忆体 spread 策略 v2 —— 每边 2× 基准 + 加成窗口 ×{BOOST_MULT}</h1>
<div class="sub">方向：{src['meta']['spread']} · 权益 ${EQUITY:,.0f} ·
样本 {src['meta']['from_'].split(' ')[0]} → {src['meta']['to'].split(' ')[0]} ·
生成 {time.strftime('%Y-%m-%d %H:%M')} · 止损口径按参数注释</div>

<div class="cards">{cardhtml}</div>

<h2>1 · 杠杆排班</h2>
{table(["ET 窗口", "Arcus 归类", "积分", "每边名义", "总名义", "保证金占用"], sched)}
<p>三档仓位只在「加成核心窗口」拉开：常规时段与韩股避让窗口都保持基准 {f2(n0)}×。
韩股避让窗口（20:00–21:30、01:00–04:00 ET）虽然拿得到加成，但波动率是核心窗口的
{f2(blk['BOOST_EDGE']['sigma_1h_bps']/core['sigma_1h_bps'])} 倍，所以只挂基准仓位。</p>

<h2>2 · 保证金预算</h2>
{table(["仓位档", "每边", "总名义", "常规占用", "加成每边", "加成总名义", "加成占用"], lev_rows)}
<p>核心结论：<b>加成核心窗口的保证金占用 {usage0*100:.0f}%</b>，可用抵押只剩权益的
{free0*100:.0f}%（${EQUITY*free0:,.0f}）。再往上加就不可执行了 —— 更激进方案里的
「每边 2× + 加成 2×」（总 8×）需要 120% 权益。</p>
<table>
<thead><tr><th>阈值</th><th>权益回撤</th><th>折算成 spread 位移</th><th>说明</th></tr></thead>
<tbody>
<tr><td>可用抵押吃光</td><td>−{dd_to_full*100:.1f}%</td><td>{f0(bps_to_full)} bps</td>
<td>保证金占用到 100%，不能再开新仓</td></tr>
<tr><td>交易所强平线</td><td>−{dd_to_liq*100:.1f}%</td><td>{f0(bps_to_liq)} bps</td>
<td>维持保证金 6.67% × 总名义，是真正的强平点</td></tr>
<tr><td>自设熔断（−12%）</td><td>−12.0%</td><td>{1200/(n0*k0):,.0f} bps</td>
<td>比强平线早 {f1(bps_to_liq/(1200/(n0*k0)))} 倍</td></tr>
</tbody></table>

<h2>3 · 风险与积分估算（×{BOOST_MULT} 档）</h2>
{table(["仓位档", "常规时段最差回撤", "加成核心最差回撤", "常规 P95", "加成 P95"], risk_rows)}
<p>样本内最差的单场回撤：常规时段（16h 一场）{f0(reg['worst_dd_bps'])} bps，
加成核心（7.9h 一场）{f0(core['worst_dd_bps'])} bps。换算成权益就是上表 ——
<b>每边 2× 时，常规时段的一次坏行情（{abs(reg['worst_dd_bps'])*n0/1e4*100:.2f}%）比加成核心
（{abs(core['worst_dd_bps'])*n0*k0/1e4*100:.2f}%）更接近降杠杆线</b>，
因为基准 2× 在常规时段也是满额生效。</p>
{table(["指标", "数值", "口径"], [
    ["每周 OI-time", f"{f2(oi_mh)} $M·h", f"每边 {f2(n0)}× × {f1(hours['REG']+hours['BOOST_EDGE'])}h + "
     f"{f2(n0*k0)}× × {f1(hours['BOOST_CORE'])}h"],
    ["折算 $M-week", f"{m_week:.4f}", "OI-time ÷ 168"],
    ["预估积分", f"{f1(pts[0])} – {f1(pts[1])} 分/周", "× 1,000–4,000 分/$M-week（@trippan0 / @Im_SHIVya 锚点）"],
    ["同样仓位但不加成窗口加杠杆", f"{f2(oi_no_mh)} $M·h", f"少 {f1((1-oi_no_mh/oi_mh)*100)}% —— 加成窗口的 ×{BOOST_MULT} 只买到这些"]])}

<h2>4 · 降杠杆阶梯（按实盘参数）</h2>
{table(["权益回撤触发", "动作", "从上一档到这里还要再亏", "降到"], ladder)}
<p><b>2026-10-08 重估：三档从 −5/−8/−12% 放宽到 −12/−20/−30%（见 risk-mm.html）。</b>
原三档是拿「初始保证金占用」拍的，把能力上限当成了风险线。真正决定强平的是维持保证金：
R = equity ÷ (总名义 × 6.667%)，R=1 才强平 —— 每边 2.0× 时进场 R=3.75、强平要 spread 走
3,667 bps，而历史最深回撤只有 401 bps。所以止损改按「历史能跌到哪 + 愿意亏多少」定。</p>
<p>三条规则的设计理由：</p>
<ul>
<li><b>−5% 才第一次降杠杆</b>：加成核心窗口自己的最坏回撤是
{abs(core['worst_dd_bps'])*n0*k0/1e4*100:.2f}% 权益，够不到 −5%。
所以 −5% 几乎一定是「常规时段出的事」，而不是加成杠杆造成的 —— 这个阈值放在这里不会被夜间噪声误触发。</li>
<li><b>降杠杆是全账户动作</b>：按既定口径，−5% 时不管有没有加成仓位，整账户降到每边 1.5×
（总 3×），加成窗口的额外杠杆一并去掉。−8% 继续降到每边 1×。</li>
<li><b>−12% 熔断</b>：全平 + 停机 + 撤单。它比交易所强平线（−{dd_to_liq*100:.0f}%）早
{f1(dd_to_liq*100/12)} 倍，留足执行时间 —— 薄簿上减仓要花十几分钟，不能等到最后。</li>
</ul>
<h3>4.1 恢复规则</h3>
<p>权益回到峰值的 −1% 以内并维持 <b>24 小时</b>，才逐级恢复（1.0× → 1.5× → 2.0×），
每次只上一档，隔 24 小时再评估。避免在震荡里反复升降。</p>

<h2>5 · 执行细则</h2>
{table(["项目", "设定", "理由"], [
    ["方向与权重", "多 MU 50.54% / SKHY 40.88% / SNDK 8.59%，空 DRAM 100%",
     "DRAM 持仓权重 ÷ 可交易占比 54.14%，美元中性"],
    ["<b>成交方式</b>", f"两条腿默认 post-only maker；<b>{FILL_TIMEOUT_S//60} 分钟未成交 → 撤单改 taker</b>（吃 1 tick 以内）",
     "保证仓位能建起来、能撤出来；代价见下表"],
    ["降杠杆/止损", "直接 taker，不等 maker", "降杠杆是防守动作，慢一拍的风险大于 9 bps 的成本"],
    ["加成窗口", "21:30–01:00 ET + 周末，避让韩股开收盘 ±1.5h",
     f"避让窗口 σ/小时 {f1(blk['BOOST_EDGE']['sigma_1h_bps'])} vs 核心 {f1(core['sigma_1h_bps'])}"],
    ["容量", "单腿 ≤ $250 日轮次 ≤ 8–16",
     "SKHY 60 天成交最小（$8.5M），是瓶颈腿"],
    ["funding", "美元中性时两腿对消；非中性时按 0.000509%/h 计",
     "所有 RWA 永续同向收 ≈4.5%/年"]])}
<h3>5.1 taker 兜底的代价（一轮 = 4 笔腿成交，档距 20 bps）</h3>
{table(["成交方式", "一轮成本", "占 20 bps 档距", "适用场景"], cost_rows)}
<p>结论：<b>taker 兜底对「建库存拿积分」可以接受</b>（持有期是小时级，5.5 bps 被时间摊薄），
但<b>对纯赚档距的网格 rung 不行</b> —— 5.5 bps 吃掉档距的 27.5%、9 bps 吃掉 45%。
所以建议：库存腿与所有防守动作按既定规则走（5 分钟改 taker），
<b>网格 rung 的进场单超时后撤单重挂而不是吃单</b>，除非该档的剩余边际 &gt; taker 成本。</p>
<h3>5.2 告警阈值（全部比例化）</h3>
{table(["级别", "触发条件", "说明"], alert_rows)}

<h2>6 · 参数文件（可直接落到配置）</h2>
<pre>{yaml_cfg}</pre>
<p><b>注意</b>：这只是策略文档与参数快照。落到 arcus-farmer 的
<code>config/mainnet.yaml</code> 并重启，才会改变真金白银的行为 —— 改动前先在测试网演练。</p>

<div class="foot">数据：<code>spread-lab/data/candles_*_1m.json</code>（Arcus /v1/candles）·
窗口风险取 <code>leverage_windows.json</code> / <code>plan_L2.json</code>。
脚本：<code>python3 strategy_v2.py</code>。生成于 {time.strftime('%Y-%m-%d %H:%M')}。</div>
</div></body></html>"""
    OUTDIR.mkdir(parents=True, exist_ok=True)
    (OUTDIR / "strategy-v2.html").write_text(page)
    (HERE / "report-strategy-v2.html").write_text(page)
    print(f"wrote {OUTDIR / 'strategy-v2.html'} ({len(page)/1024:.0f} KB)")


if __name__ == "__main__":
    main()
