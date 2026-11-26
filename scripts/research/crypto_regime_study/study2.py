"""Second pass: confirmation filters on the two fade sides. Positive in BOTH halves or it does not count."""
import sys

import numpy as np

from study import load, run, split_time

S = load()
TS = split_time(S)
long_ = lambda s: np.ones_like(s["c"])
short_ = lambda s: -np.ones_like(s["c"])
loser = lambda lo, hi: (lambda s: (s["chg24"] <= -lo) & (s["chg24"] > -hi))
gainer = lambda lo, hi: (lambda s: (s["chg24"] >= lo) & (s["chg24"] < hi))
AND = lambda *fs: (lambda s: np.logical_and.reduce([f(s) for f in fs]))

L = loser(0.08, 0.25)
G = gainer(0.08, 0.25)
SL, RR, H = 0.04, 1.5, 24
print("== BUY THE LOSERS (24h move -8..-25%), stop 4%, R:R 1.5, hold 24h")
run(S, "base", L, long_, SL, RR, H, TS)
run(S, "H1 72h trend up (dip in an uptrend)", AND(L, lambda s: s["chg72"] > 0.0), long_, SL, RR, H, TS)
run(S, "H1' 72h trend down (falling for days)", AND(L, lambda s: s["chg72"] <= -0.15), long_, SL, RR, H, TS)
run(S, "H2 BTC 24h >= -1% (coin-specific drop)", AND(L, lambda s: s["btc24"] >= -0.01), long_, SL, RR, H, TS)
run(S, "H2' BTC 24h < -3% (market-wide dump)", AND(L, lambda s: s["btc24"] < -0.03), long_, SL, RR, H, TS)
run(S, "H3 already bouncing: >=2% above 24h low, last 4h up", AND(L, lambda s: (s["bounce_lo"] >= 0.02) & (s["chg4"] > 0)), long_, SL, RR, H, TS)
run(S, "H3' still falling: last 4h down", AND(L, lambda s: s["chg4"] < -0.02), long_, SL, RR, H, TS)
run(S, "H4 RSI(1h) < 30", AND(L, lambda s: s["rsi"] < 30), long_, SL, RR, H, TS)
run(S, "H4' RSI(1h) 30-50", AND(L, lambda s: (s["rsi"] >= 30) & (s["rsi"] < 50)), long_, SL, RR, H, TS)
run(S, "H5 relative volume >= 2 (capitulation)", AND(L, lambda s: s["relvol"] >= 2.0), long_, SL, RR, H, TS)
run(S, "H5' relative volume < 1.2 (quiet drift)", AND(L, lambda s: s["relvol"] < 1.2), long_, SL, RR, H, TS)

print("\n== SHORT THE GAINERS (24h move +8..+25%), stop 4%, R:R 1.5, hold 24h")
run(S, "base", G, short_, SL, RR, H, TS)
run(S, "H1 72h trend down (rally in a downtrend)", AND(G, lambda s: s["chg72"] < 0.0), short_, SL, RR, H, TS)
run(S, "H1' 72h trend up > +25% (extended for days)", AND(G, lambda s: s["chg72"] >= 0.25), short_, SL, RR, H, TS)
run(S, "H2 BTC 24h <= +1% (coin-specific pump)", AND(G, lambda s: s["btc24"] <= 0.01), short_, SL, RR, H, TS)
run(S, "H2' BTC 24h > +3% (market-wide rally)", AND(G, lambda s: s["btc24"] > 0.03), short_, SL, RR, H, TS)
run(S, "H3 already fading: >=3% below 24h high, last 4h down", AND(G, lambda s: (s["pull_hi"] >= 0.03) & (s["chg4"] < 0)), short_, SL, RR, H, TS)
run(S, "H3' still pumping: last 4h up > 2%", AND(G, lambda s: s["chg4"] > 0.02), short_, SL, RR, H, TS)
run(S, "H4 RSI(1h) > 70", AND(G, lambda s: s["rsi"] > 70), short_, SL, RR, H, TS)
run(S, "H5 relative volume >= 2", AND(G, lambda s: s["relvol"] >= 2.0), short_, SL, RR, H, TS)
run(S, "H5' relative volume < 1.2", AND(G, lambda s: s["relvol"] < 1.2), short_, SL, RR, H, TS)

print("\n== EXIT SHAPE on the two bases (stop / R:R / max hold)")
for sl, rr, h in ((0.03, 1.0, 12), (0.03, 2.0, 24), (0.04, 1.0, 12), (0.04, 1.0, 24), (0.04, 2.0, 48), (0.06, 1.0, 24), (0.06, 1.5, 48), (0.08, 1.0, 48)):
    run(S, f"buy losers   sl{int(sl*100)} rr{rr} h{h}", L, long_, sl, rr, h, TS)
    run(S, f"short gainers sl{int(sl*100)} rr{rr} h{h}", G, short_, sl, rr, h, TS)
