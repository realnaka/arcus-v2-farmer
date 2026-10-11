#!/usr/bin/env python3
"""Tiny pure-python statistics kit (no numpy in this environment)."""

from __future__ import annotations

import math


def mean(xs):
    return sum(xs) / len(xs)


def stdev(xs, ddof=1):
    m = mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - ddof)
    return math.sqrt(var)


def quantile(xs, q):
    s = sorted(xs)
    if not s:
        return float("nan")
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def skew(xs):
    m, s = mean(xs), stdev(xs, ddof=0)
    n = len(xs)
    return sum(((x - m) / s) ** 3 for x in xs) * n / ((n - 1) * (n - 2))


def kurtosis(xs):
    m, s = mean(xs), stdev(xs, ddof=0)
    return sum(((x - m) / s) ** 4 for x in xs) / len(xs) - 3.0


def corr(xs, ys):
    mx, my = mean(xs), mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy)


# ------------------------------------------------------------------ linear algebra


def solve(a, b):
    """Gaussian elimination with partial pivoting. a is n x n (list of lists)."""
    n = len(a)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-14:
            raise ValueError("singular matrix")
        m[col], m[piv] = m[piv], m[col]
        pv = m[col][col]
        for r in range(col + 1, n):
            f = m[r][col] / pv
            if f:
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        acc = m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))
        x[r] = acc / m[r][r]
    return x


def xtx_inverse(x):
    k = len(x[0])
    a = [[sum(row[i] * row[j] for row in x) for j in range(k)] for i in range(k)]
    inv = []
    for i in range(k):
        e = [0.0] * k
        e[i] = 1.0
        inv.append(solve(a, e))
    return [[inv[j][i] for j in range(k)] for i in range(k)]  # transpose -> (X'X)^-1


class OLS:
    def __init__(self, x, y):
        self.x, self.y = x, y
        self.n, self.k = len(x), len(x[0])
        self.beta = solve(
            [[sum(row[i] * row[j] for row in x) for j in range(self.k)] for i in range(self.k)],
            [sum(row[i] * yy for row, yy in zip(x, y)) for i in range(self.k)],
        )
        self.fitted = [sum(b * v for b, v in zip(self.beta, row)) for row in x]
        self.resid = [yy - f for yy, f in zip(y, self.fitted)]
        self.ssr = sum(r * r for r in self.resid)
        self.dof = self.n - self.k
        self.sigma2 = self.ssr / self.dof
        my = mean(y)
        self.sst = sum((yy - my) ** 2 for yy in y)
        self.r2 = 1 - self.ssr / self.sst if self.sst else 0.0
        self.adj_r2 = 1 - (1 - self.r2) * (self.n - 1) / self.dof if self.dof else 0.0

    @property
    def se(self):
        inv = xtx_inverse(self.x)
        return [math.sqrt(self.sigma2 * inv[i][i]) for i in range(self.k)]

    def tstat(self, i):
        return self.beta[i] / self.se[i]


# ------------------------------------------------------------------ time series tests

# MacKinnon (1994) asymptotic critical values, constant only, no trend.
ADF_CV = {"1%": -3.43, "5%": -2.86, "10%": -2.57}


def adf(y, lags=None, label="y"):
    """Augmented Dickey-Fuller: dy_t = a + rho*y_{t-1} + sum phi_i dy_{t-i}."""
    n = len(y)
    if lags is None:
        lags = max(1, int(math.ceil(12 * (n / 100.0) ** 0.25)))
    dy = [y[i] - y[i - 1] for i in range(1, n)]
    rows, target, idx = [], [], []
    for t in range(lags, len(dy)):
        rows.append([1.0, y[t]] + [dy[t - i] for i in range(1, lags + 1)])
        target.append(dy[t])
        idx.append(t)
    fit = OLS(rows, target)
    rho, se = fit.beta[1], fit.se[1]
    t = rho / se
    verdict = "reject unit root (stationary)" if t < ADF_CV["5%"] else "cannot reject unit root"
    return {
        "label": label,
        "n": len(target),
        "lags": lags,
        "rho": rho,
        "t_stat": t,
        "critical_values": ADF_CV,
        "verdict_5pct": verdict,
        "half_life_bars": (-math.log(2) / math.log(1 + rho)) if -2 < rho < 0 else None,
    }


def half_life(y):
    """AR(1) half-life of deviations: dy_t = a + b*y_{t-1}."""
    dy = [y[i] - y[i - 1] for i in range(1, len(y))]
    fit = OLS([[1.0, y[i]] for i in range(len(y) - 1)], dy)
    b = fit.beta[1]
    hl = -math.log(2) / math.log(1 + b) if -2 < b < 0 else float("inf")
    return {"b": b, "half_life_bars": hl, "t_stat": fit.tstat(1), "r2": fit.r2}


def variance_ratios(y, lags=(2, 4, 8, 16, 32, 64)):
    r = [y[i] - y[i - 1] for i in range(1, len(y))]
    out = {}
    v1 = stdev(r, ddof=1) ** 2
    for q in lags:
        if q >= len(r):
            continue
        agg = [sum(r[i:i + q]) for i in range(0, len(r) - q + 1, q)]
        out[q] = stdev(agg, ddof=1) ** 2 / (q * v1)
    return out


def hurst(y, lags=(1, 2, 4, 8, 16, 32, 64)):
    """Aggregated-variance Hurst: slope of log(std of q-step sum) vs log(q)."""
    r = [y[i] - y[i - 1] for i in range(1, len(y))]
    pts = []
    for q in lags:
        if q * 4 >= len(r):
            continue
        agg = [sum(r[i:i + q]) for i in range(0, len(r) - q + 1, q)]
        pts.append((math.log(q), math.log(stdev(agg, ddof=1))))
    mx = mean([p[0] for p in pts])
    my = mean([p[1] for p in pts])
    num = sum((p[0] - mx) * (p[1] - my) for p in pts)
    den = sum((p[0] - mx) ** 2 for p in pts)
    return num / den


def rolling(xs, window, fn):
    """Rolling stat; first `window-1` entries are None."""
    out = [None] * len(xs)
    for i in range(window - 1, len(xs)):
        out[i] = fn(xs[i - window + 1:i + 1])
    return out


def ewma(xs, alpha):
    out, prev = [], None
    for x in xs:
        prev = x if prev is None else alpha * x + (1 - alpha) * prev
        out.append(prev)
    return out


def ewm_std(xs, alpha):
    out, mu, var = [], None, None
    for x in xs:
        if mu is None:
            mu, var = x, 0.0
        else:
            d = x - mu
            mu = mu + alpha * d
            var = (1 - alpha) * (var + alpha * d * d)
        out.append(math.sqrt(var))
    return out


def max_drawdown(series):
    peak, worst, worst_at = series[0], 0.0, 0
    for i, v in enumerate(series):
        peak = max(peak, v)
        dd = v - peak
        if dd < worst:
            worst, worst_at = dd, i
    return worst, worst_at


def lag_autocorr(r, lag):
    a, b = r[:-lag], r[lag:]
    return corr(a, b)
