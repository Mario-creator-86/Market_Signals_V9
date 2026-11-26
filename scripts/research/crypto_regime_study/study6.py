"""Wider search: trend breakouts, pullbacks in trend, selloff-day variants, blow-off shorts,
cross-sectional momentum. Long and short. Positive in BOTH halves or it does not count.

usage: study6.py <klines dir>
"""
import datetime as dt

import numpy as np

from study import MIN_VOL, fmt, load, roll, simulate, split_time, stats

S = load()
TS = split_time(S)
btc = S["BTCUSDT"]


def lagged(a, n):
    out = np.full_like(a, np.nan)
    out[n:] = a[n:] / a[:-n] - 1
    return out


b168 = lagged(btc["c"], 168)
m168 = dict(zip(btc["t"].tolist(), b168.tolist()))
for s in S.values():
    c, h, l = s["c"], s["h"], s["l"]
    s["chg168"] = lagged(c, 168)
    s["btc168"] = np.array([m168.get(x, np.nan) for x in s["t"].tolist()])
    for n in (48, 168):
        hi = roll(h, n, np.max); lo = roll(l, n, np.min)
        prev_hi = np.full_like(c, np.nan); prev_hi[1:] = hi[:-1]      # highest high of the n bars BEFORE this one
        prev_lo = np.full_like(c, np.nan); prev_lo[1:] = lo[:-1]
        s[f"brk_up{n}"] = c > prev_hi
        s[f"brk_dn{n}"] = c < prev_lo
    s["red1"] = s["c"] < s["o"]
    s["green1"] = s["c"] > s["o"]

long_ = lambda s: np.ones_like(s["c"])
short_ = lambda s: -np.ones_like(s["c"])
AND = lambda *fs: (lambda s: np.logical_and.reduce([f(s) for f in fs]))


def months(trades):
    out = {}
    for t in trades:
        out.setdefault(dt.datetime.utcfromtimestamp(t[0] / 1000).strftime("%m"), []).append(t)
    return out


def report(name, cond, side, sl, rr, hold):
    tr = simulate(S, cond, side, sl, rr, hold)
    a, b = stats(tr, TS, "train"), stats(tr, TS, "test")
    ok = bool(a and b and a["pf"] > 1.05 and b["pf"] > 1.05 and a["n"] >= 40 and b["n"] >= 40)
    mline = " ".join(f"{m}:{stats(ts)['pf']:.2f}({stats(ts)['n']})" for m, ts in sorted(months(tr).items()))
    print(f"{'PASS' if ok else '    '} {name:<62} TRAIN {fmt(a)} | TEST {fmt(b)}\n       months PF(n): {mline}")
    return ok


print("=== A. Trend breakouts (stop 6%, R:R 2, hold 72h)")
for n in (48, 168):
    report(f"A long  break of {n}h high", lambda s, n=n: s[f"brk_up{n}"], long_, 0.06, 2.0, 72)
    report(f"A long  break of {n}h high, BTC 7d >= 0", AND(lambda s, n=n: s[f"brk_up{n}"], lambda s: s["btc168"] >= 0), long_, 0.06, 2.0, 72)
    report(f"A short break of {n}h low", lambda s, n=n: s[f"brk_dn{n}"], short_, 0.06, 2.0, 72)
    report(f"A short break of {n}h low, BTC 7d <= 0", AND(lambda s, n=n: s[f"brk_dn{n}"], lambda s: s["btc168"] <= 0), short_, 0.06, 2.0, 72)

print("\n=== B. Pullback inside a 7-day trend (stop 5%, R:R 1.5, hold 48h)")
report("B long  strong coin (7d >= +15%) pulling back 3-10% in 24h", lambda s: (s["chg168"] >= 0.15) & (s["chg24"] <= -0.03) & (s["chg24"] > -0.10), long_, 0.05, 1.5, 48)
report("B long  same, BTC 7d >= 0", lambda s: (s["chg168"] >= 0.15) & (s["chg24"] <= -0.03) & (s["chg24"] > -0.10) & (s["btc168"] >= 0), long_, 0.05, 1.5, 48)
report("B short weak coin (7d <= -15%) bouncing 3-10% in 24h", lambda s: (s["chg168"] <= -0.15) & (s["chg24"] >= 0.03) & (s["chg24"] < 0.10), short_, 0.05, 1.5, 48)
report("B short same, BTC 7d <= 0", lambda s: (s["chg168"] <= -0.15) & (s["chg24"] >= 0.03) & (s["chg24"] < 0.10) & (s["btc168"] <= 0), short_, 0.05, 1.5, 48)

print("\n=== C. Selloff day (BTC 24h <= -2%): what to do with the losers")
L = lambda s: (s["chg24"] <= -0.08) & (s["chg24"] > -0.25) & (s["btc24"] <= -0.02)
report("C BUY the losers   (stop 6%, R:R 1, 24h)  [live rule]", L, long_, 0.06, 1.0, 24)
report("C SHORT the losers (stop 6%, R:R 1, 24h)", L, short_, 0.06, 1.0, 24)
report("C SHORT the losers (stop 4%, R:R 2, 48h)", L, short_, 0.04, 2.0, 48)
report("C buy coins that held up (24h > -2%) on a selloff day", lambda s: (s["chg24"] > -0.02) & (s["chg24"] < 0.03) & (s["btc24"] <= -0.02), long_, 0.06, 1.0, 24)
report("C short coins that pumped (24h >= +8%) on a selloff day", lambda s: (s["chg24"] >= 0.08) & (s["btc24"] <= -0.02), short_, 0.06, 1.0, 24)

print("\n=== D. Rally day (BTC 24h >= +2%)")
report("D buy the gainers 8-25%  (stop 6%, R:R 1, 24h)", lambda s: (s["chg24"] >= 0.08) & (s["chg24"] < 0.25) & (s["btc24"] >= 0.02), long_, 0.06, 1.0, 24)
report("D short the gainers 8-25% (stop 6%, R:R 1, 24h)", lambda s: (s["chg24"] >= 0.08) & (s["chg24"] < 0.25) & (s["btc24"] >= 0.02), short_, 0.06, 1.0, 24)
report("D buy the laggards (24h < +1%) on a rally day", lambda s: (s["chg24"] < 0.01) & (s["chg24"] > -0.05) & (s["btc24"] >= 0.02), long_, 0.06, 1.0, 24)

print("\n=== E. Blow-off short: +25% or more in 24h and already 8% off the high")
report("E short blow-off, 1h red bar (stop 8%, R:R 1.5, 48h)", lambda s: (s["chg24"] >= 0.25) & (s["pull_hi"] >= 0.08) & s["red1"], short_, 0.08, 1.5, 48)
report("E short blow-off, RSI < 60 after the spike", lambda s: (s["chg24"] >= 0.25) & (s["pull_hi"] >= 0.08) & (s["rsi"] < 60), short_, 0.08, 1.5, 48)
report("E short +15% or more with BTC 7d <= -3% and 5% off the high", lambda s: (s["chg24"] >= 0.15) & (s["pull_hi"] >= 0.05) & (s["btc168"] <= -0.03), short_, 0.06, 1.5, 48)

print("\n=== F. Capitulation long without the BTC condition: RSI(1h) < 20")
report("F long RSI < 20, coin -8% or more", lambda s: (s["rsi"] < 20) & (s["chg24"] <= -0.08), long_, 0.06, 1.0, 24)
report("F long RSI < 20, coin -8% or more, relvol >= 2", lambda s: (s["rsi"] < 20) & (s["chg24"] <= -0.08) & (s["relvol"] >= 2), long_, 0.06, 1.0, 24)

# --- G. Cross-sectional 7-day momentum, rebalanced daily at 00:00 UTC, held 24h, costs per leg ---
print("\n=== G. Cross-sectional momentum (rank by 7d return among liquid pairs, hold 24h, top/bottom 5)")
COST = 2 * 0.0004 + 0.00025 + 0.00025
days = sorted({int(t) for s in S.values() for t in s["t"] if int(t) % 86_400_000 == 0})
idx = {sym: dict(zip(s["t"].tolist(), range(len(s["t"])))) for sym, s in S.items()}
res = {"long_top": [], "short_bottom": [], "long_bottom": [], "short_top": []}
for d in days:
    rows = []
    for sym, s in S.items():
        if sym == "BTCUSDT":
            continue
        i = idx[sym].get(d); j = idx[sym].get(d + 86_400_000)
        if i is None or j is None or not np.isfinite(s["chg168"][i]) or not (s["vol24"][i] >= MIN_VOL):
            continue
        rows.append((s["chg168"][i], s["c"][j] / s["c"][i] - 1))
    if len(rows) < 30:
        continue
    rows.sort()
    bottom, top = rows[:5], rows[-5:]
    res["long_top"].append((d, np.mean([r for _, r in top]) - COST))
    res["short_top"].append((d, -np.mean([r for _, r in top]) - COST))
    res["long_bottom"].append((d, np.mean([r for _, r in bottom]) - COST))
    res["short_bottom"].append((d, -np.mean([r for _, r in bottom]) - COST))
for name, rows in res.items():
    for label, part in (("TRAIN", [r for d, r in rows if d < TS]), ("TEST", [r for d, r in rows if d >= TS])):
        a = np.array(part)
        t = a.mean() / (a.std(ddof=1) / np.sqrt(len(a))) if len(a) > 2 else 0
        print(f"   G {name:<13} {label}: days={len(a):>3} mean/day={100*a.mean():+.3f}% win={100*(a>0).mean():4.1f}% t={t:+.2f}", end="  |")
    print()
