"""Second benchmark for insider_study.py: the same kind of stocks, bought at any time.

usage: insider_vs_universe.py <dir with prices.pkl and insider_events.csv>

Insider purchases happen mostly in small companies, and small companies lagged
SPY badly in 2023-2025. So "vs SPY" may hide a real selection effect. Here each
trade is compared with the equal-weight average of ALL stocks in the price cache
(every company that had an insider purchase at some point) over the same days.
If buying right after the filing adds nothing over holding those stocks at
random times, this number is zero.
"""
import csv
import datetime as dt
import math
import pickle
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

DIR = Path(sys.argv[1])
COST = 0.002
HORIZONS = (5, 21, 63)

prices = pickle.loads((DIR / "prices.pkl").read_bytes())
frames = {s: f for s, f in prices.items() if f is not None and s != "SPY"}
opens = pd.DataFrame({s: f["Open"] for s, f in frames.items()}).sort_index()
closes = pd.DataFrame({s: f["Close"] for s, f in frames.items()}).sort_index()
days = list(opens.index)
position = {d.date(): i for i, d in enumerate(days)}
print(f"universe: {opens.shape[1]} stocks, {days[0].date()} .. {days[-1].date()}")

cache = {}


def universe_return(entry_day, h):
    key = (entry_day, h)
    if key not in cache:
        i = position.get(entry_day)
        if i is None or i + h - 1 >= len(days):
            cache[key] = None
        else:
            ratio = closes.iloc[i + h - 1] / opens.iloc[i] - 1
            ratio = ratio.dropna()
            ratio = ratio[(ratio > -0.95) & (ratio < 5)]          # ignore broken bars
            cache[key] = float(ratio.mean()) if len(ratio) >= 200 else None
    return cache[key]


rows = list(csv.DictReader((DIR / "insider_events.csv").open(encoding="utf-8")))
print(f"{'signal':34} {'hold':>5} {'n':>5} {'raw%':>7} {'universe%':>9} {'excess%':>8} {'t(months)':>9} {'months+':>8} {'qtrs+':>6}")
for signal in sorted({r["signal"] for r in rows}):
    for h in HORIZONS:
        excess, raw, base, months, quarters = [], [], [], defaultdict(list), defaultdict(list)
        for r in rows:
            if r["signal"] != signal or not r.get(f"r{h}"):
                continue
            day = dt.date.fromisoformat(r["entry_date"])
            u = universe_return(day, h)
            if u is None:
                continue
            x = float(r[f"r{h}"]) - u - COST
            excess.append(x)
            raw.append(float(r[f"r{h}"]))
            base.append(u)
            months[(day.year, day.month)].append(x)
            quarters[(day.year, (day.month - 1) // 3)].append(x)
        if len(excess) < 30:
            continue
        mm = [statistics.fmean(v) for v in months.values() if len(v) >= 3]
        qq = [statistics.fmean(v) for v in quarters.values() if len(v) >= 5]
        t = statistics.fmean(mm) / ((statistics.pstdev(mm) or 1e-12) / math.sqrt(len(mm))) if len(mm) >= 6 else float("nan")
        print(f"{signal:34} {h:>4}d {len(excess):>5} {statistics.fmean(raw) * 100:>7.2f} {statistics.fmean(base) * 100:>9.2f} "
              f"{statistics.fmean(excess) * 100:>8.2f} {t:>9.1f} {sum(x > 0 for x in mm):>3}/{len(mm):<4} "
              f"{sum(x > 0 for x in qq):>2}/{len(qq):<3}")
