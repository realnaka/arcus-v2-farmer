#!/usr/bin/env python3
"""long MU+SNDK+SKHY (ETF weights) / short DRAM  ==  short the non-Arcus basket.

Quantifies what the residual really is, how it behaved over the sample, and
whether a grid on it is viable.  Writes basket_results.json.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from pathlib import Path

from stats_lib import OLS, adf, half_life, hurst, mean, quantile, stdev, variance_ratios
import grid2

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
HOLD = json.loads((HERE / "holdings.json").read_text())
W = {h["ticker"]: h["weight_pct"] / 100 for h in HOLD["top10"]}
TRADE = {"MU": W["MU"], "SKHY": W["000660 KS"], "SNDK": W["SNDK"]}
SUM_TRADE = sum(TRADE.values())          # 0.5414
SUM_UNTRADE = 1 - SUM_TRADE - 0.0101     # 0.4485 of the ETF in the named non-Arcus names


def series(tf):
    syms = ["DRAM-USD", "MU-USD", "SKHY-USD", "SNDK-USD"]
    return grid2.prepare_family(tf, syms)


SYM = {"MU": "MU-USD", "SKHY": "SKHY-USD", "SNDK": "SNDK-USD"}


def build(prep, weights, *, normalize=False):
    """Return (index, hi_rel, lo_rel) for  sum w_i*log(P_i) - log(DRAM)."""
    w = {SYM.get(k, k): v for k, v in weights.items()}
    if normalize:
        s = sum(w.values())
        w = {k: v / s for k, v in w.items()}
    x, hi, lo = [], [], []
    for i, t in enumerate(prep["ts"]):
        c = sum(w[k] * math.log(prep["close"][k][i]) for k in w) - math.log(prep["close"]["DRAM-USD"][i])
        h = sum(w[k] * math.log(prep["hi"][k][i]) for k in w) - math.log(prep["lo"]["DRAM-USD"][i])
        l = sum(w[k] * math.log(prep["lo"][k][i]) for k in w) - math.log(prep["hi"]["DRAM-USD"][i])
        x.append((c - 0.0) * 1e4)
        hi.append((h - c) * 1e4)
        lo.append((l - c) * 1e4)
    base = x[0]
    x = [v - base for v in x]
    return {"ts": prep["ts"], "x": x, "hi": [x[i] + hi[i] for i in range(len(x))],
            "lo": [x[i] + lo[i] for i in range(len(x))], "days": prep["days"]}


def describe(s, label):
    x = s["x"]
    d = [x[i] - x[i - 1] for i in range(1, len(x))]
    hl = half_life(x)
    return {
        "label": label,
        "cum_bps": x[-1],
        "drift_bps_per_day": x[-1] / s["days"],
        "bar_std_bps": stdev(d),
        "ann_vol_pct": stdev(d) * math.sqrt(24 * 365) / 100,
        "sharpe": ((x[-1] / s["days"]) / 24) / stdev(d) * math.sqrt(24 * 365) if stdev(d) else 0,
        "max_dd_bps": min(x[i] - max(x[:i + 1]) for i in range(len(x))),
        "min": min(x), "max": max(x),
        "adf_t": adf(x)["t_stat"],
        "half_life_h": hl["half_life_bars"],
        "hurst": hurst(x),
        "vr": variance_ratios(x),
    }


def zstats(x, W=168):
    z = [None] * len(x)
    for i in range(W - 1, len(x)):
        seg = x[i - W + 1:i + 1]
        z[i] = (x[i] - mean(seg)) / stdev(seg)
    vals = [abs(v) for v in z if v is not None]
    return z, {
        "pct_within_1": 100 * sum(1 for v in vals if v < 1) / len(vals),
        "pct_within_2": 100 * sum(1 for v in vals if v < 2) / len(vals),
        "max_abs_z": max(vals),
        "mean_abs_z": mean(vals),
    }


def main():
    out = {"holdings": HOLD, "tradeable_sum": SUM_TRADE}
    short_leg = []
    for h in HOLD["top10"]:
        if h["arcus"] is None:
            short_leg.append({"name": h["name"], "ticker": h["ticker"],
                              "pct_of_dram": h["weight_pct"],
                              "pct_of_short_leg": (h["weight_pct"] / 100) / (1 - SUM_TRADE) * 100})
    out["short_leg"] = {"names": short_leg,
                        "total_pct_of_dram": round(100 * (1 - SUM_TRADE), 2),
                        "samsung_share_of_short_leg_pct": round(
                            100 * (W["005930 KS"] / (1 - SUM_TRADE)), 1)}

    prep = grid2.prepare_family("1h", ["DRAM-USD", "MU-USD", "SKHY-USD", "SNDK-USD"])
    prep1 = grid2.prepare_family("1m", ["DRAM-USD", "MU-USD", "SKHY-USD", "SNDK-USD"])

    variants = {
        "etf_weights": (TRADE, "按 ETF 权重（不归一）：多 MU 0.274 + SKHY 0.221 + SNDK 0.047 / 空 1 DRAM"),
        "etf_weights_norm": (TRADE, "把可交易权重归一（1.85× 杠杆的同方向仓位）"),
        "equal_three": ({"MU": 1 / 3, "SKHY": 1 / 3, "SNDK": 1 / 3},
                        "三条腿等权（对照：完全不看 ETF 权重）"),
    }
    out["spreads"] = {}
    for key, (w, desc) in variants.items():
        s = build(prep, w, normalize=(key == "etf_weights_norm"))
        r = describe(s, desc)
        z, zs = zstats(s["x"])
        r["z"] = zs
        r["curve"] = s["x"][::max(1, len(s["x"]) // 500)]
        r["zcurve"] = [None if v is None else v for v in z[::max(1, len(z) // 500)]]
        r["ts"] = s["ts"][::max(1, len(s["ts"]) // 500)]
        # efficient frontier comparison: variance of the same idea with min-var weights
        out["spreads"][key] = r

    # implied untradeable basket return series (per 1 unit of that basket)
    r_dram = [math.log(prep["close"]["DRAM-USD"][i] / prep["close"]["DRAM-USD"][i - 1])
              for i in range(1, len(prep["ts"]))]
    r_tr = [sum(TRADE[k] * math.log(prep["close"][SYM[k]][i] / prep["close"][SYM[k]][i - 1]) for k in TRADE)
            for i in range(1, len(prep["ts"]))]
    resid = [r_dram[i] - r_tr[i] for i in range(len(r_dram))]
    scale = 1 - SUM_TRADE
    basket = [v / scale for v in resid]
    cum, acc = [], 0.0
    for v in basket:
        acc += v * 1e4
        cum.append(acc)
    out["untradeable_basket"] = {
        "cum_bps_full": cum[-1],
        "cum_pct_full": cum[-1] / 100,
        "drift_bps_per_day": cum[-1] / ((prep["ts"][-1] - prep["ts"][0]) / 86400),
        "bar_std_bps_of_basket": stdev(basket) * 1e4,
        "ann_vol_pct": stdev(basket) * math.sqrt(24 * 365) * 100,
        "adf_t": adf(cum)["t_stat"],
        "hurst": hurst(cum),
        "half_life_h": half_life(cum)["half_life_bars"],
        "curve": cum[::max(1, len(cum) // 500)],
        "sub": [],
    }
    # per ~2-week block: how the basket and the tradeable legs moved
    step = len(prep["ts"]) // 6
    for k in range(6):
        a, b = k * step, min((k + 1) * step, len(prep["ts"]) - 1)
        if b - a < 24:
            continue
        out["untradeable_basket"]["sub"].append({
            "from": time.strftime("%m-%d", time.gmtime(prep["ts"][a])),
            "to": time.strftime("%m-%d", time.gmtime(prep["ts"][b])),
            "dram_pct": (prep["close"]["DRAM-USD"][b] / prep["close"]["DRAM-USD"][a] - 1) * 100,
            "mu_pct": (prep["close"]["MU-USD"][b] / prep["close"]["MU-USD"][a] - 1) * 100,
            "skhy_pct": (prep["close"]["SKHY-USD"][b] / prep["close"]["SKHY-USD"][a] - 1) * 100,
            "sndk_pct": (prep["close"]["SNDK-USD"][b] / prep["close"]["SNDK-USD"][a] - 1) * 100,
            "basket_pct": sum(basket[a:b] ) * 100,
        })

    # min-variance weights on the same three legs (long names / short DRAM)
    xn = [[1.0] + [math.log(prep["close"][k][i] / prep["close"][k][i - 1])
                   for k in ("MU-USD", "SKHY-USD", "SNDK-USD")] for i in range(1, len(prep["ts"]))]
    yn = [math.log(prep["close"]["DRAM-USD"][i] / prep["close"]["DRAM-USD"][i - 1])
          for i in range(1, len(prep["ts"]))]
    fit = OLS(xn, yn)
    out["mv_weights"] = {"MU": fit.beta[1], "SKHY": fit.beta[2], "SNDK": fit.beta[3],
                         "sum": fit.beta[1] + fit.beta[2] + fit.beta[3], "r2": fit.r2}
    s_mv = build(prep, {"MU-USD": fit.beta[1], "SKHY-USD": fit.beta[2], "SNDK-USD": fit.beta[3]})
    out["spreads"]["mv_weights"] = describe(s_mv, "最小方差权重（回归拟合，可正可负）")
    z, zs = zstats(s_mv["x"])
    out["spreads"]["mv_weights"]["z"] = zs
    out["spreads"]["mv_weights"]["curve"] = s_mv["x"][::max(1, len(s_mv["x"]) // 500)]
    out["spreads"]["mv_weights"]["zcurve"] = [None if v is None else v for v in z[::max(1, len(z) // 500)]]

    # grid feasibility on the ETF-weight spread (both 1h and 1m, close-only)
    out["grid"] = {}
    for tf, p in (("1h", prep), ("1m", prep1)):
        s = build(p, TRADE, normalize=True)
        hl_bars = 48 if tf == "1h" else 2880
        anchor = grid2.anchor_of(s, hl_bars)
        block = {}
        for step_bps, rungs in ((10, 24), (20, 12), (20, 24), (30, 12), (50, 12)):
            r_close = grid2.simulate(s, step=step_bps, rungs=rungs, anchor=anchor,
                                     cost_bps_round=2.0, mode="close")
            r_opt = grid2.simulate(s, step=step_bps, rungs=rungs, anchor=anchor,
                                   cost_bps_round=2.0, mode="natural")
            block[f"step{step_bps}_rungs{rungs}_hl{hl_bars}"] = {
                "close": {k: v for k, v in r_close.items() if k != "equity_curve"},
                "natural": {k: v for k, v in r_opt.items() if k != "equity_curve"},
                "equity": r_close["equity_curve"][::max(1, len(r_close["equity_curve"]) // 400)],
            }
        out["grid"][tf] = block

    (HERE / "basket_results.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out["short_leg"], indent=1)[:1200])
    for k, v in out["spreads"].items():
        print(f"{k:18s} cum={v['cum_bps']:8.0f}bps drift={v['drift_bps_per_day']:6.1f}/d "
              f"vol={v['bar_std_bps']:5.1f} ann={v['ann_vol_pct']:5.1f}% sharpe={v['sharpe']:5.2f} "
              f"maxDD={v['max_dd_bps']:8.0f} adf={v['adf_t']:5.2f} hl={v['half_life_h']:6.1f} "
              f"hurst={v['hurst']:.2f} |z|<2={v['z']['pct_within_2']:.0f}%")
    b = out["untradeable_basket"]
    print(f"untradeable basket: cum {b['cum_pct_full']:.1f}% drift {b['drift_bps_per_day']:.1f}bps/d "
          f"vol {b['ann_vol_pct']:.1f}% adf {b['adf_t']:.2f} hurst {b['hurst']:.2f}")
    print("mv weights", json.dumps(out["mv_weights"]))


if __name__ == "__main__":
    main()
