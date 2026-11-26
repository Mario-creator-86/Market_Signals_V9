"""Does it pay to position with the market's direction of the day?

usage: study.py <stock 5-minute prices dir> <crypto klines dir, ~730 days of 1h bars> [--no-index]

The owner's question: "today the whole market is green - the bot should have
been long". Three tests of that idea:

A. Index, ten years of daily bars (SPY, QQQ, IWM from Yahoo): after a gap up
   at the open, does the index keep rising to the close? After an up day, is
   the next day up?
B. Stocks, 60 trading days of 5-minute bars: on days the market opens green,
   do liquid stocks (all of them / the ones that gapped up / the ones that
   gapped down) rise from 09:35 to the close? Mirror for red days.
C. Crypto, two years: after a day on which BTC rose 2% or more (or most coins
   rose), what do the liquid coins do the next day? And inside the day: BTC up
   by noon UTC, hold to midnight.

Costs: stocks 7 bps round trip, crypto 13 bps round trip. t-statistics are over
daily means, because trades of one day move together.
"""
import datetime as dt
import json
import math
import statistics
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

STOCKS, CRYPTO = Path(sys.argv[1]), Path(sys.argv[2])
NY = ZoneInfo("America/New_York")
STOCK_COST, CRYPTO_COST = 0.0007, 0.0013


def tstat(xs):
    xs = [x for x in xs if x == x]
    if len(xs) < 3:
        return 0.0
    sd = statistics.stdev(xs)
    return statistics.fmean(xs) / (sd / math.sqrt(len(xs))) if sd > 0 else 0.0


def show(name, daily_means, trades=None, cost=0.0):
    daily_means = [x for x in daily_means if x == x]
    if len(daily_means) < 8:
        print(f"  {name:<64} too few days ({len(daily_means)})")
        return
    mean = statistics.fmean(daily_means)
    wins = sum(1 for x in daily_means if x > cost)
    extra = f" trades {trades}" if trades is not None else ""
    print(f"  {name:<64} days {len(daily_means):>4}{extra:<14} mean {100 * mean:+.3f}%  net {100 * (mean - cost):+.3f}%  "
          f"t {tstat(daily_means):+.2f}  days positive after costs {100 * wins / len(daily_means):.0f}%")


# ---------------------------------------------------------------- A. index, ten years
if "--no-index" not in sys.argv:
    print("=== A. Index, ten years of daily bars")
    try:
        import yfinance as yf

        for symbol in ("SPY", "QQQ", "IWM"):
            frame = yf.Ticker(symbol).history(period="10y", interval="1d", auto_adjust=False)
            o, c = frame["Open"].to_numpy(), frame["Close"].to_numpy()
            gap = o[1:] / c[:-1] - 1
            intraday = c[1:] / o[1:] - 1
            day = c[1:] / c[:-1] - 1
            print(f" {symbol}: {len(day)} days, mean open-to-close {100 * intraday.mean():+.3f}%, mean close-to-close {100 * day.mean():+.3f}%")
            for label, mask in (("gap up >= 0.3%", gap >= 0.003), ("gap up >= 0.75%", gap >= 0.0075),
                                ("gap down <= -0.3%", gap <= -0.003), ("gap down <= -0.75%", gap <= -0.0075)):
                show(f"{label}: long open -> close", list(intraday[mask]))
            prev = day[:-1]
            nxt = day[1:]
            for label, mask in (("after an up day >= 1%", prev >= 0.01), ("after a down day <= -1%", prev <= -0.01)):
                show(f"{label}: long the next day (close -> close)", list(nxt[mask]))
            last_two = frame.index[-1].date()
        print(f" (data to {last_two})")
    except Exception as exc:  # noqa: BLE001
        print("  index data unavailable:", exc)

# ---------------------------------------------------------------- B. stocks, 5-minute bars
print("\n=== B. Stocks, 60 trading days of 5-minute bars (entry at the 09:35 bar, exit at the close)")


def sessions(symbol):
    """Per trading day: previous close, 09:30 open, 09:35 open, close, dollar volume."""
    path = STOCKS / f"{symbol}.json"
    if not path.exists():
        return {}
    out, prev_close = {}, None
    by_day = {}
    for ts, o, h, l, c, v in json.loads(path.read_text(encoding="utf-8")):
        local = dt.datetime.fromtimestamp(ts, NY)
        minutes = local.hour * 60 + local.minute
        if 570 <= minutes < 960:
            by_day.setdefault(local.date(), []).append((minutes, o, c, v))
    for day in sorted(by_day):
        bars = sorted(by_day[day])
        first = bars[0]
        late = next((b for b in bars if b[0] >= 575), None)
        close = bars[-1][2]
        if prev_close and first[0] == 570 and late and len(bars) >= 60:
            out[day] = {"gap": first[1] / prev_close - 1, "late": late[1], "close": close,
                        "dollars": sum(b[2] * b[3] for b in bars)}
        prev_close = close
    return out


spy = sessions("SPY")
book = {}
for path in sorted(STOCKS.glob("*.json")):
    symbol = path.stem
    if symbol == "SPY":
        continue
    s = sessions(symbol)
    if len(s) < 30:
        continue
    dollars = statistics.median(d["dollars"] for d in s.values())
    price = statistics.median(d["close"] for d in s.values())
    if dollars >= 20e6 and price >= 5:
        book[symbol] = s
days = sorted(spy)
print(f" liquid stocks: {len(book)} | days: {len(days)} ({days[0]} .. {days[-1]}) | SPY over the window {100 * (spy[days[-1]]['close'] / spy[days[0]]['close'] - 1):+.1f}%")
breadth = {}
for day in days:
    gaps = [s[day]["gap"] for s in book.values() if day in s]
    breadth[day] = sum(1 for g in gaps if g > 0) / len(gaps) if gaps else float("nan")


def stock_cell(name, day_ok, pick, side):
    means, trades = [], 0
    for day in days:
        if not day_ok(day):
            continue
        spy_ret = spy[day]["close"] / spy[day]["late"] - 1
        rets = []
        for s in book.values():
            d = s.get(day)
            if d and pick(d["gap"]):
                raw = d["close"] / d["late"] - 1
                if abs(raw) < 0.5:
                    rets.append(side * raw)
        if rets:
            means.append(statistics.fmean(rets)); trades += len(rets)
    show(name, means, trades, STOCK_COST)


green = lambda day: spy[day]["gap"] >= 0.003            # noqa: E731
red = lambda day: spy[day]["gap"] <= -0.003             # noqa: E731
broad_green = lambda day: breadth[day] >= 0.65          # noqa: E731
broad_red = lambda day: breadth[day] <= 0.35            # noqa: E731
print(f" green opens (SPY gap >= +0.3%): {sum(1 for d in days if green(d))} days; red opens: {sum(1 for d in days if red(d))}; "
      f"broad green (>= 65% of stocks gap up): {sum(1 for d in days if broad_green(d))}; broad red: {sum(1 for d in days if broad_red(d))}")
for label, ok in (("green open", green), ("broad green open", broad_green)):
    stock_cell(f"{label}: long every liquid stock", ok, lambda g: True, 1)
    stock_cell(f"{label}: long stocks that gapped up >= 2%", ok, lambda g: g >= 0.02, 1)
    stock_cell(f"{label}: long stocks that gapped up >= 5%", ok, lambda g: g >= 0.05, 1)
    stock_cell(f"{label}: long stocks that gapped down <= -2%", ok, lambda g: g <= -0.02, 1)
for label, ok in (("red open", red), ("broad red open", broad_red)):
    stock_cell(f"{label}: short every liquid stock", ok, lambda g: True, -1)
    stock_cell(f"{label}: short stocks that gapped down <= -2%", ok, lambda g: g <= -0.02, -1)
    stock_cell(f"{label}: short stocks that gapped up >= 2%", ok, lambda g: g >= 0.02, -1)
stock_cell("any day: long every liquid stock (reference)", lambda day: True, lambda g: True, 1)

# ---------------------------------------------------------------- C. crypto, two years
print("\n=== C. Crypto, two years (liquid = $10M a day over the last 7 days)")
meta = json.loads((CRYPTO / "_symbols.json").read_text())
daily, hourly_close = {}, {}
for sym in meta["symbols"]:
    arr = np.loadtxt(CRYPTO / f"{sym}.csv", delimiter=",", skiprows=1)
    if arr.ndim != 2 or len(arr) < 24 * 200:
        continue
    t, o, h, l, c, qv = arr.T
    g = pd.DataFrame({"d": (t // 86_400_000).astype(np.int64), "c": c, "v": qv, "o": o,
                      "hr": ((t // 3_600_000) % 24).astype(np.int64)}).groupby("d")
    noon = pd.DataFrame({"d": (t // 86_400_000).astype(np.int64), "c": c, "hr": ((t // 3_600_000) % 24).astype(np.int64)})
    noon = noon[noon["hr"] == 11].set_index("d")["c"]          # close of the 11:00 bar = price at 12:00 UTC
    daily[sym] = {"c": g["c"].last(), "o": g["o"].first(), "v": g["v"].sum(), "noon": noon}
C = pd.DataFrame({s: d["c"] for s, d in daily.items()}).sort_index()
O = pd.DataFrame({s: d["o"] for s, d in daily.items()}).reindex(C.index)
V = pd.DataFrame({s: d["v"] for s, d in daily.items()}).reindex(C.index)
NOON = pd.DataFrame({s: d["noon"] for s, d in daily.items()}).reindex(C.index)
R = C.pct_change()
liquid = V.rolling(7).mean().shift(1) >= 10e6
basket = R.where(liquid).mean(axis=1)
btc = R["BTCUSDT"]
share_up = (R.where(liquid) > 0).sum(axis=1) / liquid.sum(axis=1)
nxt = R.shift(-1).where(liquid.shift(-1, fill_value=False))


def crypto_cell(name, mask, pick="all", side=1):
    means = []
    for day in C.index[mask.fillna(False).to_numpy()]:
        row, today = nxt.loc[day].dropna(), R.loc[day]
        if len(row) < 10:
            continue
        if pick != "all":
            ranked = today[row.index].dropna().sort_values()
            k = max(1, len(ranked) // 5)
            row = row[ranked.index[-k:]] if pick == "winners" else row[ranked.index[:k]]
        means.append(side * float(np.clip(row, -0.5, 0.5).mean()))
    show(name, means, None, CRYPTO_COST)


for label, mask in (("after BTC +2% or more", btc >= 0.02), ("after BTC +4% or more", btc >= 0.04),
                    ("after 80% of the liquid coins rose", share_up >= 0.8)):
    crypto_cell(f"{label}: long every liquid coin the next day", mask)
    crypto_cell(f"{label}: long that day's strongest fifth", mask, "winners")
    crypto_cell(f"{label}: long that day's weakest fifth", mask, "losers")
for label, mask in (("after BTC -2% or worse", btc <= -0.02), ("after 80% of the liquid coins fell", share_up <= 0.2)):
    crypto_cell(f"{label}: short every liquid coin the next day", mask, "all", -1)
    crypto_cell(f"{label}: long every liquid coin the next day (rebound)", mask, "all", 1)
crypto_cell("any day: long every liquid coin (reference)", btc == btc)

print(" inside the day: by 12:00 UTC the market is up, hold to 24:00")
half = (NOON / O - 1)
rest = (C / NOON - 1).where(liquid)
for label, mask, side in (("BTC up >= 1.5% by noon: long every liquid coin", half["BTCUSDT"] >= 0.015, 1),
                          ("BTC up >= 3% by noon: long every liquid coin", half["BTCUSDT"] >= 0.03, 1),
                          ("BTC down <= -1.5% by noon: short every liquid coin", half["BTCUSDT"] <= -0.015, -1)):
    means = [side * float(np.clip(rest.loc[d].dropna(), -0.5, 0.5).mean()) for d in C.index[mask.fillna(False).to_numpy()]
             if rest.loc[d].notna().sum() >= 10]
    show(label, means, None, CRYPTO_COST)
