"""Bounded, multi-resolution storage for the Capital equity curve.

Pure functions over the stored list of equity snapshots. The curve used to be
"the newest 500 points": once the read model started recording a point every
minute that window shrank to a few hours and the months of older history were
pushed out. Retention is now tiered by age:

* every recorded point (one per UTC minute) for the last 2 hours,
* one point per hour for the last 7 days,
* one point per day beyond that, newest ``MAX_DAILY_POINTS`` days.

A coarse point is the LAST snapshot of its bucket and carries ``high_equity`` /
``low_equity`` / ``samples`` for everything folded into it, so the equity peak
used by the drawdown constraint is never lowered by compaction. Each point is
tagged with the ``resolution`` it was kept at so the UI can label the stretch.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

RESOLUTION_FINE = "1m"
RESOLUTION_HOURLY = "hourly"
RESOLUTION_DAILY = "daily"

FINE_WINDOW = timedelta(hours=2)
HOURLY_WINDOW = timedelta(days=7)
MAX_DAILY_POINTS = 200
MAX_EQUITY_POINTS = 500
# Two consecutive fine points closer than this are the same equity re-recorded.
NOISE_THRESHOLD_USD = 0.01

_EQUITY_FIELDS = ("total_equity", "equity_usd", "equity", "balance")


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _point_time(entry: Mapping[str, Any]) -> Optional[datetime]:
    for field in ("date", "calendar_date"):
        raw = str(entry.get(field) or "").strip()
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _point_equity(entry: Mapping[str, Any]) -> Optional[float]:
    for field in _EQUITY_FIELDS:
        value = _finite(entry.get(field))
        if value is not None:
            return value
    return None


def _samples(entry: Mapping[str, Any]) -> int:
    try:
        return max(1, int(entry.get("samples") or 1))
    except (TypeError, ValueError):
        return 1


def _tier(age: timedelta) -> str:
    if age <= FINE_WINDOW:
        return RESOLUTION_FINE
    if age <= HOURLY_WINDOW:
        return RESOLUTION_HOURLY
    return RESOLUTION_DAILY


def _bucket_key(resolution: str, moment: datetime) -> str:
    if resolution == RESOLUTION_FINE:
        return moment.strftime("%Y-%m-%dT%H:%M")
    if resolution == RESOLUTION_HOURLY:
        return moment.strftime("%Y-%m-%dT%H")
    return moment.strftime("%Y-%m-%d")


def _fold(members: List[Tuple[datetime, Dict[str, Any]]], resolution: str) -> Dict[str, Any]:
    """One stored point for a bucket: its last snapshot plus the folded range."""
    point = dict(members[-1][1])
    highs: List[float] = []
    lows: List[float] = []
    samples = 0
    for _moment, entry in members:
        equity = _point_equity(entry)
        for candidate in (equity, _finite(entry.get("high_equity"))):
            if candidate is not None:
                highs.append(candidate)
        for candidate in (equity, _finite(entry.get("low_equity"))):
            if candidate is not None:
                lows.append(candidate)
        samples += _samples(entry)
    point["resolution"] = resolution
    if highs:
        point["high_equity"] = round(max(highs), 2)
    if lows:
        point["low_equity"] = round(min(lows), 2)
    point["samples"] = samples
    return point


def _collapse_flat_runs(
    points: List[Tuple[datetime, Dict[str, Any]]],
) -> List[Tuple[datetime, Dict[str, Any]]]:
    """Drop the middle of a flat run of fine points inside one hour bucket.

    The first and last point of a run stay, so a flat stretch is still drawn as
    flat and the next real move starts at the right time. Points in different
    hour buckets are never merged, and a run is measured against its first
    point so a slow drift is not flattened away.
    """
    kept: List[Tuple[datetime, Dict[str, Any]]] = []
    run_start = 0
    count = len(points)
    while run_start < count:
        anchor_time, anchor = points[run_start]
        anchor_equity = _point_equity(anchor)
        anchor_bucket = _bucket_key(RESOLUTION_HOURLY, anchor_time)
        run_end = run_start
        while run_end + 1 < count:
            next_time, next_point = points[run_end + 1]
            next_equity = _point_equity(next_point)
            if (
                anchor_equity is None
                or next_equity is None
                or _bucket_key(RESOLUTION_HOURLY, next_time) != anchor_bucket
                or abs(next_equity - anchor_equity) >= NOISE_THRESHOLD_USD
            ):
                break
            run_end += 1
        kept.append(points[run_start])
        if run_end > run_start:
            last_time, last_point = points[run_end]
            dropped = points[run_start + 1:run_end]
            if dropped:
                last_point = dict(last_point)
                last_point["samples"] = _samples(last_point) + sum(
                    _samples(entry) for _moment, entry in dropped
                )
            kept.append((last_time, last_point))
        run_start = run_end + 1
    return kept


def compact_equity_history(
    history: Iterable[Any],
    *,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Return ``history`` at the tiered resolution, oldest first. Idempotent.

    Legacy lists (plain snapshots without ``resolution``) are accepted as they
    are, so applying this to the stored list is the in-place migration. Entries
    that cannot be placed on the time axis are dropped.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)

    dated: List[Tuple[datetime, int, Dict[str, Any]]] = []
    for index, entry in enumerate(history or []):
        if not isinstance(entry, Mapping):
            continue
        moment = _point_time(entry)
        if moment is None or _point_equity(entry) is None:
            continue
        dated.append((moment, index, dict(entry)))
    dated.sort(key=lambda item: (item[0], item[1]))

    buckets: Dict[Tuple[str, str], List[Tuple[datetime, Dict[str, Any]]]] = {}
    order: List[Tuple[str, str]] = []
    for moment, _index, entry in dated:
        resolution = _tier(current - moment)
        key = (resolution, _bucket_key(resolution, moment))
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append((moment, entry))

    by_resolution: Dict[str, List[Tuple[datetime, Dict[str, Any]]]] = {
        RESOLUTION_DAILY: [],
        RESOLUTION_HOURLY: [],
        RESOLUTION_FINE: [],
    }
    for key in order:
        members = buckets[key]
        by_resolution[key[0]].append((members[-1][0], _fold(members, key[0])))

    daily = by_resolution[RESOLUTION_DAILY][-MAX_DAILY_POINTS:]
    fine = _collapse_flat_runs(by_resolution[RESOLUTION_FINE])
    combined = daily + by_resolution[RESOLUTION_HOURLY] + fine
    combined.sort(key=lambda item: item[0])
    if len(combined) > MAX_EQUITY_POINTS:
        combined = combined[-MAX_EQUITY_POINTS:]
    return [point for _moment, point in combined]


def needs_migration(history: Iterable[Any]) -> bool:
    """True when the stored list still holds snapshots without a resolution tag."""
    return any(
        isinstance(entry, Mapping) and not entry.get("resolution")
        for entry in history or []
    )


def describe_equity_curve(points: Iterable[Any]) -> Dict[str, Any]:
    """Which stretch of the curve is stored at which resolution."""
    segments: List[Dict[str, Any]] = []
    total = 0
    for point in points or []:
        if not isinstance(point, Mapping):
            continue
        total += 1
        resolution = str(point.get("resolution") or "unknown")
        date = point.get("date") or ""
        if segments and segments[-1]["resolution"] == resolution:
            segments[-1]["to"] = date
            segments[-1]["samples"] += 1
        else:
            segments.append({"resolution": resolution, "from": date, "to": date, "samples": 1})
    return {
        "segments": segments,
        "points": total,
        "span_start": segments[0]["from"] if segments else None,
        "span_end": segments[-1]["to"] if segments else None,
        "noise_threshold_usd": NOISE_THRESHOLD_USD,
        "retention": {
            RESOLUTION_FINE: "every recorded point (one per minute) for the last 2 hours",
            RESOLUTION_HOURLY: "last point of each hour for the last 7 days",
            RESOLUTION_DAILY: f"last point of each day before that, newest {MAX_DAILY_POINTS} days",
        },
        "point_value": "last snapshot of the bucket; high_equity / low_equity give the folded range",
    }


__all__ = [
    "MAX_DAILY_POINTS",
    "MAX_EQUITY_POINTS",
    "NOISE_THRESHOLD_USD",
    "RESOLUTION_DAILY",
    "RESOLUTION_FINE",
    "RESOLUTION_HOURLY",
    "compact_equity_history",
    "describe_equity_curve",
    "needs_migration",
]
