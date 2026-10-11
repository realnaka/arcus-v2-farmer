#!/usr/bin/env python3
"""Volatility / leverage study for the basket spread (口径 B), plus the
persistence test behind the 'laggards catch up' hypothesis.

口径 B = long $1 of MU/SKHY/SNDK at their DRAM weights (normalised) / short $1 DRAM.
All numbers are in bps of the per-side notional unless stated otherwise.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from pathlib import Path

from stats_lib import OLS, corr, mean, quantile, stdev
import grid2
from points import W, spread_series

HERE = Path(__file__).resolve().parent


def day_changes(ts, x, hours=24):
    step = int(hours)
    return [(x[i] - x[i - step], ts[i]) for i in range(step, len(x))]


def vol_profile(name, ts, x):
    h = [x[i] - x[i - 1] for i in range(1, len(x))]
    d = [v for v, _ in day_changes(ts, x)]
    w = [x[i] - x[i - 168] for i in range(168, len(x))]
    peak, mdd = x[0], 0.0
    for v in x:
        peak = max(peak, v)
        mdd = min(mdd, v - peak)
    return {
        "name": name,
        "hour_std_bps": stdev(h),
        "daily_std_bps": stdev(d),
        "daily_std_pct": stdev(d) / 100,
        "ann_vol_pct": stdev(d) * math.sqrt(365) / 100,
        "hour_ann_vol_pct": stdev(h) * math.sqrt(24 * 365) / 100,
        "mean_abs_daily_bps": mean([abs(v) for v in d]),
        "max_daily_up_bps": max(d), "max_daily_down_bps": min(d),
        "daily_p05_bps": quantile(d, 0.05), "daily_p95_bps": quantile(d, 0.95),
        "max_3d_down_bps": min(v for v, _ in day_changes(ts, x, 72)),
        "max_5d_down_bps": min(v for v, _ in day_changes(ts, x, 120)),
        "max_dd_bps": mdd,
        "cum_bps": x[-1],
    }


def main():
    out = {}
    prep = grid2.prepare_family("1h", ["DRAM-USD", "MU-USD", "SKHY-USD", "SNDK-USD"])
    ts = prep["ts"]
    L = {k: [math.log(v) * 1e4 for v in prep["close"][k]] for k in prep["close"]}
    for k in L:
        L[k] = [v - L[k][0] for v in L[k]]
    sp = spread_series(prep)
    basket = [sum(W[k] * L[k][i] for k in W) for i in range(len(ts))]

    prof = {}
    for sym, label in (("DRAM-USD", "DRAM（ETF 本身）"), ("MU-USD", "MU"),
                       ("SKHY-USD", "SKHY"), ("SNDK-USD", "SNDK")):
        prof[label] = vol_profile(label, ts, L[sym])
    prof["三条腿组合（等权，未对冲）"] = vol_profile("basket", ts, basket)
    prof["口径 B spread"] = vol_profile("spread", ts, sp["x"])
    # worst peak-to-trough excursion using intrabar highs/lows
    peak, worst_hl, worst_at = sp["hi"][0], 0.0, 0
    for i in range(len(sp["x"])):
        peak = max(peak, sp["hi"][i])
        exc = sp["lo"][i] - peak
        if exc < worst_hl:
            worst_hl, worst_at = exc, i
    out["intrabar"] = {"max_excursion_bps": worst_hl,
                       "at": time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts[worst_at]))}
    # largest adverse excursion inside one calendar day
    daily_exc = []
    for start in range(0, len(ts), 24):
        seg_hi, seg_lo = sp["hi"][start:start + 24], sp["lo"][start:start + 24]
        if not seg_hi:
            continue
        p = seg_hi[0]
        w = 0.0
        for h, l in zip(seg_hi, seg_lo):
            p = max(p, h)
            w = min(w, l - p)
        daily_exc.append(w)
    out["intrabar"]["worst_intraday_excursion_bps"] = min(daily_exc)
    out["intrabar"]["mean_intraday_excursion_bps"] = mean(daily_exc)
    out["vol_profile"] = prof

    d_spread = [v for v, _ in day_changes(ts, sp["x"])]
    d_dram = [v for v, _ in day_changes(ts, L["DRAM-USD"])]
    out["vol_ratio"] = {
        "daily_std_ratio_spread_over_dram": stdev(d_spread) / stdev(d_dram),
        "hourly_std_ratio_spread_over_dram": prof["口径 B spread"]["hour_std_bps"] / prof["DRAM（ETF 本身）"]["hour_std_bps"],
        "corr_daily_spread_vs_dram": corr(d_spread, d_dram),
    }

    # ------------------------------------------------------------ persistence
    x = sp["x"]
    win = 168
    z = [None] * len(x)
    for i in range(win - 1, len(x)):
        seg = x[i - win + 1:i + 1]
        z[i] = (x[i] - mean(seg)) / stdev(seg)
    out["current"] = {
        "spread_level_bps": x[-1],
        "z_7d": z[-1],
        "trailing_7d_bps": x[-1] - x[-1 - win],
        "trailing_30d_bps": x[-1] - x[-1 - 720] if len(x) > 720 else None,
        "last": time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts[-1])),
    }
    pred = {}
    for h in (24, 72, 168):
        xs, ys = [], []
        for i in range(win, len(x) - h):
            xs.append([1.0, z[i]])
            ys.append(x[i + h] - x[i])
        fit = OLS(xs, ys)
        pred[f"{h}h"] = {"slope_bps_per_sigma": fit.beta[1], "t": fit.tstat(1),
                         "r2": fit.r2, "n": fit.n}
    out["z_predictive"] = pred
    # trailing 20-day momentum -> next 20-day (non overlapping)
    mom = []
    step = 480
    i = 480
    while i + 480 < len(x):
        mom.append((x[i] - x[i - 480], x[i + 480] - x[i]))
        i += 480
    if len(mom) > 3:
        fit = OLS([[1.0, a] for a, _ in mom], [b for _, b in mom])
        out["momentum_20d"] = {"slope": fit.beta[1], "t": fit.tstat(1), "n": fit.n,
                               "corr": corr([a for a, _ in mom], [b for _, b in mom])}

    # ------------------------------------------------------------ leverage
    spread_dd = -prof["口径 B spread"]["max_dd_bps"] / 1e4      # fraction of side notional
    deep_dd = max(spread_dd, -out["intrabar"]["max_excursion_bps"] / 1e4)
    worst_day = -prof["口径 B spread"]["max_daily_down_bps"] / 1e4
    deep_day = max(worst_day, -out["intrabar"]["worst_intraday_excursion_bps"] / 1e4)
    sigma3 = 3 * prof["口径 B spread"]["daily_std_bps"] / 1e4
    rows = []
    for lev in (0.8, 1, 1.5, 2, 2.5, 3):
        rows.append({
            "leverage_side_per_equity": lev,
            "gross_per_equity": 2 * lev,
            "worst_day_pct_of_equity": 100 * worst_day * lev,
            "worst_intraday_pct_of_equity": 100 * deep_day * lev,
            "three_sigma_day_pct_of_equity": 100 * sigma3 * lev,
            "max_dd_pct_of_equity": 100 * spread_dd * lev,
            "max_dd_intrabar_pct_of_equity": 100 * deep_dd * lev,
            "margin_used_pct_offhours": 100 * 0.15 * 2 * lev / 1.0,
        })
    out["leverage"] = {
        "spread_max_dd_pct_of_side": 100 * spread_dd,
        "spread_max_dd_intrabar_pct_of_side": 100 * deep_dd,
        "spread_worst_day_pct_of_side": 100 * worst_day,
        "spread_worst_intraday_pct_of_side": 100 * deep_day,
        "spread_daily_sigma_pct": 100 * prof["口径 B spread"]["daily_std_bps"] / 100 / 100,
        "rows": rows,
        "note": "margin = 15% off-hours IMF on both legs (gross = 2x side notional)",
    }
    (HERE / "risk.json").write_text(json.dumps(out, indent=1, default=str))

    print(f"{'series':28s} {'hr σ':>7s} {'day σ':>7s} {'ann%':>7s} {'mean|d|':>8s} "
          f"{'max↑':>7s} {'max↓':>7s} {'maxDD':>7s}")
    for k, v in prof.items():
        print(f"{k:28s} {v['hour_std_bps']:7.1f} {v['daily_std_bps']:7.1f} {v['ann_vol_pct']:7.1f} "
              f"{v['mean_abs_daily_bps']:8.1f} {v['max_daily_up_bps']:7.0f} {v['max_daily_down_bps']:7.0f} "
              f"{v['max_dd_bps']:7.0f}")
    print("ratios:", json.dumps(out["vol_ratio"], indent=1))
    print("current:", json.dumps(out["current"], indent=1))
    print("z predictive:", json.dumps(pred, indent=1))
    print("momentum:", json.dumps(out.get("momentum_20d"), indent=1))
    print("leverage:")
    for r in rows:
        print("  L=%-4s gross=%-4s day %5.1f%% | intraday %5.1f%% | 3sig %5.1f%% | maxDD %5.1f%% | "
              "DDintrabar %5.1f%% | margin %5.0f%%"
              % (r["leverage_side_per_equity"], r["gross_per_equity"],
                 r["worst_day_pct_of_equity"], r["worst_intraday_pct_of_equity"],
                 r["three_sigma_day_pct_of_equity"], r["max_dd_pct_of_equity"],
                 r["max_dd_intrabar_pct_of_equity"], r["margin_used_pct_offhours"]))


if __name__ == "__main__":
    main()
