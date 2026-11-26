"""Point-in-time contract (spec §29) and the no-synthetic-historical-facts guard (§32).

At historical timestamp T the system may use only data available at T.
`as_of(records, T)` returns the newest record whose `available_from <= T`, or
the `UNAVAILABLE` sentinel when none qualifies — through this API it is
impossible to read a record that was not available at T (§32).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

UTC = timezone.utc


def to_utc(dt: datetime) -> datetime:
    """Normalize a datetime to UTC-aware. Naive datetimes are assumed UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def assert_utc(dt: datetime) -> datetime:
    """Raise ValueError if dt is tz-naive; return dt normalized to UTC."""
    if dt.tzinfo is None:
        raise ValueError(f"tz-naive datetime not allowed: {dt!r}")
    return dt.astimezone(UTC)


class _Unavailable:
    """Sentinel for §32: missing historical information must remain UNAVAILABLE."""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNAVAILABLE"

    def __bool__(self) -> bool:
        return False

    def __eq__(self, other) -> bool:
        return other is self

    def __hash__(self) -> int:
        return hash("UNAVAILABLE")


UNAVAILABLE = _Unavailable()


@dataclass(frozen=True)
class PointInTime:
    event_time: datetime | None = None        # when the event happened
    publication_time: datetime | None = None   # when the info was published
    provider_time: datetime | None = None      # provider's timestamp on the record
    received_at: datetime | None = None        # when V9 received it
    available_from: datetime | None = None     # when it became usable (>= publication_time)
    fetched_at: datetime | None = None         # when V9 fetched it (UTC-aware)
    version: int = 0                           # monotonic per (symbol, entity)

    def __post_init__(self):
        for f in (self.event_time, self.publication_time, self.provider_time,
                  self.received_at, self.available_from, self.fetched_at):
            if f is not None and f.tzinfo is None:
                raise ValueError(f"PointInTime field tz-naive: {f!r}")


def as_of(records: Iterable[Any], T: datetime, *, prefer: str = "available_from") -> Any:
    """Return the newest record with `pit.<prefer> <= T`, or UNAVAILABLE (§32).

    Records without a `pit` attribute, or whose `pit.<prefer>` is None, are
    treated as not-available-at-T. Ties on `prefer` are broken by `pit.version`.
    """
    T = assert_utc(T)
    best: Any = None
    best_ts: datetime | None = None
    for r in records:
        pit = getattr(r, "pit", None)
        if pit is None:
            continue
        ts = getattr(pit, prefer, None)
        if ts is None:
            continue
        ts = to_utc(ts)
        if ts > T:
            continue
        if best is None or ts > best_ts or (ts == best_ts and pit.version > best.pit.version):
            best = r
            best_ts = ts
    return best if best is not None else UNAVAILABLE