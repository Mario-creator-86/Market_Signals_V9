import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from core import hotlist_news, news_agent_access, news_pulse
from core.request_context import request_context


pytestmark = pytest.mark.unit


def test_news_agent_initialization_is_singleflight_throttled(monkeypatch):
    news_agent_access.reset_news_agent_access_state()
    calls = {"count": 0}

    monkeypatch.setattr(news_agent_access, "get_news_tab_agent_if_ready", lambda: None)

    def kickoff():
        calls["count"] += 1
        time.sleep(0.03)

    monkeypatch.setattr(news_agent_access, "kickoff_news_tab_agent_init", kickoff)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: news_agent_access.get_news_agent_cached(), range(12)))

    assert results == [None] * 12
    assert calls["count"] == 1


def test_ready_news_agent_bypasses_initializer(monkeypatch):
    ready = object()
    monkeypatch.setattr(news_agent_access, "get_news_tab_agent_if_ready", lambda: ready)
    monkeypatch.setattr(
        news_agent_access,
        "kickoff_news_tab_agent_init",
        lambda: pytest.fail("ready agent must not be initialized again"),
    )

    assert news_agent_access.get_news_agent_cached() is ready


def test_cache_only_request_never_initializes_news_agent(monkeypatch):
    news_agent_access.reset_news_agent_access_state()
    monkeypatch.setattr(news_agent_access, "get_news_tab_agent_if_ready", lambda: None)
    monkeypatch.setattr(
        news_agent_access,
        "kickoff_news_tab_agent_init",
        lambda: pytest.fail("cache-only request must not initialize an agent"),
    )

    with request_context(reason="UI_READ_CACHE_ONLY", path="/api/state", cache_only=True):
        assert news_agent_access.get_news_agent_cached() is None


def test_news_pulse_negative_cache_avoids_startup_stampede(monkeypatch):
    ticker = "NEGATIVE-CACHE-NEWS-PULSE"
    calls = {"count": 0}
    news_pulse._CACHE.pop(ticker, None)

    def unavailable(**_kwargs):
        calls["count"] += 1
        return None

    monkeypatch.setattr(news_pulse, "get_news_agent_cached", unavailable)

    first = news_pulse.build_news_pulse(ticker)
    second = news_pulse.build_news_pulse(ticker)

    assert first["score"] == 0
    assert second == first
    assert calls["count"] == 1


def test_hotlist_news_negative_cache_avoids_startup_stampede(monkeypatch):
    ticker = "NEGATIVE-CACHE-HOTLIST"
    cache_key = (ticker, 7, 60)
    hotlist_news._NEWS_CACHE.pop(cache_key, None)
    calls = {"count": 0}

    def unavailable(**_kwargs):
        calls["count"] += 1
        return None

    monkeypatch.setattr(hotlist_news, "get_news_agent_cached", unavailable)

    assert hotlist_news.get_ticker_news(ticker) == []
    assert hotlist_news.get_ticker_news(ticker) == []
    assert calls["count"] == 1
