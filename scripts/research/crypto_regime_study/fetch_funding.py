"""Download funding-rate history from Binance USD-M futures (public API, no key).

usage: fetch_funding.py <klines dir with _symbols.json> <outdir> [days=730]
Writes one CSV per spot symbol that has a perpetual: funding_time_ms,funding_rate
and _funding.json with the perp symbol used and the funding interval in hours.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

KL = Path(sys.argv[1])
OUT = Path(sys.argv[2])
DAYS = int(sys.argv[3]) if len(sys.argv) > 3 else 730
OUT.mkdir(parents=True, exist_ok=True)
BASE = "https://fapi.binance.com"


def get(path: str):
    for attempt in range(5):
        try:
            req = urllib.request.Request(BASE + path, headers={"User-Agent": "market-research-script"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 404):
                return None
            if attempt == 4:
                raise
            time.sleep(2.0 * (attempt + 1))
        except Exception:  # noqa: BLE001
            if attempt == 4:
                raise
            time.sleep(2.0 * (attempt + 1))


info = get("/fapi/v1/exchangeInfo")
perps = {s["symbol"] for s in info["symbols"] if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"}
print("USDT perpetuals listed:", len(perps))
spot = json.loads((KL / "_symbols.json").read_text())["symbols"]
mapping = {}
for sym in spot:
    for cand in (sym, "1000" + sym, "1000000" + sym):
        if cand in perps:
            mapping[sym] = cand
            break
print("spot pairs with a perpetual:", len(mapping), "of", len(spot))

END = int(time.time() * 1000)
START = END - DAYS * 86400 * 1000


def fetch(item):
    sym, perp = item
    cur, rows = START, []
    while cur < END:
        data = get(f"/fapi/v1/fundingRate?symbol={perp}&startTime={cur}&limit=1000")
        if not data:
            break
        rows.extend((int(d["fundingTime"]), float(d["fundingRate"])) for d in data)
        nxt = int(data[-1]["fundingTime"]) + 1
        if nxt <= cur or len(data) < 1000:
            break
        cur = nxt
        time.sleep(0.25)
    rows = sorted(set(rows))
    (OUT / f"{sym}.csv").write_text("funding_time_ms,funding_rate\n" + "\n".join(f"{t},{r}" for t, r in rows), encoding="utf-8")
    gaps = sorted(b[0] - a[0] for a, b in zip(rows, rows[1:]))
    interval_h = round(gaps[len(gaps) // 2] / 3_600_000) if gaps else 8
    return sym, perp, len(rows), interval_h


with ThreadPoolExecutor(max_workers=3) as pool:
    done = list(pool.map(fetch, mapping.items()))
meta = {sym: {"perp": perp, "rows": n, "interval_h": ih} for sym, perp, n, ih in done}
(OUT / "_funding.json").write_text(json.dumps(meta), encoding="utf-8")
ns = sorted(m["rows"] for m in meta.values())
print("downloaded:", len(meta), "| settlements median:", ns[len(ns) // 2], "| funding interval hours:",
      {h: sum(1 for m in meta.values() if m["interval_h"] == h) for h in sorted({m["interval_h"] for m in meta.values()})})
