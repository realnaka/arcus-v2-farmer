#!/usr/bin/env python3
"""分时段统计：交易时段 vs 非交易时段 —— 波动率、回撤、最大变化、以及杠杆空间。

主口径 = 实际要做的方向：long MU+SKHY+SNDK（DRAM 的持仓权重归一）/ short DRAM。
对照口径 = 上一版报告的方向：long DRAM / short 0.71 MU + 0.29 SKHY。

两套时段切法：
  市场口径      美股常规 RTH 09:30-16:00 ET vs 其它
  Arcus 口径    常规 04:00-20:00 ET（含盘前+盘后）vs 加成窗口 20:00-04:00 ET + 周末
                依据 /v1/markets 的 regularTradingHours 字段

输出 sessions_stats.json + spread-sessions.html。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import math
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from report import chart, table
from stats_lib import mean, quantile, stdev

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
OUTDIR = Path(os.environ.get("LAB_OUT", HERE / "out"))
ET = ZoneInfo("America/New_York")

LEGS = ["DRAM-USD", "MU-USD", "SKHY-USD", "SNDK-USD"]
TRADEABLE = 0.5414                      # DRAM 里能在 Arcus 交易的权重（holdings.json）
SPREADS = {
    "BASKET": dict(
        zh="篮子 − DRAM", full="long MU+SKHY+SNDK（DRAM 持仓权重归一）/ short DRAM",
        long={"MU-USD": 0.2736 / TRADEABLE, "SKHY-USD": 0.2213 / TRADEABLE,
              "SNDK-USD": 0.0465 / TRADEABLE},
        short={"DRAM-USD": 1.0}),
    "LEG": dict(
        zh="DRAM − 两条腿", full="long DRAM / short 0.71 MU + 0.29 SKHY",
        long={"DRAM-USD": 1.0},
        short={"MU-USD": 0.7059, "SKHY-USD": 0.2941}),
}
PRIMARY = "BASKET"
PMAX = [PRIMARY, "DRAM-USD", "MU-USD", "SKHY-USD"]
SERIES = [PRIMARY, "LEG"] + LEGS
NAMES = {"DRAM-USD": "DRAM", "MU-USD": "MU", "SKHY-USD": "SKHY", "SNDK-USD": "SNDK",
         "BASKET": "篮子−DRAM", "LEG": "DRAM−腿"}

FINE = ["PRE", "RTH", "AH", "OVN", "WKND"]
LABEL = {"PRE": "盘前 PRE", "RTH": "交易时段 RTH", "AH": "盘后 AH",
         "OVN": "隔夜 OVN", "WKND": "周末 WKND", "REG": "常规时段", "BONUS": "加成窗口"}
SHORT = {"PRE": "盘前", "RTH": "交易", "AH": "盘后", "OVN": "隔夜", "WKND": "周末",
         "REG": "常规", "BONUS": "加成"}
WINDOW = {"PRE": "04:00-09:30 ET", "RTH": "09:30-16:00 ET", "AH": "16:00-20:00 ET",
          "OVN": "20:00-04:00 ET", "WKND": "Fri 20:00 - Sun 20:00 ET",
          "REG": "04:00-20:00 ET（含盘前盘后）", "BONUS": "20:00-04:00 ET + 周末"}
WEEK_HOURS = {"PRE": 27.5, "RTH": 32.5, "AH": 20.0, "OVN": 40.0, "WKND": 48.0,
              "REG": 80.0, "BONUS": 88.0}
SESSION_H = {"PRE": 5.5, "RTH": 6.5, "AH": 4.0, "OVN": 8.0, "WKND": 48.0}


def load(sym, tf):
    rows = json.loads((DATA / f"candles_{sym}_{tf}.json").read_text())
    return {int(r["openTime"]) // 1_000_000: {
        "o": float(r["open"]), "h": float(r["high"]), "l": float(r["low"]),
        "c": float(r["close"]), "v": float(r.get("notionalVolume") or 0),
        "n": int(r.get("tradeCount") or 0)} for r in rows}


def build(tf="1m"):
    px = {s: load(s, tf) for s in LEGS}
    ts = sorted(set.intersection(*[set(v) for v in px.values()]))
    n = len(ts)
    close = {s: [px[s][t]["c"] for t in ts] for s in LEGS}
    x = {name: [0.0] * n for name in SPREADS}
    prev = {}
    for i, t in enumerate(ts):
        for name, spec in SPREADS.items():
            lvl = (sum(w * math.log(px[s][t]["c"]) for s, w in spec["long"].items())
                   - sum(w * math.log(px[s][t]["c"]) for s, w in spec["short"].items()))
            x[name][i] = 0.0 if i == 0 else x[name][i - 1] + (lvl - prev[name]) * 1e4
            prev[name] = lvl
    return {"tf": tf, "ts": ts, "close": close, "x": x,
            "v": {s: [px[s][t]["v"] for t in ts] for s in LEGS},
            "n": {s: [px[s][t]["n"] for t in ts] for s in LEGS},
            "days": (ts[-1] - ts[0]) / 86400}


def series_of(prep):
    xs = dict(prep["x"])
    for s in LEGS:
        base = math.log(prep["close"][s][0])
        xs[s] = [(math.log(c) - base) * 1e4 for c in prep["close"][s]]
    return xs


# ------------------------------------------------------------------- session tags

def bucket_of(e):
    wd, h = e.weekday(), e.hour + e.minute / 60
    if wd == 5:
        return "WKND"
    if wd == 6:
        return "OVN" if h >= 20 else "WKND"
    if wd == 4 and h >= 20:
        return "WKND"
    if 4 <= h < 9.5:
        return "PRE"
    if 9.5 <= h < 16:
        return "RTH"
    if 16 <= h < 20:
        return "AH"
    return "OVN"


def session_id(e, bk):
    d = e.date()
    if bk == "OVN" and e.hour < 4:
        d -= dt.timedelta(days=1)
    elif bk == "WKND":
        d -= dt.timedelta(days=e.weekday() - 4)
    return f"{bk}:{d.isoformat()}"


def tag(ts):
    out = []
    for t in ts:
        e = dt.datetime.fromtimestamp(t, ET)
        bk = bucket_of(e)
        out.append((bk, session_id(e, bk)))
    return out


ARCUS_REG = {"PRE", "RTH", "AH"}
ARCUS_BONUS = {"OVN", "WKND"}


# ------------------------------------------------------------------ block metrics

def block(ts, x, tags, keep):
    """Minute stats over bars whose session sits in `keep`."""
    d, runs, cur = [], [], None
    for i in range(1, len(ts)):
        bk, sid = tags[i]
        if bk not in keep:
            if cur is not None:
                runs.append(cur)
            cur = None
            continue
        step = x[i] - x[i - 1]
        d.append(step)
        if cur is None or cur["id"] != sid:
            if cur is not None:
                runs.append(cur)
            cur = {"id": sid, "i0": i, "i1": i, "path": 0.0, "peak": x[i], "trough": x[i],
                   "dd": 0.0, "up": 0.0, "max_step": 0.0, "t_max": ts[i]}
        cur["i1"] = i
        cur["path"] += abs(step)
        if abs(step) > cur["max_step"]:
            cur["max_step"], cur["t_max"] = abs(step), ts[i]
        cur["peak"] = max(cur["peak"], x[i])
        cur["trough"] = min(cur["trough"], x[i])
        cur["dd"] = min(cur["dd"], x[i] - cur["peak"])
        cur["up"] = max(cur["up"], x[i] - cur["trough"])
    if cur is not None:
        runs.append(cur)
    n = len(d)
    sd = stdev(d) if n > 2 else float("nan")
    hr = []
    for i in range(60, len(ts)):
        if tags[i][0] not in keep or tags[i][1] != tags[i - 60][1]:
            continue
        if any(tags[j][0] not in keep for j in range(i - 59, i + 1)):
            continue
        hr.append(x[i] - x[i - 60])
    nets = [x[r["i1"]] - x[r["i0"]] for r in runs]
    return {
        "minutes": n, "sessions": len(runs),
        "sigma_1m_bps": sd,
        "sigma_1h_bps": stdev(hr) if len(hr) > 2 else float("nan"),
        "mean_abs_bps": mean([abs(v) for v in d]) if d else float("nan"),
        "p99_abs_bps": quantile([abs(v) for v in d], 0.99) if d else float("nan"),
        "max_abs_bps": max(abs(v) for v in d) if d else float("nan"),
        "path_per_session_bps": mean([r["path"] for r in runs]) if runs else float("nan"),
        "net_per_session_bps": mean(nets) if runs else float("nan"),
        "net_abs_median_bps": quantile([abs(v) for v in nets], 0.5) if runs else float("nan"),
        "worst_dd_bps": min(r["dd"] for r in runs) if runs else float("nan"),
        "dd_p05_bps": quantile([r["dd"] for r in runs], 0.05) if runs else float("nan"),
        "dd_median_bps": quantile([r["dd"] for r in runs], 0.5) if runs else float("nan"),
        "worst_up_bps": max(r["up"] for r in runs) if runs else float("nan"),
        "path_total_bps": sum(r["path"] for r in runs),
        "_runs": runs,
    }


def flow(prep, tags, keep, sym):
    idx = [i for i in range(len(tags)) if tags[i][0] in keep]
    if not idx:
        return {"notional": 0.0, "pct_active": float("nan")}
    return {"notional": sum(prep["v"][sym][i] for i in idx),
            "pct_active": 100.0 * sum(1 for i in idx if prep["n"][sym][i] > 0) / len(idx)}


# ---------------------------------------------------------------------- leverage

def risk_row(blocks, name, bk, hours, ref):
    b = blocks[name][bk]
    cum = b["sigma_1h_bps"] * math.sqrt(hours)
    return dict(hours=hours, sigma_1m_bps=b["sigma_1m_bps"], sigma_1h_bps=b["sigma_1h_bps"],
                cum_sigma_bps=cum, risk_vs_rth=cum / ref, k_equal_risk=ref / cum,
                oi_time_per_risk=math.sqrt(hours) / b["sigma_1h_bps"],
                worst_dd_bps=b["worst_dd_bps"], dd_p05_bps=b["dd_p05_bps"],
                worst_up_bps=b["worst_up_bps"], sessions=b["sessions"])


def leverage_table(blocks, name):
    ref = blocks[name]["RTH"]["sigma_1h_bps"] * math.sqrt(SESSION_H["RTH"])
    rows = {bk: risk_row(blocks, name, bk, SESSION_H[bk], ref) for bk in FINE}
    rows["REG"] = risk_row(blocks, name, "REG", 16.0, ref)
    bench = rows["RTH"]["oi_time_per_risk"]
    for bk in rows:
        rows[bk]["oi_time_eff"] = rows[bk]["oi_time_per_risk"] / bench
    return rows


def rth_days(ts, tags):
    out = {}
    for i, t in enumerate(ts):
        if tags[i][0] != "RTH":
            continue
        d = dt.datetime.fromtimestamp(t, ET).date().isoformat()
        b = out.setdefault(d, {"i0": i, "i1": i})
        b["i1"] = i
    return out


def open_gaps(prep, tags, name):
    days = rth_days(prep["ts"], tags)
    keys = sorted(days)
    x = prep["x"][name]
    gap, first5, first30, sess = [], [], [], []
    for k in range(1, len(keys)):
        a, b = days[keys[k - 1]], days[keys[k]]
        gap.append(x[b["i0"]] - x[b["i0"] - 1])
        if b["i0"] + 5 <= b["i1"]:
            first5.append(x[b["i0"] + 5] - x[b["i0"]])
        if b["i0"] + 30 <= b["i1"]:
            first30.append(x[b["i0"] + 30] - x[b["i0"]])
        sess.append(x[b["i1"]] - x[b["i0"]])
    return dict(n=len(gap), gap_sigma=stdev(gap), gap_max_up=max(gap), gap_max_down=min(gap),
                first5_sigma=stdev(first5), first5_max_abs=max(abs(v) for v in first5),
                gap_pct_gt_20=100.0 * sum(1 for v in gap if abs(v) > 20) / len(gap),
                first30_sigma=stdev(first30), session_sigma=stdev(sess))


def overnight_intraday(prep, tags, xs):
    days = rth_days(prep["ts"], tags)
    keys = sorted(days)
    res = {}
    for name in SERIES:
        s = xs[name]
        over, intra = [], []
        for k in range(1, len(keys)):
            a, b = days[keys[k - 1]], days[keys[k]]
            over.append(s[b["i0"]] - s[a["i1"]])
            intra.append(s[b["i1"]] - s[b["i0"]])
        v = stdev(over) ** 2 + stdev(intra) ** 2
        res[name] = dict(overnight=over, intraday=intra, os=stdev(over), isx=stdev(intra),
                         ratio=stdev(over) / stdev(intra), share=100 * stdev(over) ** 2 / v,
                         max_down=min(over), intra_max_down=min(intra))
    return res, len(keys)


# --------------------------------------------------------------------------- main

def main():
    prep = build("1m")
    ts = prep["ts"]
    tags = tag(ts)
    xs = series_of(prep)
    days = prep["days"]
    span = (time.strftime("%Y-%m-%d", time.gmtime(ts[0])) + " → "
            + time.strftime("%Y-%m-%d", time.gmtime(ts[-1])))
    mk = {m["marketDisplayName"]: m
          for m in json.loads((DATA / "markets.json").read_text())["markets"]}

    blocks = {name: {} for name in SERIES}
    for name in SERIES:
        for bk in FINE:
            blocks[name][bk] = block(ts, xs[name], tags, {bk})
        blocks[name]["REG"] = block(ts, xs[name], tags, ARCUS_REG)
        blocks[name]["BONUS"] = block(ts, xs[name], tags, ARCUS_BONUS)
        blocks[name]["ALL"] = block(ts, xs[name], tags, set(FINE))
        var_sum = sum(blocks[name][bk]["sigma_1m_bps"] ** 2 * blocks[name][bk]["minutes"]
                      for bk in FINE)
        tot_min = blocks[name]["ALL"]["minutes"]
        tot_path = blocks[name]["ALL"]["path_total_bps"]
        for bk in FINE:
            b = blocks[name][bk]
            b["pct_minutes"] = 100.0 * b["minutes"] / tot_min
            b["pct_variance"] = 100.0 * b["sigma_1m_bps"] ** 2 * b["minutes"] / var_sum
            b["tv_share_pct"] = 100.0 * b["path_total_bps"] / tot_path
        for bk, keep in (("REG", ARCUS_REG), ("BONUS", ARCUS_BONUS), ("ALL", set(FINE))):
            b = blocks[name][bk]
            b["pct_minutes"] = 100.0 * b["minutes"] / tot_min
            b["pct_variance"] = 100.0 * sum(
                blocks[name][k]["sigma_1m_bps"] ** 2 * blocks[name][k]["minutes"]
                for k in keep) / var_sum
            b["tv_share_pct"] = 100.0 * sum(
                blocks[name][k]["path_total_bps"] for k in keep) / tot_path

    flow_all = flow(prep, tags, set(FINE), "DRAM-USD")
    flow_fine, flow_arcus = {}, {}
    for bk in FINE:
        f = flow(prep, tags, {bk}, "DRAM-USD")
        f["pct_notional"] = 100.0 * f["notional"] / flow_all["notional"]
        flow_fine[bk] = f
    for bk, keep in (("REG", ARCUS_REG), ("BONUS", ARCUS_BONUS)):
        f = flow(prep, tags, keep, "DRAM-USD")
        f["pct_notional"] = 100.0 * f["notional"] / flow_all["notional"]
        flow_arcus[bk] = f
    leg_flow = {}
    for s in LEGS:
        f = flow(prep, tags, set(FINE), s)
        fo = flow(prep, tags, ARCUS_BONUS, s)
        f["bonus_share"] = 100.0 * fo["notional"] / f["notional"] if f["notional"] else float("nan")
        leg_flow[s] = f

    lev = {name: leverage_table(blocks, name) for name in SERIES}
    gaps = {name: open_gaps(prep, tags, name) for name in (PRIMARY, "LEG")}
    oi, n_days = overnight_intraday(prep, tags, xs)

    profile = {}
    for name in PMAX:
        per_hour = {h: [] for h in range(24)}
        for i in range(1, len(ts)):
            e = dt.datetime.fromtimestamp(ts[i], ET).astimezone(dt.timezone.utc)
            per_hour[e.hour].append(xs[name][i] - xs[name][i - 1])
        profile[name] = [stdev(per_hour[h]) for h in range(24)]

    paths = {}
    for bk in FINE:
        runs, cur, acc = [], None, []
        for i in range(1, len(ts)):
            tbk, sid = tags[i]
            if tbk != bk:
                if cur is not None:
                    runs.append(acc)
                cur, acc = None, []
                continue
            if cur is None or cur != sid:
                if cur is not None:
                    runs.append(acc)
                cur, acc = sid, []
            acc.append((acc[-1] if acc else 0.0)
                       + abs(xs[PRIMARY][i] - xs[PRIMARY][i - 1]))
        if cur is not None:
            runs.append(acc)
        n = max(len(r) for r in runs)
        paths[bk] = [mean([r[j] for r in runs if len(r) > j]) for j in range(n)]

    movers = []
    for name in SERIES:
        s = xs[name]
        for i in range(1, len(ts)):
            movers.append((abs(s[i] - s[i - 1]), s[i] - s[i - 1], ts[i], tags[i][0], NAMES[name]))
    top = sorted(movers, reverse=True)[:20]

    worst = []
    for bk in FINE:
        rs = blocks[PRIMARY][bk]["_runs"]
        if not rs:
            continue
        w = min(rs, key=lambda r: r["dd"])
        worst.append(dict(bucket=bk, dd=w["dd"],
                          at=time.strftime("%m-%d %H:%M", time.gmtime(w["t_max"])),
                          avg_dd=mean([r["dd"] for r in rs]),
                          avg_up=mean([r["up"] for r in rs]), sessions=len(rs)))

    out = dict(
        meta=dict(frm=span.split(" → ")[0], to=span.split(" → ")[1], days=days, bars=len(ts),
                  tf="1m", us_days=n_days, primary=PRIMARY),
        market={s: mk[s] for s in LEGS if s in mk},
        blocks={n: {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                    for k, v in blocks[n].items()} for n in SERIES},
        flow=dict(fine=flow_fine, arcus=flow_arcus, all=flow_all, legs=leg_flow),
        leverage=lev, gaps=gaps,
        overnight={k: {kk: vv for kk, vv in v.items() if kk not in ("overnight", "intraday")}
                   for k, v in oi.items()},
        profile=profile,
        top_moves=[(m[3], time.strftime("%Y-%m-%d %H:%M", time.gmtime(m[2])), m[4], m[1])
                   for m in top],
        worst_sessions=worst)
    (HERE / "sessions_stats.json").write_text(json.dumps(out, indent=1, default=str))
    render(blocks, lev, gaps, oi, profile, paths, out, span, days, flow_fine, flow_arcus, leg_flow)

    print(f"bars {len(ts):,} · {days:.1f}d · {span} · {n_days} US sessions · primary={PRIMARY}")
    for n in SERIES:
        b = blocks[n]
        print(f"{NAMES[n]:10s} σ1h RTH {b['RTH']['sigma_1h_bps']:6.2f} | REG {b['REG']['sigma_1h_bps']:6.2f}"
              f" | BONUS {b['BONUS']['sigma_1h_bps']:6.2f} | 方差 RTH {b['RTH']['pct_variance']:5.1f}%"
              f" REG {b['REG']['pct_variance']:5.1f}% / BONUS {b['BONUS']['pct_variance']:5.1f}%")
    for nm in (PRIMARY, "LEG"):
        lv = lev[nm]
        print(f"{NAMES[nm]} k_equal_risk:", {k: round(lv[k]["k_equal_risk"], 2) for k in FINE},
              "| oi_eff:", {k: round(lv[k]["oi_time_eff"], 2) for k in FINE})
    return out


# ------------------------------------------------------------------------- charts

COLORS = {"BASKET": "#0f766e", "LEG": "#a855f7", "DRAM-USD": "#0284c7",
          "MU-USD": "#dc2626", "SKHY-USD": "#059669", "SNDK-USD": "#f59e0b"}
BCOLORS = {"PRE": "#f59e0b", "RTH": "#0284c7", "AH": "#8b5cf6", "OVN": "#0f766e", "WKND": "#dc2626"}


def hour_bars(profile, keys, *, width=1000, height=310, pad=(58, 14, 34, 58)):
    left, right, top, bottom = pad
    hi = max(profile[k][h] for k in keys for h in range(24)) * 1.22
    py = lambda v: (height - bottom) - (height - bottom - top) * (v / hi)
    gw = (width - left - right) / 24.0
    bw = gw / (len(keys) + 0.6)
    p = ['<svg viewBox="0 0 %d %d" class="chart" role="img">' % (width, height)]
    h0, h1 = 13.5, 20.0
    p.append('<rect x="%.1f" y="%d" width="%.1f" height="%.1f" fill="rgba(2,132,199,.09)"/>'
             % (left + gw * h0, top, gw * (h1 - h0), height - bottom - top))
    p.append('<text x="%.1f" y="%d" class="ax" text-anchor="middle">US 常规时段</text>'
             % (left + gw * (h0 + h1) / 2, top + 12))
    p.append('<text x="%.1f" y="%d" class="ax" text-anchor="middle">13:30-20:00 UTC</text>'
             % (left + gw * (h0 + h1) / 2, top + 24))
    p.append('<rect x="%.1f" y="%d" width="%.1f" height="%.1f" fill="rgba(245,158,11,.10)"/>'
             % (left, top, gw * 4, height - bottom - top))
    p.append('<text x="%.1f" y="%d" class="ax" text-anchor="middle">盘前</text>'
             % (left + gw * 2, top + 12))
    for k in range(6):
        v = hi * k / 5
        y = py(v)
        p.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="grid"/>' % (left, y, width - right, y))
        p.append('<text x="%d" y="%.1f" class="ax" text-anchor="end">%.1f</text>' % (left - 8, y + 4, v))
    for h in range(24):
        for j, key in enumerate(keys):
            xx = left + gw * h + 0.3 * bw + j * bw
            y = py(profile[key][h])
            p.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" opacity=".9"/>'
                     % (xx, y, bw - 1.2, height - bottom - y, COLORS[key]))
        if h % 2 == 0:
            p.append('<text x="%.1f" y="%d" class="ax" text-anchor="middle">%02d</text>'
                     % (left + gw * (h + 0.5), height - bottom + 18, h))
    p.append('<text x="14" y="%d" class="ax">σ per 1m (bps)</text>' % (top + 4))
    x = left + 4
    for key in keys:
        p.append('<rect x="%.1f" y="%d" width="10" height="10" fill="%s"/>' % (x, top + 4, COLORS[key]))
        lab = NAMES[key]
        p.append('<text x="%.1f" y="%d" class="lg">%s</text>' % (x + 14, top + 13, lab))
        x += 32 + 7.2 * len(lab)
    p.append('<text x="%d" y="%d" class="ax" text-anchor="end">UTC</text>'
             % (left - 8, height - bottom + 18))
    p.append("</svg>")
    return "\n".join(p)


def path_lines(paths, *, width=1000, height=290, pad=(58, 14, 24, 48)):
    n = max(len(v) for v in paths.values())
    series = []
    for bk in FINE:
        ys = list(paths[bk]) + [None] * (n - len(paths[bk]))
        series.append({"name": LABEL[bk], "ys": ys, "color": BCOLORS[bk]})
    return chart(series, width=width, height=height, pad=pad,
                 xlab=[str(i) for i in range(n)], ylab="累计 |Δ| (bps)", yfmt="{:,.0f}",
                 zero=False, xticks=6)


def risk_horizon(sig, ref, *, width=1000, height=290, pad=(58, 14, 30, 48)):
    hs = list(range(1, 73))
    series = [{"name": "常规时段 σ_h", "ys": [sig["REG"] * math.sqrt(h) for h in hs], "color": "#0284c7"},
              {"name": "隔夜 OVN σ_h", "ys": [sig["OVN"] * math.sqrt(h) for h in hs], "color": "#0f766e"},
              {"name": "周末 WKND σ_h", "ys": [sig["WKND"] * math.sqrt(h) for h in hs], "color": "#dc2626"}]
    band = {"lo": ref - 1.6, "hi": ref + 1.6, "fill": "rgba(220,38,38,.16)",
            "label": "一个常规时段的风险 (1×)"}
    return chart(series, width=width, height=height, pad=pad,
                 xlab=[str(h) for h in hs], ylab="累计 σ (bps)", yfmt="{:,.0f}",
                 bands=[band], zero=False, xticks=6)


CSS = """
:root{--ink:#0f172a;--mut:#64748b;--line:#e2e8f0;--bg:#f8fafc}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",Arial,sans-serif}
.wrap{max-width:1140px;margin:0 auto;padding:36px 24px 80px}
h1{font-size:26px;margin:0 0 6px}
h2{font-size:20px;margin:44px 0 6px;padding-bottom:8px;border-bottom:2px solid var(--line)}
h3{font-size:16px;margin:26px 0 8px}
.sub{color:var(--mut);font-size:13.5px;margin-bottom:24px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px;margin:22px 0 8px}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:15px 16px}
.ct{font-size:12px;letter-spacing:.06em;color:var(--mut)}
.cv{font-size:17px;font-weight:650;margin:6px 0 6px;line-height:1.35}
.cd{font-size:13px;color:#475569}
.panel{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:14px 0}
.chart{width:100%;height:auto;display:block}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);
border-radius:10px;overflow:hidden;font-size:13.5px;margin:12px 0}
th{text-align:left;padding:9px 11px;background:#f1f5f9;font-weight:600;color:#334155;
font-size:12.5px;white-space:nowrap}
td{padding:8px 11px;border-top:1px solid var(--line);white-space:nowrap}
tr:hover td{background:#fafcff}
.grid{stroke:#eef2f7;stroke-width:1}
.ax{fill:#94a3b8;font-size:11px}
.lg{fill:#475569;font-size:11.5px}
code{background:#f1f5f9;padding:1px 5px;border-radius:4px;font-size:13px}
.k{color:#0f766e;font-weight:600}
.b{color:#b91c1c;font-weight:600}
ul{padding-left:20px}li{margin:4px 0}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:760px){.two{grid-template-columns:1fr}}
.foot{color:var(--mut);font-size:12.5px;margin-top:36px;border-top:1px solid var(--line);padding-top:14px}
"""


def render(blocks, lev, gaps, oi, profile, paths, out, span, days, flow_fine, flow_arcus, leg_flow):
    f0 = lambda v: f"{v:,.0f}"
    f1 = lambda v: f"{v:,.1f}"
    f2 = lambda v: f"{v:,.2f}"
    sp, lv, lv2 = blocks[PRIMARY], lev[PRIMARY], lev["LEG"]
    mk = out["market"]
    reg, bon = sp["REG"], sp["BONUS"]
    ratio1h = reg["sigma_1h_bps"] / bon["sigma_1h_bps"]
    g = gaps[PRIMARY]
    ddr = sp["RTH"]["worst_dd_bps"]
    ddb = sp["WKND"]["worst_dd_bps"]
    lev_book = 1.5
    bud = {k: 10000.0 * pct / lev_book for k, pct in (("L1", 0.05), ("L2", 0.10), ("L3", 0.22))}
    wk = lambda nr, no, nw: 80.0 * nr + 40.0 * no + 48.0 * nw
    base_oi = wk(1.0, 1.0, 1.0)
    b_ovn = lv["OVN"]["k_equal_risk"]
    b_wknd = lv["WKND"]["k_equal_risk"]
    d_ovn = bud["L1"] / abs(sp["OVN"]["worst_dd_bps"])
    d_wknd = bud["L1"] / abs(sp["WKND"]["worst_dd_bps"])
    worst_bk = max(FINE, key=lambda k: abs(sp[k]["worst_dd_bps"]))
    worst_bk_dd = sp[worst_bk]["worst_dd_bps"]

    tl = [
        ("方向已按实际策略改过来", SPREADS[PRIMARY]["zh"], SPREADS[PRIMARY]["full"]),
        ("Arcus 的「非交易时段」不含盘前盘后",
         "常规时段 = 04:00-20:00 ET；加成窗口 = 20:00-04:00 ET + 周末",
         "盘前 04:00-09:30 与盘后 16:00-20:00 都算常规：无积分加成、初始保证金 10%"),
        ("非交易时段波动确实低",
         f"加成窗口 σ_1h = {f1(bon['sigma_1h_bps'])} bps，是常规时段 {f1(reg['sigma_1h_bps'])} 的 1/{f2(ratio1h)}",
         f"RTH {f1(sp['RTH']['sigma_1h_bps'])} → 盘后 {f1(sp['AH']['sigma_1h_bps'])} → 周末 {f1(sp['WKND']['sigma_1h_bps'])} bps/小时"),
        ("加杠杆的空间按口径差 2 倍",
         f"等波动率 {f2(b_ovn)}×（隔夜）/ {f2(b_wknd)}×（周末）",
         f"等回撤预算 {f2(d_ovn)}× / {f2(d_wknd)}× —— 差额就是开盘缺口"),
    ]
    cards = "".join('<div class="card"><div class="ct">%s</div><div class="cv">%s</div>'
                    '<div class="cd">%s</div></div>' % t for t in tl)

    def_rows = []
    for bk in FINE:
        def_rows.append([LABEL[bk], WINDOW[bk], f"{WEEK_HOURS[bk]:.1f} h",
                         f"{sp[bk]['pct_minutes']:.1f}%",
                         "常规 · 积分×1" if bk in ("PRE", "RTH", "AH") else "加成 · 积分更高",
                         f"{sp[bk]['sessions']:,}"])
    def_rows.append(["<b>Arcus 常规时段</b>", WINDOW["REG"], f"{WEEK_HOURS['REG']:.1f} h",
                     f"{reg['pct_minutes']:.1f}%", "<b>积分×1</b>", f"{reg['sessions']:,}"])
    def_rows.append(["<b>Arcus 加成窗口</b>", WINDOW["BONUS"], f"{WEEK_HOURS['BONUS']:.1f} h",
                     f"{bon['pct_minutes']:.1f}%", "<b>积分更高</b>", f"{bon['sessions']:,}"])

    show = [PRIMARY, "LEG"] + LEGS
    hdr = ["时段"] + [NAMES[n] for n in show]
    vol_rows = [[LABEL[bk]] + [f1(blocks[n][bk]["sigma_1h_bps"]) for n in show]
                for bk in FINE + ["REG", "BONUS"]]
    var_rows = [[LABEL[bk]] + [f1(blocks[n][bk]["pct_variance"]) + "%" for n in show]
                for bk in FINE + ["REG", "BONUS"]]
    mx_rows = [[LABEL[bk]] + [f0(blocks[n][bk]["max_abs_bps"]) for n in show] for bk in FINE]
    mx_rows.append(["<b>全样本</b>"] + [f"<b>{f0(blocks[n]['ALL']['max_abs_bps'])}</b>" for n in show])

    def risk_table(name):
        rows = []
        for bk in FINE:
            b = blocks[name][bk]
            rows.append([LABEL[bk], f0(b["path_per_session_bps"]), f"{b['net_per_session_bps']:+,.0f}",
                         f1(b["path_per_session_bps"] / abs(b["worst_dd_bps"])),
                         f"<span class='b'>{f0(b['worst_dd_bps'])}</span>", f0(b["dd_p05_bps"]),
                         f0(b["dd_median_bps"]), f0(b["worst_up_bps"]), f0(b["max_abs_bps"])])
        return table(["时段", "平均路径 Σ|Δ|", "平均净变动", "路径/|最差回撤|", "最差回撤",
                      "回撤 P95", "回撤中位", "最大反弹", "最大单分钟"], rows)

    def lev_rows_of(name):
        return [[LABEL[bk], f0(lev[name][bk]["cum_sigma_bps"]),
                 f1(lev[name][bk]["risk_vs_rth"]),
                 f"<b>{f2(lev[name][bk]['k_equal_risk'])}×</b>",
                 f1(lev[name][bk]["oi_time_eff"]),
                 f0(lev[name][bk]["worst_dd_bps"]), lev[name][bk]["sessions"]] for bk in FINE]

    oi_rows = [[NAMES[n], f0(oi[n]["isx"]), f0(oi[n]["os"]), f2(oi[n]["ratio"]),
                f"{f0(oi[n]['share'])}% / {f0(100-oi[n]['share'])}%",
                f0(oi[n]["max_down"]), f0(oi[n]["intra_max_down"])]
               for n in [PRIMARY, "LEG"] + LEGS]
    m_rows = [[s, f1(float(mk[s]["initialMarginFraction"]) * 100) + "%",
               f1(float(mk[s]["offHoursInitialMarginFraction"]) * 100) + "%",
               f2(float(mk[s]["initialMarginFraction"]) / float(mk[s]["offHoursInitialMarginFraction"])),
               f1(float(mk[s]["maintenanceMarginFraction"]) * 100) + "%",
               f"${float(mk[s]['volume24hNotional'])/1e6:.2f}M",
               f"${float(mk[s]['openInterest'])*float(mk[s]['oraclePrice'])/1e3:,.0f}k"] for s in LEGS]
    mv_rows = [[m[2], SHORT[m[0]], m[1], f"<span class='{'k' if m[3] > 0 else 'b'}'>{m[3]:+,.0f}</span>"]
               for m in out["top_moves"][:20]]
    cnt = lambda b: sum(1 for m in out["top_moves"][:20] if m[0] == b)
    ws_rows = [[LABEL[w["bucket"]], w["at"], f"<span class='b'>{f0(w['dd'])}</span>",
                f0(w["avg_dd"]), f0(w["avg_up"]), w["sessions"]] for w in out["worst_sessions"]]
    flow_rows = [[LABEL[bk], f1(flow_fine[bk]["pct_notional"]) + "%",
                  f1(flow_fine[bk]["pct_active"]) + "%", f1(flow_fine[bk]["notional"]/1e3) + "k"]
                 for bk in FINE]
    flow_rows.append(["<b>加成窗口</b>", f"<b>{f1(flow_arcus['BONUS']['pct_notional'])}%</b>",
                      f"<b>{f1(flow_arcus['BONUS']['pct_active'])}%</b>",
                      f"<b>{f1(flow_arcus['BONUS']['notional']/1e3)}k</b>"])
    legflow_rows = [[NAMES[s], f"${leg_flow[s]['notional']/1e6:.2f}M",
                     f1(leg_flow[s]['pct_active']) + "%", f1(leg_flow[s]['bonus_share']) + "%"]
                    for s in LEGS]
    dd_rows = []
    for bk in FINE:
        b = sp[bk]
        dd_rows.append([LABEL[bk], f"<span class='b'>{f0(b['worst_dd_bps'])}</span>",
                        f0(b["dd_p05_bps"]), f0(b["dd_median_bps"]),
                        f"{f0(abs(b['worst_dd_bps'])/bud['L1']*100)}%",
                        f"<b>{f2(bud['L1']/abs(b['worst_dd_bps']))}×</b>",
                        f"{f2(bud['L2']/abs(b['worst_dd_bps']))}×",
                        f"{f2(bud['L2']/abs(b['dd_p05_bps']))}×"])
    plan_rows = []
    for lab, a, o, w in (("A · 全程 1×（现状）", 1.0, 1.0, 1.0),
                         ("B · 按等风险加杠杆", 1.0, b_ovn, b_wknd),
                         ("C · 加成窗口 1.5×", 1.0, 1.5, 1.5),
                         ("D · 加成窗口 2.0×", 1.0, 2.0, 2.0)):
        oi_wk = wk(a, o, w)
        risk = max(a, o * lv["OVN"]["risk_vs_rth"], w * lv["WKND"]["risk_vs_rth"])
        plan_rows.append([lab, f"{f1(a)}× / {f1(o)}× / {f1(w)}×", f0(oi_wk),
                          f"{oi_wk/base_oi:.2f}×", f"{f1(risk)}×"])
    sig = {k: sp[k]["sigma_1h_bps"] for k in ("REG", "OVN", "WKND")}
    ref = lv["RTH"]["cum_sigma_bps"]
    w_basket = SPREADS[PRIMARY]
    wtxt = " / ".join(f"{NAMES[k]} {v:.2%}" for k, v in w_basket["long"].items())
    mkr = mk.get("DRAM-USD", {})

    page = f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>分时段统计：波动、回撤、最大变化与杠杆空间</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>交易时段 vs 非交易时段：波动、回撤、最大变化 —— 以及非交易时段能加多少杠杆</h1>
<div class="sub">主口径：<b>{w_basket['full']}</b> ·
对照：{SPREADS['LEG']['full']} · 1 分钟线 {out['meta']['bars']:,} 根（{span}，{days:.1f} 天，
4 条腿时间戳完全对齐）· {out['meta']['us_days']} 个美股交易日 ·
生成 {time.strftime('%Y-%m-%d %H:%M')}</div>

<div class="cards">{cards}</div>

<h2>1 · 方向与时段口径</h2>
<h3>1.1 主口径已换成实际要做的方向</h3>
<p><code>{w_basket['zh']}</code> = 多 $1 篮子（{wtxt}，按 DRAM 持仓权重在「可交易部分」
{0.5414:.2%} 内归一）、空 $1 DRAM，美元中性。权重来自 <code>holdings.json</code>：
DRAM 的 22 只持仓里只有 MU 27.36% + SKHY 22.13% + SNDK 4.65% = {0.5414:.2%} 在 Arcus 有对应永续。
对照行是上一版报告的相反方向 <code>{SPREADS['LEG']['zh']}</code>，两者在同一张表里并列。</p>
<p>要强调一点：<b>方向反过来不等于数字取负</b>。两条口径的腿数和权重都不同
（0.51 / 0.41 / 0.09 对 0.71 / 0.29），再加上 SNDK 这条额外风险源，
所以波动率、回撤、杠杆空间都必须重算 —— 本页全部是重算结果。</p>
<h3>1.2 Arcus 的「非交易时段」到底含不含盘前盘后？</h3>
<p><b>不含。</b>官方 <code>/v1/markets</code> 的 <code>regularTradingHours</code> 字段写得很明确：
<code>startSecondsOfDay</code> = 14400（04:00 ET）、<code>endSecondsOfDay</code> = 72000（20:00 ET）、
<code>timezone</code> = America/New_York。</p>
<ul>
<li>04:00-09:30 ET 盘前 → <b>Arcus 常规时段</b>（无积分加成、初始保证金 10%）</li>
<li>09:30-16:00 ET 盘中 → 常规时段</li>
<li>16:00-20:00 ET 盘后 → <b>Arcus 常规时段</b>（无积分加成、初始保证金 10%）</li>
<li>20:00-04:00 ET ＋ 周末/假期 → <b>加成窗口</b>（<code>offHoursInitialMarginFraction</code> = 15%）</li>
</ul>
<p>现场验证：抓这份快照的时刻是 <b>2026-10-08 06:30 ET（盘前）</b>，同一份 JSON 里
<code>isOutsideRth</code> = <b>False</b>（DRAM / MU / SKHY / SNDK / QQQ / SPY 全部 False）。
也就是说盘前这一刻，Arcus 认定它就是「常规时段」。保证金同理：15% 只在 20:00-04:00 与周末生效。</p>
{table(["时段", "ET 时间窗", "每周时长", "样本时间占比", "Arcus 积分", "样本场次"], def_rows)}
<p>注意 OVN 正好覆盖 KOSPI 的 09:00-15:30 KST（00:00-06:30 UTC）—— SKHY 的母国盘就在加成窗口里。
而盘前盘后这两段（每周 47.5 h）是「美股不开盘、Arcus 也不给加成」的双输窗口：
风险照担，积分不拿。它们属于 Arcus 常规时段，所以在下面的表里被算进「常规」。</p>

<h2>2 · 波动率的 24 小时剖面</h2>
<div class="panel">{hour_bars(profile, PMAX)}</div>
<p>13:00 UTC（09:00 ET）是尖峰：{w_basket['zh']} {f1(profile[PRIMARY][13])} bps/分钟，
MU / SKHY 到 {f0(profile['MU-USD'][13])} / {f0(profile['SKHY-USD'][13])}；
00:00 UTC（20:00 ET，加成窗口起点兼 KOSPI 开盘）是第二峰 {f1(profile[PRIMARY][0])}。
图中浅黄区是盘前 —— 它没有积分加成，但波动已经明显抬起来。</p>

<h2>3 · 分时段波动率</h2>
<h3>3.1 小时 σ（bps）</h3>
{table(hdr, vol_rows)}
<h3>3.2 方差贡献占比（对照时间占比）</h3>
{table(hdr, var_rows)}
<p>主口径下 RTH 只占 {f1(sp['RTH']['pct_minutes'])}% 的时间、贡献 {f1(sp['RTH']['pct_variance'])}% 的方差。
按 Arcus 口径：常规 {f1(reg['pct_variance'])}% 的方差 / {f1(reg['pct_minutes'])}% 的时间，
加成 {f1(bon['pct_variance'])}% 的方差 / {f1(bon['pct_minutes'])}% 的时间 ——
<b>加成窗口用一半以上的时间只产生三分之一的方差</b>，这正是加杠杆想法的来源。</p>
<p>方向对照：RTH 的 σ_1h 主口径 {f1(sp['RTH']['sigma_1h_bps'])} bps、对照方向
{f1(blocks['LEG']['RTH']['sigma_1h_bps'])} bps，量级接近但不相等；
差异主要来自权重不同（对照的 MU 权重 0.71 vs 主口径 0.51）与 SNDK 这条额外腿。</p>

<h2>4 · 每场时段的回撤与最大变化</h2>
<div class="panel">{path_lines(paths)}</div>
<p>主口径在每个时段内「累计 |Δ|」的平均推进：RTH 前 30 分钟吃掉一大截（开盘那一跳），
隔夜匀速，周末因为横跨 48 h 累积最长。</p>
<h3>4.1 {w_basket['zh']}（主口径）</h3>
{risk_table(PRIMARY)}
<h3>4.2 {SPREADS['LEG']['zh']}（对照方向）</h3>
{risk_table("LEG")}
<p><b>换方向以后回撤的形状变了，这一条很重要：</b>主口径的 RTH 回撤明显更浅
（{f0(ddr)} vs 对照方向 {f0(blocks['LEG']['RTH']['worst_dd_bps'])} bps）—— 篮子确实把单腿跳空分散掉了；
但<b>隔夜反而更深</b>（{f0(sp['OVN']['worst_dd_bps'])} vs
{f0(blocks['LEG']['OVN']['worst_dd_bps'])}），周末也更深（{f0(ddb)} vs
{f0(blocks['LEG']['WKND']['worst_dd_bps'])}）。
主口径全样本最危险的一场落在 <b>{LABEL[worst_bk]}（{f0(worst_bk_dd)} bps）</b>，
而对照方向是落在 RTH。上一版那句「最差回撤都在交易时段」对本方向<b>不成立</b>。</p>
<h3>4.3 最差场次明细（主口径）</h3>
{table(["时段", "最差回撤时刻 (UTC)", "最差回撤", "均值回撤", "均值反弹", "场次"], ws_rows)}
<h3>4.4 最大单分钟变动（各时段 × 各序列，bps）</h3>
{table(hdr, mx_rows)}
<p>主口径 spread 最厚的尾部在 RTH（{f0(sp['RTH']['max_abs_bps'])} bps），
其后是周末 {f0(sp['WKND']['max_abs_bps'])}、隔夜 {f0(sp['OVN']['max_abs_bps'])}。
单腿能到 {f0(blocks['SNDK-USD']['ALL']['max_abs_bps'])}（SNDK）与
{f0(blocks['MU-USD']['ALL']['max_abs_bps'])} bps（MU），组合把大部分跳空吸收掉了。</p>
<h3>4.5 全部序列混排 Top 20</h3>
{table(["序列", "时段", "时间 (UTC)", "变动 bps"], mv_rows)}
<p>分布：交易 {cnt('RTH')}/20、盘前 {cnt('PRE')}/20、隔夜 {cnt('OVN')}/20、
周末 {cnt('WKND')}/20、盘后 {cnt('AH')}/20。<b>盘前占 {cnt('PRE')}/20，却没有积分加成</b> ——
这是「担风险不拿分」的最典型窗口。</p>

<h2>5 · 隔夜 vs 日内</h2>
{table(["序列", "日内 σ (bps)", "隔夜 σ (bps)", "隔夜/日内", "隔夜方差占比", "最差隔夜下跌", "最差日内回撤"], oi_rows)}
<p>主口径隔夜占日方差 {f0(oi[PRIMARY]['share'])}%，但隔夜窗口（17.5 h）是日内（6.5 h）的 2.7 倍长，
按每小时折算，<b>日内的波动密度是隔夜的
{f1((oi[PRIMARY]['isx']/6.5)/(oi[PRIMARY]['os']/17.5))} 倍</b>。</p>

<h2>6 · 成交分布</h2>
{table(["时段", "名义成交占比", "有成交分钟占比", "名义成交"], flow_rows)}
<h3>6.1 四条腿的流动性（60 天）</h3>
{table(["标的", "名义成交", "有成交分钟占比", "其中落在加成窗口"], legflow_rows)}
<p><b>SKHY 是最小的腿：${leg_flow['SKHY-USD']['notional']/1e6:.2f}M，只有 SNDK
（${leg_flow['SNDK-USD']['notional']/1e6:.2f}M）的
{leg_flow['SKHY-USD']['notional']/leg_flow['SNDK-USD']['notional']:.0%}</b>，
而它的权重是第二大（{0.2213/0.5414:.0%}）。这是主口径真实存在的容量瓶颈。</p>

<h2>7 · 非交易时段能加多少杠杆？</h2>
<p>两个口径给出不同答案：<b>按波动率只有 {f2(b_ovn)}×（隔夜）/ {f2(b_wknd)}×（周末）；
按账户自己的回撤预算能到 {f2(d_ovn)}× / {f2(d_wknd)}×。</b>
差额就是开盘缺口 —— 波动率口径看不见它，回撤口径又因为样本太短而低估它。</p>
<div class="panel">{risk_horizon(sig, ref)}</div>
<p>周末曲线要持有 48 h 才到 {f0(lv['WKND']['cum_sigma_bps'])} bps，是「一个 RTH 时段」
（{f0(ref)} bps）的 {f1(lv['WKND']['risk_vs_rth'])} 倍：<b>密度低，但窗口长</b>。
隔夜（8 h）到 {f0(lv['OVN']['cum_sigma_bps'])} bps，只有 0.83 倍，所以隔夜能加、周末不该加。</p>
<h3>7.1 等风险杠杆表（主口径）</h3>
{table(["时段", "单场累计 σ", "相对 RTH 风险", "可加杠杆 k", "单位风险 OI-time", "单场最差回撤", "场次"], lev_rows_of(PRIMARY))}
<h3>7.2 对照方向</h3>
{table(["时段", "单场累计 σ", "相对 RTH 风险", "可加杠杆 k", "单位风险 OI-time", "单场最差回撤", "场次"], lev_rows_of("LEG"))}
<p>两个方向的 k 量级接近（隔夜 {f2(b_ovn)} vs {f2(lv2['OVN']['k_equal_risk'])}、
周末 {f2(b_wknd)} vs {f2(lv2['WKND']['k_equal_risk'])}），但<b>周末这个数翻了方向</b>：
对照方向只有 0.93×（周末该减仓），主口径是 {f2(b_wknd)}×（刚好持平）。
换方向后隔夜的空间也略大（{f2(b_ovn)} vs {f2(lv2['OVN']['k_equal_risk'])}）。
所以「时段属性」这个说法要收紧：<b>大方向一致，但具体倍数随 spread 的构成而变</b>，必须按实际持仓重算。</p>
<h3>7.3 保证金约束</h3>
{table(["标的", "常规初始保证金", "非交易时段", "同占用可挂名义倍数", "维持保证金", "24h 名义成交", "当前 OI"], m_rows)}
<p>加成窗口初始保证金是常规的 1.5 倍 → 同样的保证金占用下能挂的名义只有 <b>0.67×</b>，
和「加杠杆」正好相反。资金宽松时这条不紧；一旦吃满，加成窗口会先撞墙。</p>
<h3>7.4 开盘缺口（真正的爆仓来源）</h3>
{table(["指标", "主口径", "对照方向", "说明"], [
    ["进入 RTH 第一分钟 σ", f"{f1(g['gap_sigma'])} bps", f"{f1(gaps['LEG']['gap_sigma'])} bps",
     f"是加成窗口分钟 σ（{f1(bon['sigma_1m_bps'])}）的 {f1(g['gap_sigma']/bon['sigma_1m_bps'])}×"],
    ["最差开盘跳空", f"{f0(g['gap_max_down'])} bps", f"{f0(gaps['LEG']['gap_max_down'])} bps",
     f"样本内 {g['n']} 个交易日的最差一次"],
    ["跳空 >20 bps 的占比", f"{f1(g['gap_pct_gt_20'])}%", f"{f1(gaps['LEG']['gap_pct_gt_20'])}%",
     "约每 4-5 个交易日一次"],
    ["开盘后 5 分钟最差", f"{f0(g['first5_max_abs'])} bps", f"{f0(gaps['LEG']['first5_max_abs'])} bps",
     "降杠杆的可操作窗口只有这几分钟"]])}
<h3>7.5 用账户自己的回撤预算算（每边 L = {f1(lev_book)}×）</h3>
<p>5% / 10% / 22% 的权益回撤告警线，对应 spread 走 {f0(bud['L1'])} / {f0(bud['L2'])} /
{f0(bud['L3'])} bps。</p>
{table(["时段", "单场最差回撤", "回撤 P95", "回撤中位", "占 L1 预算", "顶到 L1 的倍数", "顶到 L2 的倍数", "按 P95 顶到 L2"], dd_rows)}
<p>按回撤预算，隔夜还有 {f2(d_ovn)}×、周末 {f2(d_wknd)}×、交易时段
{f2(bud['L1']/abs(ddr))}×。注意主口径里<b>最深的单场回撤出现在隔夜
（{f0(sp['OVN']['worst_dd_bps'])} bps），不是交易时段</b>，所以隔夜的空间比对照方向小
（{f2(d_ovn)}× vs 对照 {f2(bud['L1']/abs(blocks['LEG']['OVN']['worst_dd_bps']))}×），
<b>两个口径的差额就是 gap 风险</b>；周末只有 {sp['WKND']['sessions']} 场样本，尾部远没被采样到，
所以取两者之间，不要取上限。</p>
<h3>7.6 每周 OI-time 推演（加杠杆到底买到什么）</h3>
{table(["方案", "常规 / 隔夜 / 周末 名义倍数", "每周 OI-time (N·h)", "相对 A", "最大单场风险（1.0 = 一个 RTH 时段）"], plan_rows)}
<p>按等风险加杠杆，每周 OI-time 只从 {f0(base_oi)} 涨到
{f0(wk(1.0, b_ovn, b_wknd))}（{f2(wk(1.0, b_ovn, b_wknd)/base_oi)}×）—— 几乎白忙。
加成窗口整体 1.5× / 2.0× 能买到 1.26× / 1.52× 的 OI-time，但最大单场风险也从 1.1 涨到 1.6 / 2.1：
<b>加杠杆换 OI-time 基本是 1:1 交换</b>，划不划算取决于官方加成倍数，不是波动率。
真正免费的是时间：单位风险的 OI-time，周末是 RTH 的 {f1(lv['WKND']['oi_time_eff'])}×、
隔夜 {f1(lv['OVN']['oi_time_eff'])}×。</p>

<h2>8 · 落地建议</h2>
{table(["问题", "答案", "依据"], [
    ["Arcus 非交易时段含盘前盘后吗", "不含。常规 = 04:00-20:00 ET",
     "regularTradingHours 字段 + 06:30 ET 实测 isOutsideRth=False"],
    ["非交易时段波动更低吗", f"是，σ_1h 低到 1/{f2(ratio1h)}",
     f"{f1(reg['sigma_1h_bps'])} → {f1(bon['sigma_1h_bps'])} bps/小时"],
    ["能按这个比例加杠杆吗", f"不能。等风险口径只有 {f2(b_ovn)}×（隔夜）/ {f2(b_wknd)}×（周末）",
     f"周末连挂 48 h，累计 σ {f0(lv['WKND']['cum_sigma_bps'])} bps 已超过一个 RTH 时段"],
    ["那按回撤预算呢", f"隔夜 {f2(d_ovn)}×、周末 {f2(d_wknd)}× 才顶到 L1",
     f"最深单场回撤在 {LABEL[worst_bk]}（{f0(worst_bk_dd)} bps），不是交易时段"],
    ["建议参数", "隔夜 1.4×、周末 1.2×，常规时段 1.0×，04:00 ET 起降回 1.0×",
     "在等风险杠杆与回撤预算之间取中，并向周末样本不足的一侧偏"],
    ["盘前盘后怎么办", "不加仓：无积分加成，却占 4.5 节尾部事件的大头",
     f"盘前占 Top20 的 {cnt('PRE')}/20"],
    ["保证金警戒", "非交易时段占用率按 15% 算", "同名义占用率高 1.5 倍，跨保证金会挤压其它标的"],
    ["容量瓶颈", "SKHY 腿（成交最小、权重第二大）",
     f"${leg_flow['SKHY-USD']['notional']/1e6:.2f}M vs SNDK ${leg_flow['SNDK-USD']['notional']/1e6:.2f}M"]])}

<h2>9 · 口径与复现</h2>
<ul>
<li>数据：<code>spread-lab/data/candles_*_1m.json</code>（Arcus <code>/v1/candles</code>），
4 条腿各 {out['meta']['bars']:,} 根、时间戳完全对齐，{span}（{days:.1f} 天）。</li>
<li>主口径权重 = DRAM 持仓权重 ÷ 可交易占比 {0.5414:.2%}：MU {0.2736/0.5414:.4f}、
SKHY {0.2213/0.5414:.4f}、SNDK {0.0465/0.5414:.4f}；空 DRAM 1.0000。</li>
<li>保证金 / 费率 / regularTradingHours 来自 <code>data/markets.json</code>（{time.strftime('%Y-%m-%d')} 抓取）：
股票类 imf 10% / off-hours 15% / 维持
{f1(float(mkr.get('maintenanceMarginFraction', 0.066667))*100)}%，funding
{float(mkr.get('fundingRate', 0.000005093))*100:.6f}%/h ≈ 4.5%/年。</li>
<li>时段判定用 <code>America/New_York</code>；样本期内全程 EDT，RTH = 13:30-20:00 UTC。</li>
<li>分钟收益归其起始分钟的时段；跨时段那一分钟算给前一段。</li>
<li>σ_1h 用「同一时段内连续 60 根 1 分钟线」直接算，不做 √60 外推。</li>
<li>2026-09-07 Labor Day（美股休市、Arcus 照常报价）计入样本。</li>
<li>脚本：<code>python3 sessions.py</code> → <code>sessions_stats.json</code> + 本页。</li>
</ul>
<div class="foot">单位统一为「单腿 $1 名义」的 bps。生成于 {time.strftime('%Y-%m-%d %H:%M')}。</div>
</div></body></html>"""
    OUTDIR.mkdir(parents=True, exist_ok=True)
    (OUTDIR / "spread-sessions.html").write_text(page)
    (HERE / "report-sessions.html").write_text(page)
    print(f"wrote {OUTDIR / 'spread-sessions.html'} ({len(page)/1024:.0f} KB)")


if __name__ == "__main__":
    main()
