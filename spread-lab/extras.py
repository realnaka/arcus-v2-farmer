#!/usr/bin/env python3
"""Out-of-sample stability, capacity and tick math for the memory spread."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from stats_lib import OLS, adf, corr, half_life, hurst, mean, stdev

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"


def load(sym, tf):
    rows = json.loads((DATA / f"candles_{sym}_{tf}.json").read_text())
    return {int(r["openTime"]) // 1_000_000: r for r in rows}


def align(tf, syms):
    s = {x: load(x, tf) for x in syms}
    ts = sorted(set.intersection(*[set(v) for v in s.values()]))
    return ts, s


def meta(sym):
    ms = json.loads((Path(__file__).resolve().parent / "data" / "markets.json").read_text())["markets"]
    for m in ms:
        if m["marketDisplayName"] == sym:
            return m
    raise KeyError(sym)


def main():
    out = {}
    tf = "1h"
    ts, s = align(tf, ["DRAM-USD", "MU-USD", "SKHY-USD", "SNDK-USD"])
    close = {k: [float(s[k][t]["close"]) for t in ts] for k in s}
    logc = {k: [math.log(v) for v in close[k]] for k in close}
    dr = [logc["DRAM-USD"][i] - logc["DRAM-USD"][i - 1] for i in range(1, len(ts))]
    mr = [logc["MU-USD"][i] - logc["MU-USD"][i - 1] for i in range(1, len(ts))]
    sr = [logc["SKHY-USD"][i] - logc["SKHY-USD"][i - 1] for i in range(1, len(ts))]

    # ---------------------------------------------------------------- OOS split
    cut = int(len(ts) * 0.6)
    x_in = [[1.0, mr[i], sr[i]] for i in range(cut)]
    fit_in = OLS(x_in, dr[:cut])
    x_out = [[1.0, mr[i], sr[i]] for i in range(cut, len(dr))]
    fit_out = OLS(x_out, dr[cut:])
    out["beta_stability"] = {
        "in_sample": {"from": time.strftime("%Y-%m-%d", time.gmtime(ts[0])),
                      "to": time.strftime("%Y-%m-%d", time.gmtime(ts[cut])),
                      "beta_MU": fit_in.beta[1], "beta_SKHY": fit_in.beta[2], "r2": fit_in.r2},
        "out_sample": {"from": time.strftime("%Y-%m-%d", time.gmtime(ts[cut])),
                       "to": time.strftime("%Y-%m-%d", time.gmtime(ts[-1])),
                       "beta_MU": fit_out.beta[1], "beta_SKHY": fit_out.beta[2], "r2": fit_out.r2},
        "beta_MU_drift": fit_out.beta[1] - fit_in.beta[1],
        "beta_SKHY_drift": fit_out.beta[2] - fit_in.beta[2],
    }

    # fixed in-sample betas applied out of sample vs refit
    b_mu, b_s = fit_in.beta[1], fit_in.beta[2]
    fixed = [logc["DRAM-USD"][i] - b_mu * logc["MU-USD"][i] - b_s * logc["SKHY-USD"][i]
             for i in range(cut, len(ts))]
    refit = [logc["DRAM-USD"][i] - fit_out.beta[1] * logc["MU-USD"][i]
             - fit_out.beta[2] * logc["SKHY-USD"][i] for i in range(cut, len(ts))]

    def block(y, name):
        d = [y[i] - y[i - 1] for i in range(1, len(y))]
        return {"name": name, "level_std_bps": stdev(y) * 1e4,
                "bar_std_bps": stdev(d) * 1e4,
                "adf_t": adf(y)["t_stat"],
                "half_life_h": half_life(y)["half_life_bars"],
                "hurst": hurst(y),
                "mean_drift_bps_per_day": (y[-1] - y[0]) / ((ts[-1] - ts[cut]) / 86400) * 1e4}

    out["oos_spread"] = [block(fixed, "betas frozen at in-sample values"),
                         block(refit, "betas refit on the test window")]

    # ---------------------------------------------------------------- sub-periods
    sub = []
    step = len(ts) // 6
    for k in range(6):
        a, b = k * step, min((k + 1) * step, len(ts) - 1)
        if b - a < 24:
            continue
        seg = slice(a, b + 1)
        y = [logc["DRAM-USD"][i] - b_mu * logc["MU-USD"][i] - b_s * logc["SKHY-USD"][i]
             for i in range(a, b + 1)]
        sub.append({
            "from": time.strftime("%m-%d", time.gmtime(ts[a])),
            "to": time.strftime("%m-%d", time.gmtime(ts[b])),
            "dram_pct": (close["DRAM-USD"][b] / close["DRAM-USD"][a] - 1) * 100,
            "mu_pct": (close["MU-USD"][b] / close["MU-USD"][a] - 1) * 100,
            "skhy_pct": (close["SKHY-USD"][b] / close["SKHY-USD"][a] - 1) * 100,
            "sndk_pct": (close["SNDK-USD"][b] / close["SNDK-USD"][a] - 1) * 100,
            "spread_bps": (y[-1] - y[0]) * 1e4,
            "spread_level_bps": y[-1] * 1e4,
            "spread_std_bps": stdev(y) * 1e4,
        })
    out["sub_periods"] = sub

    # ---------------------------------------------------------------- 3-leg hedge
    xn = [[1.0, mr[i - 1], sr[i - 1], (logc["SNDK-USD"][i] - logc["SNDK-USD"][i - 1])]
          for i in range(1, len(ts))]
    fit3 = OLS(xn, dr)
    b3 = fit3.beta
    y3 = [logc["DRAM-USD"][i] - b3[1] * logc["MU-USD"][i] - b3[2] * logc["SKHY-USD"][i]
          - b3[3] * logc["SNDK-USD"][i] for i in range(len(ts))]
    out["three_leg"] = {
        "betas": {"MU": b3[1], "SKHY": b3[2], "SNDK": b3[3]},
        "r2": fit3.r2,
        "spread_std_bps": stdev(y3) * 1e4,
        "adf_t": adf(y3)["t_stat"],
        "half_life_h": half_life(y3)["half_life_bars"],
        "hurst": hurst(y3),
        "drift_bps_per_day": (y3[-1] - y3[0]) / ((ts[-1] - ts[0]) / 86400) * 1e4,
    }

    # ---------------------------------------------------------------- costs / capacity
    tick = {}
    for sym in ("DRAM-USD", "MU-USD", "SKHY-USD", "SNDK-USD"):
        m = meta(sym)
        px = float(m["markPrice"])
        tick[sym] = {
            "mark": px, "tickSize": float(m["tickSize"]),
            "tick_bps": float(m["tickSize"]) / px * 1e4,
            "volume24h_notional": float(m["volume24hNotional"]),
            "trades24h": int(m["trades24h"]),
            "oi_notional": float(m["openInterest"]) * px,
            "min_order_notional": float(m["minOrderNotional"]),
            "imf": float(m["initialMarginFraction"]),
            "off_hours_imf": float(m["offHoursInitialMarginFraction"]),
            "funding_apr_pct": float(m["fundingRate"]) * 24 * 365 * 100,
        }
    out["markets"] = tick

    capacity = []
    for unit_usd in (50, 100, 250, 500, 1000):
        for rounds_per_day in (4, 8, 16):
            turnover = 2 * unit_usd * rounds_per_day          # open + close
            capacity.append({
                "unit_usd_per_leg": unit_usd, "rounds_per_day": rounds_per_day,
                "turnover_usd_per_day": turnover,
                "share_of_DRAM_volume": 100 * turnover / tick["DRAM-USD"]["volume24h_notional"],
                "share_of_SKHY_volume": 100 * turnover / tick["SKHY-USD"]["volume24h_notional"],
                "max_inventory_usd": unit_usd * 24,
                "margin_at_10pct_usd": unit_usd * 24 * 2 * 0.10,
            })
    out["capacity"] = capacity

    # ---------------------------------------------------------------- session split
    sess = {"us_cash_1330_2000": 0.0, "other": 0.0}
    vol_by_hour = {}
    for k in ("DRAM-USD", "SKHY-USD"):
        rows = json.loads((DATA / f"candles_{k}_1m.json").read_text())
        tot = us = 0.0
        for r in rows:
            t = int(r["openTime"]) // 1_000_000
            h = (t % 86400) / 3600
            v = float(r["notionalVolume"])
            tot += v
            if 13.5 <= h < 20.0:
                us += v
            vol_by_hour[k] = vol_by_hour.get(k, {})
        sess[k] = {"us_cash_share_pct": 100 * us / tot, "total_notional": tot}
    out["session"] = sess
    out["total_variation"] = {}
    for tf2 in ("1m", "1h"):
        ts2, s2 = align(tf2, ["DRAM-USD", "MU-USD", "SKHY-USD"])
        c = [math.log(float(s2["DRAM-USD"][t]["close"])) - b_mu * math.log(float(s2["MU-USD"][t]["close"]))
             - b_s * math.log(float(s2["SKHY-USD"][t]["close"])) for t in ts2]
        tv = sum(abs(c[i] - c[i - 1]) for i in range(1, len(c))) * 1e4
        days = (ts2[-1] - ts2[0]) / 86400
        out["total_variation"][tf2] = {"tv_bps_per_day": tv / days, "grid_ceiling_bps_per_day": tv / 2 / days}

    (HERE / "extras.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out["beta_stability"], indent=1, default=str))
    print(json.dumps(out["oos_spread"], indent=1, default=str))
    print(json.dumps(out["three_leg"], indent=1, default=str))
    print(json.dumps(out["total_variation"], indent=1, default=str))
    print(json.dumps(out["session"], indent=1, default=str))
    for r in sub:
        print(f"{r['from']}->{r['to']} DRAM {r['dram_pct']:6.1f}% MU {r['mu_pct']:6.1f}% "
              f"SKHY {r['skhy_pct']:6.1f}% SNDK {r['sndk_pct']:6.1f}% | spread {r['spread_bps']:7.0f}bps "
              f"(level {r['spread_level_bps']:7.0f}) std {r['spread_std_bps']:5.0f}bps")
    print(json.dumps(tick["DRAM-USD"], indent=1))


if __name__ == "__main__":
    main()
