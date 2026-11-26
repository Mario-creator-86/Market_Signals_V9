"""
tests/unit/test_pre_earnings_stale_filter.py
=============================================
Regression for audit B4: past-date filtering must compare against the full
event DATETIME (not just the date) so a BMO event reported at 12:00 UTC no
longer lingers as "upcoming" later the same day.

Also pins:
- Undated items are KEPT (they surface a missing-data flag, not a stale event).
- A future date-CONFIRMED event is kept (confirmed ≠ reported; future earnings
  can carry confirmed=True for a locked date).
- An event already in the past is dropped regardless of confirmed state.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents.pre_earnings_agent import PreEarningsAgent


def _item(symbol, dt_iso=None, confirmed=False):
    return {"symbol": symbol, "earnings_time_utc": dt_iso, "confirmed": confirmed,
            "gap_risk": 50, "direction_bias_score": 0}


NOW = datetime(2026, 8, 1, 20, 0, tzinfo=timezone.utc)  # evening UTC


@pytest.mark.unit
def test_past_event_dropped_even_same_day():
    """BMO at 12:00 UTC, evaluated at 20:00 UTC same day → dropped (the B4 bug:
    previously the date-only filter kept it because date == today)."""
    bmo_today = (NOW.replace(hour=12, minute=0)).isoformat()
    items = [_item("AAPL", bmo_today, confirmed=True)]
    out = PreEarningsAgent._filter_stale_items(items, NOW)
    assert out == [], f"same-day past BMO should be dropped, got {out}"


@pytest.mark.unit
def test_future_event_kept():
    future = (NOW + timedelta(days=3)).isoformat()
    items = [_item("AAPL", future, confirmed=True)]
    out = PreEarningsAgent._filter_stale_items(items, NOW)
    assert len(out) == 1
    assert out[0]["symbol"] == "AAPL"


@pytest.mark.unit
def test_future_confirmed_event_kept():
    """confirmed=True on a FUTURE event means date-confirmed, not reported —
    must NOT be dropped."""
    future = (NOW + timedelta(days=2)).isoformat()
    items = [_item("MSFT", future, confirmed=True)]
    out = PreEarningsAgent._filter_stale_items(items, NOW)
    assert len(out) == 1


@pytest.mark.unit
def test_undated_item_kept():
    items = [_item("FOO", None)]
    out = PreEarningsAgent._filter_stale_items(items, NOW)
    assert len(out) == 1, "undated items surface a missing-data flag, keep them"


@pytest.mark.unit
def test_yesterday_event_dropped():
    past = (NOW - timedelta(days=1)).isoformat()
    items = [_item("OLD", past)]
    out = PreEarningsAgent._filter_stale_items(items, NOW)
    assert out == []


@pytest.mark.unit
def test_mixed_batch_filters_correctly():
    items = [
        _item("PAST", (NOW - timedelta(hours=2)).isoformat()),      # drop
        _item("TODAY_FUTURE", (NOW + timedelta(hours=2)).isoformat()),  # keep
        _item("UNDATED", None),                                     # keep
        _item("FUTURE", (NOW + timedelta(days=5)).isoformat()),      # keep
        _item("YESTERDAY", (NOW - timedelta(days=1)).isoformat()),  # drop
    ]
    out = PreEarningsAgent._filter_stale_items(items, NOW)
    syms = [it["symbol"] for it in out]
    assert syms == ["TODAY_FUTURE", "UNDATED", "FUTURE"]


@pytest.mark.unit
def test_event_exactly_now_kept():
    """An event exactly at `now` is not yet in the past — keep (boundary)."""
    items = [_item("EDGE", NOW.isoformat())]
    out = PreEarningsAgent._filter_stale_items(items, NOW)
    assert len(out) == 1


@pytest.mark.unit
def test_non_dict_items_skipped():
    items = [_item("OK", (NOW + timedelta(days=1)).isoformat()), None, "junk", 42]
    out = PreEarningsAgent._filter_stale_items(items, NOW)
    assert len(out) == 1
    assert out[0]["symbol"] == "OK"