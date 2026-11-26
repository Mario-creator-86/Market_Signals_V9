import argparse
import sys
from typing import Any, Dict, List
from urllib.parse import quote

import requests


def _extract_tickers(raw_watchlist: Any) -> List[str]:
    tickers: List[str] = []

    if isinstance(raw_watchlist, dict):
        for key in ("order", "yahoo", "binance", "stocks", "crypto", "cryptos"):
            values = raw_watchlist.get(key, [])
            if isinstance(values, list):
                for item in values:
                    t = str(item or "").upper().strip()
                    if t:
                        tickers.append(t)
    elif isinstance(raw_watchlist, list):
        for item in raw_watchlist:
            if isinstance(item, dict):
                t = str(item.get("ticker") or item.get("symbol") or "").upper().strip()
            else:
                t = str(item or "").upper().strip()
            if t:
                tickers.append(t)

    seen = set()
    out: List[str] = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _get_json(url: str, timeout: int = 20) -> Any:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _post_json(url: str, payload: Dict[str, Any], timeout: int = 60) -> Any:
    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check News Tab ticker coverage from local API."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api")
    parser.add_argument("--max-items", type=int, default=5, help="Max items per ticker request.")
    parser.add_argument("--limit", type=int, default=10, help="How many watchlist tickers to check.")
    parser.add_argument(
        "--warm-history-days",
        type=int,
        default=0,
        help="If >0, calls /news-tab/history per ticker before checking.",
    )
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    try:
        raw_watchlist = _get_json(f"{base}/watchlist")
    except Exception as e:
        print(f"ERROR: failed to fetch watchlist: {e}")
        return 2

    tickers = _extract_tickers(raw_watchlist)
    if not tickers:
        print("ERROR: no tickers found in /api/watchlist response.")
        return 2

    selected = tickers[: max(1, int(args.limit))]
    print(f"Checking {len(selected)} tickers (from {len(tickers)} in watchlist)")
    print("-" * 72)

    if int(args.warm_history_days) > 0:
        days = int(args.warm_history_days)
        for t in selected:
            try:
                result = _post_json(f"{base}/news-tab/history", {"ticker": t, "days": days}, timeout=90)
                status = str(result.get("status", ""))
                found = int(result.get("articles_found", 0) or 0)
                added = int(result.get("articles_added", 0) or 0)
                print(f"[warm] {t:<10} status={status:<8} found={found:<3} added={added:<3}")
            except Exception as e:
                print(f"[warm] {t:<10} ERROR: {e}")
        print("-" * 72)

    ok = 0
    zero = 0
    failed = 0

    for t in selected:
        try:
            url = f"{base}/news-tab/ticker/{quote(t)}?max_items={max(1, int(args.max_items))}"
            payload = _get_json(url)
            items = payload.get("news", []) if isinstance(payload, dict) else []
            count = len(items) if isinstance(items, list) else 0
            if count > 0:
                ok += 1
                first = items[0] if isinstance(items[0], dict) else {}
                title = str(first.get("title", "")).strip()
                when = str(first.get("published_at_formatted", "")).strip()
                print(f"[OK]   {t:<10} count={count:<3} latest={when or '-'} | {title[:90]}")
            else:
                zero += 1
                print(f"[ZERO] {t:<10} count=0")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {t:<10} {e}")

    print("-" * 72)
    print(f"summary: ok={ok} zero={zero} fail={failed} total={len(selected)}")

    if failed > 0:
        return 2
    if ok == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

