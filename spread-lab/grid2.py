#!/usr/bin/env python3
"""Grid feasibility study on the DRAM / MU+SKHY synthetic spread (fast version).

One unit = $1 long DRAM + $w_MU short MU + $w_SKHY short SKHY (dollar neutral,
$1 of notional per side).  The spread index is in bps of that $1 per-side basis.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from stats_lib import ewma, max_drawdown, mean, quantile, stdev

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
W_MU = 0.7059
W_SKHY = 0.2941
TAKER_BPS = 2.25      # Arcus base tier, per leg-trade


def load(sym, tf):
    rows = json.loads((DATA / f"candles_{sym}_{tf}.json").read_text())
    return {int(r["openTime"]) // 1_000_000: (float(r["high"]), float(r["low"]), float(r["close"]))
            for r in rows}


def prepare(tf):
    s = {x: load(x, tf) for x in ("DRAM-USD", "MU-USD", "SKHY-USD")}
    ts = sorted(set.intersection(*[set(v) for v in s.values()]))
    return _build(s, ts, tf)


def prepare_family(tf, syms):
    """Same alignment, but keeps every leg's OHLC so arbitrary weight vectors work."""
    s = {x: load(x, tf) for x in syms}
    ts = sorted(set.intersection(*[set(v) for v in s.values()]))
    return {
        "tf": tf, "ts": ts,
        "close": {k: [s[k][t][2] for t in ts] for k in s},
        "hi": {k: [s[k][t][0] for t in ts] for k in s},
        "lo": {k: [s[k][t][1] for t in ts] for k in s},
        "days": (ts[-1] - ts[0]) / 86400,
    }


def _build(s, ts, tf):
    x, hi, lo = [], [], []
    prev_level = None
    for t in ts:
        ld, lm, ls = s["DRAM-USD"][t], s["MU-USD"][t], s["SKHY-USD"][t]
        close = math.log(ld[2]) - W_MU * math.log(lm[2]) - W_SKHY * math.log(ls[2])
        h = math.log(ld[0]) - W_MU * math.log(lm[1]) - W_SKHY * math.log(ls[1])
        l = math.log(ld[1]) - W_MU * math.log(lm[0]) - W_SKHY * math.log(ls[0])
        if prev_level is None:
            x.append(0.0)
        else:
            x.append(x[-1] + (close - prev_level) * 1e4)
        prev_level = close
        hi.append(x[-1] + (h - close) * 1e4)
        lo.append(x[-1] + (l - close) * 1e4)
    return {"tf": tf, "ts": ts, "x": x, "hi": hi, "lo": lo,
            "days": (ts[-1] - ts[0]) / 86400}


def anchor_of(prep, halflife_bars):
    if halflife_bars is None:
        return [prep["x"][0]] * len(prep["x"])
    alpha = 1 - math.exp(-math.log(2) / halflife_bars)
    return ewma(prep["x"], alpha)


def simulate(prep, *, step, rungs, anchor, cost_bps_round, mode="natural"):
    """Rung grid on the synthetic spread.

    mode="natural": each bar is walked as open -> low -> high -> close on an up
        bar (and open -> high -> low -> close on a down bar), i.e. one reversal
        per bar.  This is the headline assumption.
    mode="close":   only the close-to-close move is walked, so nothing inside
        the bar can be harvested.  This is the pessimistic floor.
    mode="full":    both extremes are always visited in the profitable order.
        Upper bound only.
    """
    x, hi, lo = prep["x"], prep["hi"], prep["lo"]
    long_open: dict[int, float] = {}
    short_open: dict[int, float] = {}
    rounds = 0
    gross = cost = 0.0
    max_units = 0
    worst_mtm = 0.0
    worst_equity = 0.0
    equity_curve = []
    inventory_curve = []
    for i in range(1, len(x)):
        a, xi = anchor[i], x[i]
        h, l = hi[i], lo[i]
        o, c = x[i - 1], x[i]
        if mode == "close":
            path = (o, c)
        elif mode == "full":
            path = (o, l, h, c) if c >= o else (o, h, l, c)
        else:
            path = (o, l, h, c) if c >= o else (o, h, l, c)
        for p, q in zip(path, path[1:]):
            if q >= p:                                    # ---- move up
                for k in list(long_open):
                    if q >= long_open[k] + step:
                        rounds += 1
                        gross += step
                        cost += cost_bps_round
                        del long_open[k]
                for k in range(rungs):
                    if k not in short_open:
                        lvl = a + (k + 1) * step
                        if p <= lvl <= q:
                            short_open[k] = lvl
            else:                                          # ---- move down
                for k in list(short_open):
                    if q <= short_open[k] - step:
                        rounds += 1
                        gross += step
                        cost += cost_bps_round
                        del short_open[k]
                for k in range(rungs):
                    if k not in long_open:
                        lvl = a - (k + 1) * step
                        if q <= lvl <= p:
                            long_open[k] = lvl
        units = len(long_open) + len(short_open)
        if units > max_units:
            max_units = units
        mtm = sum(xi - e for e in long_open.values()) + sum(e - xi for e in short_open.values())
        if mtm < worst_mtm:
            worst_mtm = mtm
        equity = gross - cost + mtm
        if equity < worst_equity:
            worst_equity = equity
        equity_curve.append(equity)
        inventory_curve.append(units)
    days = prep["days"]
    return {
        "mode": mode,
        "step_bps": step, "rungs": rungs, "ladder_span_bps": step * rungs,
        "rounds": rounds, "rounds_per_day": rounds / days,
        "gross_bps_per_day": gross / days,
        "cost_bps_per_day": cost / days,
        "net_bps_per_day": (gross - cost) / days,
        "taker_net_bps_per_day": (gross - 4 * TAKER_BPS * rounds) / days,
        "max_open_units": max_units,
        "open_units_at_end": len(long_open) + len(short_open),
        "worst_open_mtm_bps": worst_mtm,
        "final_equity_bps": equity_curve[-1],
        "worst_equity_bps": worst_equity,
        "equity_curve": equity_curve,
        "inventory_curve": inventory_curve,
    }


def main():
    out = {"params": {"w_mu": W_MU, "w_skhy": W_SKHY, "taker_bps_per_leg": TAKER_BPS}}
    cost_maker = 4 * 0.5    # ~half a tick of slippage per leg-trade
    cost_taker = 4 * TAKER_BPS
    for tf, halflives, steps, rungsets in (
        ("1h", [None, 48, 120, 240, 720], [10, 20, 30, 40, 60, 100], [6, 12, 24, 48]),
        ("1m", [None, 2880, 10080], [10, 20, 30, 50, 100], [12, 24, 48]),
    ):
        prep = prepare(tf)
        block = {
            "bars": len(prep["ts"]), "days": prep["days"],
            "from": time.strftime("%Y-%m-%d %H:%M", time.gmtime(prep["ts"][0])),
            "to": time.strftime("%Y-%m-%d %H:%M", time.gmtime(prep["ts"][-1])),
            "index_std_bps": stdev(prep["x"]),
            "index_curve": prep["x"][::max(1, len(prep["x"]) // 800)],
            "sims": {}, "anchors": {},
        }
        for hl in halflives:
            name = "static" if hl is None else f"hl{hl}"
            anchor = anchor_of(prep, hl)
            dev = [prep["x"][i] - anchor[i] for i in range(len(anchor))]
            block["anchors"][name] = {
                "dev_std_bps": stdev(dev),
                "dev_p01": quantile(dev, 0.01), "dev_p99": quantile(dev, 0.99),
                "dev_min": min(dev), "dev_max": max(dev),
                "anchor_drift_bps_per_day": (anchor[-1] - anchor[0]) / prep["days"],
                "dev_curve": dev[::max(1, len(dev) // 800)],
            }
            for step in steps:
                for rungs in rungsets:
                    if step * rungs < 100:
                        continue
                    r = simulate(prep, step=step, rungs=rungs, anchor=anchor,
                                 cost_bps_round=cost_maker)
                    r_close = simulate(prep, step=step, rungs=rungs, anchor=anchor,
                                       cost_bps_round=cost_maker, mode="close")
                    r["close_only"] = {k: v for k, v in r_close.items() if k != "equity_curve"}
                    r_full = simulate(prep, step=step, rungs=rungs, anchor=anchor,
                                      cost_bps_round=cost_maker, mode="full")
                    r["full_intrabar"] = {k: v for k, v in r_full.items() if k != "equity_curve"}
                    r["taker_net_bps_per_day"] = (r["gross_bps_per_day"]
                                                  - cost_taker * r["rounds_per_day"])
                    r["equity_curve"] = r["equity_curve"][::max(1, len(r["equity_curve"]) // 800)]
                    block["sims"][f"{name}_step{step}_rungs{rungs}"] = r
        block["anchor_ts"] = prep["ts"][::max(1, len(prep["ts"]) // 800)]
        out[tf] = block
        print(f"[{tf}] bars={len(prep['ts'])} days={prep['days']:.1f} index std={stdev(prep['x']):.0f}bps",
              flush=True)
        ranked = sorted(block["sims"].items(), key=lambda kv: -kv[1]["net_bps_per_day"])[:8]
        for k, v in ranked:
            co, fu = v["close_only"], v["full_intrabar"]
            print(f"   {k:26s} natural: {v['rounds_per_day']:7.2f}r/d net={v['net_bps_per_day']:7.1f} "
                  f"taker={v['taker_net_bps_per_day']:7.1f} units={v['max_open_units']:3.0f} "
                  f"worstMTM={v['worst_open_mtm_bps']:7.0f} | "
                  f"close: {co['rounds_per_day']:6.2f}r/d net={co['net_bps_per_day']:7.1f} | "
                  f"full: {fu['rounds_per_day']:7.2f}r/d net={fu['net_bps_per_day']:8.1f}",
                  flush=True)
    (HERE / "grid_results.json").write_text(json.dumps(out, indent=1, default=str))
    print("wrote grid_results.json")


if __name__ == "__main__":
    main()
