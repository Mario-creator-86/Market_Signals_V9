"""The bot's stock rule fades big movers (shorts what is up 8-40%, buys what is down). Is that wise on a day the whole market moves?

usage: fade_by_market_day.py <stock 5-minute prices dir>

Replays the live rule on 60 trading days of 5-minute bars: at 10:00, 11:00,
12:30 and 14:00 New York time, every liquid stock that is 8-40% away from the
previous close gets a fade trade (first hit of the day only), entered at the
next bar: stop 4%, target 6% (the live settings), otherwise out at the close.
The same entries are also replayed as "follow" trades (with the move).

Each result is split by what the market was doing at that moment:
- breadth: share of liquid stocks above the previous close;
- SPY against its previous close.
Returns are shown raw and against SPY over the same minutes, because in this
window SPY fell 9% and any short looks good. Costs 7 bps. t over daily means.
"""
import datetime as dt
import json
import math
import statistics
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

STOCKS = Path(sys.argv[1])
NY = ZoneInfo("America/New_York")
COST = 0.0007
SCANS = (600, 660, 750, 840)
STOP, TARGET = 0.04, 0.06


def load(symbol):
    by_day = {}
    for ts, o, h, l, c, v in json.loads((STOCKS / f"{symbol}.json").read_text(encoding="utf-8")):
        local = dt.datetime.fromtimestamp(ts, NY)
        minutes = local.hour * 60 + local.minute
        if 570 <= minutes < 960:
            by_day.setdefault(local.date(), []).append((minutes, o, h, l, c, v))
    out, prev_close = {}, None
    for day in sorted(by_day):
        bars = sorted(by_day[day])
        if prev_close and len(bars) >= 60:
            out[day] = {"prev": prev_close, "bars": bars, "dollars": sum(b[4] * b[5] for b in bars)}
        prev_close = bars[-1][4]
    return out


def price_at(bars, minute):
    """Last close at or before `minute`."""
    last = None
    for bar in bars:
        if bar[0] + 5 <= minute:
            last = bar[4]
        else:
            break
    return last


def trade(bars, minute, side, stop, target):
    """Enter at the open of the first bar at or after `minute`; return (net return, exit minute)."""
    later = [b for b in bars if b[0] >= minute]
    if len(later) < 3:
        return None
    entry = later[0][1]
    for bar in later:
        high, low = bar[2], bar[3]
        adverse = (high / entry - 1) if side < 0 else (1 - low / entry)
        favourable = (1 - low / entry) if side < 0 else (high / entry - 1)
        if adverse >= stop:                      # the stop first: the cautious reading of a bar that hit both
            return -stop - COST, bar[0] + 5, entry
        if favourable >= target:
            return target - COST, bar[0] + 5, entry
    return side * (later[-1][4] / entry - 1) - COST, 960, entry


spy = load("SPY")
book = {}
for path in sorted(STOCKS.glob("*.json")):
    if path.stem == "SPY":
        continue
    s = load(path.stem)
    if len(s) >= 30 and statistics.median(d["dollars"] for d in s.values()) >= 20e6 \
            and statistics.median(d["bars"][-1][4] for d in s.values()) >= 5:
        book[path.stem] = s
days = sorted(spy)
print(f"liquid stocks: {len(book)} | days: {len(days)} ({days[0]} .. {days[-1]})")

rows = []
for day in days:
    taken = set()
    for minute in SCANS:
        spy_now = price_at(spy[day]["bars"], minute)
        if not spy_now:
            continue
        spy_change = spy_now / spy[day]["prev"] - 1
        changes = {}
        for symbol, s in book.items():
            d = s.get(day)
            if d:
                px = price_at(d["bars"], minute)
                if px:
                    changes[symbol] = px / d["prev"] - 1
        if len(changes) < 100:
            continue
        breadth = sum(1 for c in changes.values() if c > 0) / len(changes)
        for symbol, change in changes.items():
            if symbol in taken or not 0.08 <= abs(change) <= 0.40:
                continue
            taken.add(symbol)
            bars = book[symbol][day]["bars"]
            fade_side = -1 if change > 0 else 1
            fade = trade(bars, minute, fade_side, STOP, TARGET)
            follow = trade(bars, minute, -fade_side, 0.025, 0.05)
            if not fade or not follow:
                continue

            def vs_spy(result, side):
                spy_entry = price_at(spy[day]["bars"], minute + 5) or spy_now
                spy_exit = price_at(spy[day]["bars"], result[1]) or spy[day]["bars"][-1][4]
                return result[0] - side * (spy_exit / spy_entry - 1)

            rows.append({"day": day, "up": change > 0, "breadth": breadth, "spy": spy_change,
                         "fade": fade[0], "fade_x": vs_spy(fade, fade_side),
                         "follow": follow[0], "follow_x": vs_spy(follow, -fade_side)})

print(f"trades: {len(rows)} ({sum(1 for r in rows if r['up'])} movers up, {sum(1 for r in rows if not r['up'])} movers down)\n")


def cell(name, pick):
    chosen = [r for r in rows if pick(r)]
    if len({r["day"] for r in chosen}) < 6:
        print(f"  {name:<58} too few days ({len({r['day'] for r in chosen})} days, {len(chosen)} trades)")
        return
    out = []
    for key in ("fade", "fade_x", "follow", "follow_x"):
        by_day = {}
        for r in chosen:
            by_day.setdefault(r["day"], []).append(r[key])
        means = [statistics.fmean(v) for v in by_day.values()]
        t = statistics.fmean(means) / (statistics.stdev(means) / math.sqrt(len(means))) if len(means) > 2 and statistics.stdev(means) > 0 else 0.0
        out.append(f"{100 * statistics.fmean(r[key] for r in chosen):+.2f}% (t {t:+.1f})")
    wins = 100 * sum(1 for r in chosen if r["fade"] > 0) / len(chosen)
    print(f"  {name:<58} n={len(chosen):<5} days={len({r['day'] for r in chosen}):<3} fade {out[0]}, vs SPY {out[1]}, wins {wins:.0f}% | follow {out[2]}, vs SPY {out[3]}")


print("=== movers UP (the live rule shorts them)")
cell("all days", lambda r: r["up"])
cell("broad market up (>= 65% of stocks green)", lambda r: r["up"] and r["breadth"] >= 0.65)
cell("very broad market up (>= 80% green)", lambda r: r["up"] and r["breadth"] >= 0.80)
cell("mixed market (35-65% green)", lambda r: r["up"] and 0.35 < r["breadth"] < 0.65)
cell("broad market down (<= 35% green)", lambda r: r["up"] and r["breadth"] <= 0.35)
cell("SPY up >= 0.75% at that moment", lambda r: r["up"] and r["spy"] >= 0.0075)
cell("SPY down <= -0.75% at that moment", lambda r: r["up"] and r["spy"] <= -0.0075)
print("\n=== movers DOWN (the live rule buys them)")
cell("all days", lambda r: not r["up"])
cell("broad market down (<= 35% green)", lambda r: not r["up"] and r["breadth"] <= 0.35)
cell("very broad market down (<= 20% green)", lambda r: not r["up"] and r["breadth"] <= 0.20)
cell("mixed market (35-65% green)", lambda r: not r["up"] and 0.35 < r["breadth"] < 0.65)
cell("broad market up (>= 65% green)", lambda r: not r["up"] and r["breadth"] >= 0.65)
cell("SPY down <= -0.75% at that moment", lambda r: not r["up"] and r["spy"] <= -0.0075)
cell("SPY up >= 0.75% at that moment", lambda r: not r["up"] and r["spy"] >= 0.0075)
