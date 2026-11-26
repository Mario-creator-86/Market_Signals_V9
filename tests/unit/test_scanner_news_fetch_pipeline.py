import json
from datetime import datetime, timezone

from core import news_pulse
from core.ollama_decision import build_trade_review_prompt


class _PersistingNewsAgent:
    def __init__(self):
        self.ensure_calls = []
        self.persisted = False

    def ensure_ticker_news_cached(self, ticker, force_refresh=False, allow_remote=True):
        self.ensure_calls.append((ticker, force_refresh, allow_remote))
        self.persisted = True
        return {
            "status": "refreshed",
            "reason": "remote_fetch_on_miss",
            "ticker": ticker,
            "count": 1,
            "new_items": 1,
            "fetched": True,
        }

    def get_ticker_news(self, ticker, max_items=180):
        if not self.persisted:
            return []
        return [{
            "id": "nvda-earnings-1",
            "ticker": ticker,
            "title": "Nvidia earnings beat expectations and guidance increased",
            "summary": "The company reported stronger revenue and raised its outlook.",
            "published_at": datetime.now(timezone.utc).isoformat(),
            "url": "https://example.test/nvda-earnings",
            "source": "provider",
            "category": "earnings",
            "impact": "high",
            "impact_score": 82,
            "action_score": 88,
            "sentiment": "bullish",
            "ai_micro_summary": {
                "briefing_version": 3,
                "overall_sentiment": "bullish",
                "article_explainer": "Nvidia beat and raised guidance.",
                "market_implication": "Supports AI semiconductor demand.",
                "ticker_implication": {"ticker": "NVDA", "direction": "up"},
            },
        }]

    def get_breaking_news(self, max_age_minutes=10080):
        return []


def test_scanner_hydration_fetches_persists_and_rebuilds_empty_pulse(monkeypatch):
    agent = _PersistingNewsAgent()
    monkeypatch.setattr(news_pulse, "_get_news_agent", lambda allow_blocking: agent)
    news_pulse.reset_news_pulse_cache()
    news_pulse._CACHE["NVDA"] = {
        "ts": 999999999999.0,
        "ttl": 3600,
        "data": {"score": 0, "top_headlines": [], "sentiment_trend": "unknown"},
    }

    pulse = news_pulse.hydrate_news_pulse(
        "NVDA",
        ttl_seconds=45,
        allow_remote=True,
    )

    assert agent.ensure_calls == [("NVDA", False, True)]
    assert pulse["hydration"]["status"] == "refreshed"
    assert pulse["hydration"]["persisted"] is True
    assert pulse["24h"]["article_count"] == 1
    assert pulse["24h"]["source_backed_count"] == 1
    assert pulse["24h"]["source_backed_ai_analyzed_count"] == 1
    assert pulse["top_headlines"][0]["summary"].startswith("The company reported")
    assert pulse["top_headlines"][0]["category"] == "earnings"
    assert pulse["ai_sentiment_trend"] == "bullish"


def test_trade_ai_prompt_receives_source_summary_and_hydration_receipt():
    signal = {
        "ticker": "NVDA",
        "action": "BUY",
        "autonomous_source": "scanner",
        "news_sentiment": "bullish",
        "top_headlines": [{
            "title": "Nvidia raises guidance",
            "summary": "Revenue beat estimates and management increased its outlook.",
            "source": "provider",
            "url": "https://example.test/nvda",
            "published_at": "2026-08-29T06:00:00+00:00",
            "category": "earnings",
        }],
        "news_context": {
            "status": "available",
            "source": "news_pulse",
            "hydration": {"requested": True, "status": "refreshed", "persisted": True},
        },
    }

    prompt = build_trade_review_prompt(signal, {"accepted": True})
    context = json.loads(prompt.split("Context:\n", 1)[1])
    ai_signal = context["signal"]

    assert ai_signal["top_headlines"][0]["summary"].startswith("Revenue beat")
    assert ai_signal["top_headlines"][0]["url"] == "https://example.test/nvda"
    assert ai_signal["news_context"]["hydration"]["persisted"] is True
