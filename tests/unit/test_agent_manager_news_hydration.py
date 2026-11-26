from core.agent_manager import _attach_cached_news_context_for_ai


def test_scanner_candidate_receives_cached_earnings_news_before_ai(monkeypatch):
    signal = {
        "ticker": "OKTA",
        "autonomous_source": "scanner",
        "signal_mode": "AUTONOMOUS_SCANNER",
        "asset_class": "US_STOCKS",
        "news_sentiment": "unknown",
        "news_headlines": [],
    }

    def fake_pulse(ticker, ttl_seconds=45, max_items=180, allow_remote=True, force_refresh=False):
        assert ticker == "OKTA"
        assert ttl_seconds == 30
        assert max_items == 40
        assert allow_remote is True
        assert force_refresh is False
        return {
            "score": 88,
            "sentiment_trend": "bullish",
            "ai_sentiment_trend": "unknown",
            "generated_at": "2026-08-27T16:09:00+00:00",
            "24h": {"article_count": 7},
            "7d": {"article_count": 12},
            "top_headlines": [
                {
                    "title": "OKTA Q2 earnings beat and FY27 outlook raised",
                    "source": "yahoo_rss",
                    "url": "https://example.test/okta",
                    "published_at": "2026-08-27T15:59:00+00:00",
                    "category": "earnings",
                }
            ],
            "hydration": {
                "requested": True,
                "status": "refreshed",
                "fetched": True,
                "persisted": True,
                "count": 12,
            },
        }

    monkeypatch.setattr("core.news_pulse.hydrate_news_pulse", fake_pulse)
    result = _attach_cached_news_context_for_ai(
        signal,
        {"autonomous_news_gate": {"ttl_seconds": 30, "allow_blocking_fetch": True}},
    )

    assert result["news_sentiment"] == "bullish"
    assert result["news_score"] == 88.0
    # Fix #2 (2026-09-02): compact headlines keep source + age metadata so the
    # §41 reviewer can weigh the outlet (age label is relative — assert prefix).
    headline = result["news_headlines"][0]
    assert headline.startswith("[src=yahoo_rss | age=")
    assert headline.endswith("OKTA Q2 earnings beat and FY27 outlook raised")
    assert result["top_headlines"][0]["category"] == "earnings"
    assert result["latest_news"][0]["title"] == "OKTA Q2 earnings beat and FY27 outlook raised"
    assert result["news_context"]["status"] == "available"
    assert result["news_context"]["source"] == "news_pulse"
    assert result["news_context"]["generated_at"] == "2026-08-27T16:09:00+00:00"
    assert result["news_context"]["article_count_24h"] == 7
    assert result["news_context"]["article_count_7d"] == 12
    assert result["news_context"]["sentiment"] == "bullish"
    assert result["news_context"]["score"] == 88.0
    assert result["news_context"]["hydration"]["persisted"] is True
    assert result["news_context"]["top_headlines"][0]["category"] == "earnings"


def test_existing_complete_news_context_is_not_replaced(monkeypatch):
    signal = {
        "ticker": "AAPL",
        "autonomous_source": "pre_earnings",
        "news_sentiment": "neutral",
        "news_headlines": ["Existing verified headline"],
        # Rich (dict with source/time/url) headlines count as complete news:
        # titles-only sets must still hydrate (fix #2, 2026-09-02).
        "top_headlines": [{
            "title": "Existing verified headline",
            "source": "reuters",
            "time": "2026-09-01T10:00:00+00:00",
            "url": "https://example.test/aapl",
        }],
    }

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("news pulse should not be called for complete context")

    monkeypatch.setattr("core.news_pulse.build_news_pulse", unexpected_call)
    result = _attach_cached_news_context_for_ai(signal, {})

    assert result["news_headlines"] == ["Existing verified headline"]
    assert result["news_sentiment"] == "neutral"


def test_scanner_existing_headline_still_runs_cache_first_hydration(monkeypatch):
    signal = {
        "ticker": "MSFT",
        "autonomous_source": "scanner",
        "news_sentiment": "neutral",
        "news_headlines": ["Unverified scanner headline"],
    }
    calls = []

    def fake_hydrate(ticker, **kwargs):
        calls.append((ticker, kwargs))
        return {
            "score": 55,
            "sentiment_trend": "neutral",
            "24h": {"article_count": 1, "source_backed_count": 1},
            "7d": {"article_count": 1},
            "top_headlines": [{
                "title": "Microsoft publishes verified update",
                "source": "provider",
                "url": "https://example.test/msft",
                "published_at": "2026-08-29T06:00:00+00:00",
            }],
            "hydration": {"requested": True, "status": "hit", "persisted": True},
        }

    monkeypatch.setattr("core.news_pulse.hydrate_news_pulse", fake_hydrate)
    result = _attach_cached_news_context_for_ai(signal, {})

    assert calls and calls[0][0] == "MSFT"
    headline = result["news_headlines"][0]
    assert headline.startswith("[src=provider | age=")
    assert headline.endswith("Microsoft publishes verified update")
    assert result["news_context"]["hydration"]["status"] == "hit"
