"""Pre-event price features must be point-in-time and honest about gaps."""
from datetime import datetime, timedelta, timezone

import pytest

from core import pre_event_features as pf


def _bars(closes, start=datetime(2026, 8, 3, tzinfo=timezone.utc)):
    out, day = [], start
    for close in closes:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        out.append({"dt": day, "open": close, "close": close})
        day += timedelta(days=1)
    return out


@pytest.mark.unit
def test_relative_return_uses_completed_sessions_only():
    stock = _bars([100.0] * 20 + [100, 101, 102, 103, 104, 120])
    spy = _bars([400.0] * 25 + [404.0])
    as_of = stock[-1]["dt"]  # the capture day's own bar must be ignored
    feats = pf.relative_returns(stock, spy, as_of)
    assert feats["predicted_price_features_as_of"] == stock[-2]["dt"].date().isoformat()
    assert feats["predicted_rel_return_5d_pct"] == pytest.approx(4.0)
    assert feats["predicted_rel_return_20d_pct"] == pytest.approx(4.0)


@pytest.mark.unit
def test_relative_return_subtracts_benchmark():
    stock = _bars([100.0] * 21 + [110.0])
    spy = _bars([400.0] * 21 + [420.0])
    feats = pf.relative_returns(stock, spy, stock[-1]["dt"] + timedelta(days=1))
    assert feats["predicted_rel_return_5d_pct"] == pytest.approx(5.0)


@pytest.mark.unit
def test_short_or_stale_history_is_missing_not_guessed():
    assert pf.relative_returns(_bars([100.0] * 4), _bars([400.0] * 4), datetime(2026, 8, 20, tzinfo=timezone.utc)) is None
    stock, spy = _bars([100.0] * 30), _bars([400.0] * 30)
    assert pf.relative_returns(stock, spy, stock[-1]["dt"] + timedelta(days=30)) is None
    assert pf.relative_returns([], spy, datetime(2026, 9, 1, tzinfo=timezone.utc)) is None


@pytest.mark.unit
def test_reversal_rule_is_contrarian_above_threshold():
    assert pf.reversal_direction({"predicted_rel_return_5d_pct": -12.0}) == 1
    assert pf.reversal_direction({"predicted_rel_return_5d_pct": 15.0}) == 0
    assert pf.reversal_direction({"predicted_rel_return_5d_pct": 9.9}) is None
    assert pf.reversal_direction({}) is None
