"""Do open-market insider purchases predict the next weeks? (SEC Form 4, 2023 onwards)

usage: insider_study.py <dir with *_form345.zip>

Events come from the SEC's quarterly Insider Transactions Data Sets: Form 4,
transaction code P (open-market purchase), not under a 10b5-1 plan, filed within
ten days of the trade. A position is opened at the OPEN of the first trading day
after the filing date (the filing is public by then) and held 5, 21 or 63
trading days.

Stocks rose over these years, so a raw long return proves nothing. Every result
is also shown against SPY over the same days ("vs SPY"), and the verdict uses
that number, judged per calendar month of entry: purchases bunch up after
selloffs, so single trades are not independent.

Prices: Yahoo daily bars through yfinance, cached next to the data. Tickers
Yahoo no longer serves (bankrupt, acquired) are missing: the share is printed,
and it flatters the result if the missing ones were mostly losers.
"""
import csv
import datetime as dt
import io
import math
import pickle
import re
import statistics
import sys
import time
import zipfile
from collections import defaultdict
from pathlib import Path

import pandas as pd

DIR = Path(sys.argv[1])
HORIZONS = (5, 21, 63)
COST = 0.002          # round trip: spread and slippage on a liquid US stock
COST_STRESS = 0.006
MIN_DOLLAR_VOLUME = 1_000_000.0
MIN_PRICE = 2.0
MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def parse_date(text):
    try:
        day, mon, year = text.strip().split("-")
        return dt.date(int(year), MONTHS[mon.upper()], int(day))
    except (ValueError, KeyError, AttributeError):
        return None


def table(zf, name):
    with zf.open(name) as handle:
        yield from csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", errors="replace"), delimiter="\t")


def number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


# ----------------------------------------------------------------------------- events
def load_filings():
    """One row per Form 4 filing that holds open-market purchases."""
    filings = {}
    for path in sorted(DIR.glob("*_form345.zip")):
        zf = zipfile.ZipFile(path)
        subs = {}
        for row in table(zf, "SUBMISSION.tsv"):
            if row["DOCUMENT_TYPE"] != "4" or row.get("AFF10B5ONE") == "1":
                continue
            symbol = (row.get("ISSUERTRADINGSYMBOL") or "").strip().upper()
            if not re.fullmatch(r"[A-Z]{1,5}([.\-][A-Z]{1,2})?", symbol):
                continue
            filed = parse_date(row["FILING_DATE"])
            if filed:
                subs[row["ACCESSION_NUMBER"]] = {"symbol": symbol.replace(".", "-"), "filed": filed,
                                                 "issuer_cik": row["ISSUERCIK"]}
        owners = defaultdict(list)
        for row in table(zf, "REPORTINGOWNER.tsv"):
            if row["ACCESSION_NUMBER"] in subs:
                owners[row["ACCESSION_NUMBER"]].append(row)
        for row in table(zf, "NONDERIV_TRANS.tsv"):
            acc = row["ACCESSION_NUMBER"]
            if acc not in subs or row["TRANS_CODE"] != "P" or row["TRANS_ACQUIRED_DISP_CD"] != "A":
                continue
            shares, price = number(row["TRANS_SHARES"]), number(row["TRANS_PRICEPERSHARE"])
            traded = parse_date(row["TRANS_DATE"])
            if shares <= 0 or price <= 0 or not traded:
                continue
            item = filings.setdefault(acc, {**subs[acc], "value": 0.0, "traded": traded, "owners": owners.get(acc, [])})
            item["value"] += shares * price
            item["traded"] = max(item["traded"], traded)
    out = []
    for acc, f in filings.items():
        lag = (f["filed"] - f["traded"]).days
        if lag < 0 or lag > 10 or f["value"] > 5e8:     # stale, or a reporting error
            continue
        rel = " ".join((o.get("RPTOWNER_RELATIONSHIP") or "") for o in f["owners"])
        titles = " ".join((o.get("RPTOWNER_TITLE") or "") for o in f["owners"]).upper()
        f["officer"] = "Officer" in rel
        f["director"] = "Director" in rel
        f["insider"] = f["officer"] or f["director"]          # not only a 10% holder
        f["chief"] = bool(re.search(r"\bCEO\b|CHIEF EXECUTIVE|\bCFO\b|CHIEF FINANCIAL", titles))
        f["owner_ids"] = sorted({o.get("RPTOWNERCIK") for o in f["owners"] if o.get("RPTOWNERCIK")})
        out.append(f)
    return sorted(out, key=lambda f: f["filed"])


def build_events(filings):
    """-> list of (signal, symbol, filing date). One event per symbol and signal every 30 days."""
    events, last = [], {}

    def emit(signal, f):
        key = (signal, f["symbol"])
        if key in last and (f["filed"] - last[key]).days < 30:
            return
        last[key] = f["filed"]
        events.append({"signal": signal, "symbol": f["symbol"], "filed": f["filed"], "value": f["value"]})

    recent = defaultdict(list)     # symbol -> [(filed, owner ids)] of insider buys >= $10k
    for f in filings:
        if not f["insider"]:
            continue
        if f["value"] >= 25_000:
            emit("any insider buy >= $25k", f)
        if f["value"] >= 100_000:
            emit("insider buy >= $100k", f)
        if f["value"] >= 500_000:
            emit("insider buy >= $500k", f)
        if f["chief"] and f["value"] >= 100_000:
            emit("CEO/CFO buy >= $100k", f)
        if f["value"] >= 10_000:
            window = [(d, ids) for d, ids in recent[f["symbol"]] if (f["filed"] - d).days <= 30]
            window.append((f["filed"], f["owner_ids"]))
            recent[f["symbol"]] = window
            distinct = {i for _, ids in window for i in ids}
            if len(distinct) >= 3:
                emit("cluster: 3+ insiders in 30 days", f)
    return events


# ----------------------------------------------------------------------------- prices
def load_prices(symbols, start):
    import yfinance as yf

    cache = DIR / "prices.pkl"
    prices = pickle.loads(cache.read_bytes()) if cache.exists() else {}
    todo = [s for s in symbols if s not in prices]
    # Small, slow batches: the app on this machine uses the same Yahoo quota, and a
    # fast bulk download gets the whole address rate-limited for several minutes.
    for i in range(0, len(todo), 40):
        chunk = todo[i:i + 40]
        try:
            data = yf.download(chunk, start=start, auto_adjust=True, progress=False, threads=False, group_by="ticker")
        except Exception as exc:  # noqa: BLE001
            print(f"  price chunk failed: {exc.__class__.__name__}; stopping the download", flush=True)
            break
        got = {}
        for s in chunk:
            try:
                frame = data[s] if len(chunk) > 1 else data
                frame = frame[["Open", "Close", "Volume"]].dropna()
            except (KeyError, TypeError):
                frame = None
            got[s] = frame if frame is not None and len(frame) >= 30 else None
        if not any(frame is not None for frame in got.values()):
            # A whole batch with nothing is a rate limit, not forty delisted stocks:
            # stop, and do not record these symbols as missing.
            print(f"  nothing came back for a batch of {len(chunk)}: rate limited, stopping at {i}/{len(todo)}", flush=True)
            break
        prices.update(got)
        cache.write_bytes(pickle.dumps(prices))
        print(f"  prices: {min(i + 40, len(todo))}/{len(todo)} symbols fetched", flush=True)
        time.sleep(6)
    return prices


# ----------------------------------------------------------------------------- measure
def measure(events, prices):
    spy = prices.get("SPY")
    rows, missing = [], 0
    for e in events:
        frame = prices.get(e["symbol"])
        if frame is None or spy is None:
            missing += 1
            continue
        after = frame.index[frame.index.date > e["filed"]]
        if len(after) == 0:
            continue
        idx = frame.index.get_loc(after[0])
        if idx < 20:
            continue
        hist = frame.iloc[idx - 20:idx]
        entry = float(frame["Open"].iloc[idx])
        if entry < MIN_PRICE or float((hist["Close"] * hist["Volume"]).median()) < MIN_DOLLAR_VOLUME:
            continue
        if (after[0].date() - e["filed"]).days > 7:      # the stock did not trade right after the filing
            continue
        row = {**e, "entry_date": after[0].date(), "entry": entry}
        for h in HORIZONS:
            if idx + h - 1 >= len(frame):
                row[f"r{h}"] = row[f"spy{h}"] = None
                continue
            exit_day = frame.index[idx + h - 1]
            row[f"r{h}"] = float(frame["Close"].iloc[idx + h - 1]) / entry - 1
            try:
                row[f"spy{h}"] = float(spy["Close"].loc[exit_day]) / float(spy["Open"].loc[after[0]]) - 1
            except KeyError:
                row[f"spy{h}"] = None
        rows.append(row)
    return rows, missing


def quarter(day):
    return f"{day.year}Q{(day.month - 1) // 3 + 1}"


def describe(values):
    n = len(values)
    mean = statistics.fmean(values)
    sd = statistics.pstdev(values) or 1e-12
    return mean, mean / (sd / math.sqrt(n))


def main():
    filings = load_filings()
    print(f"Form 4 filings with open-market purchases (not 10b5-1, filed within 10 days): {len(filings)}, "
          f"{filings[0]['filed']} .. {filings[-1]['filed']}")
    events = build_events(filings)
    by_signal = defaultdict(int)
    for e in events:
        by_signal[e["signal"]] += 1
    print("events:", dict(by_signal))
    symbols = sorted({e["symbol"] for e in events}) + ["SPY"]
    prices = load_prices(symbols, (filings[0]["filed"] - dt.timedelta(days=60)).isoformat())
    served = sum(1 for s in symbols if prices.get(s) is not None)
    print(f"symbols with Yahoo prices: {served} of {len(symbols)}")
    rows, missing = measure(events, prices)
    print(f"events measured: {len(rows)} (no price history: {missing}; the rest failed the price or liquidity filter)")
    with (DIR / "insider_events.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print(f"\nLONG at the next open after the filing; net of {COST * 100:.1f}% costs. 'vs SPY' = same trade minus SPY over the same days.")
    print(f"{'signal':34} {'hold':>5} {'n':>5} {'mean%':>7} {'med%':>7} {'win%':>5} {'vsSPY%':>7} {'t(months)':>9} "
          f"{'months+':>8} {'qtrs+':>6} {'stress%':>8}")
    verdicts = []
    for signal in sorted(by_signal):
        for h in HORIZONS:
            use = [r for r in rows if r["signal"] == signal and r.get(f"r{h}") is not None and r.get(f"spy{h}") is not None]
            if len(use) < 30:
                continue
            raw = [r[f"r{h}"] - COST for r in use]
            rel = [r[f"r{h}"] - r[f"spy{h}"] - COST for r in use]
            months, quarters = defaultdict(list), defaultdict(list)
            for r, x in zip(use, rel):
                months[(r["entry_date"].year, r["entry_date"].month)].append(x)
                quarters[quarter(r["entry_date"])].append(x)
            month_means = [statistics.fmean(v) for v in months.values() if len(v) >= 3]
            quarter_means = [statistics.fmean(v) for v in quarters.values() if len(v) >= 5]
            m_mean, m_t = describe(month_means) if len(month_means) >= 6 else (float("nan"), float("nan"))
            stress = statistics.fmean(r[f"r{h}"] - r[f"spy{h}"] - COST_STRESS for r in use)
            q_pos, q_all = sum(x > 0 for x in quarter_means), len(quarter_means)
            print(f"{signal:34} {h:>4}d {len(use):>5} {statistics.fmean(raw) * 100:>7.2f} {statistics.median(raw) * 100:>7.2f} "
                  f"{sum(x > 0 for x in raw) / len(raw) * 100:>5.0f} {statistics.fmean(rel) * 100:>7.2f} {m_t:>9.1f} "
                  f"{sum(x > 0 for x in month_means):>3}/{len(month_means):<4} {q_pos:>2}/{q_all:<3} {stress * 100:>8.2f}")
            if statistics.fmean(rel) > 0 and m_t >= 2.0 and stress > 0 and q_all >= 6 and q_pos / q_all >= 0.65:
                verdicts.append((signal, h, len(use), statistics.fmean(rel), m_t, q_pos, q_all))
    print("\nRules that pass the bar (beats SPY after costs, t over entry months >= 2, positive at 0.6% cost, >= 65% of quarters):")
    if not verdicts:
        print("  none")
    for signal, h, n, rel, t, q_pos, q_all in verdicts:
        print(f"  {signal}, hold {h} days: n={n}, {rel * 100:+.2f}% vs SPY per trade, t={t:.1f}, quarters {q_pos}/{q_all}")


if __name__ == "__main__":
    pd.options.mode.chained_assignment = None
    main()
