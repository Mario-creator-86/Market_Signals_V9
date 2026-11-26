"""Daily trend following (time-series momentum, Donchian) over two years, long and short.

usage: study8.py <klines dir with ~730 days of 1h bars>
Portfolio = equal weight over the pairs that have a signal that day. Costs: 6.5 bps per unit of
turnover (4 bps commission + 2.5 bps slippage), charged whenever a position changes.
"""
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np

DATA = Path(sys.argv[1])
COST = 0.00065
meta = json.loads((DATA / "_symbols.json").read_text())
daily = {}
for sym in meta["symbols"]:
    arr = np.loadtxt(DATA / f"{sym}.csv", delimiter=",", skiprows=1)
    if arr.ndim != 2 or len(arr) < 24 * 200:
        continue
    t, o, h, l, c, qv = arr.T
    day = (t // 86_400_000).astype(np.int64)
    days = np.unique(day)
    close = np.array([c[day == d][-1] for d in days])
    high = np.array([h[day == d].max() for d in days])
    low = np.array([l[day == d].min() for d in days])
    vol = np.array([qv[day == d].sum() for d in days])
    daily[sym] = {"d": days, "c": close, "h": high, "l": low, "v": vol}

all_days = np.unique(np.concatenate([s["d"] for s in daily.values()]))
idx = {int(d): i for i, d in enumerate(all_days)}
N, M = len(all_days), len(daily)
syms = sorted(daily)
C = np.full((N, M), np.nan); H = np.full((N, M), np.nan); L = np.full((N, M), np.nan); V = np.full((N, M), np.nan)
for j, sym in enumerate(syms):
    s = daily[sym]
    rows = [idx[int(d)] for d in s["d"]]
    C[rows, j], H[rows, j], L[rows, j], V[rows, j] = s["c"], s["h"], s["l"], s["v"]
R = np.full((N, M), np.nan); R[1:] = C[1:] / C[:-1] - 1          # return of day i (close to close)
liquid = np.full((N, M), False)
for i in range(7, N):
    liquid[i] = np.nanmean(V[i - 7:i], axis=0) >= 10_000_000      # known before day i
btc = syms.index("BTCUSDT")


def quarter(d):
    x = dt.date.fromordinal(dt.date(1970, 1, 1).toordinal() + int(d))
    return f"{x.year % 100}Q{(x.month - 1) // 3 + 1}"


def evaluate(name, pos):
    """pos[i, j] in {-1, 0, +1}: position held DURING day i, decided from data up to day i-1."""
    pos = np.where(liquid & np.isfinite(R), pos, 0.0)
    prev = np.vstack([np.zeros((1, M)), pos[:-1]])
    n_act = np.maximum(1, (pos != 0).sum(axis=1))
    gross = np.nansum(pos * np.nan_to_num(R), axis=1) / n_act
    turn = np.abs(pos - prev).sum(axis=1) / n_act
    net = gross - turn * COST
    live = (pos != 0).sum(axis=1) > 0
    net = net[live]; days = all_days[live]
    if len(net) < 60:
        print(f"     {name}: too few days"); return
    sharpe = net.mean() / net.std(ddof=1) * np.sqrt(365)
    t = net.mean() / (net.std(ddof=1) / np.sqrt(len(net)))
    eq = np.cumprod(1 + net); dd = (eq / np.maximum.accumulate(eq) - 1).min()
    qs = {}
    for d, r in zip(days, net):
        qs.setdefault(quarter(d), []).append(r)
    good = sum(1 for v in qs.values() if np.sum(v) > 0)
    ok = sharpe > 1.0 and t > 2.0 and good >= int(round(0.7 * len(qs)))
    print(f"{'PASS' if ok else '    '} {name:<52} days={len(net):>3} total={100*(eq[-1]-1):+7.1f}% Sharpe={sharpe:+5.2f} t={t:+5.2f} maxDD={100*dd:6.1f}% good quarters {good}/{len(qs)}")
    print("       " + " ".join(f"{q}:{100*np.sum(v):+.0f}%" for q, v in sorted(qs.items())))


def past_return(n):
    out = np.full((N, M), np.nan)
    out[n + 1:] = C[n:-1] / C[:-n - 1] - 1                         # return over the n days ending yesterday
    return out


print("pairs:", M, "| days:", N, "| window:", quarter(all_days[0]), "->", quarter(all_days[-1]))
print("\n=== Time-series momentum: position = sign of the coin's own past return")
for n in (7, 14, 28, 56):
    pr = past_return(n)
    evaluate(f"TSMOM {n}d long/short", np.sign(np.nan_to_num(pr)))
    evaluate(f"TSMOM {n}d long only (flat when down)", np.where(pr > 0, 1.0, 0.0))
    evaluate(f"TSMOM {n}d short only (flat when up)", np.where(pr < 0, -1.0, 0.0))

print("\n=== Same, gated by BTC's own trend (above its 30-day average = longs only, below = shorts only)")
sma30 = np.full(N, np.nan)
for i in range(31, N):
    sma30[i] = np.nanmean(C[i - 30:i, btc])
btc_up = np.full(N, np.nan); btc_up[1:] = (C[:-1, btc] > sma30[1:]).astype(float)
btc_up[~np.isfinite(sma30)] = np.nan
for n in (14, 28):
    pr = past_return(n)
    gate_long = (btc_up == 1.0)[:, None]; gate_short = (btc_up == 0.0)[:, None]
    evaluate(f"TSMOM {n}d, longs in BTC uptrend + shorts in downtrend", np.where((pr > 0) & gate_long, 1.0, np.where((pr < 0) & gate_short, -1.0, 0.0)))
    evaluate(f"TSMOM {n}d, shorts only in BTC downtrend", np.where((pr < 0) & gate_short, -1.0, 0.0))
    evaluate(f"TSMOM {n}d, longs only in BTC uptrend", np.where((pr > 0) & gate_long, 1.0, 0.0))

print("\n=== Donchian channel (enter on N-day high/low, exit on N/2-day opposite extreme)")
for n in (20, 55):
    pos = np.zeros((N, M))
    for j in range(M):
        state = 0.0
        for i in range(n + 1, N):
            if not np.isfinite(C[i - 1, j]):
                state = 0.0; continue
            hi_n = np.nanmax(H[i - 1 - n:i - 1, j]); lo_n = np.nanmin(L[i - 1 - n:i - 1, j])
            hi_x = np.nanmax(H[i - 1 - n // 2:i - 1, j]); lo_x = np.nanmin(L[i - 1 - n // 2:i - 1, j])
            c1 = C[i - 1, j]
            if state == 0.0:
                state = 1.0 if c1 > hi_n else (-1.0 if c1 < lo_n else 0.0)
            elif state > 0 and c1 < lo_x:
                state = 0.0
            elif state < 0 and c1 > hi_x:
                state = 0.0
            pos[i, j] = state
    evaluate(f"Donchian {n}d long/short", pos)
    evaluate(f"Donchian {n}d long only", np.where(pos > 0, 1.0, 0.0))
    evaluate(f"Donchian {n}d short only", np.where(pos < 0, -1.0, 0.0))

print("\n=== Reference: just hold")
evaluate("hold every liquid pair long (equal weight)", np.ones((N, M)))
only_btc = np.zeros((N, M)); only_btc[:, btc] = 1.0
evaluate("hold BTC", only_btc)
