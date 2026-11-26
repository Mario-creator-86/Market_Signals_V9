"""Earnings + pre-earnings tools (read-only wrappers; surprises is a net-new seam)."""
import pytest

from core.ai.tools.earnings import (
    make_get_earnings_event_tool, make_get_historical_earnings_tool,
    make_get_earnings_surprises_tool, make_get_pre_earnings_analysis_tool, earnings_tools,
)


def test_get_earnings_event_delegates():
    def fake(ticker):
        return {"ticker": ticker, "date": "2026-09-15", "eps_estimated": 1.2}
    t = make_get_earnings_event_tool(get_earnings_for_ticker=fake)
    r = t.execute(ticker="NVDA")
    assert r.ok and r.data["date"] == "2026-09-15"


def test_get_historical_earnings_delegates():
    def fake(symbol):
        return [{"quarter": "Q1", "epsActual": 1.1}]
    t = make_get_historical_earnings_tool(get_symbol_earnings=fake)
    r = t.execute(symbol="AAPL")
    assert r.ok and r.data["value"][0]["epsActual"] == 1.1


def test_get_earnings_surprises_is_net_new_seam():
    def fake(symbol):
        return [
            {"quarter": "Q1", "epsActual": 1.3, "epsEstimate": 1.1},
            {"quarter": "Q2", "epsActual": 0.9, "epsEstimate": 1.2},
        ]
    t = make_get_earnings_surprises_tool(get_symbol_earnings=fake)
    r = t.execute(symbol="AAPL")
    assert r.ok
    assert r.data["symbol"] == "AAPL"
    assert r.data["surprises"][0]["surprise"] == pytest.approx(0.2)
    assert r.data["surprises"][1]["surprise"] == pytest.approx(-0.3)


def test_get_pre_earnings_analysis_delegates():
    def fake(*a, **k):
        return {"items": [{"ticker": "AAPL", "expected_move_pct": 4.0}]}
    t = make_get_pre_earnings_analysis_tool(get_state=fake)
    r = t.execute()
    assert r.ok and "items" in r.data


def test_earnings_tools_returns_four():
    assert [t.name for t in earnings_tools()] == [
        "get_earnings_event", "get_historical_earnings",
        "get_earnings_surprises", "get_pre_earnings_analysis",
    ]