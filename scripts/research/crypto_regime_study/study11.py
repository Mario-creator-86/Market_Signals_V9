"""Is the cross-sectional momentum result of study10.py real? Robustness checks.

usage: study11.py <klines dir, ~730 days of 1h bars> <funding dir from fetch_funding.py>

Rule under test: every week rank the liquid coins by their return over the last
14 days, hold the top fifth long and the bottom fifth short (equal weight,
dollar neutral). study10.py: +196%, Sharpe 1.98, t 2.77, 6 of 9 quarters.

Checks: neighbouring parameters, the weekday of the re-ranking, higher costs,
one day of delay, funding payments, liquidity floor, dependence on the market
falling (beta), dependence on a few coins, and each leg against the average coin.
"""
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DATA, FUND = Path(sys.argv[1]), Path(sys.argv[2])
meta = json.loads((DATA / "_symbols.json").read_text())
daily = {}
for sym in meta["symbols"]:
    arr = np.loadtxt(DATA / f"{sym}.csv", delimiter=",", skiprows=1)
    if arr.ndim != 2 or len(arr) < 24 * 200:
        continue
    t, o, h, l, c, qv = arr.T
    g = pd.DataFrame({"d": (t // 86_400_000).astype(np.int64), "c": c, "v": qv}).groupby("d")
    fund = None
    f = FUND / f"{sym}.csv"
    if f.exists():
        fa = np.loadtxt(f, delimiter=",", skiprows=1)
        if fa.ndim == 2 and len(fa) > 200:
            fund = pd.Series(fa[:, 1]).groupby((fa[:, 0] // 86_400_000).astype(np.int64)).sum()
    daily[sym] = {"d": g["c"].last().index.to_numpy(), "c": g["c"].last().to_numpy(), "v": g["v"].sum().to_numpy(), "f": fund}

all_days = np.unique(np.concatenate([s["d"] for s in daily.values()]))
idx = {int(d): i for i, d in enumerate(all_days)}
N, M = len(all_days), len(daily)
syms = sorted(daily)
C = np.full((N, M), np.nan); V = np.full((N, M), np.nan); F = np.full((N, M), np.nan)
for j, sym in enumerate(syms):
    s = daily[sym]
    rows = [idx[int(d)] for d in s["d"]]
    C[rows, j], V[rows, j] = s["c"], s["v"]
    if s["f"] is not None:
        for d, rate in s["f"].items():
            if int(d) in idx:
                F[idx[int(d)], j] = rate
R = np.full((N, M), np.nan); R[1:] = C[1:] / C[:-1] - 1
np.seterr(invalid="ignore", divide="ignore")
avg_vol = np.full((N, M), np.nan)
for i in range(7, N):
    avg_vol[i] = np.nanmean(V[i - 7:i], axis=0)
btc = syms.index("BTCUSDT")


def quarter(d):
    x = dt.date.fromordinal(dt.date(1970, 1, 1).toordinal() + int(d))
    return f"{x.year % 100}Q{(x.month - 1) // 3 + 1}"


def past_return(n):
    out = np.full((N, M), np.nan); out[n:] = C[n:] / C[:-n] - 1
    return out


def ranked(n, hold=7, frac=5, offset=0, floor=10e6, skip=0):
    metric = past_return(n)
    if skip:
        metric = np.vstack([np.full((skip, M), np.nan), metric[:-skip]])
    liquid = avg_vol >= floor
    sig = np.zeros((N, M)); last = np.zeros(M)
    for i in range(N):
        if i % hold == offset % hold:
            last = np.zeros(M)
            ok = np.isfinite(metric[i]) & liquid[i]
            if ok.sum() >= 15:
                k = max(1, int(ok.sum() // frac))
                order = np.argsort(np.where(ok, metric[i], np.nan))[:ok.sum()]
                last[order[-k:]] = 1.0; last[order[:k]] = -1.0
        sig[i] = last
    return sig


def run(sig, cost=0.00065, delay=1, ret=None, floor=10e6, drop=()):
    ret = R if ret is None else ret
    pos = np.zeros((N, M)); pos[delay:] = sig[:-delay]
    pos = np.where(np.isfinite(ret) & (avg_vol >= floor), pos, 0.0)
    for j in drop:
        pos[:, j] = 0.0
    prev = np.vstack([np.zeros((1, M)), pos[:-1]])
    n_act = np.maximum(1, (pos != 0).sum(axis=1))
    contrib = pos * np.nan_to_num(ret) / n_act[:, None]
    net = contrib.sum(axis=1) - np.abs(pos - prev).sum(axis=1) / n_act * cost
    live = (pos != 0).sum(axis=1) > 0
    return net, live, contrib


def stats(net, live):
    x = net[live]; days = all_days[live]
    sd = x.std(ddof=1)
    sharpe = x.mean() / sd * np.sqrt(365); t = x.mean() / (sd / np.sqrt(len(x)))
    eq = np.cumprod(1 + x); dd = (eq / np.maximum.accumulate(eq) - 1).min()
    qs = {}
    for d, r in zip(days, x):
        qs.setdefault(quarter(d), []).append(r)
    return sharpe, t, 100 * (eq[-1] - 1), 100 * dd, sum(1 for v in qs.values() if np.sum(v) > 0), len(qs), qs


def line(name, net, live):
    s, t, tot, dd, good, nq, _ = stats(net, live)
    print(f"  {name:<58} total {tot:+7.1f}%  Sharpe {s:+5.2f}  t {t:+5.2f}  maxDD {dd:6.1f}%  quarters {good}/{nq}")


base_sig = ranked(14)
net, live, contrib = run(base_sig)
print("=== Base rule (14-day return, weekly, top/bottom fifth, 6.5 bps)")
line("base", net, live)
print("  per quarter: " + " ".join(f"{q}:{100 * np.sum(v):+.0f}%" for q, v in sorted(stats(net, live)[6].items())))

print("\n=== Sharpe by lookback (rows) and holding period in days (columns), top/bottom fifth")
print("  lookback " + "".join(f"{h:>8}d" for h in (1, 3, 7, 14)))
for n in (3, 5, 7, 10, 14, 21, 28, 42):
    cells = []
    for hold in (1, 3, 7, 14):
        s = stats(*run(ranked(n, hold))[:2])
        cells.append(f"{s[0]:+5.2f}({s[4]}/{s[5]})")
    print(f"  {n:>6}d  " + " ".join(cells))

print("\n=== Day of the week on which the ranking is redone (14d, weekly)")
for off in range(7):
    line(f"offset {off}", *run(ranked(14, offset=off))[:2])

print("\n=== Size of the two groups")
for frac, label in ((3, "top/bottom third"), (5, "top/bottom fifth"), (10, "top/bottom tenth")):
    line(label, *run(ranked(14, frac=frac))[:2])

print("\n=== Skip the most recent day(s) in the ranking")
for skip in (1, 2):
    line(f"skip {skip} day(s)", *run(ranked(14, skip=skip))[:2])

print("\n=== Costs and delay")
for cost in (0.00065, 0.0015, 0.003):
    line(f"cost {cost * 1e4:.1f} bps per unit of turnover", *run(base_sig, cost=cost)[:2])
line("entered one day late", *run(base_sig, delay=2)[:2])
line("entered two days late", *run(base_sig, delay=3)[:2])

print("\n=== Funding payments (perpetuals: a long pays a positive rate, a short receives it)")
has_f = np.isfinite(F)
print(f"  coins with funding history: {int(has_f.any(axis=0).sum())} of {M}")
ret_f = np.where(has_f, R - F, np.nan)
line("price only, coins with a perpetual", *run(base_sig, ret=np.where(has_f, R, np.nan))[:2])
line("price and funding, coins with a perpetual", *run(base_sig, ret=ret_f)[:2])

print("\n=== Liquidity floor (average daily volume of the last 7 days)")
for floor in (10e6, 30e6, 50e6):
    liq = avg_vol >= floor
    line(f">= ${floor / 1e6:.0f}M (median {int(np.median(liq.sum(axis=1)[30:]))} coins a day)", *run(ranked(14, floor=floor), floor=floor)[:2])

print("\n=== The two legs, each against the average liquid coin")
liquid = avg_vol >= 10e6
basket = np.nanmean(np.where(liquid, R, np.nan), axis=1)
pos = np.zeros((N, M)); pos[1:] = base_sig[:-1]
pos = np.where(np.isfinite(R) & liquid, pos, 0.0)
for label, side in (("winners (long leg)", 1.0), ("losers (short leg, as a long)", -1.0)):
    mask = pos == side
    leg = np.array([np.nanmean(R[i][mask[i]]) if mask[i].any() else np.nan for i in range(N)])
    ok = np.isfinite(leg) & np.isfinite(basket)
    diff = leg[ok] - basket[ok]
    print(f"  {label:<32} mean daily {100 * np.nanmean(leg[ok]):+.3f}%  average coin {100 * basket[ok].mean():+.3f}%  "
          f"difference {100 * diff.mean():+.3f}% a day (t {diff.mean() / (diff.std(ddof=1) / np.sqrt(len(diff))):+.2f})")

print("\n=== Does it need a falling market? Regression of the daily result on the average coin and on BTC")
ok = live & np.isfinite(basket)
X = np.column_stack([np.ones(ok.sum()), basket[ok], R[ok, btc]])
beta, *_ = np.linalg.lstsq(X, net[ok], rcond=None)
resid = net[ok] - X @ beta
se = np.sqrt(resid.var(ddof=3) * np.linalg.inv(X.T @ X).diagonal())
print(f"  alpha {100 * beta[0]:+.3f}% a day (t {beta[0] / se[0]:+.2f}); beta to the average coin {beta[1]:+.2f} (t {beta[1] / se[1]:+.2f}); beta to BTC {beta[2]:+.2f}")
up = ok & (basket > 0); down = ok & (basket <= 0)
print(f"  days the average coin rose: {int(up.sum())}, mean {100 * net[up].mean():+.3f}%;  days it fell: {int(down.sum())}, mean {100 * net[down].mean():+.3f}%")
month = np.array([int(d) // 30 for d in all_days])
mret = pd.Series(basket).groupby(month).sum(); mnet = pd.Series(np.where(live, net, 0.0)).groupby(month).sum()
print(f"  30-day blocks in which the average coin rose: {int((mret > 0).sum())}, rule positive in {int(((mret > 0) & (mnet > 0)).sum())}; "
      f"blocks it fell: {int((mret <= 0).sum())}, rule positive in {int(((mret <= 0) & (mnet > 0)).sum())}")

print("\n=== Dependence on a few coins")
per_coin = contrib.sum(axis=0)
order = np.argsort(per_coin)[::-1]
print("  biggest contributors: " + ", ".join(f"{syms[j]} {100 * per_coin[j]:+.0f}%" for j in order[:6]))
print("  biggest detractors:   " + ", ".join(f"{syms[j]} {100 * per_coin[j]:+.0f}%" for j in order[-4:]))
line("without the 5 biggest contributors", *run(base_sig, drop=tuple(order[:5]))[:2])
line("without the 10 biggest contributors", *run(base_sig, drop=tuple(order[:10]))[:2])

print("\n=== First year / second year")
half = N // 2
for label, sl in (("first year", slice(0, half)), ("second year", slice(half, N))):
    x = net[sl][live[sl]]
    print(f"  {label:<12} total {100 * (np.prod(1 + x) - 1):+.1f}%  Sharpe {x.mean() / x.std(ddof=1) * np.sqrt(365):+.2f}  t {x.mean() / (x.std(ddof=1) / np.sqrt(len(x))):+.2f}")

print("\n=== Without picking the best setting: average over all 7 re-ranking days, and over lookbacks")
for label, lookbacks in (("14d, all 7 weekdays", (14,)), ("7/10/14/21/28d, all 7 weekdays", (7, 10, 14, 21, 28))):
    parts = [run(ranked(n, offset=off))[0] for n in lookbacks for off in range(7)]
    avg = np.mean(parts, axis=0)
    ok_days = np.zeros(N, dtype=bool); ok_days[45:] = True
    line(label, avg, ok_days)
    s = stats(avg, ok_days)
    print("    per quarter: " + " ".join(f"{q}:{100 * np.sum(v):+.0f}%" for q, v in sorted(s[6].items())))
    half = (N + 45) // 2
    for name, sl in (("first year", slice(45, half)), ("second year", slice(half, N))):
        x = avg[sl]
        print(f"    {name:<12} total {100 * (np.prod(1 + x) - 1):+.1f}%  Sharpe {x.mean() / x.std(ddof=1) * np.sqrt(365):+.2f}  t {x.mean() / (x.std(ddof=1) / np.sqrt(len(x))):+.2f}")
