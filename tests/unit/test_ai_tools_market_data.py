"""Market-data tools delegate to existing read-only services (injectable)."""
from core.ai.tools.market_data import (
    make_get_market_snapshot_tool, make_get_ohlcv_tool, make_get_technical_features_tool,
    make_get_market_regime_tool, make_get_sector_regime_tool, market_data_tools,
)


def test_get_market_snapshot_delegates_to_get_state():
    seen = {}

    def fake_get_state(*args, **kwargs):
        seen.update(kwargs)
        return {"tickers": ["NVDA"], "capital": 100000.0}

    tool = make_get_market_snapshot_tool(get_state=fake_get_state)
    res = tool.execute()
    assert res.ok and res.data == {"tickers": ["NVDA"], "capital": 100000.0}


def test_get_ohlcv_delegates_and_shapes_result():
    def fake_fetch(ticker, period="1mo", interval="1d", **_):
        return {"ticker": ticker, "bars": [{"o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}]}
    tool = make_get_ohlcv_tool(fetch_history=fake_fetch)
    res = tool.execute(ticker="NVDA", period="1mo", interval="1d")
    assert res.ok and res.data["ticker"] == "NVDA" and len(res.data["bars"]) == 1


def test_get_technical_features_delegates_to_build_context():
    def fake(ticker, bars=None, **_):
        return {"rsi": 55.0, "atr": 2.1}
    tool = make_get_technical_features_tool(build_context=fake)
    res = tool.execute(ticker="AAPL", bars=[{"c": 1}])
    assert res.ok and res.data["rsi"] == 55.0


def test_get_market_regime_delegates_to_regime_engine():
    def fake_detect(context=None, **_):
        return {"regime": "TREND", "confidence": 0.8, "sub_scores": {}}
    tool = make_get_market_regime_tool(detect=fake_detect)
    res = tool.execute(context={"rsi": 55})
    assert res.ok and res.data["regime"] == "TREND"


def test_get_sector_regime_is_net_new_rollup():
    # Net-new read seam: rolls regime per ticker in a sector. Honest when empty.
    def fake_detect(context=None, **_):
        return {"regime": "RANGE", "confidence": 0.6}
    def fake_sector_tickers(sector):
        return ["AAPL", "MSFT"]
    tool = make_get_sector_regime_tool(detect=fake_detect, sector_tickers=fake_sector_tickers)
    res = tool.execute(sector="Technology")
    assert res.ok and res.data["sector"] == "Technology"
    assert set(res.data["per_ticker"].keys()) == {"AAPL", "MSFT"}


def test_market_data_tools_returns_five_registered_tools():
    tools = market_data_tools()
    assert [t.name for t in tools] == [
        "get_market_snapshot", "get_ohlcv", "get_technical_features",
        "get_market_regime", "get_sector_regime",
    ]


def test_market_data_tool_error_is_caught():
    def boom(*a, **k):
        raise RuntimeError("no data")
    tool = make_get_ohlcv_tool(fetch_history=boom)
    res = tool.execute(ticker="X")
    assert res.ok is False and "no data" in res.error