import logging
import threading

from agents import news_tab_agent as news_module
from agents.news_tab_agent import NewsTabAgent
from core.request_context import (
    ExternalCallBudget,
    get_request_context,
    request_context,
)


def _bare_news_agent() -> NewsTabAgent:
    agent = NewsTabAgent.__new__(NewsTabAgent)
    agent._cache_lock = threading.RLock()
    agent._yahoo_news_cache = {}
    agent._seen_ids = set()
    return agent


def test_timeout_worker_propagates_request_context():
    agent = _bare_news_agent()
    budget = ExternalCallBudget(limit=2)
    observed = {}

    def capture_context():
        ctx = get_request_context()
        observed["request_id"] = ctx.request_id
        observed["agent_name"] = ctx.agent_name
        observed["budget"] = ctx.external_budget
        return "done"

    with request_context(
        reason="TEST",
        agent_name="pre_earnings",
        external_budget=budget,
    ) as caller_ctx:
        result = agent._call_with_timeout(
            capture_context,
            timeout_seconds=1.0,
            op_name="context-propagation",
            default="timeout",
        )

    assert result == "done"
    assert observed["request_id"] == caller_ctx.request_id
    assert observed["agent_name"] == "pre_earnings"
    assert observed["budget"] is budget


def test_yahoo_news_native_calls_obey_shared_hard_cap_and_warn_once(
    monkeypatch,
    caplog,
):
    agent = _bare_news_agent()
    provider_calls = []

    class FakeTicker:
        @property
        def news(self):
            return []

    def fake_ticker(symbol):
        provider_calls.append(symbol)
        return FakeTicker()

    monkeypatch.setattr(news_module.yf, "Ticker", fake_ticker)
    monkeypatch.setattr(news_module.time, "sleep", lambda _seconds: None)
    budget = ExternalCallBudget(limit=1)
    caplog.set_level(logging.WARNING, logger="NewsTabAgent")

    with request_context(
        reason="TEST",
        agent_name="pre_earnings",
        external_budget=budget,
    ):
        for symbol in ("AAA", "BBB", "CCC"):
            result = agent._call_with_timeout(
                lambda symbol=symbol: agent._fetch_yahoo_news(symbol),
                timeout_seconds=1.0,
                op_name=f"yahoo-news:{symbol}",
                default=[],
            )
            assert result == []

    snapshot = budget.snapshot()
    assert provider_calls == ["AAA"]
    assert snapshot["used"] == 1
    assert snapshot["blocked"] == 2
    assert snapshot["by_provider"] == {"yfinance_news": 1}
    assert snapshot["blocked_by_provider"] == {"yfinance_news": 2}
    warnings = [
        record
        for record in caplog.records
        if "External-call budget exhausted for agent=pre_earnings"
        in record.getMessage()
    ]
    assert len(warnings) == 1


def test_yahoo_news_cache_hit_does_not_consume_another_budget_slot(
    monkeypatch,
):
    agent = _bare_news_agent()
    provider_calls = []

    class FakeTicker:
        @property
        def news(self):
            return []

    def fake_ticker(symbol):
        provider_calls.append(symbol)
        return FakeTicker()

    monkeypatch.setattr(news_module.yf, "Ticker", fake_ticker)
    monkeypatch.setattr(news_module.time, "sleep", lambda _seconds: None)
    budget = ExternalCallBudget(limit=1)

    with request_context(
        reason="TEST",
        agent_name="news_tab",
        external_budget=budget,
    ):
        first = agent._fetch_yahoo_news("AAA")
        second = agent._fetch_yahoo_news("AAA")

    assert first == second == []
    assert provider_calls == ["AAA"]
    assert budget.snapshot()["used"] == 1
    assert budget.snapshot()["blocked"] == 0
