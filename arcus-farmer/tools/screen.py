#!/usr/bin/env python3
"""Rank Arcus markets for the maker grid using only public endpoints.

Nothing here needs an API key or trading permission, so it runs while the
perps waitlist is still pending.

    .venv/bin/python tools/screen.py --env mainnet --out reports

The scoring is deliberately transparent rather than clever.  Everything the
model assumes is printed in the report header so the numbers can be argued
with - the point is to shortlist, not to predict.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

DEFAULT_CANDIDATES = ["QQQ-USD", "NVDA-USD", "SPY-USD", "AAPL-USD", "AMZN-USD", "GOOGL-USD", "MSFT-USD", "GLD-USD"]

# Assumptions baked into the expected-value column.  Change them with the CLI
# flags; they are the honest part of the estimate.
DEFAULTS = {
    "our_notional": Decimal("200"),
    "tp_bps": Decimal("3"),
    "capture_rate": Decimal("0.5"),  # share of proportionally-entitled flow we win
    "avg_inventory_frac": Decimal("0.5"),  # fraction of the ladder held on average
}


@dataclass
class MarketScreener:
    symbol: str
    our_notional: Decimal = DEFAULTS["our_notional"]
    tp_bps: Decimal = DEFAULTS["tp_bps"]
    capture_rate: Decimal = DEFAULTS["capture_rate"]
    avg_inventory_frac: Decimal = DEFAULTS["avg_inventory_frac"]

    market: dict[str, Any] = field(default_factory=dict)
    trades: list[dict[str, Any]] = field(default_factory=list)
    funding: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    # ---------------------------------------------------------- derived
    @property
    def tick(self) -> Decimal:
        return Decimal(str(self.market.get("tickSize") or "0.01"))

    @property
    def volume_24h(self) -> Decimal:
        return Decimal(str(self.market.get("volume24hNotional") or 0))

    @property
    def trades_24h(self) -> int:
        return int(self.market.get("trades24h") or 0)

    @property
    def median_trade_notional(self) -> Decimal:
        notionals = sorted(
            (Decimal(str(t["price"])) * Decimal(str(t["size"])) for t in self.trades),
            key=lambda d: d,
        )
        if not notionals:
            return Decimal(0)
        return notionals[len(notionals) // 2]

    @property
    def price_cluster(self) -> tuple[Decimal, Decimal, Decimal]:
        """(high, low, spread) over the sampled tape - an upper bound on spread."""
        prices = [Decimal(str(t["price"])) for t in self.trades[:25]]
        if len(prices) < 2:
            return Decimal(0), Decimal(0), Decimal(0)
        hi, lo = max(prices), min(prices)
        return hi, lo, hi - lo

    @property
    def spread_bps(self) -> Decimal:
        hi, lo, spread = self.price_cluster
        mid = (hi + lo) / 2 if hi else Decimal(0)
        return spread / mid * 10000 if mid else Decimal(0)

    @property
    def spread_ticks(self) -> Decimal:
        _, _, spread = self.price_cluster
        return spread / self.tick if self.tick else Decimal(0)

    @property
    def tp_ticks(self) -> Decimal:
        price = Decimal(str(self.market.get("markPrice") or 1))
        return price * self.tp_bps / 10000 / self.tick if self.tick else Decimal(0)

    @property
    def funding_apr(self) -> Decimal:
        """Annualised funding, using the most recent hourly samples."""
        rates = [Decimal(str(f["fundingRate"])) for f in self.funding[:24]]
        if not rates:
            return Decimal(str(self.market.get("fundingRate") or 0)) * 24 * 365 * 100
        mean = sum(rates, Decimal(0)) / Decimal(len(rates))
        return mean * 24 * 365 * 100

    @property
    def trades_per_hour(self) -> Decimal:
        return Decimal(self.trades_24h) / 24

    @property
    def our_share(self) -> Decimal:
        med = self.median_trade_notional
        if med <= 0:
            return Decimal(0)
        return min(Decimal(1), self.our_notional / med)

    @property
    def rounds_per_hour(self) -> Decimal:
        return self.trades_per_hour * self.our_share * self.capture_rate

    @property
    def spread_gate_ok(self) -> bool:
        return self.spread_bps <= self.tp_bps / 2

    @property
    def ev_per_hour(self) -> Decimal:
        """USD per hour: gross spread capture minus funding on carried inventory."""
        gross = self.rounds_per_hour * self.our_notional * self.tp_bps / 10000
        inventory = self.our_notional * self.avg_inventory_frac * Decimal(5)  # max_orders=5
        funding = inventory * (self.funding_apr / 100) / (365 * 24)
        return gross - funding

    @property
    def score(self) -> float:
        """Rank score: expected value, punished for a wide spread and thin flow."""
        if not self.spread_gate_ok:
            return -1.0
        ev = float(self.ev_per_hour)
        depth_bonus = min(1.0, float(self.rounds_per_hour) / 60.0)
        return ev * (0.5 + 0.5 * depth_bonus)

    def row(self) -> dict[str, Any]:
        hi, lo, _ = self.price_cluster
        return {
            "symbol": self.symbol,
            "market_id": self.market.get("marketId"),
            "category": self.market.get("category"),
            "status": self.market.get("status"),
            "mark": self.market.get("markPrice"),
            "tick": str(self.tick),
            "volume_24h_usd": f"{self.volume_24h:,.0f}",
            "trades_24h": self.trades_24h,
            "median_trade_usd": f"{self.median_trade_notional:,.0f}",
            "spread_bps_proxy": f"{self.spread_bps:.2f}",
            "spread_ticks_proxy": f"{self.spread_ticks:.1f}",
            "tp_ticks": f"{self.tp_ticks:.1f}",
            "gate_ok": "yes" if self.spread_gate_ok else "NO",
            "our_share": f"{self.our_share:.3f}",
            "rounds_per_hour_est": f"{self.rounds_per_hour:.1f}",
            "funding_apr_pct": f"{self.funding_apr:.2f}",
            "off_hours_imf": self.market.get("offHoursInitialMarginFraction"),
            "ev_per_hour_usd": f"{self.ev_per_hour:.3f}",
            "score": f"{self.score:.3f}",
            "error": self.error,
        }


async def collect(client: httpx.AsyncClient, symbol: str, opts: dict[str, Decimal], market: dict[str, Any]) -> MarketScreener:
    s = MarketScreener(symbol=symbol, **{k: v for k, v in opts.items() if k in DEFAULTS})
    s.market = market
    try:
        trades = await client.get("/v1/trades", params={"market": symbol})
        s.trades = trades.json().get("trades", [])
    except Exception as exc:  # noqa: BLE001
        s.error = f"trades: {exc}"
    try:
        funding = await client.get("/v1/fundingRates", params={"market": symbol})
        s.funding = funding.json().get("fundingRates", [])
    except Exception as exc:  # noqa: BLE001
        s.error = (s.error + f" funding: {exc}").strip()
    return s


def render_html(rows: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    def cell(r: dict[str, Any], k: str) -> str:
        v = r.get(k, "")
        if k == "gate_ok":
            return f'<span class="{"ok" if v == "yes" else "bad"}">{v}</span>'
        if k == "ev_per_hour_usd" or k == "score":
            cls = "ok" if float(v) > 0 else "bad"
            return f'<span class="{cls}">{v}</span>'
        return str(v)

    cols = list(rows[0].keys()) if rows else []
    head = "".join(f"<th>{c}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{cell(r, c)}</td>" for c in cols) + "</tr>" for r in rows)
    top = rows[0]["symbol"] if rows else "—"
    assumptions = "".join(f"<li><code>{k}</code> = {v}</li>" for k, v in meta.items())
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Arcus 标的筛选</title><style>
body{{background:#0f1115;color:#e8eaed;font:14px/1.6 -apple-system,"PingFang SC",sans-serif;margin:0;padding:28px}}
h1{{font-size:22px;margin:0 0 4px}}.sub{{color:#9aa3b2;font-size:13px;margin-bottom:18px}}
.card{{background:#171a21;border:1px solid #2a2f3a;border-radius:11px;padding:14px 16px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}}
th,td{{padding:7px 8px;border-bottom:1px solid #2a2f3a;text-align:right;white-space:nowrap}}
th:first-child,td:first-child{{text-align:left}}
th{{color:#9aa3b2;font-size:11px;text-transform:uppercase}}
.ok{{color:#3ddc97}}.bad{{color:#ff6b6b}}
code{{background:#232833;padding:1px 6px;border-radius:5px}}
li{{font-size:13px;color:#9aa3b2}}
</style></head><body>
<h1>Arcus 标的筛选</h1>
<div class="sub">生成时间 {time.strftime('%Y-%m-%d %H:%M:%S')} · 推荐首选 <b>{top}</b> · 数据全部来自公开接口</div>
<div class="card"><b>模型假设</b><ul>{assumptions}</ul>
<div class="sub" style="margin:8px 0 0">
价差是「最近 25 笔成交的价格簇跨度」，是真实报价价差的<b>上界代理</b>；最终数值请在 WS 盘口上采样确认。<br>
硬门槛：<code>价差 bps ≤ 止盈 bps / 2</code>。轮次估计 = 市场成交笔数/小时 × 我方占流量比 × capture_rate。
</div></div>
<div class="card"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>
</body></html>"""


async def main_async(args: argparse.Namespace) -> int:
    base = "https://api.testnet.arcus.xyz" if args.env == "testnet" else "https://api.arcus.xyz"
    opts = {
        "our_notional": Decimal(str(args.our_notional)),
        "tp_bps": Decimal(str(args.tp_bps)),
        "capture_rate": Decimal(str(args.capture_rate)),
        "avg_inventory_frac": Decimal(str(args.avg_inventory_frac)),
    }
    candidates = [c.strip().upper() for c in (args.symbols or "").split(",") if c.strip()] or DEFAULT_CANDIDATES
    async with httpx.AsyncClient(base_url=base, timeout=30) as client:
        markets = (await client.get("/v1/markets")).json().get("markets", [])
        by_symbol = {m.get("marketDisplayName"): m for m in markets}
        states = []
        for sym in candidates:
            market = by_symbol.get(sym)
            if market is None:
                print(f"  skip {sym}: not listed on {args.env}")
                continue
            if str(market.get("status", "")).upper() != "ONLINE":
                print(f"  skip {sym}: status {market.get('status')}")
                continue
            states.append(await collect(client, sym, opts, market))

    states.sort(key=lambda s: s.score, reverse=True)
    rows = [s.row() for s in states]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    csv_path = out / f"screen-{args.env}-{stamp}.csv"
    html_path = out / f"screen-{args.env}-{stamp}.html"
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else [])
        writer.writeheader()
        writer.writerows(rows)
    meta = {
        "our_notional": f"${opts['our_notional']}",
        "tp_bps": f"{opts['tp_bps']} bps",
        "capture_rate": opts["capture_rate"],
        "avg_inventory_frac": opts["avg_inventory_frac"],
        "env": args.env,
    }
    html_path.write_text(render_html(rows, meta))

    print(f"{'symbol':10s} {'gate':5s} {'spread':>8s} {'tp_ticks':>9s} {'rounds/h':>9s} {'funding%':>9s} {'EV/h':>8s}")
    for r in rows:
        print(
            f"{r['symbol']:10s} {r['gate_ok']:5s} {r['spread_bps_proxy']:>8s} {r['tp_ticks']:>9s}"
            f" {r['rounds_per_hour_est']:>9s} {r['funding_apr_pct']:>9s} {r['ev_per_hour_usd']:>8s}"
        )
    print(f"\nCSV : {csv_path}\nHTML: {html_path}")
    if rows:
        print(f"推荐首选: {rows[0]['symbol']}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="screen Arcus markets for the maker grid")
    p.add_argument("--env", choices=["testnet", "mainnet"], default="mainnet")
    p.add_argument("--symbols", help="comma separated override of the candidate list")
    p.add_argument("--our-notional", default=str(DEFAULTS["our_notional"]))
    p.add_argument("--tp-bps", default=str(DEFAULTS["tp_bps"]))
    p.add_argument("--capture-rate", default=str(DEFAULTS["capture_rate"]))
    p.add_argument("--avg-inventory-frac", default=str(DEFAULTS["avg_inventory_frac"]))
    p.add_argument("--out", default="reports")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
