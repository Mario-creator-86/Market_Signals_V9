from datetime import datetime, timedelta, timezone
import threading
import time
from unittest.mock import patch

from agents.news_tab_agent import NewsArticle, NewsTabAgent


def _mk_article(ticker: str, minutes_ago: int) -> NewsArticle:
    return NewsArticle(
        id=f"{ticker}-{minutes_ago}",
        title=f"{ticker} update",
        url=f"https://example.com/{ticker}/{minutes_ago}",
        source="FMP",
        published_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        ticker=ticker,
        summary=f"{ticker} company update",
    )


def test_active_watchlist_context_keeps_tail_ticker_visible():
    with patch.object(NewsTabAgent, "_load_backup", return_value=None):
        agent = NewsTabAgent(
            config={
                "news": {
                    "state_limit_tickers": 3,
                    "state_limit_ticker": 5,
                },
                "watchlist": {
                    "yahoo": ["A1", "B1", "C1", "D1", "RCKY"],
                    "binance": [],
                },
            }
        )

    with agent._cache_lock:
        agent._state.ticker_news = {
            "A1": [_mk_article("A1", 240)],
            "B1": [_mk_article("B1", 180)],
            "C1": [_mk_article("C1", 120)],
            "D1": [_mk_article("D1", 60)],
            "RCKY": [_mk_article("RCKY", 5)],
        }

    state = agent.get_news_tab_state(active_tickers=["A1", "B1", "C1", "D1", "RCKY"])
    keys = list((state.get("ticker_news") or {}).keys())

    assert len(keys) == 5
    assert "RCKY" in keys


def test_state_exposes_refresh_cadence_and_delayed_provider_work():
    with patch.object(NewsTabAgent, "_load_backup", return_value=None):
        agent = NewsTabAgent(
            config={
                "news": {
                    "news_refresh_minutes": 7,
                    "ticker_refresh_timeout_seconds": 5,
                }
            }
        )

    ticker_event = threading.Event()
    feed_event = threading.Event()
    with agent._cache_lock:
        agent._ticker_refresh_inflight["FTFT"] = ticker_event
        agent._ticker_refresh_started["FTFT"] = time.time() - 20
    with agent._feed_cache_lock:
        agent._feed_cache_inflight["https://example.com/feed"] = feed_event
        agent._feed_cache_started["https://example.com/feed"] = time.time() - 30

    with patch.object(agent, "get_market_narrative", return_value={}):
        state = agent.get_news_tab_state()

    assert state["settings"]["news_refresh_minutes"] == 7
    assert state["flags"]["refreshing_tickers"] == ["FTFT"]
    assert state["flags"]["stuck_tickers"] == ["FTFT"]
    assert state["flags"]["refreshing_ticker_ages_seconds"]["FTFT"] >= 19
    assert state["flags"]["stuck_feed_count"] == 1
