"""Record Binance USD-M positioning data before it expires.

Binance publishes open interest, three long/short ratios and taker buy/sell
volume per perpetual, hourly, but only for the last 30 days. A rule built on
positioning can therefore never be tested on history unless that history is
saved while it is still available. This module saves it:

    python -m core.crypto_derivatives_recorder            # one sync
    python -m core.crypto_derivatives_recorder --status   # what is stored

Each sync backfills whatever the 30-day window still holds and then adds only
the new hours, so running it once a day loses nothing and running it more often
is harmless (rows are keyed by symbol, series and hour).

Stored in ``<data>/research/crypto_derivatives.db``:

``series_1h(symbol, series, ts, a, b, c)`` — ``ts`` is the hour in UTC ms.
    open_interest          a = open interest in contracts, b = its value in USD
    ratio_global_account   a = long/short ratio, b = long share, c = short share (all accounts)
    ratio_top_account      same, top traders by account
    ratio_top_position     same, top traders by position
    taker                  a = buy/sell ratio, b = taker buy volume, c = taker sell volume
``mark_snapshots(symbol, ts, mark_price, index_price, last_funding_rate)`` — one row per sync.
``symbols(symbol, first_seen, last_seen)`` and ``runs(...)``.

Liquidations are not here: Binance removed their public history, they exist
only as a live stream.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

BASE_URL = "https://fapi.binance.com"
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
WINDOW_DAYS = 30
# Stay a little inside the window: a start exactly 30 days old is refused.
_WINDOW_MARGIN_MS = 2 * HOUR_MS
_PAGE_ROWS = 500
DEFAULT_MIN_QUOTE_VOLUME = 5_000_000.0
DEFAULT_PACE_SECONDS = 0.3

# series name -> (endpoint, keys of the three stored values)
SERIES: Dict[str, Tuple[str, Tuple[Optional[str], Optional[str], Optional[str]]]] = {
    "open_interest": ("/futures/data/openInterestHist", ("sumOpenInterest", "sumOpenInterestValue", None)),
    "ratio_global_account": ("/futures/data/globalLongShortAccountRatio", ("longShortRatio", "longAccount", "shortAccount")),
    "ratio_top_account": ("/futures/data/topLongShortAccountRatio", ("longShortRatio", "longAccount", "shortAccount")),
    "ratio_top_position": ("/futures/data/topLongShortPositionRatio", ("longShortRatio", "longAccount", "shortAccount")),
    "taker": ("/futures/data/takerlongshortRatio", ("buySellRatio", "buyVol", "sellVol")),
}

Fetch = Callable[[str], Tuple[int, Any]]


class RateLimited(RuntimeError):
    """Binance answered 429/418: stop now, an IP that keeps asking gets banned."""


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _http_fetch(path: str) -> Tuple[int, Any]:
    """GET one public endpoint. Network trouble comes back as status 0, never as an exception."""
    last: Tuple[int, Any] = (0, "not attempted")
    for attempt in range(3):
        request = urllib.request.Request(BASE_URL + path, headers={"User-Agent": "market-research-script"})
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            try:
                last = (exc.code, json.loads(body))
            except ValueError:
                last = (exc.code, body)
            if exc.code < 500:
                return last
        except Exception as exc:  # noqa: BLE001 - DNS, timeout, reset: retried below
            last = (0, f"{exc.__class__.__name__}: {exc}")
        time.sleep(1.5 * (attempt + 1))
    return last


def default_db_path() -> Path:
    from core.config import data_path

    return data_path("research", "crypto_derivatives.db")


class DerivativesRecorder:
    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        fetch: Optional[Fetch] = None,
        now_ms: Optional[Callable[[], int]] = None,
        pace_seconds: float = DEFAULT_PACE_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.db_path = Path(db_path) if db_path else default_db_path()
        self._fetch = fetch or _http_fetch
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._pace = max(0.0, float(pace_seconds))
        self._sleep = sleep
        self._statuses: List[int] = []
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS series_1h (
                    symbol TEXT NOT NULL, series TEXT NOT NULL, ts INTEGER NOT NULL,
                    a REAL, b REAL, c REAL,
                    PRIMARY KEY (symbol, series, ts)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS mark_snapshots (
                    symbol TEXT NOT NULL, ts INTEGER NOT NULL,
                    mark_price REAL, index_price REAL, last_funding_rate REAL,
                    PRIMARY KEY (symbol, ts)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS symbols (
                    symbol TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
                    symbols INTEGER NOT NULL, rows_added INTEGER NOT NULL,
                    error_count INTEGER NOT NULL, note TEXT NOT NULL DEFAULT ''
                );
                """
            )

    # ------------------------------------------------------------------ plumbing
    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _get(self, path: str) -> Tuple[int, Any]:
        status, data = self._fetch(path)
        self._statuses.append(int(status))
        if status in (429, 418):
            raise RateLimited(f"Binance answered {status} to {path.split('?')[0]}")
        if self._pace:
            self._sleep(self._pace)
        return status, data

    @staticmethod
    def _iso(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()

    # ------------------------------------------------------------------ universe
    def select_universe(self, min_quote_volume: float = DEFAULT_MIN_QUOTE_VOLUME,
                        max_symbols: Optional[int] = None) -> List[str]:
        """Liquid USDT perpetuals, plus every symbol already recorded that still trades.

        A symbol stays in once it is recorded: dropping it on a quiet day would
        leave a hole in its series that can never be filled.
        """
        status, info = self._get("/fapi/v1/exchangeInfo")
        if status != 200 or not isinstance(info, dict):
            raise RuntimeError(f"exchangeInfo unavailable (status {status})")
        trading = {
            str(item.get("symbol")) for item in info.get("symbols") or []
            if item.get("contractType") == "PERPETUAL" and item.get("quoteAsset") == "USDT"
            and item.get("status") == "TRADING"
        }
        status, tickers = self._get("/fapi/v1/ticker/24hr")
        if status != 200 or not isinstance(tickers, list):
            raise RuntimeError(f"24h tickers unavailable (status {status})")
        volume = {str(t.get("symbol")): _number(t.get("quoteVolume")) or 0.0 for t in tickers}
        liquid = sorted((s for s in trading if volume.get(s, 0.0) >= float(min_quote_volume)),
                        key=lambda s: -volume.get(s, 0.0))
        if max_symbols:
            liquid = liquid[: int(max_symbols)]
        with self._connect() as db:
            known = {row[0] for row in db.execute("SELECT symbol FROM symbols")}
        return sorted(set(liquid) | (known & trading))

    # ---------------------------------------------------------------------- sync
    def sync(self, symbols: Optional[Sequence[str]] = None, *,
             min_quote_volume: float = DEFAULT_MIN_QUOTE_VOLUME,
             max_symbols: Optional[int] = None) -> Dict[str, Any]:
        self._statuses = []
        started = self._now_ms()
        chosen = list(symbols) if symbols is not None else self.select_universe(min_quote_volume, max_symbols)
        errors: List[str] = []
        rows_added = 0
        note = ""
        db = self._connect()
        try:
            stamp = self._iso(started)
            db.executemany(
                "INSERT INTO symbols(symbol, first_seen, last_seen) VALUES (?, ?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET last_seen = excluded.last_seen",
                [(symbol, stamp, stamp) for symbol in chosen],
            )
            db.commit()
            try:
                self._snapshot_marks(db, set(chosen), errors)
                for symbol in chosen:
                    for series in SERIES:
                        rows_added += self._sync_series(db, symbol, series, errors)
                    db.commit()
            except RateLimited as exc:
                note = str(exc)
                raise
            finally:
                db.commit()
                db.execute(
                    "INSERT INTO runs(started_at, finished_at, symbols, rows_added, error_count, note) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (self._iso(started), self._iso(self._now_ms()), len(chosen), rows_added, len(errors), note),
                )
                db.commit()
        finally:
            db.close()
        return {
            "started_at": self._iso(started), "finished_at": self._iso(self._now_ms()),
            "symbols": len(chosen), "rows_added": rows_added, "errors": errors,
            "requests": len(self._statuses), "http_statuses": sorted(set(self._statuses)),
        }

    def _snapshot_marks(self, db: sqlite3.Connection, symbols: set, errors: List[str]) -> None:
        status, data = self._get("/fapi/v1/premiumIndex")
        if status != 200 or not isinstance(data, list):
            errors.append(f"premiumIndex: status {status}")
            return
        rows = [
            (str(item.get("symbol")), int(item.get("time") or self._now_ms()), _number(item.get("markPrice")),
             _number(item.get("indexPrice")), _number(item.get("lastFundingRate")))
            for item in data if str(item.get("symbol")) in symbols
        ]
        db.executemany("INSERT OR IGNORE INTO mark_snapshots VALUES (?, ?, ?, ?, ?)", rows)

    def _sync_series(self, db: sqlite3.Connection, symbol: str, series: str, errors: List[str]) -> int:
        endpoint, keys = SERIES[series]
        now = self._now_ms()
        window_start = now - WINDOW_DAYS * DAY_MS + _WINDOW_MARGIN_MS
        last = db.execute(
            "SELECT MAX(ts) FROM series_1h WHERE symbol = ? AND series = ?", (symbol, series),
        ).fetchone()[0]
        start = max(window_start, int(last) + 1) if last is not None else window_start
        added = 0
        while start <= now:
            end = min(start + (_PAGE_ROWS - 1) * HOUR_MS, now)
            status, data = self._get(
                f"{endpoint}?symbol={quote(symbol)}&period=1h&limit={_PAGE_ROWS}&startTime={start}&endTime={end}"
            )
            if status != 200 or not isinstance(data, list):
                errors.append(f"{symbol} {series}: status {status} {str(data)[:120]}")
                break
            rows = []
            for item in data:
                ts = item.get("timestamp") if isinstance(item, dict) else None
                if ts is None:
                    continue
                values = [_number(item.get(key)) if key else None for key in keys]
                rows.append((symbol, series, int(ts), *values))
            before = db.total_changes
            db.executemany("INSERT OR IGNORE INTO series_1h VALUES (?, ?, ?, ?, ?, ?)", rows)
            added += db.total_changes - before
            start = end + 1
        return added

    # -------------------------------------------------------------------- status
    def status(self) -> Dict[str, Any]:
        with self._connect() as db:
            per_series = {
                series: {"rows": rows, "symbols": symbols,
                         "from": self._iso(first) if first else None, "to": self._iso(last) if last else None}
                for series, rows, symbols, first, last in db.execute(
                    "SELECT series, COUNT(*), COUNT(DISTINCT symbol), MIN(ts), MAX(ts) FROM series_1h GROUP BY series"
                )
            }
            run = db.execute(
                "SELECT started_at, finished_at, symbols, rows_added, error_count, note FROM runs "
                "ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
            runs = db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        return {
            "db": str(self.db_path), "series": per_series, "runs": runs,
            "last_run": dict(zip(("started_at", "finished_at", "symbols", "rows_added", "error_count", "note"), run))
            if run else None,
        }


def _log_line(text: str) -> None:
    try:
        from core.config import LOG_DIR

        path = Path(LOG_DIR) / "crypto_derivatives_recorder.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(timezone.utc).isoformat()} {text}\n")
    except Exception:  # noqa: BLE001 - logging must never fail the sync
        pass


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Record Binance USD-M positioning data (30-day window).")
    parser.add_argument("--min-volume", type=float, default=DEFAULT_MIN_QUOTE_VOLUME,
                        help="24h quote volume a perpetual needs to enter the universe")
    parser.add_argument("--max-symbols", type=int, default=None)
    parser.add_argument("--status", action="store_true", help="print what is stored and exit")
    args = parser.parse_args(list(argv) if argv is not None else None)
    recorder = DerivativesRecorder()
    if args.status:
        print(json.dumps(recorder.status(), indent=1))
        return 0
    try:
        summary = recorder.sync(min_quote_volume=args.min_volume, max_symbols=args.max_symbols)
    except RateLimited as exc:
        _log_line(f"stopped: {exc}")
        print(json.dumps({"stopped": str(exc)}))
        return 2
    except Exception as exc:  # noqa: BLE001 - report and exit non-zero for the scheduler
        _log_line(f"failed: {exc.__class__.__name__}: {exc}")
        print(json.dumps({"failed": f"{exc.__class__.__name__}: {exc}"}))
        return 1
    _log_line(json.dumps({k: summary[k] for k in ("symbols", "rows_added", "requests")} | {"errors": len(summary["errors"])}))
    print(json.dumps(summary | {"errors": summary["errors"][:10]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
