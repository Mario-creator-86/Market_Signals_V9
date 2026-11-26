import asyncio
from unittest.mock import patch

from api.main import (
    ConfigUpdateRequest,
    QuickAIRequest,
    TickerRequest,
    TickerSettingsRequest,
    TickersDeleteRequest,
    TickersReorderRequest,
    api_add_ticker,
    api_get_state,
    api_get_ticker_settings,
    api_preview_global_settings,
    api_remove_tickers,
    api_reorder_tickers,
    api_update_ticker_settings,
    api_watchlist_pulse,
    api_watchlist_quick_ai,
)


def test_watchlist_endpoints_smoke_direct_calls():
    with patch("api.main.get_state") as mock_state:
        mock_state.return_value = {"tickers": [], "signals": []}
        out = api_get_state()
        assert "tickers" in out

    with patch("api.main.threading.Thread") as mock_thread, patch("api.main.add_ticker") as mock_add:
        mock_thread.return_value.start.return_value = None
        mock_add.return_value = {"status": "ok", "ticker": "AAPL"}
        out = api_add_ticker(TickerRequest(ticker="AAPL", return_state=True))
        assert out["status"] == "ok"

    with patch("api.main.remove_tickers") as mock_remove, patch("agents.economic_calendar_agent.get_economic_calendar_agent") as mock_calendar:
        mock_remove.return_value = {"status": "ok"}
        mock_calendar.return_value.refresh_data.return_value = None
        out = api_remove_tickers(TickersDeleteRequest(tickers=["AAPL"]))
        assert out["status"] == "ok"

    with patch("api.actions_service.reorder_watchlist") as mock_reorder:
        mock_reorder.return_value = {"status": "ok"}
        out = api_reorder_tickers(TickersReorderRequest(tickers=["MSFT", "AAPL"]))
        assert out["status"] == "ok"

    with patch("api.actions_service.get_ticker_settings") as mock_get_settings:
        mock_get_settings.return_value = {"ticker": "AAPL", "signals_enabled": True}
        out = api_get_ticker_settings("AAPL")
        assert out["ticker"] == "AAPL"

    with patch("api.actions_service.update_ticker_settings") as mock_update_settings:
        mock_update_settings.return_value = {"status": "ok", "ticker": "AAPL"}
        out = api_update_ticker_settings(
            TickerSettingsRequest(ticker="AAPL", settings={"min_win_rate": 52, "signals_enabled": True})
        )
        assert out["status"] == "ok"

    with patch("api.main.preview_global_settings_apply") as mock_preview:
        mock_preview.return_value = {"total_tickers": 3, "affected_tickers": 2, "changed_fields": ["min_win_rate"]}
        out = api_preview_global_settings(ConfigUpdateRequest(config={"min_win_rate": 50}))
        assert out["preview"]["affected_tickers"] == 2

    with patch("api.main.quick_analyze_ticker") as mock_quick:
        mock_quick.return_value = {"ticker": "AAPL", "quick_ai": {"enabled": True}}
        out = api_watchlist_quick_ai(QuickAIRequest(ticker="AAPL", force_refresh=False))
        assert out["ticker"] == "AAPL"

    with patch("core.config.ConfigManager") as mock_config_cls, patch("core.news_pulse.build_news_pulse_bulk") as mock_pulse:
        mock_cfg = mock_config_cls.return_value
        mock_cfg.watchlist = {"order": ["AAPL"], "yahoo": ["AAPL"], "binance": []}
        mock_cfg.news_pulse = {"ttl_seconds": 45}
        mock_pulse.return_value = {"AAPL": {"score": 42}}
        out = api_watchlist_pulse()
        assert out["status"] == "ok"
        assert "AAPL" in out["tickers"]
