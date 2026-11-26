from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import threading

from agents.news_tab_agent import NewsTabAgent
from core import news_pulse
from core.news_pulse import _score_news_pulse, build_news_pulse


def test_news_pulse_score_penalizes_duplicates():
    window_24h = {"avg_impact_score": 80, "avg_actionability": 70, "breaking_count": 2}
    window_7d = {
        "avg_impact_score": 74,
        "sentiment_distribution": {"bullish": 6, "neutral": 1, "bearish": 2},
    }
    score_no_penalty = _score_news_pulse(window_24h, window_7d, duplicate_penalty=0)
    score_with_penalty = _score_news_pulse(window_24h, window_7d, duplicate_penalty=20)
    assert score_with_penalty < score_no_penalty


def test_build_news_pulse_from_cached_agent_state(monkeypatch):
    now = datetime.now(timezone.utc)

    class FakeNewsAgent:
        def get_ticker_news(self, ticker, max_items=40):
            return [
                {
                    "title": "Positive catalyst",
                    "url": "https://n/1",
                    "id": "source-1",
                    "source": "Primary Wire",
                    "published_at": (now - timedelta(hours=2)).isoformat(),
                    "impact": "high",
                    "impact_score": 85,
                    "action_score": 78,
                    "sentiment": "bullish",
                    "category": "earnings",
                    "ai_micro_summary": {"overall_sentiment": "bullish", "tldr": "Verified catalyst"},
                },
                {
                    "title": "Duplicate same title",
                    "url": "https://n/dup",
                    "published_at": (now - timedelta(hours=3)).isoformat(),
                    "impact": "medium",
                    "impact_score": 55,
                    "action_score": 40,
                    "sentiment": "neutral",
                    "category": "analyst",
                },
                {
                    "title": "Duplicate same title",
                    "url": "https://n/dup",
                    "published_at": (now - timedelta(hours=3)).isoformat(),
                    "impact": "medium",
                    "impact_score": 55,
                    "action_score": 40,
                    "sentiment": "neutral",
                    "category": "analyst",
                },
            ]

        def get_breaking_news(self, max_age_minutes=10080):
            return [
                {
                    "ticker": "AAPL",
                    "title": "Breaking event",
                    "url": "https://n/break",
                    "id": "source-2",
                    "source": "Exchange Notice",
                    "published_at": (now - timedelta(hours=1)).isoformat(),
                    "impact": "critical",
                    "impact_score": 95,
                    "action_score": 90,
                    "sentiment": "bullish",
                    "category": "macro",
                }
            ]

    monkeypatch.setattr("core.news_pulse._get_news_agent", lambda allow_blocking=False: FakeNewsAgent())
    pulse = build_news_pulse("AAPL", ttl_seconds=1)

    assert "24h" in pulse and "7d" in pulse
    assert pulse["24h"]["article_count"] >= 2
    assert 0 <= pulse["score"] <= 100
    assert pulse["duplicate_count"] >= 1
    assert len(pulse.get("top_headlines", [])) <= 3
    assert pulse["24h"]["source_backed_count"] >= 2
    assert pulse["24h"]["ai_analyzed_count"] >= 1
    assert pulse["24h"]["source_backed_ai_analyzed_count"] >= 1
    assert pulse["ai_sentiment_trend"] == "bullish"
    assert pulse["top_headlines"][0]["url"].startswith("https://")


def test_breaking_news_does_not_match_ambiguous_ticker_as_common_word(monkeypatch):
    now = datetime.now(timezone.utc)

    class FakeNewsAgent:
        @staticmethod
        def get_ticker_news(ticker, max_items=40):
            return [{
                "ticker": ticker,
                "title": "ServiceNow receives an analyst upgrade",
                "url": "https://example.test/servicenow",
                "source": "wire",
                "published_at": now.isoformat(),
                "impact": "medium",
                "sentiment": "bullish",
            }]

        @staticmethod
        def get_breaking_news(max_age_minutes=10080):
            return [{
                "ticker": "BTC-USD",
                "related_tickers": ["BTC-USD"],
                "title": "Bitcoin reaches its most important level right now",
                "url": "https://example.test/bitcoin",
                "source": "crypto-wire",
                "published_at": now.isoformat(),
                "impact": "high",
                "sentiment": "bullish",
            }]

    news_pulse.reset_news_pulse_cache()
    monkeypatch.setattr("core.news_pulse._get_news_agent", lambda allow_blocking=False: FakeNewsAgent())

    pulse = build_news_pulse("NOW", ttl_seconds=1)

    assert pulse["24h"]["article_count"] == 1
    assert [item["title"] for item in pulse["top_headlines"]] == [
        "ServiceNow receives an analyst upgrade"
    ]


def test_breaking_news_accepts_explicit_ambiguous_ticker_tag():
    article = {"ticker": "", "title": "ServiceNow ($NOW) raises its outlook"}

    assert news_pulse._breaking_article_matches_symbol(article, "NOW") is True


def test_nonblocking_pulse_returns_stale_snapshot_during_news_commit(monkeypatch):
    ticker = "LOCKED-NEWS-PULSE"
    stale = {
        "24h": {"article_count": 4},
        "7d": {"article_count": 9},
        "score": 71,
        "duplicate_count": 0,
        "top_headlines": [],
        "sentiment_trend": "bullish",
    }
    news_pulse._CACHE[ticker] = {"ts": 0.0, "ttl": 1, "data": stale}

    class LockedAgent:
        @staticmethod
        def try_get_ticker_news(_ticker, max_items=40):
            return None

        @staticmethod
        def get_ticker_news(*_args, **_kwargs):
            raise AssertionError("nonblocking pulse must not wait on the legacy reader")

    monkeypatch.setattr("core.news_pulse._get_news_agent", lambda allow_blocking=False: LockedAgent())

    pulse = build_news_pulse(ticker, ttl_seconds=1, allow_blocking=False)

    assert pulse["score"] == 71
    assert pulse["stale"] is True
    assert pulse["stale_reason"] == "news_refresh_commit_in_progress"


def test_news_agent_nonblocking_readers_do_not_wait_for_commit_lock():
    agent = NewsTabAgent.__new__(NewsTabAgent)
    agent._cache_lock = threading.RLock()
    agent._state = SimpleNamespace(ticker_news={}, breaking_news=[])
    agent._get_watchlist_context = lambda include=None: {}
    acquired = threading.Event()
    release = threading.Event()

    def hold_cache_lock():
        with agent._cache_lock:
            acquired.set()
            release.wait(timeout=2)

    worker = threading.Thread(target=hold_cache_lock)
    worker.start()
    assert acquired.wait(timeout=1)
    try:
        assert agent.try_get_ticker_news("AAPL") is None
        assert agent.try_get_breaking_news() is None
    finally:
        release.set()
        worker.join(timeout=2)
