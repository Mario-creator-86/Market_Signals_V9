"""Is the "opening gaps reverse" pattern from study.py real, or an artefact of the opening print?

usage: gap_fade_check.py <news_backup.json> <news_evidence_ledger.jsonl> <prices dir>

study.py found that following an opening gap of 3% or more to the close lost
about 0.7% against SPY (t about -5), i.e. fading it would have earned that.
Before believing it:
- the first print of the day sits on one side of a wide spread, which fakes a
  reversal: enter at the 09:35 bar instead;
- small, thin stocks cannot be traded at the app's cost assumption: repeat on
  liquid names only;
- in a falling market shorting anything works: split up-gaps (short) from
  down-gaps (long) and look at the return against SPY and against the stock's
  own drift;
- one good fortnight is not a pattern: count the weeks.
"""
import collections
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import study  # noqa: E402  (reads the same command line)

COST = study.COST
PRICES = Path(sys.argv[3])


def liquidity(symbol):
    """Median regular-session dollar volume and price."""
    rows = json.loads((PRICES / f"{symbol}.json").read_text(encoding="utf-8"))
    per_day = collections.defaultdict(float)
    closes = []
    for t, _o, _h, _l, c, v in rows:
        if study.in_session(t):
            per_day[(t + study.ny_offset(t)) // 86400] += c * v
            closes.append(c)
    if not per_day:
        return 0.0, 0.0
    return statistics.median(per_day.values()), statistics.median(closes)


def collect(symbols, news_days):
    spy = study.prices("SPY")
    out = []
    for symbol in symbols:
        book = study.prices(symbol)
        if not book or symbol == "SPY":
            continue
        dollars, price = liquidity(symbol)
        for pos in range(1, len(book["days"])):
            day, prev = book["days"][pos], book["days"][pos - 1]
            if day not in spy["first"]:
                continue
            first = book["first"][day]
            open_px, prev_close, close_px = book["op"][first], book["cl"][book["last"][prev]], book["cl"][book["last"][day]]
            gap = open_px / prev_close - 1
            if not 0.03 <= abs(gap) <= 0.30:
                continue
            start = book["ts"][first]
            if (start + study.ny_offset(start)) % 86400 != 34200:        # the 09:30 bar itself must exist
                continue
            late = study.price_at(book, start + 300, day)               # open of the 09:35 bar
            spy_open, spy_late = spy["op"][spy["first"][day]], study.price_at(spy, start + 300, day)
            spy_close = spy["cl"][spy["last"][day]]
            row = {"symbol": symbol, "day": day, "gap": gap, "dollars": dollars, "price": price,
                   "news": (symbol, day) in news_days}
            for tag, px, spx, minutes in (("0930", open_px, spy_open, 390.0), ("0935", late, spy_late, 385.0)):
                raw = close_px / px - 1
                if abs(raw) < 0.6:
                    exc = raw - (spy_close / spx - 1)
                    row[tag] = (raw, exc, exc - book["drift_min"] * minutes)
            out.append(row)
    return out


def report(name, rows, tag):
    rows = [r for r in rows if tag in r]
    if len(rows) < 40:
        print(f"{name:58} n={len(rows)} (too few)")
        return
    by_day_abn, by_week, nets, abns, excs = collections.defaultdict(list), collections.defaultdict(list), [], [], []
    for r in rows:
        d = -1 if r["gap"] > 0 else 1                                  # fade the gap
        raw, exc, abn = r[tag]
        net = max(-0.2, min(0.2, d * raw)) - COST
        nets.append(net), abns.append(max(-0.2, min(0.2, d * abn))), excs.append(max(-0.2, min(0.2, d * exc)))
        by_day_abn[r["day"]].append(abns[-1])
        by_week[r["day"] // 7].append(net)
    means = [statistics.fmean(v) for v in by_day_abn.values()]
    t = statistics.fmean(means) / (statistics.stdev(means) / math.sqrt(len(means))) if len(means) > 2 else 0.0
    weeks = [statistics.fmean(v) for v in by_week.values() if len(v) >= 5]
    print(f"{name:58} n={len(rows):<5} net {100 * statistics.fmean(nets):+.2f}%  median {100 * statistics.median(nets):+.2f}%  "
          f"win {100 * sum(1 for x in nets if x > 0) / len(nets):.0f}%  vs SPY {100 * statistics.fmean(excs):+.2f}%  "
          f"abnormal {100 * statistics.fmean(abns):+.2f}% (t {t:+.1f}, {len(means)} days)  "
          f"weeks positive {sum(1 for w in weeks if w > 0)}/{len(weeks)}")


def main():
    articles = study.load_articles()
    measured = [m for a in articles for m in [study.measure(a["symbol"], a["t0"], 0)] if m]
    news_days = {(m["symbol"], m["day"]) for m in measured if m["gap_entry"]}
    rows = collect(sorted({a["symbol"] for a in articles}), news_days)
    print(f"gaps of 3-30%: {len(rows)} on {len({r['symbol'] for r in rows})} stocks; fade = short up-gaps, buy down-gaps, exit at the close\n")
    liquid = [r for r in rows if r["dollars"] >= 20e6 and r["price"] >= 5]
    big = [r for r in rows if r["dollars"] >= 100e6 and r["price"] >= 10]
    for tag in ("0930", "0935"):
        print(f"--- entry at the {tag[:2]}:{tag[2:]} bar open")
        report("all stocks", rows, tag)
        report("liquid (>= $20M a day, price >= $5)", liquid, tag)
        report("very liquid (>= $100M a day, price >= $10)", big, tag)
        report("liquid, up-gaps (short)", [r for r in liquid if r["gap"] > 0], tag)
        report("liquid, down-gaps (long)", [r for r in liquid if r["gap"] < 0], tag)
        report("liquid, gap 3-6%", [r for r in liquid if abs(r["gap"]) < 0.06], tag)
        report("liquid, gap 6-30%", [r for r in liquid if abs(r["gap"]) >= 0.06], tag)
        report("liquid, an article behind the gap", [r for r in liquid if r["news"]], tag)
        report("liquid, no article in the app's archive", [r for r in liquid if not r["news"]], tag)
        print()
    print(f"liquid stocks: {len({r['symbol'] for r in liquid})}, very liquid: {len({r['symbol'] for r in big})}")


if __name__ == "__main__":
    main()
