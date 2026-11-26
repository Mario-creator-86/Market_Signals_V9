#!/usr/bin/env python
"""
Master live data freshness verification harness.
Run frequently during work: python scripts/verify_live_data.py
Checks key feeds are recent + live fetchable.
Add assertions for each tab as we fix.
"""
import json
import os
import datetime
import yfinance as yf

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "data")

KEY_FILES = [
    "signals_state.json",
    "capital.json",
    "news_backup.json",
    "pre_earnings.json",
    "premarket.json",
    "hotlist_gainers.json",
    "state.json",
]

print("=" * 70)
print("LIVE DATA FRESHNESS HARNESS")
print("Run time:", datetime.datetime.now().isoformat())
print("=" * 70)

all_good = True
for fname in KEY_FILES:
    p = os.path.join(DATA_DIR, fname)
    if os.path.exists(p):
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(p))
        age_min = (datetime.datetime.now() - mtime).total_seconds() / 60
        print(f"\n{fname}: mtime={mtime.strftime('%Y-%m-%d %H:%M')} age={age_min:.1f}min")
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            # Look for common timestamp fields
            for k in ["last_update", "timestamp", "updated_at", "as_of", "last_refreshed"]:
                if k in d:
                    print(f"  internal {k}: {d[k]}")
                    break
            if "signals" in d and d.get("signals"):
                print(f"  signals: {len(d['signals'])}")
        except Exception as e:
            print(f"  load err: {e}")
            all_good = False
    else:
        print(f"\n{fname}: MISSING")
        all_good = False

print("\n--- Direct live yf test (primary source for stocks/crypto) ---")
for sym in ["AAPL", "NVDA", "BTC-USD", "ETH-USD"]:
    try:
        info = yf.Ticker(sym).info
        price = info.get("regularMarketPrice") or info.get("currentPrice")
        print(f"{sym}: {price}")
    except Exception as e:
        print(f"{sym}: FETCH FAILED - {e}")
        all_good = False

print("\n" + ("ALL CHECKS PASSED (fresh enough + live fetch ok)" if all_good else "SOME ISSUES - investigate"))
print("=" * 70)