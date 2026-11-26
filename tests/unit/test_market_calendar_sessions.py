from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pytest
from core.market_hours import (
    us_equity_market_status, earnings_session_reported, MarketHoursStatus,
)
from core.data_model.entities import TradingSession

UTC = timezone.utc
ET = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def _no_market_hours_override(monkeypatch):
    """Ensure MS_MARKET_HOURS_OVERRIDE does not leak from other tests/env."""
    monkeypatch.delenv("MS_MARKET_HOURS_OVERRIDE", raising=False)


def _et(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


def test_regular_session():
    # Wednesday 2026-08-12 11:00 ET (not a holiday, not a half day)
    s = us_equity_market_status(_et(2026, 8, 12, 11, 0))
    assert s.is_open is True
    assert s.session is TradingSession.REGULAR
    assert s.is_half_day is False
    assert s.exchange == "XNYS"


def test_premarket_session():
    s = us_equity_market_status(_et(2026, 8, 12, 8, 0))
    assert s.is_open is False
    assert s.session is TradingSession.PRE_MARKET


def test_after_hours_session():
    s = us_equity_market_status(_et(2026, 8, 12, 17, 0))
    assert s.is_open is False
    assert s.session is TradingSession.AFTER_HOURS


def test_weekend_closed():
    s = us_equity_market_status(_et(2026, 8, 15, 11, 0))  # Saturday
    assert s.is_open is False
    assert s.session is TradingSession.CLOSED
    assert s.reason == "weekend"


def test_holiday_closed():
    # 2026-01-01 is Thursday; New Year -> holiday
    s = us_equity_market_status(_et(2026, 1, 1, 11, 0))
    assert s.is_open is False
    assert s.session is TradingSession.CLOSED


def test_half_day_early_close():
    # 2026-11-27 is Friday (day after Thanksgiving) -> early close 13:00 ET.
    # 12:30 is within the shortened regular session.
    s = us_equity_market_status(_et(2026, 11, 27, 12, 30))
    assert s.is_open is True
    assert s.is_half_day is True
    assert s.session is TradingSession.REGULAR
    assert s.close_time_et.strftime("%H:%M") == "13:00"


def test_half_day_after_early_close_is_after_hours():
    # Same half day at 15:00 -> after the 13:00 early close -> after hours.
    s = us_equity_market_status(_et(2026, 11, 27, 15, 0))
    assert s.is_open is False
    assert s.is_half_day is True
    assert s.session is TradingSession.AFTER_HOURS


def test_before_preopen_is_closed():
    # 02:00 ET is before pre-market (04:00) -> CLOSED (not PRE_MARKET).
    s = us_equity_market_status(_et(2026, 8, 12, 2, 0))
    assert s.is_open is False
    assert s.session is TradingSession.CLOSED
    assert s.reason == "before_regular_session"


def test_earnings_session_reported_bmo():
    assert earnings_session_reported("bmo", datetime(2026, 8, 12, 13, 29, tzinfo=UTC)) is False
    assert earnings_session_reported("bmo", datetime(2026, 8, 12, 13, 30, tzinfo=UTC)) is True


def test_earnings_session_reported_amc():
    assert earnings_session_reported("amc", datetime(2026, 8, 12, 21, 59, tzinfo=UTC)) is False
    assert earnings_session_reported("amc", datetime(2026, 8, 12, 22, 0, tzinfo=UTC)) is True


def test_earnings_session_reported_unknown_kept():
    assert earnings_session_reported("nonsense", datetime(2026, 8, 12, 23, 0, tzinfo=UTC)) is False


def test_earnings_session_reported_naive_assumed_utc():
    # Naive now_utc is assumed UTC (13:30 in August -> 09:30 ET).
    assert earnings_session_reported("bmo", datetime(2026, 8, 12, 13, 30)) is True


def test_status_is_a_market_hours_status():
    s = us_equity_market_status(_et(2026, 8, 12, 11, 0))
    assert isinstance(s, MarketHoursStatus)
    # existing fields preserved
    assert hasattr(s, "now_et") and hasattr(s, "open_time_et") and hasattr(s, "close_time_et")
