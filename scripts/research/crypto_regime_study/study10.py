"""The classic, widely published trading rules on two years of Binance data.

usage: study10.py <klines dir with ~730 days of 1h bars>

Moving-average and MACD trend rules, RSI and Bollinger mean reversion,
Supertrend, Ichimoku, cross-sectional momentum / reversal / low volatility,
rotation into the strongest coins, pairs against BTC, weekday and hour-of-day
seasonality. Daily bars, a signal from the close of day i-1 is held during day i.
Portfolio = equal weight over the pairs with a position. Costs: 6.5 bps per unit
of turnover (4 bps commission + 2.5 bps slippage). Funding on shorts is ignored.

A rule passes when Sharpe > 1, t > 2 and at least 70% of the quarters are
positive - the same bar as study8.py. About 100 rules are tested here, so one
or two passes by luck alone would not be surprising.
"""
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(sys.argv[1])
COST = 0.00065
meta = json.loads((DATA / "_symbols.json").read_text())
hourly, daily = {}, {}
for sym in meta["symbols"]:
    arr = np.loadtxt(DATA / f"{sym}.csv", delimiter=",", skiprows=1)
    if arr.ndim != 2 or len(arr) < 24 * 200:
        continue
    t, o, h, l, c, qv = arr.T
    hourly[sym] = (t, c)
    frame = pd.DataFrame({"d": (t // 86_400_000).astype(np.int64), "h": h, "l": l, "c": c, "v": qv})
    g = frame.groupby("d")
    daily[sym] = {"d": g["c"].last().index.to_numpy(), "c": g["c"].last().to_numpy(), "h": g["h"].max().to_numpy(),
                  "l": g["l"].min().to_numpy(), "v": g["v"].sum().to_numpy()}

all_days = np.unique(np.concatenate([s["d"] for s in daily.values()]))
idx = {int(d): i for i, d in enumerate(all_days)}
N, M = len(all_days), len(daily)
syms = sorted(daily)
C = np.full((N, M), np.nan); H = np.full((N, M), np.nan); L = np.full((N, M), np.nan); V = np.full((N, M), np.nan)
for j, sym in enumerate(syms):
    s = daily[sym]
    rows = [idx[int(d)] for d in s["d"]]
    C[rows, j], H[rows, j], L[rows, j], V[rows, j] = s["c"], s["h"], s["l"], s["v"]
R = np.full((N, M), np.nan); R[1:] = C[1:] / C[:-1] - 1
liquid = np.full((N, M), False)
for i in range(7, N):
    liquid[i] = np.nanmean(V[i - 7:i], axis=0) >= 10_000_000      # known before day i
btc = syms.index("BTCUSDT")
Cdf, Hdf, Ldf, Rdf = pd.DataFrame(C), pd.DataFrame(H), pd.DataFrame(L), pd.DataFrame(R)
rows_out = []


def quarter(d):
    x = dt.date.fromordinal(dt.date(1970, 1, 1).toordinal() + int(d))
    return f"{x.year % 100}Q{(x.month - 1) // 3 + 1}"


def score(name, net, days):
    if len(net) < 60:
        rows_out.append((False, name, "too few days")); return
    sd = net.std(ddof=1)
    if sd == 0:
        rows_out.append((False, name, "no variation")); return
    sharpe = net.mean() / sd * np.sqrt(365)
    t = net.mean() / (sd / np.sqrt(len(net)))
    eq = np.cumprod(1 + net); dd = (eq / np.maximum.accumulate(eq) - 1).min()
    qs = {}
    for d, r in zip(days, net):
        qs.setdefault(quarter(d), []).append(r)
    good = sum(1 for v in qs.values() if np.sum(v) > 0)
    half = len(net) // 2
    ok = sharpe > 1.0 and t > 2.0 and good >= int(round(0.7 * len(qs)))
    rows_out.append((ok, name, f"days={len(net):>3} total={100 * (eq[-1] - 1):+8.1f}% Sharpe={sharpe:+5.2f} t={t:+5.2f} "
                               f"maxDD={100 * dd:6.1f}% quarters {good}/{len(qs)}  halves {100 * net[:half].sum():+.0f}%/{100 * net[half:].sum():+.0f}%"))


def evaluate(name, sig, ret=None, legs=1.0, use_liquid=True):
    """sig[i, j]: signal at the close of day i; it is held during day i+1."""
    ret = R if ret is None else ret
    pos = np.zeros((N, M)); pos[1:] = np.nan_to_num(sig[:-1])
    ok = np.isfinite(ret) & (liquid if use_liquid else True)
    pos = np.where(ok, pos, 0.0)
    prev = np.vstack([np.zeros((1, M)), pos[:-1]])
    n_act = np.maximum(1, (pos != 0).sum(axis=1))
    net = np.nansum(pos * np.nan_to_num(ret), axis=1) / n_act - np.abs(pos - prev).sum(axis=1) / n_act * COST * legs
    live = (pos != 0).sum(axis=1) > 0
    score(name, net[live], all_days[live])


def three(name, sig):
    evaluate(f"{name}, long/short", sig)
    evaluate(f"{name}, long only", np.where(sig > 0, 1.0, 0.0))
    evaluate(f"{name}, short only", np.where(sig < 0, -1.0, 0.0))


def stateful(enter_long, exit_long, enter_short, exit_short):
    sig = np.zeros((N, M))
    for j in range(M):
        state = 0.0
        for i in range(N):
            if not np.isfinite(C[i, j]):
                state = 0.0; continue
            if state == 0.0:
                state = 1.0 if enter_long[i, j] else (-1.0 if enter_short[i, j] else 0.0)
            elif (state > 0 and exit_long[i, j]) or (state < 0 and exit_short[i, j]):
                state = 0.0
            sig[i, j] = state
    return sig


def rsi(n):
    diff = Cdf.diff()
    up = diff.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    down = (-diff.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return (100 - 100 / (1 + up / down)).to_numpy()


def section(title):
    rows_out.append((None, title, ""))


np.seterr(invalid="ignore", divide="ignore")
sma = {n: Cdf.rolling(n).mean().to_numpy() for n in (5, 20, 50, 100, 200)}
ema = {n: Cdf.ewm(span=n, adjust=False, min_periods=n).mean().to_numpy() for n in (12, 20, 26, 50)}
NO = np.zeros((N, M), dtype=bool)

section("Trend: price against its moving average")
for n in (20, 50, 100, 200):
    three(f"close above/below SMA{n}", np.sign(C - sma[n]))
section("Trend: moving-average crossovers")
three("golden/death cross SMA50 vs SMA200", np.sign(sma[50] - sma[200]))
three("EMA20 vs EMA50", np.sign(ema[20] - ema[50]))
macd = pd.DataFrame(ema[12] - ema[26])
three("MACD(12,26,9) above/below its signal line", np.sign((macd - macd.ewm(span=9, adjust=False, min_periods=9).mean()).to_numpy()))

section("Supertrend(10,3) and Ichimoku cloud")
prev_c = np.vstack([np.full((1, M), np.nan), C[:-1]])
tr = np.fmax(H - L, np.fmax(np.abs(H - prev_c), np.abs(L - prev_c)))
atr = pd.DataFrame(tr).ewm(alpha=1 / 10, adjust=False, min_periods=10).mean().to_numpy()
st = np.zeros((N, M))
for j in range(M):
    up = lo = np.nan; d = 0.0
    for i in range(N):
        if not np.isfinite(atr[i, j]):
            continue
        mid = (H[i, j] + L[i, j]) / 2
        bu, bl = mid + 3 * atr[i, j], mid - 3 * atr[i, j]
        if d == 0.0:
            up, lo, d = bu, bl, 1.0
        else:
            up = bu if (bu < up or C[i - 1, j] > up) else up
            lo = bl if (bl > lo or C[i - 1, j] < lo) else lo
            d = 1.0 if C[i, j] > up else (-1.0 if C[i, j] < lo else d)
        st[i, j] = d
three("Supertrend(10,3)", st)
tenkan = (Hdf.rolling(9).max() + Ldf.rolling(9).min()) / 2
kijun = (Hdf.rolling(26).max() + Ldf.rolling(26).min()) / 2
span_a = ((tenkan + kijun) / 2).shift(26).to_numpy()
span_b = ((Hdf.rolling(52).max() + Ldf.rolling(52).min()) / 2).shift(26).to_numpy()
three("Ichimoku: above the cloud long, below short", np.where(C > np.fmax(span_a, span_b), 1.0, np.where(C < np.fmin(span_a, span_b), -1.0, 0.0)))

section("Mean reversion: RSI")
r14, r2 = rsi(14), rsi(2)
sig = stateful(r14 < 30, r14 > 50, r14 > 70, r14 < 50)
three("RSI14: buy below 30, short above 70, exit at 50", sig)
sig = stateful((r2 < 10) & (C > sma[200]), C > sma[5], (r2 > 90) & (C < sma[200]), C < sma[5])
three("Connors RSI2 with SMA200 filter, exit at SMA5", sig)
sig = stateful(r2 < 10, C > sma[5], r2 > 90, C < sma[5])
three("Connors RSI2 without trend filter", sig)

section("Bollinger bands (20, 2)")
sd20 = Cdf.rolling(20).std().to_numpy()
upper, lower = sma[20] + 2 * sd20, sma[20] - 2 * sd20
three("Bollinger reversion: fade the band, exit at the middle", stateful(C < lower, C >= sma[20], C > upper, C <= sma[20]))
three("Bollinger breakout: follow the band, exit at the middle", stateful(C > upper, C < sma[20], C < lower, C > sma[20]))

section("Cross-sectional: rank the liquid coins against each other (top / bottom fifth)")


def past_return(n):
    out = np.full((N, M), np.nan); out[n:] = C[n:] / C[:-n] - 1    # known at the close of day i
    return out


def ranked(metric, hold):
    """+1 for the top fifth, -1 for the bottom fifth of `metric`, re-ranked every `hold` days."""
    sig = np.zeros((N, M)); last = np.zeros(M)
    for i in range(N):
        if i % hold == 0:
            last = np.zeros(M)
            ok = np.isfinite(metric[i]) & liquid[i]
            if ok.sum() >= 15:
                k = max(1, int(ok.sum() // 5))
                order = np.argsort(np.where(ok, metric[i], np.nan))[:ok.sum()]
                last[order[-k:]] = 1.0; last[order[:k]] = -1.0
        sig[i] = last
    return sig


for n in (7, 14, 28):
    three(f"momentum {n}d, weekly: long winners / short losers", ranked(past_return(n), 7))
for n in (1, 3, 7):
    three(f"reversal {n}d, daily: long losers / short winners", -ranked(past_return(n), 1))
vol30 = Rdf.rolling(30).std().to_numpy()
three("low volatility, weekly: long calmest / short wildest", -ranked(vol30, 7))
m28 = past_return(28); rot = np.zeros((N, M)); last = np.zeros(M)
for i in range(N):
    if i % 7 == 0:
        last = np.zeros(M)
        ok = np.isfinite(m28[i]) & liquid[i]
        if np.isfinite(sma[100][i, btc]) and C[i, btc] > sma[100][i, btc] and ok.sum() >= 15:
            best = np.argsort(np.where(ok, m28[i], -np.inf))[-3:]
            last[best[m28[i][best] > 0]] = 1.0
    rot[i] = last
evaluate("rotation: 3 strongest coins (28d) while BTC is above SMA100, weekly", rot)

section("Pairs against BTC: a coin's price ratio to BTC, 30-day z-score beyond 2, exit at 0")
ratio = pd.DataFrame(C / C[:, [btc]])
z = ((ratio - ratio.rolling(30).mean()) / ratio.rolling(30).std()).to_numpy()
spread = R - R[:, [btc]]; spread[:, btc] = np.nan
sig = stateful(z < -2, z >= 0, z > 2, z <= 0)
evaluate("pairs reversion: long the cheap coin / short BTC and the reverse", sig, ret=spread, legs=2.0)
evaluate("pairs momentum: the opposite side", -sig, ret=spread, legs=2.0)
eth = syms.index("ETHUSDT"); only = np.zeros((N, M)); only[:, eth] = sig[:, eth]
evaluate("ETH/BTC only, reversion", only, ret=spread, legs=2.0)

section("BTC alone")
for n in (50, 100, 200):
    one = np.zeros((N, M)); one[:, btc] = np.sign(C[:, btc] - sma[n][:, btc])
    evaluate(f"BTC above/below SMA{n}, long/short", one)
    evaluate(f"BTC above SMA{n}, long only", np.where(one > 0, 1.0, 0.0))

section("Calendar: weekdays that were positive in the first year, traded in the second year")
basket = np.where(liquid, R, np.nan)
weekday = np.array([(int(d) + 3) % 7 for d in all_days])           # 1970-01-01 was a Thursday; 0 = Monday
half = N // 2
names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
for label, series in (("all liquid coins", np.nanmean(basket, axis=1)), ("BTC", R[:, btc])):
    good_days = [w for w in range(7) if np.nanmean(series[:half][weekday[:half] == w]) > 0]
    sig = np.zeros((N, M))
    hold = np.isin(weekday, good_days); hold[:half] = False
    # evaluate() shifts by one day, so mark the day before
    mark = np.zeros(N, dtype=bool); mark[:-1] = hold[1:]
    if label == "BTC":
        sig[mark, btc] = 1.0
    else:
        sig[mark] = 1.0
    evaluate(f"{label}: long on {'/'.join(names[w] for w in good_days) or 'no day'} (second year only)", sig)

section("Reference: just hold")
evaluate("hold every liquid coin (equal weight)", np.ones((N, M)))
one = np.zeros((N, M)); one[:, btc] = 1.0
evaluate("hold BTC", one)
evaluate("short every liquid coin / long BTC", -np.ones((N, M)), ret=spread, legs=2.0)

for ok, name, text in rows_out:
    if ok is None:
        print(f"\n=== {name}")
    else:
        print(f"{'PASS' if ok else '    '} {name:<66} {text}")
print(f"\npairs: {M} | days: {N} | {quarter(all_days[0])} -> {quarter(all_days[-1])} | rules tested: {sum(1 for r in rows_out if r[0] is not None)} | "
      f"passed: {sum(1 for r in rows_out if r[0])}")

# ---- hour of day, on hourly bars: hours that were positive in the first year, traded in the second
print("\n=== Hour of day (UTC): the 6 best hours of the first year, held in the second year")
for label, members in (("BTC", ["BTCUSDT"]), ("20 most traded coins", sorted(hourly, key=lambda s: -np.nansum(daily[s]["v"][-90:]))[:20])):
    frames = []
    for sym in members:
        t, c = hourly[sym]
        frames.append(pd.Series(c[1:] / c[:-1] - 1, index=(t[1:] // 3_600_000).astype(np.int64)))
    ret = pd.concat(frames, axis=1).mean(axis=1).sort_index()
    hours = ret.index.to_numpy() % 24
    cut = len(ret) // 2
    by_hour = pd.Series(ret.to_numpy()[:cut]).groupby(hours[:cut]).mean()
    best = set(by_hour.sort_values().index[-6:])
    hold = np.isin(hours[cut:], list(best))
    second = ret.to_numpy()[cut:]
    switches = np.abs(np.diff(np.concatenate([[0], hold.astype(float)])))
    net_h = np.where(hold, second, 0.0) - switches * COST
    day = (ret.index.to_numpy()[cut:] // 24)
    net_d = pd.Series(net_h).groupby(day).sum().to_numpy()
    gross_d = pd.Series(np.where(hold, second, 0.0)).groupby(day).sum().to_numpy()
    t_stat = net_d.mean() / (net_d.std(ddof=1) / np.sqrt(len(net_d)))
    print(f"     {label:<22} hours {sorted(int(h) for h in best)}: second-year gross {100 * gross_d.sum():+.1f}%, "
          f"net of costs {100 * net_d.sum():+.1f}% (t {t_stat:+.2f}, {len(net_d)} days)")
