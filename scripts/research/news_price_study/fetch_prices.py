"""5-minute bars (with pre/post market) for every stock that has news in the app's archive.

usage: fetch_prices.py <news_backup.json> <cache dir>

Yahoo keeps 5-minute bars for 60 days only. The download is deliberately small
and slow (a few symbols per call, a pause between calls, no threads): a fast
bulk pull once got this machine rate-limited and the live app lost its quotes.
It stops at the first batch that comes back completely empty.
"""
import json
import sys
import time
from pathlib import Path

import yfinance as yf

BACKUP = Path(sys.argv[1])
CACHE = Path(sys.argv[2])
CACHE.mkdir(parents=True, exist_ok=True)
BATCH = 8
PAUSE = 4.0
BENCHMARKS = ["SPY", "QQQ"]


def symbols():
    data = json.loads(BACKUP.read_text(encoding="utf-8"))
    counts = {}
    for bucket, articles in (data.get("ticker_news") or {}).items():
        if "-" in bucket or "." in bucket or not bucket.isalnum() or not bucket.isascii():
            continue                                    # crypto pairs and non-US listings
        fresh = [a for a in articles if not a.get("is_duplicate") and str(a.get("published_at")) >= "2026-09-20"]
        if fresh:
            counts[bucket] = len(fresh)
    ordered = sorted(counts, key=lambda s: -counts[s])
    return BENCHMARKS + [s for s in ordered if s not in BENCHMARKS]


def save(symbol, frame):
    frame = frame.dropna(subset=["Open", "Close"])
    if frame.empty:
        return 0
    rows = [[int(ts.timestamp()), round(float(o), 4), round(float(h), 4), round(float(lo), 4),
             round(float(c), 4), int(v) if v == v else 0]
            for ts, o, h, lo, c, v in zip(frame.index, frame["Open"], frame["High"], frame["Low"],
                                          frame["Close"], frame["Volume"])]
    (CACHE / f"{symbol}.json").write_text(json.dumps(rows), encoding="utf-8")
    return len(rows)


def main():
    todo = [s for s in symbols() if not (CACHE / f"{s}.json").exists() and not (CACHE / f"{s}.empty").exists()]
    print(f"{len(todo)} symbols to fetch", flush=True)
    got_any = False
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        try:
            data = yf.download(batch, period="60d", interval="5m", prepost=True, group_by="ticker",
                               threads=False, progress=False, auto_adjust=False)
        except Exception as exc:                         # noqa: BLE001 - reported, then stop
            print(f"stopped: download failed at {batch[0]}: {str(exc)[:200]}", flush=True)
            return
        saved = 0
        for symbol in batch:
            try:
                frame = data[symbol] if len(batch) > 1 else data
                if hasattr(frame.columns, "nlevels") and frame.columns.nlevels > 1:
                    frame = frame.droplevel(0, axis=1) if symbol in frame.columns.get_level_values(0) else frame
                rows = save(symbol, frame)
            except Exception:                            # noqa: BLE001 - symbol missing from the answer
                rows = 0
            if rows:
                saved += 1
            else:
                (CACHE / f"{symbol}.empty").write_text("", encoding="utf-8")
        print(f"{start + len(batch)}/{len(todo)} batch saved {saved}/{len(batch)}", flush=True)
        if saved == 0 and got_any:
            for symbol in batch:                         # do not remember a rate-limited batch as empty
                (CACHE / f"{symbol}.empty").unlink(missing_ok=True)
            print("stopped: a whole batch came back empty (rate limit?)", flush=True)
            return
        got_any = got_any or saved > 0
        time.sleep(PAUSE)
    print("done", flush=True)


if __name__ == "__main__":
    main()
