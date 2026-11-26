"""Download 1h klines for the most-traded Binance USDT spot pairs (public API, no key).

usage: fetch_klines.py <outdir> [days=150] [top=110]
Writes one CSV per symbol: open_time_ms,open,high,low,close,quote_volume
"""
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

OUT = Path(sys.argv[1])
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 150
TOP = int(sys.argv[3]) if len(sys.argv) > 3 else 110
OUT.mkdir(parents=True, exist_ok=True)
BASE = "https://api.binance.com"
STABLE = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "EURI", "AEUR", "USD1", "XUSD", "BFUSD", "USDE", "PYUSD", "RLUSD", "USDS", "FRAX", "GBP", "TRY", "BRL"}


def get(path: str):
    for attempt in range(5):
        try:
            req = urllib.request.Request(BASE + path, headers={"User-Agent": "market-research-script"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as exc:  # noqa: BLE001
            if attempt == 4:
                raise
            time.sleep(1.5 * (attempt + 1))


tickers = get("/api/v3/ticker/24hr")
rows = []
for t in tickers:
    sym = t["symbol"]
    if not sym.endswith("USDT") or not (sym.isascii() and sym.isalnum()):
        continue
    base = sym[:-4]
    if base in STABLE or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
        continue
    rows.append((float(t["quoteVolume"]), sym))
rows.sort(reverse=True)
symbols = [s for _, s in rows[:TOP]]
if "BTCUSDT" not in symbols:
    symbols.append("BTCUSDT")
print("symbols:", len(symbols), "| smallest 24h quote volume in set: %.1fM" % (rows[min(TOP, len(rows)) - 1][0] / 1e6))

END = int(time.time() * 1000)
START = END - DAYS * 86400 * 1000


def fetch(sym: str):
    out = OUT / f"{sym}.csv"
    cur, lines = START, []
    while cur < END:
        data = get(f"/api/v3/klines?symbol={sym}&interval=1h&startTime={cur}&limit=1000")
        if not data:
            break
        for k in data:
            lines.append(f"{k[0]},{k[1]},{k[2]},{k[3]},{k[4]},{k[7]}")
        nxt = data[-1][0] + 3600_000
        if nxt <= cur or len(data) < 1000:
            break
        cur = nxt
        time.sleep(0.05)
    out.write_text("open_time_ms,open,high,low,close,quote_volume\n" + "\n".join(lines), encoding="utf-8")
    return sym, len(lines)


with ThreadPoolExecutor(max_workers=6) as pool:
    done = list(pool.map(fetch, symbols))
short = [d for d in done if d[1] < DAYS * 24 * 0.9]
print("downloaded:", len(done), "| bars median:", sorted(n for _, n in done)[len(done) // 2], "| listed for less than the full window:", len(short))
(OUT / "_symbols.json").write_text(json.dumps({"symbols": symbols, "bars": dict(done), "days": DAYS}), encoding="utf-8")
