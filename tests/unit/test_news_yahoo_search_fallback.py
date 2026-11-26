"""Yahoo headlines survive an outage of the `Ticker.news` endpoint.

2026-11-23: Yahoo answered "*** YAHOO! FINANCE IS CURRENTLY DOWN! ***" on the
endpoint behind `yf.Ticker(...).news` while its search endpoint kept serving
the same headlines. The source stayed red for the whole server run.
"""
import threading

from agents import news_tab_agent as news_module
from agents.news_tab_agent import NewsTabAgent


def _bare_news_agent() -> NewsTabAgent:
    agent = NewsTabAgent.__new__(NewsTabAgent)
    agent._cache_lock = threading.RLock()
    agent._yahoo_news_cache = {}
    agent._seen_ids = set()
    return agent


class _DownTicker:
    @property
    def news(self):
        raise RuntimeError("*** YAHOO! FINANCE IS CURRENTLY DOWN! ***")


class _Search:
    def __init__(self, query, news_count=0, **_kwargs):
        self.news = [
            {"title": "Futures rebound before the open", "publisher": "Reuters",
             "link": "https://finance.yahoo.com/news/futures-rebound.html",
             "providerPublishTime": 1795421698},
            {"title": "No link, must be skipped", "publisher": "X", "link": "",
             "providerPublishTime": 1795421698},
        ]


def test_search_endpoint_is_used_when_ticker_news_is_down(monkeypatch):
    agent = _bare_news_agent()
    monkeypatch.setattr(news_module.yf, "Ticker", lambda symbol: _DownTicker())
    monkeypatch.setattr(news_module.yf, "Search", _Search)
    monkeypatch.setattr(news_module.time, "sleep", lambda _seconds: None)

    articles = agent._fetch_yahoo_news("SPY")

    assert [a.title for a in articles] == ["Futures rebound before the open"]
    assert articles[0].source == "Reuters"
    assert articles[0].published_at.year == 2026          # the real publish time, not "now"
    status = agent._source_status["yahoo_finance_search"]
    assert status["failures"] == 0 and status["last_success"]
    assert not getattr(agent, "_yahoo_search_failed", False)


def test_failure_is_still_reported_when_both_endpoints_are_down(monkeypatch):
    agent = _bare_news_agent()

    def broken_search(*_args, **_kwargs):
        raise RuntimeError("search is down too")

    monkeypatch.setattr(news_module.yf, "Ticker", lambda symbol: _DownTicker())
    monkeypatch.setattr(news_module.yf, "Search", broken_search)
    monkeypatch.setattr(news_module.time, "sleep", lambda _seconds: None)

    assert agent._fetch_yahoo_news("SPY") == []
    status = agent._source_status["yahoo_finance_search"]
    assert status["failures"] == 1
    assert "CURRENTLY DOWN" in status["last_error"]        # the primary error is the one shown
    assert agent._yahoo_search_failed is True
