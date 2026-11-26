"""Futures positioning of a coin, for the AI reviewer.

Reads the database kept by ``core.crypto_derivatives_recorder`` (Binance USD-M
open interest, long/short account ratios, taker buy/sell volume, hourly) and
turns the last 30 days of one perpetual into a few numbers a reviewer can read:
where open interest is going, how crowded the long side is compared with the
recent past, and who was hitting the book in the last hours.

Context only. No rule built on positioning has passed this project's tests yet
(Binance keeps just 30 days of history, which is why the recorder exists), and
the note in the result says so. The lookup is local and read-only: no network.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HOUR_MS = 3_600_000
STALE_AFTER_HOURS = 8.0
NOTE = ("Context only: no rule built on futures positioning has been validated in this system yet. "
        "Percentiles compare the latest reading with this coin's own last 30 days.")


def _symbols(ticker: str) -> List[str]:
    text = str(ticker or "").upper().strip()
    if not text.endswith("-USD"):
        return []
    base = text[:-4].replace("-", "")
    return [f"{base}USDT", f"1000{base}USDT"] if base else []


def _series(db: sqlite3.Connection, symbol: str, name: str, since: int) -> List[Tuple[int, Any, Any, Any]]:
    return db.execute(
        "SELECT ts, a, b, c FROM series_1h WHERE symbol = ? AND series = ? AND ts >= ? ORDER BY ts",
        (symbol, name, since),
    ).fetchall()


def _at_or_before(rows: List[Tuple[int, Any, Any, Any]], ts: int) -> Optional[Tuple[int, Any, Any, Any]]:
    found = None
    for row in rows:
        if row[0] > ts:
            break
        found = row
    return found


def _change_pct(rows: List[Tuple[int, Any, Any, Any]], hours: int, column: int) -> Optional[float]:
    if not rows:
        return None
    past = _at_or_before(rows, rows[-1][0] - hours * HOUR_MS)
    if not past or not past[column] or rows[-1][column] is None:
        return None
    return round((rows[-1][column] / past[column] - 1) * 100, 2)


def _ratio_block(rows: List[Tuple[int, Any, Any, Any]]) -> Optional[Dict[str, Any]]:
    values = [row[1] for row in rows if row[1] is not None]
    if not values:
        return None
    latest = values[-1]
    return {
        "ratio": round(latest, 4),
        "long_share_pct": round(latest / (1 + latest) * 100, 1),
        "percentile_30d": round(sum(v <= latest for v in values) / len(values) * 100, 0),
        "change_24h_pct": _change_pct(rows, 24, 1),
    }


def positioning_evidence(ticker: str, *, db_path: Optional[Path] = None, now_ms: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Positioning summary for a crypto ticker ('ACE-USD'), or None when nothing is recorded for it."""
    symbols = _symbols(ticker)
    if not symbols:
        return None
    if db_path is None:
        from core.crypto_derivatives_recorder import default_db_path

        db_path = default_db_path()
    path = Path(db_path)
    if not path.exists():
        return None
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    since = now - 30 * 24 * HOUR_MS
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        for symbol in symbols:
            oi = _series(db, symbol, "open_interest", since)
            if not oi:
                continue
            accounts = _series(db, symbol, "ratio_global_account", since)
            top = _series(db, symbol, "ratio_top_position", since)
            taker = _series(db, symbol, "taker", since)
            latest_ts = oi[-1][0]

            def flow(hours: int) -> Optional[float]:
                window = [row for row in taker if row[0] > (taker[-1][0] if taker else 0) - hours * HOUR_MS]
                buy, sell = sum(row[2] or 0.0 for row in window), sum(row[3] or 0.0 for row in window)
                return round(buy / sell, 3) if sell > 0 else None

            age_hours = round((now - latest_ts) / HOUR_MS, 1)
            return {
                "symbol": symbol,
                "as_of": datetime.fromtimestamp(latest_ts / 1000, timezone.utc).isoformat(),
                "age_hours": age_hours,
                "stale": age_hours > STALE_AFTER_HOURS,
                "open_interest": {
                    "value_usd": round(oi[-1][2], 2) if oi[-1][2] is not None else None,
                    "change_24h_pct": _change_pct(oi, 24, 2),
                    "change_7d_pct": _change_pct(oi, 24 * 7, 2),
                },
                "long_short_accounts": _ratio_block(accounts),
                "top_traders_positions": _ratio_block(top),
                "taker_flow": {"buy_sell_ratio_4h": flow(4), "buy_sell_ratio_24h": flow(24)} if taker else None,
                "note": NOTE,
            }
    except sqlite3.Error:
        return None
    finally:
        db.close()
    return None


__all__ = ["positioning_evidence"]
