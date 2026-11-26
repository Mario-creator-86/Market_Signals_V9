import numpy as np
from study import fmt, load, simulate, split_time, stats
S = load(); TS = split_time(S)
long_ = lambda s: np.ones_like(s["c"])
cond = lambda s: (s["chg24"] <= -0.08) & (s["chg24"] > -0.25) & (s["btc24"] <= -0.02)
for sl, rr, h in ((0.06, 1.0, 24), (0.06, 1.0, 72), (0.06, 1.0, 400), (0.05, 1.0, 400), (0.04, 1.5, 400), (0.06, 1.5, 400)):
    tr = simulate(S, cond, long_, sl, rr, h)
    print(f"stop {int(sl*100)}% R:R {rr} hold<= {h:>3}h   TRAIN {fmt(stats(tr, TS, 'train'))} | TEST {fmt(stats(tr, TS, 'test'))}")
