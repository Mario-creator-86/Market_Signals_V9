"""Robustness of the candidate rule: neighbours of every parameter, both halves."""
import numpy as np
from study import fmt, load, simulate, split_time, stats

S = load(); TS = split_time(S)
long_ = lambda s: np.ones_like(s["c"])
def rule(lo, hi, btc_max):
    return lambda s: (s["chg24"] <= -lo) & (s["chg24"] > -hi) & (s["btc24"] <= btc_max)
def show(name, cond, sl, rr, h):
    tr = simulate(S, cond, long_, sl, rr, h)
    days = len({t[0] // 86_400_000 for t in tr})
    print(f"{name:<44} TRAIN {fmt(stats(tr, TS, 'train'))} | TEST {fmt(stats(tr, TS, 'test'))} | entry days={days}")
    return tr
print("== BTC 24h threshold (coin -8..-25%, stop 6%, R:R 1.0, hold 24h)")
for b in (0.0, -0.01, -0.015, -0.02, -0.03, -0.04):
    show(f"BTC 24h <= {b*100:+.1f}%", rule(0.08, 0.25, b), 0.06, 1.0, 24)
print("== coin band (BTC <= -2%)")
for lo, hi in ((0.06, 0.25), (0.08, 0.25), (0.10, 0.30), (0.12, 0.40), (0.06, 0.12)):
    show(f"coin -{int(lo*100)}..-{int(hi*100)}%", rule(lo, hi, -0.02), 0.06, 1.0, 24)
print("== exit shape (coin -8..-25%, BTC <= -2%)")
for sl, rr, h in ((0.04, 1.0, 24), (0.04, 1.5, 24), (0.05, 1.0, 24), (0.06, 1.0, 12), (0.06, 1.0, 24), (0.06, 1.0, 48), (0.06, 1.5, 48), (0.08, 1.0, 48)):
    show(f"stop {int(sl*100)}% R:R {rr} hold {h}h", rule(0.08, 0.25, -0.02), sl, rr, h)
print("== chosen rule, exits by type and worst days")
tr = show("CHOSEN coin -8..-25%, BTC<=-2%, sl6 rr1 h24", rule(0.08, 0.25, -0.02), 0.06, 1.0, 24)
import collections, datetime as dt
print("exit reasons:", dict(collections.Counter(t[4] for t in tr)))
byday = collections.defaultdict(list)
for t in tr: byday[dt.datetime.utcfromtimestamp(t[0]/1000).date()].append(t[3])
d = sorted(((sum(v), len(v), k) for k, v in byday.items()))
print("worst 4 entry days (sum of net % over trades, n):", [(str(k), n, round(100*s,1)) for s, n, k in d[:4]])
print("best 4 entry days:", [(str(k), n, round(100*s,1)) for s, n, k in d[-4:]])
pos = sum(1 for s, n, k in d if s > 0)
print(f"entry days: {len(d)}, profitable days: {pos} ({100*pos/len(d):.0f}%), median trades/day: {sorted(n for s, n, k in d)[len(d)//2]}")
btc = S["BTCUSDT"]
print("BTC 24h now: %+.2f%% | share of hours with BTC 24h <= -2%%: %.1f%%" % (100*btc["chg24"][-1], 100*np.nanmean(btc["chg24"] <= -0.02)))
