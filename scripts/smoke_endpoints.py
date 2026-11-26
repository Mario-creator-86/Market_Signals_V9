import argparse
import time
from datetime import date, timedelta
from typing import List, Tuple, Optional, Dict, Any

import requests


def _hit(method: str, url: str, timeout: float = 5.0, json_body: Optional[Dict[str, Any]] = None) -> Tuple[int, float]:
    start = time.perf_counter()
    resp = requests.request(method, url, timeout=timeout, json=json_body)
    elapsed = (time.perf_counter() - start) * 1000.0
    return resp.status_code, elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test Market Signals API endpoints.")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="Base URL")
    parser.add_argument("--timeout", type=float, default=5.0, help="Timeout per request (seconds)")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    timeout = float(args.timeout)

    today = date.today()
    start = today.isoformat()
    end = (today + timedelta(days=7)).isoformat()

    analyze_payload = {
        "ticker": "AAPL",
        "include_heavy": False,
        "disable_ollama": True,
        "force_refresh": False,
    }

    endpoints: List[Tuple[str, str, Optional[Dict[str, Any]], Optional[float]]] = [
        ("GET", f"{base}/api/health", None, None),
        ("GET", f"{base}/api/ready", None, None),
        ("GET", f"{base}/api/state?lean=true", None, None),
        ("GET", f"{base}/api/watchlist", None, None),
        ("GET", f"{base}/api/news-tab/state", None, None),
        ("POST", f"{base}/api/news-tab/refresh", None, None),
        ("GET", f"{base}/api/scanner/state", None, None),
        ("POST", f"{base}/api/analyze", analyze_payload, max(timeout, 25.0)),
        ("POST", f"{base}/api/analyze-ticker", analyze_payload, None),
        ("GET", f"{base}/api/calendar/state", None, None),
        ("GET", f"{base}/api/calendar/events?days=7", None, None),
        ("GET", f"{base}/api/calendar/today", None, None),
        ("GET", f"{base}/api/calendar/range?start={start}&end={end}", None, None),
    ]

    print(f"Smoke test against: {base}")
    ok = True
    for method, url, body, timeout_override in endpoints:
        try:
            status, ms = _hit(method, url, timeout=timeout_override or timeout, json_body=body)
            print(f"{method} {url} -> {status} ({ms:.0f} ms)")
            if status >= 400:
                ok = False
        except Exception as e:
            ok = False
            print(f"{method} {url} -> ERROR ({e})")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
