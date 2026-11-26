"""Signals + strategy + backtest tools (read-only wrappers; get_signal is a net-new by-id seam)."""
from core.ai.tools.signals_strategy import (
    make_get_signal_tool, make_get_strategy_tool, make_get_strategy_performance_tool,
    make_compare_strategies_tool, signals_strategy_tools,
)
from core.ai.tools.backtest import make_run_backtest_tool, make_get_backtest_result_tool, backtest_tools


def _fake_sm(open_signals=None, closed_signals=None):
    class _SM:
        def __init__(self):
            self.open_signals = open_signals or {}
            self.closed_signals = closed_signals or []
    return _SM()


def test_get_signal_reads_open_by_id():
    sm = _fake_sm(open_signals={"S1": {"id": "S1", "ticker": "NVDA"}})
    t = make_get_signal_tool(signal_manager=sm)
    r = t.execute(signal_id="S1")
    assert r.ok and r.data["ticker"] == "NVDA"


def test_get_signal_falls_back_to_closed():
    sm = _fake_sm(closed_signals=[{"id": "S2", "ticker": "AAPL"}])
    t = make_get_signal_tool(signal_manager=sm)
    r = t.execute(signal_id="S2")
    assert r.ok and r.data["ticker"] == "AAPL"


def test_get_signal_missing_returns_error():
    sm = _fake_sm()
    t = make_get_signal_tool(signal_manager=sm)
    r = t.execute(signal_id="X")
    assert r.ok is False and "X" in r.error


def test_get_strategy_delegates():
    def fake(*a, **k):
        return {"strategies": [{"id": "S1"}]}
    t = make_get_strategy_tool(list_strategies=fake)
    r = t.execute()
    assert r.ok and r.data["strategies"][0]["id"] == "S1"


def test_get_strategy_performance_delegates():
    def fake(symbol):
        return {"symbol": symbol, "rows": []}
    t = make_get_strategy_performance_tool(get_strategy_performance=fake)
    r = t.execute(symbol="NVDA")
    assert r.ok and r.data["symbol"] == "NVDA"


def test_compare_strategies_delegates():
    def fake(ticker, days=30, **k):
        return {"ticker": ticker, "days": days, "ranked": []}
    t = make_compare_strategies_tool(compare=fake)
    r = t.execute(ticker="NVDA", days=60)
    assert r.ok and r.data["days"] == 60


def test_run_backtest_delegates_and_shapes_result():
    def fake(**k):
        return {"ok": True, "summary": "done"}
    t = make_run_backtest_tool(run_backtest=fake)
    r = t.execute(tickers=["NVDA"], days=30)
    assert r.ok and r.data["summary"] == "done"


def test_get_backtest_result_delegates():
    def fake():
        return [{"id": "bt1"}]
    t = make_get_backtest_result_tool(get_history=fake)
    r = t.execute()
    assert r.ok and r.data["value"] == [{"id": "bt1"}]


def test_signals_strategy_and_backtest_tool_lists():
    assert [t.name for t in signals_strategy_tools()] == [
        "get_signal", "get_strategy", "get_strategy_performance", "compare_strategies"]
    assert [t.name for t in backtest_tools()] == ["run_backtest", "get_backtest_result"]