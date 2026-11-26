from types import SimpleNamespace
from datetime import date

import pytest

from api import state_service
from core.request_context import request_context


pytestmark = pytest.mark.unit


def _patch_minimal_state(monkeypatch, captured):
    config = SimpleNamespace(
        ui={"refresh_interval_ms": 5000},
        watchlist={},
        hot_list=[],
        config={},
    )
    monkeypatch.setattr(state_service, "get_shared_config", lambda: config)
    monkeypatch.setattr(
        state_service,
        "_load_json_cached",
        lambda path, *_args, **_kwargs: (
            {"open": {}, "closed": []}
            if path == state_service.SIGNALS_PATH
            else {"tickers": {}, "log": [], "activity_log": []}
        ),
    )
    monkeypatch.setattr(state_service, "_enrich_open_signals_current_price", lambda *_args: None)
    monkeypatch.setattr(state_service, "build_tickers_list", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(state_service, "build_signals_list", lambda *_args: [])
    monkeypatch.setattr(state_service, "build_stats", lambda *_args: {})
    monkeypatch.setattr(state_service, "get_trade_audit", lambda **_kwargs: [])

    def fake_capital(*_args, fast_only=False, **_kwargs):
        captured.append(fast_only)
        return {"positions": []}

    monkeypatch.setattr(state_service, "_build_capital_payload", fake_capital)


def test_cache_only_aggregate_state_never_requests_live_capital_quotes(monkeypatch):
    captured = []
    _patch_minimal_state(monkeypatch, captured)

    with request_context(reason="UI_READ_CACHE_ONLY", path="/api/state", cache_only=True):
        payload = state_service.get_state(
            include_news=False,
            include_hotlist=False,
            hotlist_details=False,
            lean=False,
        )

    assert payload["capital"] == {"positions": []}
    assert captured == [True]


def test_non_cache_only_aggregate_state_can_request_live_capital_quotes(monkeypatch):
    captured = []
    _patch_minimal_state(monkeypatch, captured)

    with request_context(reason="INTERNAL_REFRESH", path="/internal/state", cache_only=False):
        state_service.get_state(
            include_news=False,
            include_hotlist=False,
            hotlist_details=False,
            lean=False,
        )

    assert captured == [False]


def test_cache_only_earnings_lookup_never_starts_provider_work(monkeypatch):
    ticker = "CACHE-ONLY-EARNINGS"
    with state_service._yf_earnings_cache_lock:
        state_service._yf_earnings_cache.pop(ticker, None)
    monkeypatch.setattr(
        state_service,
        "_fetch_yf_earnings_date",
        lambda _ticker: pytest.fail("cache-only lookup must not fetch yfinance"),
    )

    result = state_service._resolve_earnings_from_yfinance(
        ticker,
        date(2026, 7, 26),
        allow_remote=False,
    )

    assert result == ("N/A", -1, "", "")


def test_cache_only_earnings_lookup_uses_nonblocking_calendar_reader():
    calls = []

    class Calendar:
        @staticmethod
        def try_get_earnings_for_ticker(ticker):
            calls.append(ticker)
            return {"date": "2026-08-28", "when": "amc"}

        @staticmethod
        def get_earnings_for_ticker(_ticker):
            raise AssertionError("cache-only state must not use the blocking calendar reader")

    result = state_service._resolve_earnings_from_sources(
        Calendar(), "AAPL", date(2026, 8, 20), allow_remote=False
    )

    assert calls == ["AAPL"]
    assert result[:3] == ("2026-08-28", 8, "AMC")


def test_cache_only_ticker_build_propagates_policy_and_skips_snapshot_write(monkeypatch):
    captured = []
    config = SimpleNamespace(
        hot_list=[],
        watchlist={"order": ["AAPL"], "yahoo": ["AAPL"]},
        news_pulse={"ttl_seconds": 45},
        config={},
        get_ticker_settings=lambda _ticker: {},
        get_effective_ticker_settings=lambda _ticker: {},
    )

    class Workspace:
        @staticmethod
        def get_market_data(_ticker):
            return {}

    monkeypatch.setattr(state_service, "get_ticker_workspace_store", lambda: Workspace())
    monkeypatch.setattr(state_service, "build_news_pulse_bulk", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        "agents.economic_calendar_agent.get_economic_calendar_agent",
        lambda _config: object(),
    )

    def resolve(_calendar, _ticker, _today, allow_remote=True):
        captured.append(allow_remote)
        return "N/A", -1, "", ""

    monkeypatch.setattr(state_service, "_resolve_earnings_from_sources", resolve)
    monkeypatch.setattr(
        "core.sqlite_store.get_sqlite_store",
        lambda: pytest.fail("cache-only ticker build must not persist a snapshot"),
    )

    rows = state_service.build_tickers_list(
        {"tickers": {"AAPL": {"price": 100.0}}},
        {"open": {}, "closed": []},
        config,
        allow_remote=False,
    )

    assert len(rows) == 1
    assert captured == [False]
