"""Free, point-in-time analyst EPS estimate revisions (Yahoo eps_trend / eps_revisions).

Estimate revisions before an earnings report are a documented source of
information. Yahoo exposes the current-quarter consensus today and 7/30/90
days ago, plus counts of upward/downward revisions. Captured at registration,
before the event, the snapshot is point-in-time by construction, so the
evidence lab can test it honestly as events settle. Nothing is backfilled.
"""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Optional

SOURCE = "yahoo_eps_trend_revisions"
FIELDS = (
    "predicted_eps_trend_change_7d_pct",
    "predicted_eps_trend_change_30d_pct",
    "predicted_eps_trend_change_90d_pct",
    "predicted_eps_revisions_up_30d",
    "predicted_eps_revisions_down_30d",
    "predicted_eps_net_revision_ratio_30d",
    "predicted_eps_analysts",
    "predicted_eps_revision_captured_utc",
)
REVISION_THRESHOLD_PCT = 0.5


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _row(frame: Any, period: str = "0q") -> Dict[str, Any]:
    try:
        if frame is None or getattr(frame, "empty", True) or period not in frame.index:
            return {}
        return {str(k): v for k, v in frame.loc[period].items()}
    except Exception:
        return {}


def _pct_change(current: Optional[float], past: Optional[float]) -> Optional[float]:
    if current is None or past is None or past == 0.0:
        return None
    return round((current - past) / abs(past) * 100.0, 4)


def snapshot_from_frames(eps_trend: Any, eps_revisions: Any, earnings_estimate: Any,
                         *, captured_at: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Build the stored snapshot from Yahoo frames. None when nothing usable."""
    trend = _row(eps_trend)
    revisions = _row(eps_revisions)
    estimate = _row(earnings_estimate)
    current = _finite(trend.get("current"))
    up = _finite(revisions.get("upLast30days"))
    down = _finite(revisions.get("downLast30days"))
    analysts = _finite(estimate.get("numberOfAnalysts"))
    snapshot = {
        "predicted_eps_trend_change_7d_pct": _pct_change(current, _finite(trend.get("7daysAgo"))),
        "predicted_eps_trend_change_30d_pct": _pct_change(current, _finite(trend.get("30daysAgo"))),
        "predicted_eps_trend_change_90d_pct": _pct_change(current, _finite(trend.get("90daysAgo"))),
        "predicted_eps_revisions_up_30d": int(up) if up is not None else None,
        "predicted_eps_revisions_down_30d": int(down) if down is not None else None,
        "predicted_eps_net_revision_ratio_30d": (
            round((up - down) / max(1.0, analysts if analysts else up + down), 4)
            if up is not None and down is not None else None
        ),
        "predicted_eps_analysts": int(analysts) if analysts is not None else None,
    }
    if all(value is None for value in snapshot.values()):
        return None
    snapshot["predicted_eps_revision_captured_utc"] = (captured_at or datetime.now(timezone.utc)).isoformat()
    return snapshot


def fetch_snapshot(symbol: str, *, ticker_factory: Optional[Callable[[str], Any]] = None,
                   now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """One network read. Any provider failure returns None (missing stays missing)."""
    symbol = str(symbol or "").upper().strip()
    if not symbol:
        return None
    try:
        if ticker_factory is None:
            import yfinance as yf

            ticker_factory = yf.Ticker
        ticker = ticker_factory(symbol)
        return snapshot_from_frames(
            getattr(ticker, "eps_trend", None),
            getattr(ticker, "eps_revisions", None),
            getattr(ticker, "earnings_estimate", None),
            captured_at=now,
        )
    except Exception:
        return None


class RevisionSnapshotCache:
    """Per-symbol snapshots with capture time; misses are remembered briefly."""

    def __init__(self, *, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: Dict[str, Dict[str, Any]] = {}

    def get(self, symbol: str, max_age_seconds: float) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._entries.get(str(symbol).upper())
        if not entry or self._clock() - entry["ts"] > max_age_seconds:
            return None
        return dict(entry["snapshot"]) if entry["snapshot"] else None

    def is_fresh(self, symbol: str, max_age_seconds: float) -> bool:
        with self._lock:
            entry = self._entries.get(str(symbol).upper())
        return bool(entry) and self._clock() - entry["ts"] <= max_age_seconds

    def put(self, symbol: str, snapshot: Optional[Dict[str, Any]]) -> None:
        with self._lock:
            self._entries[str(symbol).upper()] = {"ts": self._clock(), "snapshot": snapshot}


def refresh_snapshots(symbols: Iterable[str], cache: RevisionSnapshotCache, *, max_fetch: int,
                      max_age_seconds: float,
                      fetch: Callable[[str], Optional[Dict[str, Any]]] = fetch_snapshot) -> int:
    """Fetch stale symbols in the given priority order, at most ``max_fetch``."""
    fetched = 0
    for symbol in dict.fromkeys(str(s).upper().strip() for s in symbols if str(s).strip()):
        if fetched >= max(0, int(max_fetch)):
            break
        if cache.is_fresh(symbol, max_age_seconds):
            continue
        cache.put(symbol, fetch(symbol))
        fetched += 1
    return fetched


def revision_direction(record: Dict[str, Any], threshold_pct: float = REVISION_THRESHOLD_PCT) -> Optional[int]:
    """1 = estimates revised up, 0 = revised down, None = flat or unknown."""
    change = _finite(record.get("predicted_eps_trend_change_30d_pct"))
    if change is None or abs(change) < threshold_pct:
        return None
    return 1 if change > 0 else 0
