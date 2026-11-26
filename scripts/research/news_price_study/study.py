"""Exploratory: do the app's scored stock news, alone or with the chart, say where price goes next?

usage: study.py <news_backup.json> <news_evidence_ledger.jsonl> <prices dir> [results.json]

Two months of scored articles and 60 trading days of 5-minute bars: this can
show where to look, it cannot prove an edge (the bar for that is two years,
quarter by quarter).

How it measures
- An article becomes a trade at the first regular-session 5-minute bar that
  opens after it was published (next session open when published outside
  regular hours). "+15" variants wait 15 minutes first, so the first reaction
  is known before the entry. No look-ahead: nothing after the entry bar's open
  is used to pick the trade.
- "reaction" = entry price against the last regular-session close before the
  article (for out-of-hours news this is the opening gap).
- "trend" = the five sessions before the article.
- Returns are measured against SPY over the same minutes (excess) and, as a
  trade, raw minus the app's US-stock costs (7 bps round trip).
- The stocks with the most news fell against SPY over these two months, so a
  short "works" on them with or without news. "abnormal" removes that: it is
  the excess minus what the same stock did on average over the same length of
  time in the whole sample (its intraday drift per minute and its overnight
  drift). The t-statistic and the two halves are on the abnormal return: they
  answer "does the article add information", not "did the stock fall".
- Articles on one stock in one session are not independent: each test keeps
  the first one per stock and session. Stocks move together inside a day, so
  the t-statistic is computed over daily averages, not over trades.
"""
import bisect
import collections
import datetime as dt
import json
import math
import statistics
import sys
from pathlib import Path

BACKUP, LEDGER, PRICES = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
OUT = Path(sys.argv[4]) if len(sys.argv) > 4 else None
COST = (2 * 2.0 + 1.5 + 1.5) / 10_000          # US_STOCKS: commission both sides + entry and exit slippage
CLIP = 0.20                                    # one bad print must not decide a cell
HORIZONS = ("30m", "2h", "close", "next_close")
MIN_N = 40
DST_END = int(dt.datetime(2026, 11, 1, 6, 0, tzinfo=dt.timezone.utc).timestamp())   # New York leaves DST
START = "2026-09-01"


def ny_offset(ts):
    return -4 * 3600 if ts < DST_END else -5 * 3600


def parse_ts(value):
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return int((t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).timestamp())
    except Exception:
        return None


def in_session(ts):
    local = ts + ny_offset(ts)
    return (local // 86400 + 3) % 7 < 5 and 34200 <= local % 86400 < 57600


_PRICES = {}


def prices(symbol):
    if symbol not in _PRICES:
        path = PRICES / f"{symbol}.json"
        book = None
        if path.exists():
            ts, op, cl, day = [], [], [], []
            for t, o, _h, _l, c, _v in json.loads(path.read_text(encoding="utf-8")):
                if in_session(t) and o > 0 and c > 0:
                    ts.append(t), op.append(o), cl.append(c), day.append((t + ny_offset(t)) // 86400)
            if ts:
                first, last = {}, {}
                for i, d in enumerate(day):
                    first.setdefault(d, i)
                    last[d] = i
                book = {"ts": ts, "op": op, "cl": cl, "day": day, "days": sorted(first), "first": first,
                        "last": last, "drift_min": 0.0, "drift_night": 0.0}
                spy = prices("SPY") if symbol != "SPY" else None
                if spy:
                    intraday, night = [], []
                    for pos, d in enumerate(book["days"]):
                        if d not in spy["first"]:
                            continue
                        o, c = op[first[d]], cl[last[d]]
                        so, sc = spy["op"][spy["first"][d]], spy["cl"][spy["last"][d]]
                        if abs(c / o - 1) < 0.6:
                            intraday.append((c / o - 1) - (sc / so - 1))
                        prev = book["days"][pos - 1] if pos else None
                        if prev is not None and prev in spy["last"]:
                            gap = o / cl[last[prev]] - 1
                            if abs(gap) < 0.6:
                                night.append(gap - (so / spy["cl"][spy["last"][prev]] - 1))
                    if len(intraday) >= 10:
                        book["drift_min"] = statistics.fmean(intraday) / 390.0
                    if len(night) >= 10:
                        book["drift_night"] = statistics.fmean(night)
        _PRICES[symbol] = book
    return _PRICES[symbol]


def price_at(book, when, day):
    """Open of the first bar at or after `when` inside `day`; the session close when there is none."""
    if day not in book["first"]:
        return None
    i = bisect.bisect_left(book["ts"], when)
    if i <= book["last"][day] and book["day"][i] == day:
        return book["op"][i]
    return book["cl"][book["last"][day]]


def measure(symbol, t0, delay_min):
    """Entry, chart state and forward returns for one article; None when it cannot be traded."""
    book, spy = prices(symbol), prices("SPY")
    if not book or not spy:
        return None
    i = bisect.bisect_left(book["ts"], t0 + delay_min * 60)
    if i >= len(book["ts"]) or book["ts"][i] - t0 > 4 * 86400:
        return None
    pre_i = bisect.bisect_right(book["ts"], t0 - 300) - 1          # last bar that had closed by t0
    if pre_i < 0:
        return None
    day, entry_ts, entry, pre = book["day"][i], book["ts"][i], book["op"][i], book["cl"][pre_i]
    spy_entry = price_at(spy, entry_ts, day)
    if not spy_entry:
        return None
    pos = book["days"].index(day)
    exits = {
        "30m": (price_at(book, entry_ts + 1800, day), price_at(spy, entry_ts + 1800, day)),
        "2h": (price_at(book, entry_ts + 7200, day), price_at(spy, entry_ts + 7200, day)),
        "close": (book["cl"][book["last"][day]], spy["cl"][spy["last"][day]]),
    }
    if pos + 1 < len(book["days"]):
        nxt = book["days"][pos + 1]
        if nxt in spy["last"]:
            exits["next_close"] = (book["cl"][book["last"][nxt]], spy["cl"][spy["last"][nxt]])
    session_left = (book["ts"][book["last"][day]] + 300 - entry_ts) / 60.0
    held = {"30m": min(30.0, session_left), "2h": min(120.0, session_left), "close": session_left}
    returns = {}
    for name, (px, spx) in exits.items():
        if px and spx:
            raw = px / entry - 1
            if abs(raw) < 0.6:                                      # split or bad print
                expected = (book["drift_min"] * held[name] if name in held
                            else book["drift_min"] * (session_left + 390.0) + book["drift_night"])
                exc = raw - (spx / spy_entry - 1)
                returns[name] = (raw, exc, exc - expected)
    pre_day = book["day"][pre_i]
    pre_pos = book["days"].index(pre_day)
    trend = (pre / book["cl"][book["last"][book["days"][pre_pos - 5]]] - 1) if pre_pos >= 5 else None
    return {"symbol": symbol, "day": day, "t0": t0, "entry_ts": entry_ts, "returns": returns,
            "reaction": entry / pre - 1, "trend": trend, "gap_entry": i == book["first"][day],
            "intraday": in_session(t0)}


def load_articles():
    data = json.loads(BACKUP.read_text(encoding="utf-8"))
    seen, out = set(), []
    for bucket, articles in (data.get("ticker_news") or {}).items():
        if not (bucket.isascii() and bucket.isalnum()):
            continue
        for a in articles:
            t0 = parse_ts(a.get("published_at"))
            key = (bucket, a.get("story_fingerprint") or a.get("id"))
            if a.get("is_duplicate") or t0 is None or str(a.get("published_at")) < START or key in seen:
                continue
            seen.add(key)
            label = str(a.get("sentiment") or "")
            score = a.get("sentiment_score") if isinstance(a.get("sentiment_score"), (int, float)) else 0.0
            out.append({"symbol": bucket, "t0": t0, "id": a.get("id"), "category": str(a.get("category") or ""),
                        "content": str(a.get("content_type") or ""), "impact": str(a.get("impact") or ""),
                        "sent": 1 if label == "bullish" else -1 if label == "bearish" else 0,
                        "score": float(score)})
    return out


def first_seen():
    seen = {}
    with LEDGER.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except Exception:
                continue
            when = parse_ts(row.get("observed_at"))
            if when is not None and (row.get("article_id") not in seen or when < seen[row["article_id"]]):
                seen[row.get("article_id")] = when
    return seen


def sign(x):
    return 1 if x > 0 else -1 if x < 0 else 0


def cell(name, trades, results):
    """trades: (measure dict, direction). One per stock and session, earliest article first."""
    kept = {}
    for m, d in sorted(trades, key=lambda t: t[0]["t0"]):
        if d:
            kept.setdefault((m["symbol"], m["day"]), (m, d))
    if len(kept) < MIN_N:
        return
    row = {"cell": name, "n": len(kept), "h": {}}
    for h in HORIZONS:
        by_day_exc, by_day_net, exc_all = collections.defaultdict(list), collections.defaultdict(list), []
        plain = []
        for m, d in kept.values():
            if h in m["returns"]:
                raw, exc, abnormal = m["returns"][h]
                e = max(-CLIP, min(CLIP, d * abnormal))
                by_day_exc[m["day"]].append(e)
                by_day_net[m["day"]].append(max(-CLIP, min(CLIP, d * raw)) - COST)
                exc_all.append(e)
                plain.append(max(-CLIP, min(CLIP, d * exc)))
        days = sorted(by_day_exc)
        if len(days) < 8:
            continue
        means = [statistics.fmean(by_day_exc[d]) for d in days]
        sd = statistics.stdev(means)
        half = len(days) // 2
        row["h"][h] = {
            "n": len(exc_all), "days": len(days),
            "abnormal_pct": round(100 * statistics.fmean(exc_all), 3),
            "excess_pct": round(100 * statistics.fmean(plain), 3),
            "t_days": round(statistics.fmean(means) / (sd / math.sqrt(len(means))), 2) if sd > 0 else 0.0,
            "win_pct": round(100 * sum(1 for e in exc_all if e > 0) / len(exc_all), 1),
            "net_pct": round(100 * statistics.fmean(x for d in days for x in by_day_net[d]), 3),
            "first_half_pct": round(100 * statistics.fmean(x for d in days[:half] for x in by_day_exc[d]), 3),
            "second_half_pct": round(100 * statistics.fmean(x for d in days[half:] for x in by_day_exc[d]), 3),
        }
    if row["h"]:
        results.append(row)


def run_cells(tag, measured, results):
    """measured: list of (article, measure dict) for one entry variant."""
    def add(name, pick):
        cell(f"{tag} | {name}", [(m, pick(a, m)) for a, m in measured], results)

    add("any article, long (should be ~0)", lambda a, m: 1)
    add("follow sentiment", lambda a, m: a["sent"])
    add("follow strong sentiment (|score|>=0.8)", lambda a, m: a["sent"] if abs(a["score"]) >= 0.8 else 0)
    add("bullish only, long", lambda a, m: 1 if a["sent"] > 0 else 0)
    add("bearish only, short", lambda a, m: -1 if a["sent"] < 0 else 0)
    add("high/critical impact, follow sentiment", lambda a, m: a["sent"] if a["impact"] in ("high", "critical") else 0)
    add("news (not social), follow sentiment", lambda a, m: a["sent"] if a["content"] == "news" else 0)
    add("social posts, follow sentiment", lambda a, m: a["sent"] if a["content"] == "social" else 0)
    for category in ("earnings", "general", "analyst", "merger", "fda", "sector", "dividend", "macro"):
        add(f"{category}, follow sentiment", lambda a, m, c=category: a["sent"] if a["category"] == c else 0)
        add(f"{category}, follow reaction >=1%",
            lambda a, m, c=category: sign(m["reaction"]) if a["category"] == c and abs(m["reaction"]) >= 0.01 else 0)
    # chart alone, on news days
    for size in (0.01, 0.03):
        add(f"follow reaction >={size:.0%}", lambda a, m, s=size: sign(m["reaction"]) if abs(m["reaction"]) >= s else 0)
        add(f"follow reaction >={size:.0%}, out-of-hours news (gap)",
            lambda a, m, s=size: sign(m["reaction"]) if abs(m["reaction"]) >= s and not m["intraday"] else 0)
        add(f"follow reaction >={size:.0%}, news during the session",
            lambda a, m, s=size: sign(m["reaction"]) if abs(m["reaction"]) >= s and m["intraday"] else 0)
    # news + chart
    add("sentiment confirmed by reaction >=0.5% (follow both)",
        lambda a, m: a["sent"] if a["sent"] and sign(m["reaction"]) == a["sent"] and abs(m["reaction"]) >= 0.005 else 0)
    add("sentiment confirmed by reaction >=2% (follow both)",
        lambda a, m: a["sent"] if a["sent"] and sign(m["reaction"]) == a["sent"] and abs(m["reaction"]) >= 0.02 else 0)
    add("sentiment contradicted by reaction >=0.5% (follow price)",
        lambda a, m: sign(m["reaction"]) if a["sent"] and sign(m["reaction"]) == -a["sent"] and abs(m["reaction"]) >= 0.005 else 0)
    add("sentiment contradicted by reaction >=0.5% (follow the news)",
        lambda a, m: a["sent"] if a["sent"] and sign(m["reaction"]) == -a["sent"] and abs(m["reaction"]) >= 0.005 else 0)
    add("sentiment with the 5-session trend",
        lambda a, m: a["sent"] if a["sent"] and m["trend"] is not None and sign(m["trend"]) == a["sent"] else 0)
    add("sentiment against the 5-session trend",
        lambda a, m: a["sent"] if a["sent"] and m["trend"] is not None and sign(m["trend"]) == -a["sent"] else 0)
    add("sentiment + reaction + trend all agree",
        lambda a, m: a["sent"] if a["sent"] and m["trend"] is not None and sign(m["trend"]) == a["sent"]
        and sign(m["reaction"]) == a["sent"] and abs(m["reaction"]) >= 0.005 else 0)
    add("bearish news after a 5-session rise >=5%, short",
        lambda a, m: -1 if a["sent"] < 0 and m["trend"] is not None and m["trend"] >= 0.05 else 0)
    add("bullish news after a 5-session fall >=5%, long",
        lambda a, m: 1 if a["sent"] > 0 and m["trend"] is not None and m["trend"] <= -0.05 else 0)


def gap_baseline(symbols, news_days, results):
    """Chart alone: follow the opening gap to the close, with and without an article behind it."""
    spy = prices("SPY")
    trades = {"all": [], "news": [], "no_news": []}
    for symbol in symbols:
        book = prices(symbol)
        if not book:
            continue
        for pos in range(1, len(book["days"])):
            day, prev = book["days"][pos], book["days"][pos - 1]
            if day not in spy["first"]:
                continue
            open_px, prev_close = book["op"][book["first"][day]], book["cl"][book["last"][prev]]
            gap = open_px / prev_close - 1
            raw = book["cl"][book["last"][day]] / open_px - 1
            if abs(gap) < 0.01 or abs(gap) > 0.6 or abs(raw) > 0.6:
                continue
            exc = raw - (spy["cl"][spy["last"][day]] / spy["op"][spy["first"][day]] - 1)
            m = {"symbol": symbol, "day": day, "t0": book["ts"][book["first"][day]],
                 "returns": {"close": (raw, exc, exc - book["drift_min"] * 390.0)}, "gap": gap}
            trades["all"].append(m)
            trades["news" if (symbol, day) in news_days else "no_news"].append(m)
    for size in (0.01, 0.03):
        for key, label in (("all", "all gaps"), ("news", "gap with an article behind it"), ("no_news", "gap without an article")):
            cell(f"gap baseline | follow gap >={size:.0%}, {label}",
                 [(m, sign(m["gap"])) for m in trades[key] if abs(m["gap"]) >= size], results)


def show(results):
    for row in results:
        print(f"\n{row['cell']}  (n={row['n']})")
        for h in HORIZONS:
            s = row["h"].get(h)
            if s:
                flag = "  <<" if abs(s["t_days"]) >= 3 and s["first_half_pct"] * s["second_half_pct"] > 0 else ""
                print(f"   {h:>10}: abnormal {s['abnormal_pct']:+.3f}%  t {s['t_days']:+.2f}  win {s['win_pct']:.0f}%  "
                      f"halves {s['first_half_pct']:+.3f}/{s['second_half_pct']:+.3f}  | vs SPY {s['excess_pct']:+.3f}%  "
                      f"trade net {s['net_pct']:+.3f}%  "
                      f"n {s['n']} days {s['days']}{flag}")


def main():
    articles = load_articles()
    print(f"articles after de-duplication: {len(articles)} on {len({a['symbol'] for a in articles})} stocks")
    results = []
    variants = {}
    for tag, delay in (("published", 0), ("published+15", 15)):
        measured = [(a, m) for a in articles for m in [measure(a["symbol"], a["t0"], delay)] if m]
        variants[tag] = measured
        print(f"{tag}: tradable articles {len(measured)}, "
              f"during the session {sum(1 for _a, m in measured if m['intraday'])}")
        run_cells(tag, measured, results)

    # what the app could really have traded: entry when it first saw the article
    seen = first_seen()
    lags = sorted((seen[a["id"]] - a["t0"]) / 60 for a in articles if a["id"] in seen)
    if lags:
        q = lambda f: round(lags[int(f * (len(lags) - 1))])
        print(f"app saw {len(lags)} of these articles; minutes after publication: "
              f"p10 {q(.1)}, p25 {q(.25)}, median {q(.5)}, p75 {q(.75)}, p90 {q(.9)}")
    live = [(a, m) for a in articles if a["id"] in seen
            for m in [measure(a["symbol"], max(a["t0"], seen[a["id"]]), 0)] if m]
    print(f"as seen by the app: tradable articles {len(live)}")
    run_cells("as seen by the app", live, results)

    news_days = {(m["symbol"], m["day"]) for _a, m in variants["published"] if m["gap_entry"]}
    gap_baseline(sorted({a["symbol"] for a in articles}), news_days, results)

    show(results)
    tests = sum(len(r["h"]) for r in results)
    strong = sum(1 for r in results for s in r["h"].values() if abs(s["t_days"]) >= 3)
    print(f"\n{tests} cell-horizon tests; |t|>=2 expected by chance in about {round(tests * 0.05)}; "
          f"|t|>=3 found in {strong} (about {round(tests * 0.003, 1)} expected by chance)")
    if OUT:
        OUT.write_text(json.dumps({"articles": len(articles), "results": results}, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
