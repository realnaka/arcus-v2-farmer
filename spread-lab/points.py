#!/usr/bin/env python3
"""Spread statistics + Arcus points model: hold the basket spread, or grid it?

Points model from arcus-farmer/PLAN-POINTS.md (Season 1 Week 1 evidence):
    points ~= OI_time * efficiency,  efficiency ~ 1,000-4,000 pts per $M-week
    of open interest, with a multiplier for RWA held outside US regular hours.
Volume is a weak path on its own (a $5.49M-volume account scored 20 pts).
"""

from __future__ import annotations

import datetime as dt
import json
import math
import statistics
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from stats_lib import OLS, adf, corr, half_life, hurst, mean, quantile, stdev, variance_ratios
import grid2

HERE = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
W = {"MU-USD": 0.2736 / 0.5414, "SKHY-USD": 0.2213 / 0.5414, "SNDK-USD": 0.0465 / 0.5414}
EFF_LOW, EFF_HIGH = 1000.0, 4000.0        # pts per $M-week of OI
VOL_LOW, VOL_HIGH = 5.0, 50.0              # pts per $M of volume (weak path, range from samples)


def is_bonus(ts_utc: int) -> bool:
    """ET 20:00-04:00 on weekdays, plus all weekend (= Arcus bonus window)."""
    t = dt.datetime.fromtimestamp(ts_utc, ET)
    if t.weekday() >= 5:
        return True
    h = t.hour + t.minute / 60
    return h >= 20 or h < 4


def spread_series(prep):
    """long $1 basket / short $1 DRAM -> index in bps of the $1-per-side basis."""
    x, hi, lo = [], [], []
    for i in range(len(prep["ts"])):
        c = sum(W[k] * math.log(prep["close"][k][i]) for k in W) - math.log(prep["close"]["DRAM-USD"][i])
        h = sum(W[k] * math.log(prep["hi"][k][i]) for k in W) - math.log(prep["lo"]["DRAM-USD"][i])
        l = sum(W[k] * math.log(prep["lo"][k][i]) for k in W) - math.log(prep["hi"]["DRAM-USD"][i])
        x.append((c - x[0] if x else 0.0) * 1e4 if x else 0.0)
        hi.append(0.0)
        lo.append(0.0)
        if len(x) > 1:
            x[-1] = x[-2] + (c - prev_c) * 1e4
        prev_c = c
        hi[-1] = x[-1] + (h - c) * 1e4
        lo[-1] = x[-1] + (l - c) * 1e4
    return {"ts": prep["ts"], "x": x, "hi": hi, "lo": lo, "days": prep["days"],
            "close": prep["close"]}


def stats_block(s, label, *, stride=1, lags=None):
    x = s["x"][::stride]
    d = [x[i] - x[i - 1] for i in range(1, len(x))]
    per_day = 24
    hl = half_life(x)
    daily = [x[i] - x[i - per_day] for i in range(per_day, len(x))]
    m, sd = mean(d), stdev(d)
    ac = [corr(d[:-k], d[k:]) for k in (1, 2, 6, 24)]
    # market neutrality: regress spread returns on SPY / QQQ
    return {
        "label": label, "bars": len(x),
        "cum_bps": x[-1], "drift_bps_per_day": x[-1] / s["days"],
        "hour_std_bps": sd, "ann_vol_pct": sd * math.sqrt(24 * 365) / 100,
        "t_stat_of_mean": m / (sd / math.sqrt(len(d))),
        "sharpe": (m / sd) * math.sqrt(24 * 365),
        "skew": sum(((v - m) / sd) ** 3 for v in d) / len(d),
        "excess_kurtosis": sum(((v - m) / sd) ** 4 for v in d) / len(d) - 3,
        "autocorr": {"1h": ac[0], "2h": ac[1], "6h": ac[2], "24h": ac[3]},
        "vr": variance_ratios(x, (2, 4, 8, 24, 72, 168)),
        "hurst": hurst(x),
        "adf_full": adf(x, lags=lags, label="spread"),
        "half_life_h": hl["half_life_bars"],
        "daily_std_bps": stdev(daily),
        "max_dd_bps": min(x[i] - max(x[:i + 1]) for i in range(len(x))),
        "min_bps": min(x), "max_bps": max(x),
    }


def oi_time_stats(ts, gross_notional_by_bar):
    """Integral of gross open interest over time, plus the bonus-window share."""
    total = bonus = 0.0
    for i in range(1, len(ts)):
        hours = (ts[i] - ts[i - 1]) / 3600.0
        notional = gross_notional_by_bar[i]
        total += notional * hours
        if is_bonus(ts[i]):
            bonus += notional * hours
    days = (ts[-1] - ts[0]) / 86400
    per_week_dollar_hours = total / days * 7
    # 1 "$M-week" = $1M held for a week = 1e6 * 168 dollar-hours
    return {
        "oi_dollar_hours": total,
        "oi_million_week_per_week": per_week_dollar_hours / 1e6 / 168,
        "oi_dollar_hours_per_week": per_week_dollar_hours,
        "bonus_share_pct": 100 * bonus / total if total else 0.0,
        "days": days,
    }


def main():
    out = {}
    prep1 = grid2.prepare_family("1m", ["DRAM-USD", "MU-USD", "SKHY-USD", "SNDK-USD"])
    prep = grid2.prepare_family("1h", ["DRAM-USD", "MU-USD", "SKHY-USD", "SNDK-USD"])
    s1 = spread_series(prep1)
    sh = spread_series(prep)

    # ---------------------------------------------------------------- statistics
    out["stats_1h"] = stats_block(sh, "long $1 MU/SKHY/SNDK basket / short $1 DRAM (1h)")
    out["stats_1m"] = stats_block(s1, "same (1m)", stride=15, lags=12)
    # correlations of the spread return with each leg and with the market
    r_spread = [sh["x"][i] - sh["x"][i - 1] for i in range(1, len(sh["x"]))]
    ts = sh["ts"]
    out["corr_with_legs"] = {}
    for sym in ("DRAM-USD", "MU-USD", "SKHY-USD", "SNDK-USD", "SPY-USD", "QQQ-USD"):
        try:
            c = grid2.load(sym, "1h")
        except Exception:  # noqa: BLE001
            continue
        r = [math.log(c[ts[i]][2] / c[ts[i - 1]][2]) for i in range(1, len(ts))]
        out["corr_with_legs"][sym] = corr(r_spread, r)
    # sub-period stability
    step = len(ts) // 6
    sub = []
    for k in range(6):
        a, b = k * step, min((k + 1) * step, len(ts) - 1)
        if b - a < 24:
            continue
        seg = sh["x"][a:b + 1]
        d = [seg[i] - seg[i - 1] for i in range(1, len(seg))]
        sub.append({"from": time.strftime("%m-%d", time.gmtime(ts[a])),
                    "to": time.strftime("%m-%d", time.gmtime(ts[b])),
                    "ret_bps": seg[-1] - seg[0], "std_bps": stdev(d),
                    "ann_vol_pct": stdev(d) * math.sqrt(24 * 365) / 100})
    out["sub_periods"] = sub

    # ---------------------------------------------------------------- points
    # One unit of capital = S dollars of notional PER SIDE (gross = 2S across the
    # two sides of the spread).  The ladder may hold `rungs` long AND `rungs`
    # short rungs, so sizing each rung at S/(2*rungs) makes the *maximum*
    # deployment equal to a flat 2S position: the two are capital-budget
    # matched and only differ in how much of the budget is working.
    S = 1000.0
    rungs = 12
    rung_side = S / (2 * rungs)
    hold = oi_time_stats(s1["ts"], [2 * S] * len(s1["ts"]))
    anchor = grid2.anchor_of(s1, 2880)

    variants = {}
    for step_bps in (10, 20, 30, 50):
        gs = grid2.simulate(s1, step=step_bps, rungs=rungs, anchor=anchor,
                            cost_bps_round=2.0, mode="close")
        inv = gs["inventory_curve"]
        gross_path = [0.0] + [v * rung_side * 2 for v in inv]
        oi = oi_time_stats(s1["ts"], gross_path)
        rounds_per_day = gs["rounds_per_day"]
        volume_week = 4 * rung_side * rounds_per_day * 7      # 2 legs x 2 sides
        income_day = rounds_per_day * (step_bps / 1e4) * rung_side
        variants[f"step{step_bps}"] = {
            **oi,
            "avg_deployed_side_usd": mean(inv) * rung_side,
            "deployed_pct_of_ladder": 100 * mean(inv) / (2 * rungs),
            "rounds_per_day": rounds_per_day,
            "volume_per_week_usd": volume_week,
            "points_oi_low": oi["oi_million_week_per_week"] * EFF_LOW,
            "points_oi_high": oi["oi_million_week_per_week"] * EFF_HIGH,
            "points_volume_low": volume_week / 1e6 * VOL_LOW,
            "points_volume_high": volume_week / 1e6 * VOL_HIGH,
            "income_usd_per_day": income_day,
            "income_bps_per_day_of_ladder": income_day / S * 1e4,
            "worst_open_mtm_bps": gs["worst_open_mtm_bps"],
        }
        if step_bps == 20:
            k = max(1, len(inv) // 600)
            out["chart"] = {
                "ts": [time.strftime("%m-%d", time.gmtime(t)) for t in s1["ts"][::k]],
                "spread": s1["x"][::k],
                "inventory_pct": [100 * v / rungs for v in inv[::k]],
                "anchor": [grid2.anchor_of(s1, 2880)[i] for i in range(0, len(s1["x"]), k)],
            }
    out["points"] = {
        "S_side_usd": S, "rungs": rungs, "gross_notional_usd": 2 * S,
        "hold": {**hold,
                 "avg_deployed_side_usd": S,
                 "deployed_pct_of_ladder": 100.0,
                 "points_oi_low": hold["oi_million_week_per_week"] * EFF_LOW,
                 "points_oi_high": hold["oi_million_week_per_week"] * EFF_HIGH,
                 "volume_per_week_usd": 0.0,
                 "income_usd_per_day": out["stats_1h"]["drift_bps_per_day"] / 1e4 * S,
                 "income_bps_per_day_of_ladder": out["stats_1h"]["drift_bps_per_day"]},
        "grids": variants,
        "efficiency_pts_per_million_week": [EFF_LOW, EFF_HIGH],
        "volume_pts_per_million": [VOL_LOW, VOL_HIGH],
    }
    # per-account table at several capital levels
    rows = []
    for equity, leverage in ((1700, 1.0), (1700, 2.0), (1700, 3.0), (1700, 4.0),
                             (10000, 1.0), (10000, 2.0), (50000, 1.0), (50000, 2.0)):
        side = equity * 0.8 * leverage
        oi_mw = 2 * side * 168 / 1e6 / 168          # gross OI held one week
        rows.append({"equity": equity, "leverage": leverage, "side_notional": side,
                     "gross_notional": 2 * side,
                     "max_dd_usd": -out["stats_1h"]["max_dd_bps"] / 1e4 * side,
                     "max_dd_pct_of_equity": -out["stats_1h"]["max_dd_bps"] / 1e4 * side / equity * 100,
                     "hold_pts_low": oi_mw * EFF_LOW, "hold_pts_high": oi_mw * EFF_HIGH})
    out["capital_table"] = rows
    (HERE / "points_stats.json").write_text(json.dumps(out, indent=1, default=str))

    st = out["stats_1h"]
    print(f"spread 1h: cum {st['cum_bps']:.0f}bps drift {st['drift_bps_per_day']:.1f}/d "
          f"vol {st['hour_std_bps']:.1f}bps/h ann {st['ann_vol_pct']:.1f}% sharpe {st['sharpe']:.2f} "
          f"skew {st['skew']:.2f} kurt {st['excess_kurtosis']:.1f}")
    print(f"  ADF t={st['adf_full']['t_stat']:.2f} hl={st['half_life_h']:.0f}h hurst={st['hurst']:.2f} "
          f"VR {({k: round(v,2) for k,v in st['vr'].items()})}")
    print(f"  autocorr {({k: round(v,3) for k,v in st['autocorr'].items()})} maxDD {st['max_dd_bps']:.0f}")
    print("  corr:", {k: round(v, 2) for k, v in out["corr_with_legs"].items()})
    p = out["points"]
    r5 = lambda d: {k: (round(v, 5) if isinstance(v, float) else v) for k, v in d.items()}
    print("HOLD:", json.dumps(r5(p["hold"]), indent=1))
    for k, v in p["grids"].items():
        print(f"GRID {k}:", json.dumps(r5(v)))
    print("capital table:", json.dumps(rows, indent=1)[:600])


if __name__ == "__main__":
    main()
