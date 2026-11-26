"""Funding-rate signals over two years (Binance USD-M perpetuals), long and short.

usage: study9.py <klines dir, ~730 days of 1h bars> <funding dir from fetch_funding.py>
Prices: spot hourly bars (basis to the perpetual is small). Funding rates are normalised to an
8-hour equivalent because many pairs settle every 4 hours. Each rule is reported twice:
price only (what the paper broker measures) and with funding payments (a short receives a
positive rate, a long pays it). A rule counts only if it holds overall AND in most quarters.
"""
import datetime as dt
import sys
from pathlib import Path

import numpy as np

import study
from study import MIN_VOL, fmt, simulate, stats

study.DATA = Path(sys.argv[1])
FUND = Path(sys.argv[2])
S_all = study.load()
S = {"BTCUSDT": S_all["BTCUSDT"]}
H = 3_600_000
all_rates = []
for sym, s in S_all.items():
    f = FUND / f"{sym}.csv"
    if not f.exists():
        continue
    arr = np.loadtxt(f, delimiter=",", skiprows=1)
    if arr.ndim != 2 or len(arr) < 200:
        continue
    ft, fr = arr[:, 0].astype(np.int64), arr[:, 1]
    gap_h = np.diff(ft, prepend=ft[0] - 8 * H) / H
    gap_h = np.clip(np.round(gap_h), 1, 8)
    fr8 = fr * 8.0 / gap_h                                   # 8-hour equivalent of each settlement
    t = s["t"]
    k = np.searchsorted(ft, t, side="right") - 1             # last settlement at or before the bar's open
    ok = k >= 0
    last8 = np.where(ok, fr8[np.clip(k, 0, None)], np.nan)
    csum = np.concatenate([[0.0], np.cumsum(fr8 * gap_h / 8.0)])   # cumulative raw rate
    c8 = np.concatenate([[0.0], np.cumsum(fr8)])
    cnt = np.arange(len(fr8) + 1)

    def window_mean(hours):
        lo = np.searchsorted(ft, t - hours * H, side="right")
        hi = k + 1
        n = np.maximum(1, cnt[np.clip(hi, 0, None)] - cnt[lo])
        return np.where(ok & (hi > lo), (c8[np.clip(hi, 0, None)] - c8[lo]) / n, np.nan)

    s["fr8"] = last8
    s["fr8_1d"] = window_mean(24)
    s["fr8_30d"] = window_mean(720)
    sq = np.concatenate([[0.0], np.cumsum(fr8 ** 2)])
    lo30 = np.searchsorted(ft, t - 720 * H, side="right"); hi30 = np.clip(k + 1, 0, None)
    n30 = np.maximum(2, hi30 - lo30)
    var = (sq[hi30] - sq[lo30]) / n30 - ((c8[hi30] - c8[lo30]) / n30) ** 2
    s["fr8_z"] = (s["fr8_1d"] - s["fr8_30d"]) / np.sqrt(np.clip(var, 1e-12, None))
    s["settle"] = np.zeros(len(t), dtype=bool)
    hit = np.searchsorted(t, ft, side="left"); hit = hit[hit < len(t)]
    s["settle"][hit] = True                                   # first bar at or after each settlement
    s["_ft"], s["_csum"] = ft, csum
    S[sym] = s
    all_rates.append(fr8[np.isfinite(fr8)])

rates = np.concatenate(all_rates) * 100
print("pairs with funding:", len(S) - 1, "| 8h-equivalent funding in percent: p1=%.3f p5=%.3f p25=%.3f median=%.3f p75=%.3f p95=%.3f p99=%.3f" % tuple(
    np.percentile(rates, [1, 5, 25, 50, 75, 95, 99])))

long_ = lambda s: np.ones_like(s["c"])
short_ = lambda s: -np.ones_like(s["c"])


def quarter(ms):
    d = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)
    return f"{d.year % 100}Q{(d.month - 1) // 3 + 1}"


def with_funding(trades):
    out = []
    for (t, sym, d, net, why, bars) in trades:
        s = S[sym]
        t_in, t_out = t + H, t + H + bars * H
        a = np.searchsorted(s["_ft"], t_in, side="right"); b = np.searchsorted(s["_ft"], t_out, side="right")
        paid = s["_csum"][b] - s["_csum"][a]                  # sum of raw rates settled while the trade was open
        out.append((t, sym, d, net - d * paid, why, bars))
    return out


def report(name, cond, side, sl, rr, hold):
    full = lambda s: cond(s) & s["settle"]
    tr = simulate(S, full, side, sl, rr, hold)
    if len(tr) < 100:
        print(f"     {name}: only {len(tr)} trades"); return
    trf = with_funding(tr)
    a, b = stats(tr), stats(trf)
    qs = {}
    for t in trf:
        qs.setdefault(quarter(t[0]), []).append(t)
    qstats = {q: stats(v) for q, v in sorted(qs.items())}
    good = sum(1 for st in qstats.values() if st["pf"] > 1.0)
    ok = b["pf"] > 1.10 and b["t"] > 2.0 and good >= int(round(0.7 * len(qstats)))
    print(f"{'PASS' if ok else '    '} {name:<52} price only {fmt(a)}\n       {'':<52} + funding  {fmt(b)} | profitable quarters {good}/{len(qstats)}")
    print("       " + " ".join(f"{q}:{st['pf']:.2f}({st['n']})" for q, st in qstats.items()))


SL, RR, HOLD = 0.08, 1.0, 48
print("\n=== F1. Crowded longs: SHORT when the last funding rate is high (stop 8%, target 8%, 48h)")
for thr in (0.0003, 0.0005, 0.0010):
    report(f"short, funding >= {thr*100:.2f}% per 8h", lambda s, thr=thr: s["fr8"] >= thr, short_, SL, RR, HOLD)
print("\n=== F2. Crowded shorts: LONG when the last funding rate is negative")
for thr in (-0.0002, -0.0005, -0.0010):
    report(f"long, funding <= {thr*100:.2f}% per 8h", lambda s, thr=thr: s["fr8"] <= thr, long_, SL, RR, HOLD)
print("\n=== F3. Unusual for the coin itself: 1-day funding vs its own 30-day history (z-score)")
report("short, z >= +2", lambda s: s["fr8_z"] >= 2.0, short_, SL, RR, HOLD)
report("long,  z <= -2", lambda s: s["fr8_z"] <= -2.0, long_, SL, RR, HOLD)
print("\n=== F4. Funding plus price")
report("short, funding >= 0.05% and coin +10% or more in 24h", lambda s: (s["fr8"] >= 0.0005) & (s["chg24"] >= 0.10), short_, SL, RR, HOLD)
report("long,  funding <= -0.03% and coin -10% or more in 24h", lambda s: (s["fr8"] <= -0.0003) & (s["chg24"] <= -0.10), long_, SL, RR, HOLD)
report("long,  funding <= -0.03% and coin +5% or more (shorts trapped)", lambda s: (s["fr8"] <= -0.0003) & (s["chg24"] >= 0.05), long_, SL, RR, HOLD)
report("short, funding >= 0.05% and coin -5% or more (longs trapped)", lambda s: (s["fr8"] >= 0.0005) & (s["chg24"] <= -0.05), short_, SL, RR, HOLD)
print("\n=== F5. Same core rules with a 24h and a 96h hold")
for hold in (24, 96):
    report(f"short, funding >= 0.05%, hold {hold}h", lambda s: s["fr8"] >= 0.0005, short_, SL, RR, hold)
    report(f"long, funding <= -0.05%, hold {hold}h", lambda s: s["fr8"] <= -0.0005, long_, SL, RR, hold)

# --- F6. Cross-sectional carry: each day short the 5 highest-funding pairs, long the 5 lowest, hold 24h ---
print("\n=== F6. Daily carry portfolio: short the 5 highest 1-day funding, long the 5 lowest, hold 24h")
COST = 2 * 0.0004 + 0.00025 + 0.00025
idx = {sym: dict(zip(s["t"].tolist(), range(len(s["t"])))) for sym, s in S.items() if sym != "BTCUSDT"}
days = sorted({int(t) for s in S.values() for t in s["t"] if int(t) % 86_400_000 == 0})
legs = {"short_high price": [], "short_high +funding": [], "long_low price": [], "long_low +funding": []}
for d in days:
    rows = []
    for sym, ix in idx.items():
        s = S[sym]
        i, j = ix.get(d), ix.get(d + 86_400_000)
        if i is None or j is None or not np.isfinite(s["fr8_1d"][i]) or not (s["vol24"][i] >= MIN_VOL):
            continue
        a = np.searchsorted(s["_ft"], d, side="right"); b = np.searchsorted(s["_ft"], d + 86_400_000, side="right")
        rows.append((s["fr8_1d"][i], s["c"][j] / s["c"][i] - 1, s["_csum"][b] - s["_csum"][a]))
    if len(rows) < 25:
        continue
    rows.sort()
    low, high = rows[:5], rows[-5:]
    legs["short_high price"].append((d, -np.mean([r for _, r, _ in high]) - COST))
    legs["short_high +funding"].append((d, -np.mean([r for _, r, _ in high]) + np.mean([f for _, _, f in high]) - COST))
    legs["long_low price"].append((d, np.mean([r for _, r, _ in low]) - COST))
    legs["long_low +funding"].append((d, np.mean([r for _, r, _ in low]) - np.mean([f for _, _, f in low]) - COST))
for name, rows in legs.items():
    a = np.array([r for _, r in rows])
    t = a.mean() / (a.std(ddof=1) / np.sqrt(len(a)))
    qs = {}
    for d, r in rows:
        qs.setdefault(quarter(d), []).append(r)
    good = sum(1 for v in qs.values() if np.sum(v) > 0)
    print(f"   {name:<20} days={len(a)} mean/day={100*a.mean():+.3f}% Sharpe={a.mean()/a.std(ddof=1)*np.sqrt(365):+.2f} t={t:+.2f} good quarters {good}/{len(qs)}  " +
          " ".join(f"{q}:{100*np.sum(v):+.0f}%" for q, v in sorted(qs.items())))
