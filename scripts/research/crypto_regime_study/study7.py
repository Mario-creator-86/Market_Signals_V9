"""Two years, many market regimes: which rules hold quarter after quarter?

usage: study7.py <klines dir with ~730 days of 1h bars>
A rule counts only if it is profitable overall AND in most quarters.
"""
import datetime as dt
import sys

import numpy as np

import study
from study import fmt, roll, simulate, stats

study.DATA = __import__("pathlib").Path(sys.argv[1])
S = study.load()
btc = S["BTCUSDT"]


def lagged(a, n):
    out = np.full_like(a, np.nan)
    out[n:] = a[n:] / a[:-n] - 1
    return out


sma720 = roll(btc["c"], 720, np.mean)
bull = btc["c"] > sma720                      # BTC above its 30-day average
b720 = lagged(btc["c"], 720)
maps = {
    "btc_bull": dict(zip(btc["t"].tolist(), np.where(np.isfinite(sma720), bull, np.nan).tolist())),
    "btc720": dict(zip(btc["t"].tolist(), b720.tolist())),
    "btc168": dict(zip(btc["t"].tolist(), lagged(btc["c"], 168).tolist())),
}
for s in S.values():
    c, h, l = s["c"], s["h"], s["l"]
    for k, m in maps.items():
        s[k] = np.array([m.get(x, np.nan) for x in s["t"].tolist()])
    s["chg168"] = lagged(c, 168)
    for n in (48, 168):
        hi = roll(h, n, np.max); lo = roll(l, n, np.min)
        ph = np.full_like(c, np.nan); ph[1:] = hi[:-1]
        pl = np.full_like(c, np.nan); pl[1:] = lo[:-1]
        s[f"brk_up{n}"] = c > ph
        s[f"brk_dn{n}"] = c < pl

long_ = lambda s: np.ones_like(s["c"])
short_ = lambda s: -np.ones_like(s["c"])
BULL = lambda s: s["btc_bull"] == 1.0
BEAR = lambda s: s["btc_bull"] == 0.0
AND = lambda *fs: (lambda s: np.logical_and.reduce([f(s) for f in fs]))


def quarter(ms):
    d = dt.datetime.utcfromtimestamp(ms / 1000)
    return f"{d.year % 100}Q{(d.month - 1) // 3 + 1}"


def report(name, cond, side, sl, rr, hold):
    tr = simulate(S, cond, side, sl, rr, hold)
    if not tr:
        print(f"     {name}: no trades"); return False
    allst = stats(tr)
    qs = {}
    for t in tr:
        qs.setdefault(quarter(t[0]), []).append(t)
    qstats = {q: stats(v) for q, v in sorted(qs.items())}
    good = sum(1 for st in qstats.values() if st["pf"] > 1.0)
    ok = allst["pf"] > 1.10 and good >= max(1, int(round(0.7 * len(qstats)))) and allst["t"] > 1.5
    print(f"{'PASS' if ok else '    '} {name:<58} ALL {fmt(allst)} | profitable quarters {good}/{len(qstats)}")
    print("       " + " ".join(f"{q}:{st['pf']:.2f}({st['n']})" for q, st in qstats.items()))
    return ok


t0 = min(s["t"][0] for s in S.values()); t1 = max(s["t"][-1] for s in S.values())
print("pairs:", len(S) - 1, "| window:", dt.datetime.utcfromtimestamp(t0 / 1000).date(), "->", dt.datetime.utcfromtimestamp(t1 / 1000).date(),
      "| BTC above its 30d average: %.0f%% of hours" % (100 * np.nanmean(np.where(np.isfinite(sma720), bull, np.nan))))

DIP = lambda s: (s["chg24"] <= -0.08) & (s["chg24"] > -0.25) & (s["btc24"] <= -0.02)
RALLY_G = lambda s: (s["chg24"] >= 0.08) & (s["chg24"] < 0.25) & (s["btc24"] >= 0.02)
G = lambda s: (s["chg24"] >= 0.08) & (s["chg24"] < 0.25)
L = lambda s: (s["chg24"] <= -0.08) & (s["chg24"] > -0.25)

print("\n=== 1. Selloff-day dip buy (the live rule) and variants")
report("LIVE buy losers 8-25%, BTC 24h <= -2% (sl6 rr1 24h)", DIP, long_, 0.06, 1.0, 24)
report("same, only when BTC above 30d average", AND(DIP, BULL), long_, 0.06, 1.0, 24)
report("same, only when BTC below 30d average", AND(DIP, BEAR), long_, 0.06, 1.0, 24)
report("buy losers 8-25% any day (no BTC condition)", L, long_, 0.06, 1.0, 24)
report("capitulation: RSI(1h) < 20 and coin -8% or more", lambda s: (s["rsi"] < 20) & (s["chg24"] <= -0.08), long_, 0.06, 1.0, 24)

print("\n=== 2. Shorts")
report("short gainers 8-25% on a BTC rally day (>= +2%)", RALLY_G, short_, 0.06, 1.0, 24)
report("short gainers 8-25%, BTC below 30d average", AND(G, BEAR), short_, 0.06, 1.0, 24)
report("short gainers 8-25%, BTC above 30d average", AND(G, BULL), short_, 0.06, 1.0, 24)
report("short break of 48h low, BTC below 30d average", AND(lambda s: s["brk_dn48"], BEAR), short_, 0.06, 2.0, 72)
report("short break of 168h low, BTC below 30d average", AND(lambda s: s["brk_dn168"], BEAR), short_, 0.06, 2.0, 72)
report("short break of 48h low, BTC 30d return <= -10%", AND(lambda s: s["brk_dn48"], lambda s: s["btc720"] <= -0.10), short_, 0.06, 2.0, 72)
report("short weak coin (7d <= -15%) bouncing 3-10%, BTC below 30d avg", AND(lambda s: (s["chg168"] <= -0.15) & (s["chg24"] >= 0.03) & (s["chg24"] < 0.10), BEAR), short_, 0.05, 1.5, 48)
report("short the losers on a selloff day (mirror of LIVE)", DIP, short_, 0.06, 1.0, 24)

print("\n=== 3. Trend-following longs")
report("long break of 48h high, BTC above 30d average", AND(lambda s: s["brk_up48"], BULL), long_, 0.06, 2.0, 72)
report("long break of 168h high, BTC above 30d average", AND(lambda s: s["brk_up168"], BULL), long_, 0.06, 2.0, 72)
report("long break of 168h high, BTC 30d return >= +10%", AND(lambda s: s["brk_up168"], lambda s: s["btc720"] >= 0.10), long_, 0.06, 2.0, 72)
report("long break of 168h high (no regime filter)", lambda s: s["brk_up168"], long_, 0.06, 2.0, 72)
report("buy gainers 8-25% on a BTC rally day, BTC above 30d avg", AND(RALLY_G, BULL), long_, 0.06, 1.0, 24)
report("long strong coin (7d >= +15%) pulling back 3-10%, BTC above 30d avg", AND(lambda s: (s["chg168"] >= 0.15) & (s["chg24"] <= -0.03) & (s["chg24"] > -0.10), BULL), long_, 0.05, 1.5, 48)
