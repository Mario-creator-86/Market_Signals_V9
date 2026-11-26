from unittest.mock import patch

from api.main import (
    AnalyzeRequest,
    StrategyConfigRequest,
    api_analyze_ticker,
    api_get_strategy_config,
    api_get_strategy_performance,
    api_put_strategy_config,
)


def test_api_strategy_config_endpoints_smoke():
    with patch("api.main.get_strategy_config") as mock_get:
        mock_get.return_value = {"signal_mode": "V2", "min_rr": 2.0}
        out = api_get_strategy_config()
        assert out["status"] == "ok"
        assert out["config"]["signal_mode"] == "V2"

    with patch("api.main.save_strategy_config") as mock_save:
        mock_save.return_value = {"signal_mode": "BOTH_AUTO_BEST"}
        out = api_put_strategy_config(StrategyConfigRequest(config={"signal_mode": "BOTH_AUTO_BEST"}))
        assert out["status"] == "ok"
        assert out["config"]["signal_mode"] == "BOTH_AUTO_BEST"

    with patch("api.main.get_strategy_performance") as mock_perf:
        mock_perf.return_value = {"status": "ok", "count": 1, "entries": [{"symbol": "AAPL"}]}
        out = api_get_strategy_performance(symbol="aapl")
        assert out["status"] == "ok"
        assert out["count"] == 1


def test_api_analyze_ticker_uses_mode_wrapper():
    with patch("api.main.analyze_ticker_mode") as mock_mode:
        mock_mode.return_value = {"ticker": "AAPL", "selected_mode": "V2"}
        out = api_analyze_ticker(
            AnalyzeRequest(ticker="AAPL", include_heavy=False, disable_ollama=True, force_refresh=False, mode="V2")
        )
        assert out["selected_mode"] == "V2"
        mock_mode.assert_called_once()

