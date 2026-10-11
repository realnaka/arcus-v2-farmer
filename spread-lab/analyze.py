#!/usr/bin/env python3
"""Is DRAM - (MU + SKHY) a range-bound spread worth grid trading?

Reads data/candles_<SYM>_{1h,1m}.json (Arcus public candles) and writes
results.json + a printed summary.  Everything is in pure python.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from pathlib import Path

from stats_lib import (OLS, adf, corr, ewm_std, ewma, half_life, hurst, max_drawdown,
                       mean, quantile, rolling, stdev, variance_ratios)

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
TF_MINUTES = {"1m": 1, "1h": 60}


def load(sym, tf):
    rows = json.loads((DATA / f"candles_{sym}_{tf}.json").read_text())
    out = {}
    for r in rows:
        out[int(r["openTime"]) // 1_000_000] = {
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "vol": float(r.get("notionalVolume") or 0),
            "trades": int(r.get("tradeCount") or 0),
        }
    return out


def align(tf, syms):
    series = {s: load(s, tf) for s in syms}
    ts = sorted(set.intersection(*[set(v) for v in series.values()]))
    return ts, series


def fmt(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))


def log_returns(px):
    return [math.log(px[i] / px[i - 1]) for i in range(1, len(px))]


def spread_stats(y, tf, label):
    """y = spread level series (log-space). Report stationarity evidence."""
    dy = [y[i] - y[i - 1] for i in range(1, len(y))]
    sd = stdev(dy)
    per_year = 24 * 365 if tf == "1h" else 60 * 24 * 365
    hl = half_life(y)
    vr = variance_ratios(y)
    return {
        "label": label,
        "n": len(y),
        "level_std": stdev(y),
        "bar_ret_std_bps": sd * 1e4,
        "ann_vol_pct": sd * math.sqrt(per_year) * 100,
        "adf": adf(y, label=label),
        "half_life": hl,
        "hurst": hurst(y),
        "variance_ratio": vr,
        "range_bps": (quantile(y, 0.99) - quantile(y, 0.01)) * 1e4,
        "full_range_bps": (max(y) - min(y)) * 1e4,
        "max_dd_bps": max_drawdown(y)[0] * 1e4,
    }


def rolling_z(y, window, tf):
    mu = rolling(y, window, mean)
    sd = rolling(y, window, stdev)
    z = [None if mu[i] is None else (y[i] - mu[i]) / sd[i] for i in range(len(y))]
    return mu, sd, z


def z_diagnostics(y, z, tf, horizons):
    """Does the z-score predict the next move?  (negative slope = mean reversion)"""
    out = {}
    first = next(i for i, v in enumerate(z) if v is not None)
    for h in horizons:
        xs, ys = [], []
        for i in range(first, len(y) - h):
            if z[i] is None:
                continue
            xs.append([1.0, z[i]])
            ys.append((y[i + h] - y[i]) * 1e4)
        fit = OLS(xs, ys)
        out[f"{h}bar"] = {
            "bars": h,
            "slope_bps_per_sigma": fit.beta[1],
            "t_stat": fit.tstat(1),
            "r2": fit.r2,
            "n": fit.n,
        }
    return out


def z_occupancy(z):
    vals = [v for v in z if v is not None]
    absz = [abs(v) for v in vals]
    return {
        "n": len(vals),
        "mean_abs_z": mean(absz),
        "pct_within_1": 100 * sum(1 for v in absz if v < 1) / len(absz),
        "pct_within_2": 100 * sum(1 for v in absz if v < 2) / len(absz),
        "pct_within_3": 100 * sum(1 for v in absz if v < 3) / len(absz),
        "max_abs_z": max(absz),
        "zero_crossings_per_day": None,  # filled by caller
    }


def grid_sim(values, times, *, step_bps, anchor_y, sigma_y, half_width_sigma=3.0,
             taker_bps=2.25, maker_bps=0.0, cross_legs=2):
    """Rung grid on a synthetic spread index.

    values: spread level in bps (unit = bps of one-leg notional)
    anchor: rolling anchor, deviation d = values - anchor
    Long rungs open when d <= -k*step, take profit one step above entry.
    Short rungs mirror.  One fill per rung until it re-arms.
    Returns round count, gross bps, and inventory drawdown.
    """
    long_open = {}   # rung k -> entry deviation
    short_open = {}
    rounds = []
    open_since = {}
    equity = 0.0
    equity_curve = []
    max_inv_bps = 0.0
    for i, (t, v) in enumerate(zip(times, values)):
        a = anchor_y[i]
        if a is None:
            equity_curve.append(equity)
            continue
        d = v - a
        # take profits first (use the same bar only for the opposite rung)
        for k in list(long_open):
            if d >= long_open[k] + step_bps:
                equity += step_bps - taker_bps * cross_legs * 2
                rounds.append((t, step_bps))
                del long_open[k]
        for k in list(short_open):
            if d <= short_open[k] - step_bps:
                equity += step_bps - taker_bps * cross_legs * 2
                rounds.append((t, step_bps))
                del short_open[k]
        # opens
        kmax = int(abs(d) / step_bps)
        if kmax >= 1:
            rung = min(kmax, int(half_width_sigma * sigma_y[i] / step_bps)) if sigma_y[i] else 0
        else:
            rung = 0
        # re-arm logic: rung index = floor(|d| / step)
        k = int(abs(d) // step_bps)
        if k >= 1 and abs(d) <= half_width_sigma * (sigma_y[i] or 1e9):
            if d < 0 and k not in long_open:
                long_open[k] = d
            elif d > 0 and k not in short_open:
                short_open[k] = d
        inv = sum(1 for _ in long_open) + sum(1 for _ in short_open)
        max_inv_bps = max(max_inv_bps, inv * step_bps * 1.0)
        equity_curve.append(equity)
        # drop rungs that came back inside
        for k in list(long_open):
            if d > -(k - 1) * step_bps:
                del long_open[k]
        for k in list(short_open):
            if d < (k - 1) * step_bps:
                del short_open[k]
    days = (times[-1] - times[0]) / 86400
    gross = len(rounds) * step_bps
    net = gross - len(rounds) * taker_bps * cross_legs * 2
    return {
        "step_bps": step_bps,
        "rounds": len(rounds),
        "rounds_per_day": len(rounds) / days if days else 0,
        "gross_bps_per_day": gross / days if days else 0,
        "net_bps_per_day_taker": net / days if days else 0,
        "net_bps_per_day_maker": gross / days if days else 0,
        "days": days,
        "equity_curve": equity_curve,
    }


def main() -> None:
    out = {}
    syms = ["DRAM-USD", "MU-USD", "SKHY-USD"]

    # ---------------------------------------------------------------- hourly
    ts, series = align("1h", syms + ["SNDK-USD"])
    px = {s: [series[s][t]["close"] for t in ts] for s in series}
    out["meta"] = {
        "bars_1h": len(ts),
        "from": fmt(ts[0]), "to": fmt(ts[-1]),
        "days": (ts[-1] - ts[0]) / 86400,
    }
    out["levels"] = {}
    for s in syms + ["SNDK-USD"]:
        out["levels"][s] = {
            "first": px[s][0], "last": px[s][-1],
            "cum_return_pct": (px[s][-1] / px[s][0] - 1) * 100,
        }
        ts_sym = sorted(series[s])
        out["levels"][s].update({
            "own_history_from": fmt(ts_sym[0]), "own_bars": len(ts_sym),
        })

    rets = {s: log_returns(px[s]) for s in series}
    out["corr_hourly"] = {
        f"{a}~{b}": corr(rets[a], rets[b]) for a in syms for b in syms if a < b
    }
    out["vol_hourly_bps"] = {s: stdev(rets[s]) * 1e4 for s in syms}

    # regression DRAM ~ MU + SKHY (returns, hourly)
    xr = [[1.0, rets["MU-USD"][i], rets["SKHY-USD"][i]] for i in range(len(rets["MU-USD"]))]
    yr = rets["DRAM-USD"]
    fit_r = OLS(xr, yr)
    out["ret_regression_hourly"] = {
        "alpha_bps": fit_r.beta[0] * 1e4,
        "beta_MU": fit_r.beta[1], "t_MU": fit_r.tstat(1),
        "beta_SKHY": fit_r.beta[2], "t_SKHY": fit_r.tstat(2),
        "r2": fit_r.r2, "n": fit_r.n,
    }

    # rolling betas (7 days = 168 hourly bars)
    win = 168
    roll_b = []
    for i in range(win, len(yr) + 1):
        seg = xr[i - win:i]
        yy = yr[i - win:i]
        try:
            f = OLS(seg, yy)
            roll_b.append({"t": ts[i], "bMU": f.beta[1], "bSKHY": f.beta[2], "r2": f.r2})
        except ValueError:
            pass
    out["rolling_beta_7d"] = {
        "n": len(roll_b),
        "bMU": {"min": min(r["bMU"] for r in roll_b), "max": max(r["bMU"] for r in roll_b),
                "mean": mean([r["bMU"] for r in roll_b])},
        "bSKHY": {"min": min(r["bSKHY"] for r in roll_b), "max": max(r["bSKHY"] for r in roll_b),
                  "mean": mean([r["bSKHY"] for r in roll_b])},
        "series_sample": roll_b[::max(1, len(roll_b) // 200)],
    }

    ld, lm, ls = (list(map(math.log, px[s])) for s in syms)
    spreads = {}
    # A: equal dollar short (50/50)
    spreads["equal_dollar"] = {
        "y": [a - 0.5 * b - 0.5 * c for a, b, c in zip(ld, lm, ls)],
        "desc": "long $1 DRAM / short $0.50 MU + $0.50 SKHY",
    }
    # B: regression weights (dollar weights normalised from betas)
    b_mu, b_s = fit_r.beta[1], fit_r.beta[2]
    tot = b_mu + b_s
    spreads["beta_weighted"] = {
        "y": [a - b_mu * b - b_s * c for a, b, c in zip(ld, lm, ls)],
        "desc": f"long $1 DRAM / short ${b_mu/tot:.3f} MU + ${b_s/tot:.3f} SKHY (return-regression betas)",
        "raw_betas": {"MU": b_mu, "SKHY": b_s},
    }
    # C/D: 2-leg
    spreads["dram_vs_mu"] = {"y": [a - b for a, b in zip(ld, lm)], "desc": "long DRAM / short MU (1:1)"}
    spreads["dram_vs_skhy"] = {"y": [a - b for a, b in zip(ld, ls)], "desc": "long DRAM / short SKHY (1:1)"}
    # E: DRAM vs SNDK
    lsn = list(map(math.log, px["SNDK-USD"]))
    spreads["dram_vs_sndk"] = {"y": [a - b for a, b in zip(ld, lsn)], "desc": "long DRAM / short SNDK (1:1)"}

    out["spreads"] = {}
    w = 7 * 24  # 7 days of hourly bars
    for name, sp in spreads.items():
        y = sp["y"]
        st = spread_stats(y, "1h", name)
        st["desc"] = sp["desc"]
        mu, sd, z = rolling_z(y, w, "1h")
        st["z_occupancy"] = z_occupancy(z)
        st["z_occupancy"]["zero_crossings_per_day"] = (
            24 * sum(1 for i in range(1, len(z)) if z[i] is not None and z[i - 1] is not None
                     and z[i] * z[i - 1] < 0) / len(z)
        )
        st["predictive"] = z_diagnostics(y, z, "1h", [6, 24, 72, 168])
        st["rolling_anchor"] = {
            "level_drift_per_day_bps": (mean(mu[-w:]) - mean(mu[-2 * w:-w])) * 1e4 / 7 if len(mu) > 2 * w else None,
        }
        st["series_bps"] = [v * 1e4 for v in y]
        st["sample"] = {"t": ts[::max(1, len(ts) // 400)], "y": [v * 1e4 for v in y[::max(1, len(y) // 400)]],
                        "anchor": [None if v is None else v * 1e4 for v in mu[::max(1, len(mu) // 400)]],
                        "z": [None if v is None else v for v in z[::max(1, len(z) // 400)]]}
        out["spreads"][name] = st

    # ---------------------------------------------------------------- 1m microstructure
    try:
        ts1, s1 = align("1m", syms)
    except Exception as exc:  # noqa: BLE001
        out["intraday"] = {"error": str(exc)}
        (HERE / "results.json").write_text(json.dumps(out, indent=1, default=str))
        print("1m data unavailable:", exc)
        return

    px1 = {s: [s1[s][t]["close"] for t in ts1] for s in syms}
    lo1 = {s: [s1[s][t]["low"] for t in ts1] for s in syms}
    hi1 = {s: [s1[s][t]["high"] for t in ts1] for s in syms}
    ld1, lm1, ls1 = (list(map(math.log, px1[s])) for s in syms)
    out["intraday"] = {
        "bars_1m": len(ts1), "from": fmt(ts1[0]), "to": fmt(ts1[-1]),
        "days": (ts1[-1] - ts1[0]) / 86400,
    }

    intraday = {}
    for name in ("equal_dollar", "beta_weighted", "dram_vs_mu", "dram_vs_skhy"):
        if name == "equal_dollar":
            y = [a - 0.5 * b - 0.5 * c for a, b, c in zip(ld1, lm1, ls1)]
        elif name == "beta_weighted":
            y = [a - b_mu * b - b_s * c for a, b, c in zip(ld1, lm1, ls1)]
        elif name == "dram_vs_mu":
            y = [a - b for a, b in zip(ld1, lm1)]
        else:
            y = [a - b for a, b in zip(ld1, ls1)]
        y_bps = [v * 1e4 for v in y]
        dy = [y_bps[i] - y_bps[i - 1] for i in range(1, len(y_bps))]
        # per-minute realised range of the synthetic spread
        # (leg highs/lows combined: DRAM high vs MU/SKHY lows is the widest favourable move)
        rng = []
        for i in range(len(ts1)):
            hi_sp = (math.log(hi1["DRAM-USD"][i]) - b_mu * math.log(lo1["MU-USD"][i])
                     - b_s * math.log(lo1["SKHY-USD"][i])) * 1e4
            lo_sp = (math.log(lo1["DRAM-USD"][i]) - b_mu * math.log(hi1["MU-USD"][i])
                     - b_s * math.log(hi1["SKHY-USD"][i])) * 1e4
            rng.append(hi_sp - lo_sp)
        # hourly profile of activity
        byhour = {}
        for i, t in enumerate(ts1):
            if i == 0:
                continue
            hr = time.gmtime(t).tm_hour
            byhour.setdefault(hr, []).append(abs(dy[i - 1]))
        intraday[name] = {
            "minute_ret_std_bps": stdev(dy),
            "minute_range_median_bps": statistics.median(rng),
            "minute_range_p90_bps": quantile(rng, 0.9),
            "daily_range_median_bps": None,
            "hour_abs_move_bps": {h: mean(v) for h, v in sorted(byhour.items())},
            "series_bps": y_bps,
        }
    out["intraday"]["spreads"] = intraday

    # ---------------------------------------------------------------- grid feasibility
    fee = {"maker_ppm": 0, "taker_ppm": 225}
    out["costs"] = {
        "fees": fee,
        "round_trip_taker_bps_2leg": 4 * fee["taker_ppm"] / 1e4,
        "round_trip_taker_bps_short2legs": 2 * fee["taker_ppm"] / 1e4,
        "note": "one round = open+close; each side trades the DRAM leg plus the short basket",
    }
    # tick quantisation of each leg
    (HERE / "results.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps({k: v for k, v in out.items() if k != "spreads"}, indent=1, default=str)[:200])
    print("wrote results.json")
    for name, st in out["spreads"].items():
        print(f"{name:14s} adf t={st['adf']['t_stat']:6.2f} hl={st['half_life']['half_life_bars']:7.1f}h "
              f"hurst={st['hurst']:.2f} std/hour={st['bar_ret_std_bps']:.1f}bps "
              f"range={st['range_bps']:.0f}bps 1hR2={out['ret_regression_hourly']['r2']:.3f}")


if __name__ == "__main__":
    main()
