"""What does price do after a Binance listing / delisting announcement?

usage: event_study.py <dir with announcements.json> [since=2023-01-01]

For every announcement that names tradable symbols, the study enters AFTER the
announcement was published and measures the return over 4, 24 and 72 hours:

  fast entry  open of the 1-minute bar that starts at least two minutes after the
              announcement (a bot that polls the page every minute);
  slow entry  open of the next full hour;
  new market  when the pair did not trade yet, open of the second hourly bar.

Costs: the app's futures round trip (0.13%) and a stress case (0.60%) for thin
tokens. A rule counts only if it is profitable overall AND in most quarters.
Results: printed, plus events.csv next to the announcements.
"""
import csv
import datetime as dt
import json
import math
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

DIR = Path(sys.argv[1])
SINCE = sys.argv[2] if len(sys.argv) > 2 else "2023-01-01"
SINCE_MS = int(dt.datetime.fromisoformat(SINCE).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
CACHE = DIR / "klines_cache"
CACHE.mkdir(parents=True, exist_ok=True)
HOUR = 3_600_000
HORIZONS = (4, 24, 72)
COST = 2 * 0.0004 + 0.00025 + 0.00025      # same round trip as the regime studies
COST_STRESS = 0.006
SPOT = "https://api.binance.com/api/v3/klines"
FUT = "https://fapi.binance.com/fapi/v1/klines"
STABLE_OR_MAJOR = {"USDT", "USDC", "FDUSD", "USD", "BNB", "BTC", "ETH", "USDP", "TUSD", "BUSD", "DAI", "EUR", "AEUR", "USD1"}
SKIP_WORDS = ("bStocks", "TradFi", "Tokenized", "COIN-M", "USDC-Margined", "Copy Trading", "Options")


def http(url):
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "market-research-script"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 404):
                return []
            if exc.code in (418, 429):
                raise SystemExit(f"rate limited by Binance ({exc.code}); stop and retry later")
            if attempt == 3:
                return []
            time.sleep(2.0 * (attempt + 1))
        except Exception:  # noqa: BLE001
            if attempt == 3:
                return []
            time.sleep(2.0 * (attempt + 1))
    return []


def klines(market, symbol, interval, start, limit):
    key = CACHE / f"{market}_{symbol}_{interval}_{start}_{limit}.json"
    if key.exists():
        return json.loads(key.read_text())
    base = SPOT if market == "spot" else FUT
    data = http(f"{base}?symbol={symbol}&interval={interval}&startTime={start}&limit={limit}")
    rows = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[7])] for k in data] if isinstance(data, list) else []
    key.write_text(json.dumps(rows))
    time.sleep(0.06)
    return rows


# ------------------------------------------------------------------ announcements -> events
def split_tokens(text):
    text = re.sub(r"\(.*?\)", " ", text)
    return [t for t in re.split(r",|\band\b|&|\s+", text) if re.fullmatch(r"[A-Z0-9]{2,12}", t or "")]


def classify(title):
    """-> list of (event_type, base_or_symbol)."""
    if any(word in title for word in SKIP_WORDS):
        return []
    out = []
    if title.startswith("Binance Futures Will Launch"):
        syms = re.findall(r"\b([A-Z0-9]{2,15})USDT\b", title)
        if not syms:
            m = re.search(r"Margined (.+?) Perpetual", title)
            syms = split_tokens(m.group(1)) if m else []
        out += [("futures_launch", s) for s in syms]
    elif title.startswith("Binance Futures Will Delist"):
        out += [("futures_delist", s) for s in re.findall(r"\b([A-Z0-9]{2,15})USDT\b", title)]
    elif re.match(r"Binance Will Delist .+ on \d{4}-\d{2}-\d{2}", title):
        m = re.match(r"Binance Will Delist (.+?) on \d{4}-\d{2}-\d{2}", title)
        out += [("spot_delist", s) for s in split_tokens(m.group(1))]
    elif title.startswith("Binance Will List") or re.match(r"Binance Will Add .+\(([A-Z0-9]{2,12})\) on Earn", title):
        out += [("spot_listing", s) for s in re.findall(r"\(([A-Z0-9]{2,12})\)", title)]
    return [(kind, base) for kind, base in out
            if base not in STABLE_OR_MAJOR and not base.endswith(("USDC", "USDT"))]


def load_bars(base, t):
    """Hourly bars around the announcement: spot if the pair exists there, else the perpetual."""
    start = t // HOUR * HOUR - 48 * HOUR
    for market, symbol in (("spot", base + "USDT"), ("fut", base + "USDT"), ("fut", "1000" + base + "USDT")):
        bars = klines(market, symbol, "1h", start, 200)
        if bars:
            return market, symbol, bars
    return None, None, []


def measure(kind, base, t, btc):
    market, symbol, bars = load_bars(base, t)
    if len(bars) < 6:
        return None
    existed = bars[0][0] <= t - HOUR          # traded before the announcement
    if existed:
        idx = next((i for i, b in enumerate(bars) if b[0] > t), None)
        if idx is None:
            return None
        minute = klines(market, symbol, "1m", t + 120_000, 3)
        fast = minute[0][1] if minute and minute[0][0] <= t + 15 * 60_000 else None
        pre = bars[idx - 1][1] if idx >= 1 else None   # open of the hour that holds the announcement
        mode = "existing"
    else:
        if bars[0][0] - t > 14 * 24 * HOUR:   # a pair that starts weeks later is not this event
            return None
        idx = 1                                # second hourly bar of a brand-new market
        if len(bars) <= idx:
            return None
        fast, pre, mode = None, None, "new_market"
    entry_ts, slow = bars[idx][0], bars[idx][1]
    row = {"kind": kind, "base": base, "symbol": symbol, "market": market, "mode": mode,
           "announced_ms": t, "entry_ms": entry_ts, "entry_slow": slow, "entry_fast": fast,
           "jump_before_slow_entry": (slow / pre - 1) if pre else None,
           "quote_volume_24h_before": sum(b[5] for b in bars[max(0, idx - 24):idx]) if existed else None}
    for h in HORIZONS:
        if idx + h >= len(bars):
            row[f"r{h}_slow"] = row[f"r{h}_fast"] = row[f"btc{h}"] = None
            continue
        exit_px = bars[idx + h][1]
        row[f"r{h}_slow"] = exit_px / slow - 1
        row[f"r{h}_fast"] = exit_px / fast - 1 if fast else None
        b0, b1 = btc.get(entry_ts), btc.get(bars[idx + h][0])
        row[f"btc{h}"] = (b1 / b0 - 1) if b0 and b1 else None
    return row


# ------------------------------------------------------------------ statistics
def quarter(ms):
    d = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def summarise(rets, quarters):
    n = len(rets)
    if n < 8:
        return None
    mean = statistics.fmean(rets)
    sd = statistics.pstdev(rets) or 1e-12
    gains, losses = sum(r for r in rets if r > 0), -sum(r for r in rets if r < 0)
    by_q = defaultdict(list)
    for r, q in zip(rets, quarters):
        by_q[q].append(r)
    judged = [statistics.fmean(v) for v in by_q.values() if len(v) >= 3]
    return {"n": n, "mean": mean, "median": statistics.median(rets), "win": sum(r > 0 for r in rets) / n,
            "pf": gains / losses if losses else float("inf"), "t": mean / (sd / math.sqrt(n)),
            "q_pos": sum(m > 0 for m in judged), "q_all": len(judged)}


def main():
    ann = json.loads((DIR / "announcements.json").read_text(encoding="utf-8"))
    events, seen = [], set()
    for a in sorted(ann, key=lambda r: r["release_ms"]):
        if a["release_ms"] < SINCE_MS:
            continue
        for kind, base in classify(a["title"]):
            key = (kind, base, a["release_ms"] // (24 * HOUR))
            if key not in seen:
                seen.add(key)
                events.append((kind, base, a["release_ms"]))
    print(f"announcements since {SINCE}: events to measure = {len(events)}",
          {k: sum(1 for e in events if e[0] == k) for k in sorted({e[0] for e in events})})

    btc, cur = {}, SINCE_MS // HOUR * HOUR - 72 * HOUR
    now = int(time.time() * 1000)
    while cur < now:
        chunk = klines("spot", "BTCUSDT", "1h", cur, 1000) if cur + 1000 * HOUR < now else \
            [[int(k[0]), float(k[1])] for k in http(f"{SPOT}?symbol=BTCUSDT&interval=1h&startTime={cur}&limit=1000")]
        if not chunk:
            break
        for k in chunk:
            btc[k[0]] = k[1]
        cur = chunk[-1][0] + HOUR

    rows = []
    for i, (kind, base, t) in enumerate(events):
        row = measure(kind, base, t, btc)
        if row:
            rows.append(row)
        if (i + 1) % 100 == 0:
            print(f"  measured {i + 1}/{len(events)} (usable so far {len(rows)})", flush=True)
    fields = list(rows[0].keys())
    with (DIR / "events.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"usable events: {len(rows)} -> {DIR / 'events.csv'}")

    print("\nNet return per trade after costs. 'hedged' subtracts the BTC move over the same hours.")
    print(f"{'event / market':30} {'side':5} {'entry':5} {'hold':>4} {'n':>4} {'mean%':>7} {'med%':>7} {'win%':>5} {'PF':>5} "
          f"{'t':>5} {'qtrs+':>6} {'hedged%':>8} {'stress%':>8}")
    verdicts = []
    for kind in sorted({r["kind"] for r in rows}):
        for mode in ("existing", "new_market"):
            group = [r for r in rows if r["kind"] == kind and r["mode"] == mode]
            if len(group) < 8:
                continue
            for side, sign in (("LONG", 1), ("SHORT", -1)):
                for entry in (("fast", "slow") if mode == "existing" else ("slow",)):
                    for h in HORIZONS:
                        use = [r for r in group if r.get(f"r{h}_{entry}") is not None]
                        rets = [sign * r[f"r{h}_{entry}"] - COST for r in use]
                        s = summarise(rets, [quarter(r["announced_ms"]) for r in use])
                        if not s:
                            continue
                        hedged = [sign * (r[f"r{h}_{entry}"] - r[f"btc{h}"]) - 2 * COST for r in use if r.get(f"btc{h}") is not None]
                        stress = statistics.fmean(sign * r[f"r{h}_{entry}"] - COST_STRESS for r in use)
                        label = f"{kind} / {mode}"
                        print(f"{label:30} {side:5} {entry:5} {h:>3}h {s['n']:>4} {s['mean'] * 100:>7.2f} {s['median'] * 100:>7.2f} "
                              f"{s['win'] * 100:>5.0f} {min(s['pf'], 99):>5.2f} {s['t']:>5.1f} {s['q_pos']:>2}/{s['q_all']:<3} "
                              f"{(statistics.fmean(hedged) * 100 if hedged else float('nan')):>8.2f} {stress * 100:>8.2f}")
                        passes = (s["mean"] > 0 and s["t"] >= 2.0 and s["pf"] >= 1.3 and stress > 0
                                  and s["q_all"] >= 4 and s["q_pos"] / s["q_all"] >= 0.65)
                        if passes:
                            verdicts.append((label, side, entry, h, s))
    print("\nRules that pass the bar (mean>0, t>=2, PF>=1.3, positive at 0.6% cost, >=65% of quarters positive):")
    if not verdicts:
        print("  none")
    for label, side, entry, h, s in verdicts:
        print(f"  {label} {side} entry={entry} hold={h}h  n={s['n']} mean={s['mean'] * 100:.2f}% PF={s['pf']:.2f} "
              f"t={s['t']:.1f} quarters {s['q_pos']}/{s['q_all']}")


if __name__ == "__main__":
    main()
