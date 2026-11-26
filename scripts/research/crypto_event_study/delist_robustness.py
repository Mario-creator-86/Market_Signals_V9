"""Is "short after a Binance spot delisting announcement" real, or an artefact?

usage: delist_robustness.py <dir with announcements.json and events.csv>

Checks, on the spot_delist events of event_study.py:
  1. timing     the minute-by-minute path around the announcement time: if price fell
                BEFORE the timestamp, the timestamp is late and the result is look-ahead;
  2. shortable  only tokens with a live Binance USDT perpetual at that moment, measured
                on the perpetual's own prices;
  3. funding    what a short paid or received in funding while it was held;
  4. clusters   one result per announcement (tokens of one notice fall together);
  5. tails      worst trades, and the same rule with a 15% stop;
  6. costs      2% round trip, for thin books in a panic.
"""
import csv
import datetime as dt
import importlib.util
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

DIR = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("event_study", Path(__file__).with_name("event_study.py"))
es = importlib.util.module_from_spec(spec)
spec.loader.exec_module(es)
HOUR, MIN = 3_600_000, 60_000
COST = es.COST
FUT = "https://fapi.binance.com"


def stats(rets, label):
    n = len(rets)
    if n < 5:
        return f"{label:44} n={n} (too few)"
    mean, sd = statistics.fmean(rets), statistics.pstdev(rets) or 1e-12
    gains, losses = sum(r for r in rets if r > 0), -sum(r for r in rets if r < 0)
    return (f"{label:44} n={n:3} mean={mean * 100:+6.2f}% median={statistics.median(rets) * 100:+6.2f}% "
            f"win={sum(r > 0 for r in rets) / n * 100:3.0f}% PF={min(gains / losses if losses else 99, 99):5.2f} "
            f"t={mean / (sd / math.sqrt(n)):+5.1f} worst={min(rets) * 100:+.0f}%")


def funding_paid_by_short(symbol, start, end):
    """Sum of funding rates settled in (start, end]: positive = the short received it."""
    key = es.CACHE / f"funding_{symbol}_{start}_{end}.json"
    if key.exists():
        return json.loads(key.read_text())
    data = es.http(f"{FUT}/fapi/v1/fundingRate?symbol={symbol}&startTime={start}&endTime={end}&limit=1000")
    total = sum(float(x["fundingRate"]) for x in data) if isinstance(data, list) else 0.0
    key.write_text(json.dumps(total))
    time.sleep(0.06)
    return total


def main():
    rows = [r for r in csv.DictReader((DIR / "events.csv").open(encoding="utf-8"))
            if r["kind"] == "spot_delist" and r["mode"] == "existing"]
    print(f"spot delisting events with a market at the time: {len(rows)}")

    # 1. timing --------------------------------------------------------------
    path = defaultdict(list)
    for r in rows:
        t = int(r["announced_ms"])
        m = es.klines("spot", r["symbol"], "1m", t - 30 * MIN, 100)
        px = {k[0]: k[1] for k in m}
        t0 = t // MIN * MIN
        if t0 not in px:
            continue
        for label, a, b in (("-30m to -5m", -30, -5), ("-5m to 0", -5, 0), ("0 to +2m", 0, 2), ("+2m to +10m", 2, 10),
                            ("+10m to +60m", 10, 60)):
            pa, pb = px.get(t0 + a * MIN), px.get(t0 + b * MIN)
            if pa and pb:
                path[label].append(pb / pa - 1)
    print("\n1. Price path around the announcement timestamp (spot, long side, no costs):")
    for label in ("-30m to -5m", "-5m to 0", "0 to +2m", "+2m to +10m", "+10m to +60m"):
        v = path[label]
        if v:
            print(f"   {label:13} n={len(v):3} mean={statistics.fmean(v) * 100:+6.2f}% median={statistics.median(v) * 100:+6.2f}%")

    # 2 + 3. shortable on a perpetual, with funding ---------------------------
    perp = []
    for r in rows:
        t = int(r["announced_ms"])
        base = r["base"]
        symbol, bars = None, []
        for cand in (base + "USDT", "1000" + base + "USDT"):
            bars = es.klines("fut", cand, "1h", t // HOUR * HOUR - 48 * HOUR, 200)
            if bars and bars[0][0] <= t - HOUR and bars[-1][0] > t:
                symbol = cand
                break
        if not symbol:
            continue
        idx = next((i for i, b in enumerate(bars) if b[0] > t), None)
        minute = es.klines("fut", symbol, "1m", t + 2 * MIN, 3)
        if idx is None or not minute or minute[0][0] > t + 15 * MIN:
            continue
        fast, slow = minute[0][1], bars[idx][1]
        item = {"base": base, "symbol": symbol, "t": t, "fast": fast, "slow": slow}
        for h in (4, 24):
            if idx + h >= len(bars):
                continue
            exit_ts, exit_px = bars[idx + h][0], bars[idx + h][1]
            highs = [b[2] for b in bars[idx:idx + h]]
            fund = funding_paid_by_short(symbol, t, exit_ts)
            item[f"fast{h}"] = -(exit_px / fast - 1)
            item[f"slow{h}"] = -(exit_px / slow - 1)
            item[f"fund{h}"] = fund
            stopped = max(highs) >= slow * 1.15
            item[f"slow{h}_stop"] = -0.15 if stopped else item[f"slow{h}"]
            stopped_fast = max(highs + [minute[0][2]]) >= fast * 1.15
            item[f"fast{h}_stop"] = -0.15 if stopped_fast else item[f"fast{h}"]
        perp.append(item)
    print(f"\n2. Tokens that had a live Binance USDT perpetual when the notice came out: {len(perp)} of {len(rows)}")
    print("   SHORT on the perpetual's own prices, after 0.13% costs:")
    for entry in ("fast", "slow"):
        for h in (4, 24):
            key = f"{entry}{h}"
            use = [p for p in perp if key in p]
            print("   " + stats([p[key] - COST for p in use], f"{entry} entry, hold {h}h, price only"))
            print("   " + stats([p[key] + p[f'fund{h}'] - COST for p in use], f"{entry} entry, hold {h}h, with funding"))
            print("   " + stats([p[key + '_stop'] + p[f'fund{h}'] - COST for p in use], f"{entry} entry, hold {h}h, funding + 15% stop"))
            print("   " + stats([p[key] + p[f'fund{h}'] - 0.02 for p in use], f"{entry} entry, hold {h}h, funding, 2% costs"))
    f4 = [p["fund4"] for p in perp if "fund4" in p]
    f24 = [p["fund24"] for p in perp if "fund24" in p]
    if f4:
        print(f"   funding a short collected(+)/paid(-): 4h mean {statistics.fmean(f4) * 100:+.2f}%, "
              f"24h mean {statistics.fmean(f24) * 100:+.2f}%, worst 24h {min(f24) * 100:+.2f}%")

    # 4. one result per announcement ------------------------------------------
    print("\n4. One result per announcement (equal-weight basket of its tokens), perpetual prices, funding and costs in:")
    for entry in ("fast", "slow"):
        for h in (4, 24):
            key = f"{entry}{h}"
            groups = defaultdict(list)
            for p in perp:
                if key in p:
                    groups[p["t"]].append(p[key] + p[f"fund{h}"] - COST)
            baskets = [(t, statistics.fmean(v)) for t, v in sorted(groups.items())]
            print("   " + stats([b for _, b in baskets], f"{entry} entry, hold {h}h, per announcement"))
            if entry == "slow" and h == 4:
                by_year = defaultdict(list)
                for t, b in baskets:
                    by_year[dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).year].append(b)
                print("      by year: " + ", ".join(
                    f"{y}: n={len(v)} mean={statistics.fmean(v) * 100:+.1f}% win={sum(x > 0 for x in v)}/{len(v)}"
                    for y, v in sorted(by_year.items())))

    # 5. tails ---------------------------------------------------------------
    worst = sorted((p for p in perp if "slow24" in p), key=lambda p: p["slow24"])[:6]
    print("\n5. Worst shorts (slow entry, 24h, price only):")
    for p in worst:
        d = dt.datetime.fromtimestamp(p["t"] / 1000, dt.timezone.utc)
        print(f"   {d:%Y-%m-%d} {p['symbol']:14} 4h {p.get('slow4', float('nan')) * 100:+7.1f}%  24h {p['slow24'] * 100:+7.1f}%  "
              f"funding 24h {p['fund24'] * 100:+.2f}%")


if __name__ == "__main__":
    main()
