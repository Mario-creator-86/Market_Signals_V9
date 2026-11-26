import pytest
from scripts.verify_pre_earnings_history import event_gap


@pytest.mark.unit
def test_gap_uses_correct_session_and_split_adjusted_price_basis():
    bars = [{"date": "2026-09-03", "Close": 100, "Open": 99},
            {"date": "2026-09-04", "Close": 105, "Open": 97, "Stock Splits": 0.1}]
    bmo = event_gap(bars, "2026-09-04", "BMO")
    amc = event_gap(bars, "2026-09-03", "AMC")
    assert bmo == amc
    assert bmo["provider_gap_pct"] == pytest.approx(-3)
    assert bmo["split_on_reaction"] == 0.1


@pytest.mark.unit
@pytest.mark.parametrize("bars,event,session,status", [
    ([], "2026-09-04", "BMO", "missing_event_session"),
    ([{"date": "2026-09-04"}], "2026-09-04", "BMO", "missing_adjacent_session"),
    ([{"date": "2026-08-20"}, {"date": "2026-09-04"}], "2026-09-04", "BMO", "nonadjacent_sessions"),
    ([{"date": "2026-09-03", "Close": 0}, {"date": "2026-09-04", "Open": 5}], "2026-09-04", "BMO", "invalid_prices"),
])
def test_gap_rejects_missing_stale_or_invalid_bars(bars, event, session, status):
    assert event_gap(bars, event, session)["price_status"] == status


@pytest.mark.unit
def test_production_gap_uses_daily_bars_and_rejects_stale_or_unknown_session(monkeypatch):
    from datetime import datetime, timezone
    from agents.pre_earnings_agent import PreEarningsAgent
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    def dt(day):
        return datetime(2026, 9, day, tzinfo=timezone.utc)
    bars = [{"dt": dt(3), "close": 3.41, "open": 3.38}, {"dt": dt(4), "open": 3.31, "close": 3.55}]
    monkeypatch.setattr(agent, "_load_symbol_daily_bars", lambda symbol: bars)
    monkeypatch.setattr(agent, "_load_symbol_ohlc_bars", lambda symbol: pytest.fail("intraday basis must not be used"))
    assert agent._compute_event_gap_move_pct("CLGN", dt(4), "BMO") == pytest.approx(-2.9325513)
    assert agent._compute_event_gap_move_pct("CLGN", dt(4), "UNKNOWN") is None
    assert agent._compute_event_gap_move_pct("CLGN", dt(5), "BMO") is None
    stale = [{"dt": dt(1), "close": 3}, {"dt": dt(8), "open": 4}]
    assert agent._compute_event_gap_move_pct("TEST", dt(8), "BMO", bars=stale) is None
