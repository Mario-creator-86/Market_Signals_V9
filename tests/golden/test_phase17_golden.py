"""Phase 17 golden tests — close the open constitutional rows.

GOLD-003/004 — BMO/AMC earnings reporting alignment (`core/market_hours.
earnings_session_reported`, §28): BMO is same-day-reportable once past the
actual 09:30 America/New_York open, AMC once past the after-hours bulk print
(22:00 UTC), and an UNKNOWN session hint is never silently reportable.

GOLD-011 — stale-data rejection (`core/engine` Data Freshness Guard): data
older than the interval threshold is rejected and never reaches signal
generation; empty data is rejected outright.
"""
from datetime import datetime, timezone

import pandas as pd
import pytest

from core.market_hours import earnings_session_reported

pytestmark = pytest.mark.golden


def _utc(hour, minute):
    return datetime(2026, 8, 14, hour, minute, tzinfo=timezone.utc)  # a Friday


def test_gold_003_earnings_bmo_alignment():
    # August is EDT, so the actual 09:30 ET open is 13:30 UTC.
    assert earnings_session_reported("BMO", _utc(13, 0)) is False
    assert earnings_session_reported("pre-market", _utc(13, 29)) is False
    # At/after the open threshold: reportable.
    assert earnings_session_reported("BMO", _utc(13, 30)) is True
    assert earnings_session_reported("premarket", _utc(18, 0)) is True
    # Naive timestamps are treated as UTC (documented contract).
    assert earnings_session_reported("BMO", datetime(2026, 8, 14, 13, 30)) is True


def test_gold_004_earnings_amc_alignment():
    # AMC prints after the 22:00 UTC after-hours bulk print.
    assert earnings_session_reported("AMC", _utc(21, 0)) is False
    assert earnings_session_reported("after_hours", _utc(21, 59)) is False
    assert earnings_session_reported("AMC", _utc(22, 0)) is True
    assert earnings_session_reported("aft", _utc(23, 30)) is True
    # Ambiguous catalysts are never silently reportable.
    assert earnings_session_reported("", _utc(23, 0)) is False
    assert earnings_session_reported("unknown", _utc(23, 0)) is False


def test_gold_011_stale_data_rejected():
    from core.engine import MarketEngine

    engine = object.__new__(MarketEngine)  # _validate_data_freshness touches no instance state

    def frame(age_seconds):
        stamp = pd.Timestamp.now(tz="UTC") - pd.Timedelta(seconds=age_seconds)
        return pd.DataFrame({"close": [1.0, 2.0]}, index=[stamp - pd.Timedelta(minutes=1), stamp])

    # Fresh 1m data passes the 120s threshold with margin.
    fresh, fresh_age = engine._validate_data_freshness(frame(5), "1m", "AAPL")
    assert fresh is True and fresh_age < 120
    # Stale 1m data (well beyond 120s) is rejected.
    stale, stale_age = engine._validate_data_freshness(frame(600), "1m", "AAPL")
    assert stale is False and stale_age > 120
    # Daily data uses the ~25h threshold: 2h old passes, 30h old is rejected.
    assert engine._validate_data_freshness(frame(2 * 3600), "1d", "AAPL")[0] is True
    assert engine._validate_data_freshness(frame(30 * 3600), "1d", "AAPL")[0] is False
    # Empty data is rejected outright — never guessed fresh.
    assert engine._validate_data_freshness(pd.DataFrame(), "1m", "AAPL")[0] is False
