"""Session-aware reported filtering for Pre-Earnings (Task PE-premium C1).

A date-only calendar row parses as midnight UTC — a naive datetime comparison
drops a same-day BMO right after midnight, hours before the report. The new
filter decides by DATE first, then by exchange-local session cutoff (BMO >=
09:30 ET, AMC >= 18:00 ET). Unknown-same-day
session stays upcoming; undated rows stay (missing-data flag, not stale).
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents.pre_earnings_agent import PreEarningsAgent


NOW = datetime(2026, 8, 1, 20, 0, tzinfo=timezone.utc)  # Friday 20:00 UTC


def _status(dt_iso, session, now=NOW):
    return PreEarningsAgent._earnings_reported_status(dt_iso, session, now)


@pytest.mark.unit
def test_past_date_always_reported():
    assert _status("2026-07-31T00:00:00+00:00", "BMO") is True
    assert _status("2026-07-30T13:00:00+00:00", "AMC") is True


@pytest.mark.unit
def test_future_date_never_reported():
    assert _status("2026-08-03T00:00:00+00:00", "BMO") is False


@pytest.mark.unit
def test_same_day_bmo_after_cutoff_reported():
    assert _status("2026-08-01T00:00:00+00:00", "BMO") is True  # 20:00 >= 14:30


@pytest.mark.unit
def test_same_day_bmo_before_cutoff_kept():
    early = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    assert _status("2026-08-01T00:00:00+00:00", "BMO", early) is False


@pytest.mark.unit
def test_same_day_amc_after_cutoff_reported():
    late = datetime(2026, 8, 1, 22, 30, tzinfo=timezone.utc)
    assert _status("2026-08-01T00:00:00+00:00", "AMC", late) is True


@pytest.mark.unit
def test_same_day_amc_before_cutoff_kept():
    assert _status("2026-08-01T00:00:00+00:00", "AMC") is False  # 20:00 < 22:00


@pytest.mark.unit
def test_same_day_unknown_session_date_only_kept():
    assert _status("2026-08-01T00:00:00+00:00", "UNKNOWN") is False
    assert _status("2026-08-01T00:00:00+00:00", None) is False


@pytest.mark.unit
def test_same_day_unknown_session_timed_uses_time():
    # A timed same-day event with no session hint honors the exact time.
    assert _status("2026-08-01T12:00:00+00:00", None) is True   # 12:00 < 20:00
    assert _status("2026-08-01T21:00:00+00:00", None) is False  # 21:00 > 20:00


@pytest.mark.unit
def test_undated_never_reported():
    assert _status(None, "BMO") is False
    assert _status("garbage", "BMO") is False


def _item(symbol, dt_iso, session="BMO", gap_risk=50):
    return {
        "symbol": symbol,
        "earnings_time_utc": dt_iso,
        "session": session,
        "gap_risk": gap_risk,
        "direction_label": "LONG",
    }


@pytest.mark.unit
def test_filter_reported_items_partitions_and_records():
    agent_cfg = {"reported_session_filter": True}
    items = [
        _item("PAST", "2026-07-31T00:00:00+00:00"),
        _item("BMOTODAY", "2026-08-01T00:00:00+00:00"),
        _item("AMCTODAY", "2026-08-01T00:00:00+00:00", session="AMC"),
        _item("FUT", "2026-08-03T00:00:00+00:00"),
        _item("UNDATED", None),
    ]
    survivors, reported = PreEarningsAgent._filter_reported_items(
        items, agent_cfg, NOW, watchlist={"PAST"},
    )
    assert [i["symbol"] for i in survivors] == ["AMCTODAY", "FUT", "UNDATED"]
    assert [r["symbol"] for r in reported] == ["PAST", "BMOTODAY"]
    assert reported[0]["is_watchlist"] is True
    assert reported[1]["is_watchlist"] is False
    assert reported[0]["session"] == "BMO"
    assert reported[1]["gap_risk"] == 50
    assert reported[0]["direction_label"] == "LONG"


@pytest.mark.unit
def test_news_confirmed_report_is_removed_before_session_cutoff():
    early = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)  # 08:00 ET
    item = _item("DAO", "2026-08-20T00:00:00+00:00", session="BMO")
    item["flags"] = {
        "already_reported": True,
        "already_reported_evidence": ["Youdao Reports Second Quarter 2026 Results"],
    }

    survivors, reported = PreEarningsAgent._filter_reported_items(
        [item], {"reported_session_filter": True}, early
    )

    assert survivors == []
    assert reported[0]["symbol"] == "DAO"
    assert reported[0]["reason"] == "ALREADY_REPORTED_EVIDENCE"
    assert reported[0]["evidence"] == ["Youdao Reports Second Quarter 2026 Results"]


@pytest.mark.unit
def test_filter_reported_items_disabled_falls_back_to_datetime():
    agent_cfg = {"reported_session_filter": False}
    items = [
        _item("BMOTODAY", "2026-08-01T00:00:00+00:00"),
        _item("TIMED", "2026-08-01T21:00:00+00:00", session="AMC"),
    ]
    survivors, reported = PreEarningsAgent._filter_reported_items(items, agent_cfg, NOW)
    # Legacy behavior: strict datetime compare — midnight date-only rows drop,
    # future-timed rows survive. No transparency records are emitted.
    assert [i["symbol"] for i in survivors] == ["TIMED"]
    assert reported == []
