"""Third pass: rules conditioned on the BTC regime, checked month by month."""
import datetime as dt

import numpy as np

from study import fmt, load, simulate, split_time, stats

S = load()
TS = split_time(S)
btc = S["BTCUSDT"]
b72 = np.full_like(btc["c"], np.nan); b72[72:] = btc["c"][72:] / btc["c"][:-72] - 1
b168 = np.full_like(btc["c"], np.nan); b168[168:] = btc["c"][168:] / btc["c"][:-168] - 1
m72 = dict(zip(btc["t"].tolist(), b72.tolist())); m168 = dict(zip(btc["t"].tolist(), b168.tolist()))
for s in S.values():
    s["btc72"] = np.array([m72.get(x, np.nan) for x in s["t"].tolist()])
    s["btc168"] = np.array([m168.get(x, np.nan) for x in s["t"].tolist()])

long_ = lambda s: np.ones_like(s["c"])
short_ = lambda s: -np.ones_like(s["c"])
loser = lambda lo, hi: (lambda s: (s["chg24"] <= -lo) & (s["chg24"] > -hi))
gainer = lambda lo, hi: (lambda s: (s["chg24"] >= lo) & (s["chg24"] < hi))
AND = lambda *fs: (lambda s: np.logical_and.reduce([f(s) for f in fs]))


def months(trades):
    out = {}
    for t in trades:
        out.setdefault(dt.datetime.utcfromtimestamp(t[0] / 1000).strftime("%Y-%m"), []).append(t)
    return out


def report(name, cond, side, sl, rr, hold):
    tr = simulate(S, cond, side, sl, rr, hold)
    print(f"\n{name}\n   TRAIN {fmt(stats(tr, TS, 'train'))}\n   TEST  {fmt(stats(tr, TS, 'test'))}")
    line = []
    for m, ts in sorted(months(tr).items()):
        st = stats(ts)
        line.append(f"{m}: n={st['n']} PF={st['pf']:.2f} mean={st['mean']:+.2f}%")
    print("   by month: " + " | ".join(line))
    return tr


L, G = loser(0.08, 0.25), gainer(0.08, 0.25)
print("=== LONG candidates")
report("L1 buy losers when BTC 24h <= -2%  (sl4 rr1.5 h24)", AND(L, lambda s: s["btc24"] <= -0.02), long_, 0.04, 1.5, 24)
report("L2 buy losers when BTC 24h <= -2% and coin still falling (4h < 0)", AND(L, lambda s: (s["btc24"] <= -0.02) & (s["chg4"] < 0)), long_, 0.04, 1.5, 24)
report("L3 buy losers with RSI(1h) < 30 and BTC 24h <= -1%", AND(L, lambda s: (s["rsi"] < 30) & (s["btc24"] <= -0.01)), long_, 0.04, 1.5, 24)
report("L4 L1 with wider stop (sl6 rr1.0 h24)", AND(L, lambda s: s["btc24"] <= -0.02), long_, 0.06, 1.0, 24)
report("L5 buy gainers (follow) when BTC 7d >= +3%", AND(G, lambda s: s["btc168"] >= 0.03), long_, 0.04, 1.5, 24)
report("L6 buy gainers (follow) when BTC 7d >= +3% and >=3% below 24h high (pullback)", AND(G, lambda s: (s["btc168"] >= 0.03) & (s["pull_hi"] >= 0.03)), long_, 0.04, 1.5, 24)
print("\n=== SHORT candidates")
report("S1 short gainers when BTC 7d <= -3%", AND(G, lambda s: s["btc168"] <= -0.03), short_, 0.04, 1.5, 24)
report("S2 short gainers when BTC 24h <= 0 and BTC 7d <= 0", AND(G, lambda s: (s["btc24"] <= 0) & (s["btc168"] <= 0)), short_, 0.04, 1.5, 24)
report("S3 short gainers when BTC 72h <= -2%", AND(G, lambda s: s["btc72"] <= -0.02), short_, 0.04, 1.5, 24)
report("S4 short gainers when BTC 7d >= +3% (control: should lose)", AND(G, lambda s: s["btc168"] >= 0.03), short_, 0.04, 1.5, 24)
report("S5 short losers (follow down) when BTC 7d <= -3%", AND(L, lambda s: s["btc168"] <= -0.03), short_, 0.04, 1.5, 24)
print("\n=== BTC regime share of hours: 7d>=+3%%: %.0f%%, 7d<=-3%%: %.0f%%, 24h<=-2%%: %.0f%%" % (
    100 * np.nanmean(b168 >= 0.03), 100 * np.nanmean(b168 <= -0.03), 100 * np.nanmean(btc["chg24"] <= -0.02)))
