"""Exploratory: do the app's own crypto news scores say anything about the next hours?

usage: own_news_study.py <data dir with news_backup.json> <cache dir>

The app has only about two months of scored crypto articles, almost all on BTC,
ETH and SOL, so this cannot pass the two-year bar. It answers a smaller
question: is there any sign worth following up once more history exists?
Articles of one coin on one day are not independent, so they are pooled per
coin and day before anything is measured.
"""
import datetime as dt
import json
import math
import statistics
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

DATA = Path(sys.argv[1])
CACHE = Path(sys.argv[2])
CACHE.mkdir(parents=True, exist_ok=True)
HOUR = 3_600_000
COST = 2 * 0.0004 + 0.00025 + 0.00025
MAP = {"BTC-USD": "BTCUSDT", "ETH-USD": "ETHUSDT", "SOL-USD": "SOLUSDT"}


def hourly(symbol, start):
    out, cur, now = {}, start, int(time.time() * 1000)
    while cur < now:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&startTime={cur}&limit=1000"
        req = urllib.request.Request(url, headers={"User-Agent": "market-research-script"})
        with urllib.request.urlopen(req, timeout=30) as r:
            rows = json.loads(r.read().decode())
        if not rows:
            break
        for k in rows:
            out[int(k[0])] = float(k[1])
        cur = int(rows[-1][0]) + HOUR
        time.sleep(0.1)
    return out


def articles():
    root = json.loads((DATA / "news_backup.json").read_text(encoding="utf-8"))

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                if isinstance(value, list) and value and isinstance(value[0], dict) and "title" in value[0]:
                    yield from value
                else:
                    yield from walk(value)

    seen = set()
    for a in walk(root):
        if not a.get("is_crypto") or a.get("is_duplicate") or a.get("id") in seen:
            continue
        # Most crypto articles are market-wide (no coin): they are measured against BTC.
        ticker = str(a.get("ticker") or "")
        ticker = ticker if ticker in MAP else "BTC-USD"
        seen.add(a.get("id"))
        try:
            ts = int(dt.datetime.fromisoformat(str(a.get("published_at")).replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            continue
        yield ticker, ts, a


def stats(rets):
    n = len(rets)
    if n < 8:
        return f"n={n:3} (too few)"
    mean, sd = statistics.fmean(rets), statistics.pstdev(rets) or 1e-12
    return (f"n={n:3} mean={mean * 100:+.2f}% median={statistics.median(rets) * 100:+.2f}% "
            f"win={sum(r > 0 for r in rets) / n * 100:.0f}% t={mean / (sd / math.sqrt(n)):+.1f}")


def main():
    rows = list(articles())
    if not rows:
        print("no crypto articles found")
        return
    start = min(ts for _, ts, _ in rows) // HOUR * HOUR - 24 * HOUR
    prices = {t: hourly(s, start) for t, s in MAP.items()}
    print(f"scored crypto articles: {len(rows)} from "
          f"{dt.datetime.fromtimestamp(min(ts for _, ts, _ in rows) / 1000, dt.timezone.utc):%Y-%m-%d} to "
          f"{dt.datetime.fromtimestamp(max(ts for _, ts, _ in rows) / 1000, dt.timezone.utc):%Y-%m-%d}; "
          f"per coin {dict((t, sum(1 for x, _, _ in rows if x == t)) for t in MAP)}")

    # pool per coin and UTC day: mean sentiment score, count of high-impact items, entry at the next day's first hour
    pooled = defaultdict(list)
    for ticker, ts, a in rows:
        pooled[(ticker, ts // (24 * HOUR))].append(a)
    days = []
    for (ticker, day), items in pooled.items():
        entry_ts = (day + 1) * 24 * HOUR
        px = prices[ticker]
        if entry_ts not in px:
            continue
        score = statistics.fmean(float(a.get("sentiment_score") or 0.0) for a in items)
        high = sum(1 for a in items if str(a.get("impact") or "").lower() in ("high", "critical"))
        r = {h: (px[entry_ts + h * HOUR] / px[entry_ts] - 1) for h in (4, 24, 72) if entry_ts + h * HOUR in px}
        days.append({"ticker": ticker, "score": score, "n": len(items), "high": high, "r": r})
    print(f"coin-days with news and a following price: {len(days)}")
    base24 = [d["r"][24] for d in days if 24 in d["r"]]
    print(f"baseline, every coin-day, long 24h (no costs): {stats(base24)}")
    for h in (4, 24, 72):
        print(f"\nhold {h}h, entry at the next UTC midnight, net of {COST * 100:.2f}% costs")
        pos = [d["r"][h] - COST for d in days if h in d["r"] and d["score"] >= 0.2]
        neg = [-d["r"][h] - COST for d in days if h in d["r"] and d["score"] <= -0.2]
        negl = [d["r"][h] - COST for d in days if h in d["r"] and d["score"] <= -0.2]
        busy = [d["r"][h] - COST for d in days if h in d["r"] and d["high"] >= 1]
        print(f"  LONG  after a clearly positive news day (score >= +0.2): {stats(pos)}")
        print(f"  SHORT after a clearly negative news day (score <= -0.2): {stats(neg)}")
        print(f"  LONG  after a clearly negative news day (contrarian):    {stats(negl)}")
        print(f"  LONG  after a day with at least one high-impact article: {stats(busy)}")


if __name__ == "__main__":
    main()
