from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from core.data_model.pit import (
    PointInTime, UNAVAILABLE, to_utc, assert_utc, as_of,
)

UTC = timezone.utc


def test_to_utc_naive_assumed_utc():
    dt = datetime(2026, 8, 11, 12, 0, 0)  # naive
    assert to_utc(dt).tzinfo is UTC
    assert to_utc(dt).hour == 12


def test_to_utc_aware_converted():
    from zoneinfo import ZoneInfo
    dt = datetime(2026, 8, 11, 9, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    u = to_utc(dt)
    assert u.tzinfo is UTC
    assert u.hour == 13  # 09:00 ET -> 13:00 UTC (EDT)


def test_assert_utc_rejects_naive():
    try:
        assert_utc(datetime(2026, 8, 11, 12, 0, 0))
        assert False, "naive datetime must raise"
    except ValueError:
        pass


def test_point_in_time_rejects_naive_fields():
    try:
        PointInTime(event_time=datetime(2026, 8, 11, 12, 0, 0))  # naive
        assert False, "naive event_time must raise"
    except ValueError:
        pass


def test_point_in_time_defaults():
    p = PointInTime(fetched_at=datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC))
    assert p.event_time is None
    assert p.version == 0


@dataclass(frozen=True)
class _Rec:
    name: str
    pit: PointInTime


def _rec(name, available_from, version=0):
    return _Rec(name, PointInTime(available_from=available_from, fetched_at=available_from, version=version))


def test_as_of_returns_newest_qualifying():
    a = _rec("a", datetime(2026, 1, 1, tzinfo=UTC))
    b = _rec("b", datetime(2026, 3, 1, tzinfo=UTC))
    c = _rec("c", datetime(2026, 2, 1, tzinfo=UTC))
    got = as_of([a, b, c], datetime(2026, 2, 15, tzinfo=UTC))
    assert got is c  # newest with available_from <= Feb 15


def test_as_of_returns_unavailable_when_none_qualify():
    a = _rec("a", datetime(2026, 3, 1, tzinfo=UTC))
    got = as_of([a], datetime(2026, 1, 1, tzinfo=UTC))
    assert got is UNAVAILABLE
    assert not bool(UNAVAILABLE)


def test_as_of_tiebreak_by_version():
    a = _rec("a", datetime(2026, 1, 1, tzinfo=UTC), version=1)
    b = _rec("b", datetime(2026, 1, 1, tzinfo=UTC), version=3)
    got = as_of([a, b], datetime(2026, 6, 1, tzinfo=UTC))
    assert got is b  # higher version wins on tie


def test_as_of_skips_records_without_pit_or_available_from():
    @dataclass(frozen=True)
    class _NoPit:
        name: str
    a = _rec("a", datetime(2026, 1, 1, tzinfo=UTC))
    got = as_of([_NoPit("x"), a], datetime(2026, 6, 1, tzinfo=UTC))
    assert got is a