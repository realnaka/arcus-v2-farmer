#!/usr/bin/env python3
"""Live dashboard for the held memory-basket spread.

Long MU + SKHY + SNDK at their DRAM weights (normalised) / short the same
dollar amount of DRAM, held 24/7.  Samples the account once a minute, keeps a
JSONL history, and serves a page with position, risk, P&L, volatility and the
Arcus OI-time / points estimate.

    .venv/bin/python tools/spread_dashboard.py --config config/mainnet.yaml --port 8790
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import math
import sys
import time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from arcus import signing
from arcus.config import load_config
from arcus.models import Market, Position
from arcus.rest import ArcusRest

from tools.spread_basket import ETF_WEIGHTS, SHORT_LEG, TRADEABLE_SUM, STATE
from tools.margin_watch import LEVELS, alert_levels, level_of

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "state" / "spread_samples.jsonl"
ET = ZoneInfo("America/New_York")
EFF_LOW, EFF_HIGH = 1000.0, 4000.0      # points per $M-week of open interest
API = "https://api.arcus.xyz"

SNAPSHOT: dict = {"status": "starting"}
HISTORY: dict = {"spread": [], "ts": []}
PORT = 8790


def is_bonus(t: dt.datetime) -> bool:
    t = t.astimezone(ET)
    if t.weekday() >= 5:
        return True
    h = t.hour + t.minute / 60
    return h >= 20 or h < 4


async def fetch_history(days: int = 45) -> None:
    """One page of 1h candles per leg -> historical spread index in bps."""
    syms = list(ETF_WEIGHTS) + [SHORT_LEG]
    w = {k: float(v) / float(TRADEABLE_SUM) for k, v in ETF_WEIGHTS.items()}
    async with httpx.AsyncClient(timeout=20) as client:
        candles = {}
        for s in syms:
            r = await client.get(f"{API}/v1/candles",
                                 params={"market": s, "timeframe": "1h", "to": int(time.time() * 1e6)})
            r.raise_for_status()
            rows = (r.json() or {}).get("candles") or []
            candles[s] = {int(x["openTime"]) // 1_000_000: float(x["close"]) for x in rows}
    ts = sorted(set.intersection(*[set(v) for v in candles.values()]))
    ts = [t for t in ts if t >= ts[0]]  # all
    idx, base = [], None
    for t in ts:
        v = (sum(w[k] * math.log(candles[k][t]) for k in w) - math.log(candles[SHORT_LEG][t]))
        if base is None:
            base = v
        idx.append((v - base) * 1e4)
    HISTORY["ts"], HISTORY["spread"] = ts, idx


def history_stats() -> dict:
    ts, x = HISTORY["ts"], HISTORY["spread"]
    if len(x) < 50:
        return {}
    h = [x[i] - x[i - 1] for i in range(1, len(x))]
    d = [x[i] - x[i - 24] for i in range(24, len(x))]
    win = 168
    seg = x[-win:] if len(x) >= win else x
    mu = sum(seg) / len(seg)
    sd = (sum((v - mu) ** 2 for v in seg) / max(1, len(seg) - 1)) ** 0.5
    peak, mdd = x[0], 0.0
    for v in x:
        peak = max(peak, v)
        mdd = min(mdd, v - peak)
    return {
        "hist_bars": len(x),
        "hist_from": time.strftime("%Y-%m-%d", time.gmtime(ts[0])),
        "hour_std_bps": (sum((v - sum(h) / len(h)) ** 2 for v in h) / (len(h) - 1)) ** 0.5,
        "daily_std_bps": (sum((v - sum(d) / len(d)) ** 2 for v in d) / (len(d) - 1)) ** 0.5,
        "hist_min_bps": min(x), "hist_max_bps": max(x), "hist_mdd_bps": mdd,
        "z_7d": (x[-1] - mu) / sd if sd else 0.0,
        "anchor_7d_bps": mu,
    }


def pnl_breakdown(*, equity: float, net_deposits: float, upnl_total: float) -> dict:
    """账户口径的盈亏三段式：总 / 已实现 / 未实现。

    ``总盈亏 = 权益 − 净入金``，把已实现、未实现、手续费、资金费全算进去，
    而且**不随 ADL 复位重置**：复位把一条腿平掉再开，未实现瞬间归零，只看
    未实现就分不出「刚刚落袋的浮盈」和「本来就没赚」。
    ``已实现 = 总盈亏 − 未实现``（所以费用与资金费都落在已实现里）。

    净入金读不到（0 / 缺失）时三者一律返回 None，页面显示「—」—— 把 0 当成
    净入金会让每一条都显示成「亏掉全部权益」。
    """
    if not net_deposits:
        return {"net_deposits": net_deposits, "total_pnl": None,
                "total_pnl_pct": None, "realized_pnl": None}
    total = equity - net_deposits
    return {"net_deposits": net_deposits, "total_pnl": total,
            "total_pnl_pct": 100 * total / net_deposits,
            "realized_pnl": total - upnl_total}


async def sample_once(rest: ArcusRest, markets: dict, state: dict | None) -> dict:
    acct = await rest.account()
    equity = float(acct.get("equity") or 0)
    free = float(acct.get("freeCollateral") or acct.get("free_collateral") or 0)
    # ``netDeposits`` is deposits minus withdrawals, so equity - netDeposits is
    # the account's lifetime P&L: realized + unrealized + fees + funding.
    net_deposits = float(acct.get("netDeposits") or 0)
    mids = {k: float(v) for k, v in (await rest.mids()).get("mids", {}).items()}
    raw = (await rest.positions()).get("positions") or {}
    if isinstance(raw, dict):
        raw = list(raw.values())
    pos, margin_by_sym = {}, {}
    for p in raw:
        if not isinstance(p, dict):
            continue
        pp = Position.from_api(p)
        if pp.size == 0:
            continue
        sym = next((s for s, m in markets.items() if m.market_id == pp.market_id), None)
        if sym:
            pos[sym] = pp
            margin_by_sym[sym] = float(p.get("marginUsed") or 0)
    margin = sum(margin_by_sym.values())
    legs, upnl_total, long_notional, short_notional, maint = [], 0.0, 0.0, 0.0, 0.0
    per_side = float(state["per_side_notional"]) if state else 0.0
    for sym in list(ETF_WEIGHTS) + [SHORT_LEG]:
        mark = mids.get(sym) or 0.0
        pp = pos.get(sym)
        if pp is None:
            legs.append({"symbol": sym, "size": 0, "entry": None, "mark": mark,
                         "notional": 0.0, "upnl": 0.0,
                         "target_w": float(ETF_WEIGHTS.get(sym, Decimal(1))) / float(TRADEABLE_SUM)
                         if sym in ETF_WEIGHTS else 1.0})
            continue
        notional = abs(float(pp.size)) * mark
        upnl = (Decimal(str(mark)) - pp.entry_price) * pp.size
        upnl_total += float(upnl)
        maint += notional * float(markets[sym].maintenance_margin_fraction)
        if pp.size > 0:
            long_notional += notional
        else:
            short_notional += notional
        legs.append({"symbol": sym, "size": float(pp.size), "entry": float(pp.entry_price),
                     "mark": mark, "notional": notional, "upnl": float(upnl),
                     "target_w": float(ETF_WEIGHTS.get(sym, Decimal(1))) / float(TRADEABLE_SUM)
                     if sym in ETF_WEIGHTS else 1.0})
    gross = long_notional + short_notional
    snap = {
        "ts": int(time.time()), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "equity": equity, "free": free, "margin_used": margin,
        "margin_pct": 100 * margin / equity if equity else 0,
        "legs": legs, "long_notional": long_notional, "short_notional": short_notional,
        "gross_notional": gross, "net_notional": long_notional - short_notional,
        "upnl_total": upnl_total,
        "spread_bps": upnl_total / per_side * 1e4 if per_side else 0.0,
        "per_side_notional": per_side,
        "leverage": float(state["leverage"]) if state else 0.0,
        "opened_at": state["opened_at"] if state else None,
        "maint": maint,
        "buffer_usd": equity - maint,
        "buffer_pct": 100 * (equity - maint) / equity if equity else 0.0,
    }
    snap.update(pnl_breakdown(equity=equity, net_deposits=net_deposits,
                              upnl_total=upnl_total))
    # ---- alert levels (same thresholds the Feishu guard uses)
    peak = 0.0
    alert_state_path = ROOT / "state" / "margin_alert_state.json"
    if alert_state_path.exists():
        try:
            peak = float(json.loads(alert_state_path.read_text()).get("peak_equity") or 0)
            snap["alert_state"] = json.loads(alert_state_path.read_text())
        except Exception:  # noqa: BLE001
            pass
    if not peak and state:
        peak = float(state.get("equity_at_entry") or 0)
    dd, rows = alert_levels(equity, margin, free, maint, per_side, peak)
    snap["alerts"] = {"peak": peak, "dd": dd, "rows": rows,
                      "current": level_of(snap, dd)[0] or "normal"}
    if state:
        t0 = state["timestamp"]
        hours = (time.time() - t0) / 3600
        snap["hours_held"] = hours
        snap["oi_dollar_hours"] = gross * hours
        # 1 $M-week = $1M of open interest held for a week; holding `gross`
        # around the clock accrues gross/1e6 of it per week.
        oi_mw = gross / 1e6
        snap["oi_million_week_per_week"] = oi_mw
        snap["points_week_low"] = oi_mw * EFF_LOW
        snap["points_week_high"] = oi_mw * EFF_HIGH
        bonus = 0.0
        for i in range(1, 60):
            t = time.time() - i * 60
            if t < t0:
                break
            if is_bonus(dt.datetime.fromtimestamp(t, dt.timezone.utc)):
                bonus += 1
        snap["bonus_share_pct"] = 100 * bonus / max(1, i - 1)
    return snap


async def sampler(cfg) -> None:
    priv = signing.load_private_key(cfg.account.api_key_path)
    public_key = cfg.account.api_key_public or signing.public_key_hex(priv)
    rest = ArcusRest(base_url=cfg.account.base_url, address=cfg.account.address,
                     account_index=cfg.account.account_index, public_key=public_key,
                     private_key=priv)
    await rest.start()
    try:
        markets = {}
        for m in (await rest.markets()).get("markets", []):
            mk = Market.from_api(m)
            if mk.symbol in list(ETF_WEIGHTS) + [SHORT_LEG]:
                markets[mk.symbol] = mk
        state = json.loads(STATE.read_text()) if STATE.exists() else None
        HISTORY["state"] = state
        try:
            await fetch_history()
            HISTORY["stats"] = history_stats()
        except Exception as exc:  # noqa: BLE001
            HISTORY["stats"] = {"error": str(exc)}
        while True:
            try:
                snap = await sample_once(rest, markets, state)
                snap["history"] = HISTORY.get("stats", {})
                snap["spread_curve"] = [[HISTORY["ts"][i], HISTORY["spread"][i]]
                                        for i in range(0, len(HISTORY["spread"]), max(1, len(HISTORY["spread"]) // 300))]
                SNAPSHOT.clear()
                SNAPSHOT.update(snap)
                with SAMPLES.open("a") as fh:
                    fh.write(json.dumps({k: v for k, v in snap.items()
                                         if k not in ("history", "spread_curve")}) + "\n")
            except Exception as exc:  # noqa: BLE001
                SNAPSHOT["status"] = f"error: {exc}"
            await asyncio.sleep(60)
    finally:
        await rest.close()


PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Memory-basket spread</title>
<style>
:root{--ink:#0f172a;--mut:#64748b;--line:#e2e8f0;--bg:#f8fafc}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC",Helvetica,Arial,sans-serif}
.wrap{max-width:1060px;margin:0 auto;padding:28px 22px 70px}
h1{font-size:24px;margin:0 0 4px}.sub{color:var(--mut);font-size:13px;margin-bottom:20px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:18px}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:13px 15px}
.ct{font-size:11.5px;color:var(--mut);letter-spacing:.05em}
.cv{font-size:19px;font-weight:650;margin-top:4px}
.cd{font-size:12.5px;color:#64748b;margin-top:2px}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);
border-radius:10px;overflow:hidden;font-size:13.5px;margin:10px 0 18px}
th{text-align:left;padding:8px 10px;background:#f1f5f9;font-size:12px;color:#334155}
td{padding:7px 10px;border-top:1px solid var(--line)}
.panel{background:#fff;border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:16px}
.pos{color:#047857;font-weight:600}.neg{color:#b91c1c;font-weight:600}
svg{width:100%;height:auto;display:block}
.grid{stroke:#eef2f7}.zero{stroke:#94a3b8;stroke-dasharray:4 4}
.ax{fill:#94a3b8;font-size:11px}
h2{font-size:16px;margin:22px 0 6px}
</style></head><body><div class="wrap">
<h1>Memory-basket spread</h1>
<div class="sub" id="sub">loading…</div>
<div class="cards" id="cards"></div>
<h2>持仓</h2><table id="legs"></table>
<h2>净值曲线（bps，相对每边名义）</h2><div class="panel" id="chart"></div>
<h2>风险与积分</h2><table id="risk"></table>
<h2>保证金与告警</h2><div class="cards" id="acards"></div><table id="alerts"></table>
</div>
<script>
const f=(v,d=2)=>(v===null||v===undefined||isNaN(v))?'—':v.toFixed(d);
const s=(v,d=2)=>(v>=0?'+':'')+f(v,d);
async function tick(){
  const r=await fetch('/api/snapshot');const d=await r.json();
  if(d.status){document.getElementById('sub').textContent=d.status;return;}
  document.getElementById('sub').textContent=
    `持仓 ${d.opened_at||'-'} · 已持有 ${f(d.hours_held,1)} 小时 · 更新 ${d.time} · 每 60 秒采样`;
  const pnl=d.upnl_total, pnlPct=d.per_side_notional?pnl/d.per_side_notional*100:0;
  const usd=v=>(v===null||v===undefined||isNaN(v))?'—':(v>=0?'+$':'-$')+f(Math.abs(v),2);
  const tot=d.total_pnl, real=d.realized_pnl;
  const cards=[
    ['总盈亏（账户累计）', usd(tot),
     `权益 $${f(d.equity)} − 净入金 $${f(d.net_deposits)}${d.total_pnl_pct!=null?` · ${s(d.total_pnl_pct,2)}%`:''}`],
    ['已实现盈亏', usd(real), '含手续费/资金费 · 不随 ADL 复位重置'],
    ['未实现盈亏', usd(pnl), `${s(d.spread_bps,0)} bps / 每边名义`],
    ['权益', '$'+f(d.equity), `可用 $${f(d.free)}`],
    ['spread 相对入场', s(pnlPct,2)+'%', '入场以来 · ADL 复位后重新计'],
    ['总名义 / 杠杆', '$'+f(d.gross_notional,0), `L=${f(d.leverage,2)} · 保证金占 ${f(d.margin_pct,0)}%`],
    ['历史 7 天 z', s(d.history?.z_7d,2), '正 = 在高位'],
    ['预估积分/周', f(d.points_week_low,1)+' – '+f(d.points_week_high,1), `加成窗 ${f(d.bonus_share_pct,0)}%`],
  ];
  document.getElementById('cards').innerHTML=cards.map(c=>
    `<div class="card"><div class="ct">${c[0]}</div><div class="cv">${c[1]}</div><div class="cd">${c[2]}</div></div>`).join('');
  document.getElementById('legs').innerHTML=
    '<tr><th>腿</th><th>方向</th><th>数量</th><th>开仓均价</th><th>现价</th><th>名义</th><th>浮盈亏</th></tr>'+
    d.legs.map(l=>`<tr><td>${l.symbol}</td><td>${l.size>0?'多':(l.size<0?'空':'—')}</td>
      <td>${f(Math.abs(l.size),6)}</td><td>${f(l.entry,4)}</td><td>${f(l.mark,4)}</td>
      <td>$${f(l.notional,0)}</td><td class="${l.upnl>=0?'pos':'neg'}">${s(l.upnl,2)}</td></tr>`).join('')+
    `<tr><td colspan="5">合计</td><td>$${f(d.gross_notional,0)}</td>
      <td class="${pnl>=0?'pos':'neg'}">${s(pnl,2)}</td></tr>`;
  const h=d.history||{};
  const al=d.alerts||{rows:[],current:'normal'};
  const st=d.alert_state||{last_sent:{}};
  document.getElementById('acards').innerHTML=[
    ['当前级别', al.current==='normal'?'正常':al.current, `权益较峰值回撤 ${f(al.dd,1)}%`],
    ['距 L1 提示', (al.rows[0]?.dist_bps>0?'-':'')+f(Math.abs(al.rows[0]?.dist_bps||0),0)+' bps',
     `还差 $${f(Math.max(0,al.rows[0]?.dist_usd||0),0)} 权益`],
    ['距 L2 警告', (al.rows[1]?.dist_bps>0?'-':'')+f(Math.abs(al.rows[1]?.dist_bps||0),0)+' bps',
     `还差 $${f(Math.max(0,al.rows[1]?.dist_usd||0),0)} 权益`],
    ['距 L3 紧急', (al.rows[2]?.dist_bps>0?'-':'')+f(Math.abs(al.rows[2]?.dist_bps||0),0)+' bps',
     `还差 $${f(Math.max(0,al.rows[2]?.dist_usd||0),0)} 权益`],
  ].map(c=>`<div class="card"><div class="ct">${c[0]}</div><div class="cv">${c[1]}</div><div class="cd">${c[2]}</div></div>`).join('');
  const lastOf=k=>st.last_sent&&st.last_sent[k]?new Date(st.last_sent[k]*1000).toLocaleString('zh-CN'):'—';
  document.getElementById('alerts').innerHTML=
    '<tr><th>级别</th><th>触发线（权益）</th><th>还需下跌</th><th>≈ spread</th><th>主触发条件</th><th>阈值</th><th>上次发送</th></tr>'+
    al.rows.map(r=>`<tr><td>${r.level} ${r.label}</td><td>$${f(r.trigger_equity,0)}</td>
      <td>$${f(Math.max(0,r.dist_usd),0)}</td>
      <td>${r.dist_bps>0?'−':''}${f(Math.abs(r.dist_bps),0)} bps</td>
      <td>${r.driver}</td>
      <td>回撤≥${r.thresholds.dd}% / 占用≥${r.thresholds.margin_pct}% / 可用&lt;$${r.thresholds.free} / 缓冲&lt;${r.thresholds.buffer}%</td>
      <td>${lastOf(r.level)}</td></tr>`).join('')+
    `<tr><td colspan="7">推送：飞书自定义机器人 webhook（每 5 分钟评估一次，同级 6 小时冷却，升级立即发）</td></tr>`;
  document.getElementById('risk').innerHTML=
    '<tr><th>指标</th><th>值</th><th>说明</th></tr>'+
    [['历史日波动', f(h.daily_std_bps,0)+' bps', `年化 ${f(h.daily_std_bps*Math.sqrt(365)/100,1)}%（45 天样本）`],
     ['历史最大回撤', f(h.hist_mdd_bps,0)+' bps', `区间 ${f(h.hist_min_bps,0)} ~ ${f(h.hist_max_bps,0)}`],
     ['历史单位风险', f(h.hist_mdd_bps/1e4*100,1)+'% / 每边名义', `L=${f(d.leverage,2)} → 权益 ${f(Math.abs(h.hist_mdd_bps)/1e4*d.per_side_notional/d.equity*100,0)}%`],
     ['OI-time 累计', f(d.oi_dollar_hours,0)+' $·h', `${f(d.oi_million_week_per_week,5)} $M·周/周`],
     ['保证金占用', f(d.margin_pct,0)+'%', `已用 $${f(d.margin_used,0)} / 权益 $${f(d.equity,0)}`]]
    .map(r=>`<tr><td>${r[0]}</td><td>${r[1]}</td><td>${r[2]}</td></tr>`).join('');
  draw(d.spread_curve||[]);
}
function draw(curve){
  if(!curve.length){return;}
  const w=1000,h=260,l=54,r=14,t=14,b=28;
  const ys=curve.map(c=>c[1]);const y0=Math.min(...ys,0),y1=Math.max(...ys,0);
  const px=i=>l+(w-l-r)*i/(curve.length-1);
  const py=v=>h-b-((v-y0)/((y1-y0)||1))*(h-t-b);
  let g='';for(let k=0;k<=4;k++){const v=y0+(y1-y0)*k/4;
    g+=`<line x1="${l}" y1="${py(v)}" x2="${w-r}" y2="${py(v)}" class="grid"/>
        <text x="${l-6}" y="${py(v)+4}" class="ax" text-anchor="end">${v.toFixed(0)}</text>`;}
  g+=`<line x1="${l}" y1="${py(0)}" x2="${w-r}" y2="${py(0)}" class="zero"/>`;
  const d=curve.map((c,i)=>(i?'L':'M')+px(i).toFixed(1)+' '+py(c[1]).toFixed(1)).join(' ');
  g+=`<path d="${d}" fill="none" stroke="#0f766e" stroke-width="2"/>`;
  g+=`<text x="${l}" y="${h-8}" class="ax">${new Date(curve[0][0]*1000).toISOString().slice(5,10)}</text>`;
  g+=`<text x="${w-r}" y="${h-8}" class="ax" text-anchor="end">${new Date(curve[curve.length-1][0]*1000).toISOString().slice(5,10)}</text>`;
  document.getElementById('chart').innerHTML=`<svg viewBox="0 0 ${w} ${h}">${g}</svg>`;
}
tick();setInterval(tick,30000);
</script></body></html>"""


def build_app() -> FastAPI:
    app = FastAPI(title="spread-basket", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(PAGE)

    @app.get("/api/snapshot")
    async def snapshot() -> dict:
        return SNAPSHOT

    return app


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    cfg = load_config(args.config)
    app = build_app()

    @app.on_event("startup")
    async def _start() -> None:
        asyncio.create_task(sampler(cfg))

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
