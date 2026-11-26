from datetime import datetime, timezone

from agents.news_tab_agent import NewsArticle, NewsImpact, NewsTabAgent
from api.main import _is_current_news_briefing, _normalize_deep_analysis


def _mk_article(article_id: str, title: str, source: str = "Reuters", ticker: str = "AAPL") -> NewsArticle:
    return NewsArticle(
        id=article_id,
        title=title,
        url=f"https://example.com/{article_id}",
        source=source,
        published_at=datetime.now(timezone.utc),
        ticker=ticker,
    )


def test_story_fingerprint_and_cross_source_dedup():
    agent = NewsTabAgent()
    a1 = _mk_article("a1", "Apple beats earnings estimates as iPhone demand jumps", "Reuters")
    a2 = _mk_article("a2", "AAPL tops estimates on strong iPhone demand, revenue beats", "Bloomberg")

    agent._analyze_article(a1)
    agent._analyze_article(a2)
    merged = agent._apply_cross_source_dedup([a1, a2])

    assert len(merged) == 2
    assert any(x.is_duplicate for x in merged)
    primary = next(x for x in merged if not x.is_duplicate)
    duplicate = next(x for x in merged if x.is_duplicate)
    assert primary.story_fingerprint
    assert duplicate.story_fingerprint == primary.story_fingerprint
    assert "Bloomberg" in primary.also_reported_by or "Reuters" in primary.also_reported_by


def test_action_score_uses_time_horizon_and_breaking():
    agent = NewsTabAgent()
    article = _mk_article("a3", "Fed rate decision shocks markets", "CNBC", ticker=None)
    article.is_breaking = True
    article.impact = NewsImpact.CRITICAL
    article.impact_score = 60
    article.ai_micro_summary = {"time_horizon": "intraday"}
    score_intraday = agent._calculate_action_score(article)

    article.ai_micro_summary = {"time_horizon": "unknown"}
    score_unknown = agent._calculate_action_score(article)
    assert score_intraday > score_unknown
    assert 0 <= score_intraday <= 100


def test_micro_summary_normalization_fallback():
    agent = NewsTabAgent()
    normalized = agent._normalize_micro_summary({"overall_sentiment": "invalid", "confidence": "x"})
    assert normalized["briefing_version"] == 3
    assert normalized["article_explainer"] == "unknown"
    assert normalized["market_implication"] == "unknown"
    assert normalized["market_direction"] == "unclear"
    assert normalized["ticker_implication"]["direction"] == "unclear"
    assert normalized["overall_sentiment"] == "neutral"
    assert normalized["confidence"] == 0
    assert normalized["time_horizon"] == "unknown"
    assert "tldr" in normalized
    assert "what_to_watch" in normalized


def test_deep_analysis_normalization_includes_new_keys():
    normalized = _normalize_deep_analysis(
        {
            "article_explainer": "The company raised guidance.",
            "market_implication": "Peers may re-rate higher.",
            "market_direction": "risk-on",
            "ticker_implication": {
                "ticker": "AAPL",
                "has_direct_link": True,
                "relationship": "Guidance changes expected cash flow.",
                "direction": "up",
                "rationale": "Higher guidance improves the earnings outlook.",
                "confidence": 77,
            },
            "overall_sentiment": "invalid",
            "trade_setups": [{"bias": "random"}],
        },
        ticker_label="AAPL",
    )
    assert normalized["briefing_version"] == 3
    assert normalized["article_explainer"] == "The company raised guidance."
    assert normalized["market_implication"] == "Peers may re-rate higher."
    assert normalized["market_direction"] == "risk-on"
    assert normalized["ticker_implication"]["direction"] == "up"
    assert normalized["ticker_implication"]["confidence"] == 77
    assert _is_current_news_briefing(normalized) is True
    assert normalized["overall_sentiment"] == "neutral"
    assert "trade_setups" in normalized
    assert normalized["trade_setups"][0]["bias"] == "observe-only"
    assert "unknowns" in normalized


def test_briefing_version_rejects_legacy_cache_shape():
    agent = NewsTabAgent()
    legacy = {"tldr": "Legacy summary", "overall_sentiment": "bullish"}

    assert agent._has_current_briefing(legacy) is False
    assert _is_current_news_briefing(legacy) is False


def test_briefing_resolves_company_name_to_configured_ticker():
    agent = NewsTabAgent()
    agent.config.setdefault("mappings", {})["news_entity_aliases"] = {
        "MSFT": ["Microsoft", "Microsoft Corp"],
        "BTC-USD": ["Bitcoin", "BTC"],
    }
    market_article = _mk_article(
        "entity-1",
        "Microsoft shares rise as AI software fears fade",
        ticker=None,
    )

    assert agent.infer_briefing_tickers(market_article) == ["MSFT"]


def test_briefing_prefers_explicit_ticker_before_entity_aliases():
    agent = NewsTabAgent()
    agent.config.setdefault("mappings", {})["news_entity_aliases"] = {
        "MSFT": ["Microsoft"],
    }
    article = _mk_article("entity-2", "Microsoft signs cloud partnership", ticker="AAPL")

    assert agent.infer_briefing_tickers(article) == ["AAPL", "MSFT"]


def test_get_all_articles_flat_prefers_ticker_assignment_on_duplicate_id():
    agent = NewsTabAgent()
    agent.clear_cache()
    market_variant = _mk_article("dup-1", "Macro headline", "Reuters", ticker=None)
    ticker_variant = _mk_article("dup-1", "Macro headline", "Reuters", ticker="AAPL")

    # Same article id appears in both buckets; export flatten should keep ticker-rich variant.
    agent._state.market_news = [market_variant]
    agent._state.ticker_news = {"AAPL": [ticker_variant]}
    flat = agent.get_all_articles_flat()

    assert len(flat) == 1
    assert flat[0]["id"] == "dup-1"
    assert flat[0]["ticker"] == "AAPL"


def test_organize_state_assigns_multi_ticker_and_market_fallback():
    agent = NewsTabAgent()
    agent.clear_cache()

    shared = _mk_article(
        "pair-1",
        "Apple and MSFT expand cloud partnership",
        "Reuters",
        ticker="AAPL",
    )
    shared.summary = "AAPL and MSFT announced a multi-year agreement."

    outside = _mk_article(
        "out-1",
        "JMIA launches new logistics hubs",
        "Reuters",
        ticker="JMIA",
    )
    outside.summary = "Expansion update for Jumia operations."

    agent._organize_state([shared, outside], watchlist=["AAPL", "MSFT"])

    assert "AAPL" in agent._state.ticker_news
    assert "MSFT" in agent._state.ticker_news
    assert any(a.id == "pair-1" for a in agent._state.ticker_news["AAPL"])
    assert any(a.id == "pair-1" for a in agent._state.ticker_news["MSFT"])
    assert "JMIA" not in agent._state.ticker_news
    assert any(a.id == "out-1" for a in agent._state.market_news)


def test_get_all_articles_flat_keeps_shared_story_for_each_ticker_bucket():
    agent = NewsTabAgent()
    agent.clear_cache()
    shared = _mk_article("shared-1", "AAPL and MSFT earnings preview", "Reuters", ticker="AAPL")
    shared.summary = "Analysts discuss AAPL and MSFT."

    agent._state.ticker_news = {
        "AAPL": [shared],
        "MSFT": [shared],
    }
    agent._state.breaking_news = [shared]

    flat = agent.get_all_articles_flat()
    rows = [x for x in flat if x["id"] == "shared-1"]
    tickers = sorted({x["ticker"] for x in rows})

    assert tickers == ["AAPL", "MSFT"]


def test_organize_state_does_not_force_source_ticker_when_not_relevant():
    agent = NewsTabAgent()
    agent.clear_cache()
    unrelated = _mk_article(
        "jmia-noise-1",
        "Spotify reports stronger user growth than expected",
        "Reuters",
        ticker="JMIA",
    )
    unrelated.summary = "Audio streaming platform posted better margins."

    agent._organize_state([unrelated], watchlist=["JMIA", "AAPL"])

    assert "JMIA" not in agent._state.ticker_news
    assert any(a.id == "jmia-noise-1" for a in agent._state.market_news)


def test_yahoo_scoped_noise_is_not_trusted_without_entity_match():
    agent = NewsTabAgent()
    agent.clear_cache()
    agent._company_names_cache["AAPL"] = ""
    unrelated = _mk_article(
        "aapl-yahoo-noise",
        "Microsoft expands AI data center spending",
        "Yahoo Finance",
        ticker="AAPL",
    )
    unrelated.summary = "Microsoft announced another infrastructure program."
    unrelated.source_scope = "ticker"
    unrelated.source_provider = "yahoo_finance_search"

    agent._organize_state([unrelated], watchlist=["AAPL", "MSFT"])

    assert "AAPL" not in agent._state.ticker_news
    # Wave 3: company names are populated now, so a headline naming
    # "Microsoft" is evidence for MSFT (it used to fall back to market news
    # because no name was known). The AAPL scope is still not trusted.
    assert any(a.id == unrelated.id for a in agent._state.ticker_news.get("MSFT", []))
    assert unrelated.ticker == "MSFT"


def test_finnhub_company_endpoint_may_use_scoped_fallback():
    agent = NewsTabAgent()
    agent.clear_cache()
    article = _mk_article(
        "finnhub-scoped",
        "Board approves quarterly dividend",
        "Business Wire",
        ticker="AAPL",
    )
    article.source_scope = "ticker"
    article.source_provider = "finnhub_company_news"

    agent._organize_state([article], watchlist=["AAPL"])

    assert any(a.id == article.id for a in agent._state.ticker_news["AAPL"])
