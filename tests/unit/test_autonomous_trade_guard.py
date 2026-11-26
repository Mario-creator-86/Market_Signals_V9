import pytest

from core.autonomous_trade_guard import (
    attach_news_context,
    evaluate_autonomous_news,
    evaluate_autonomous_performance,
    is_autonomous_candidate,
    record_autonomous_news_evidence,
)


def _signal(direction="SHORT", entry_strategy="fade"):
    return {
        "ticker": "AAA-USD",
        "action": direction,
        "strategy_type": f"SCANNER_AI_{'LONG' if direction == 'BUY' else 'SHORT'}",
        "entry_strategy": entry_strategy,
        "autonomous_source": "scanner",
    }


def _position(pnl, *, direction="SHORT", entry_strategy="fade", closed_at="2026-07-01T10:00:00+00:00"):
    return {
        "status": "CLOSED",
        "ticker": "AAA-USD",
        "strategy_type": f"SCANNER_AI_{direction}",
        "direction": direction,
        "net_pnl": pnl,
        "pnl_pct_notional": pnl / 10.0,
        "entry_price": 100.0,
        "exit_price": 101.0,
        "entry_price_source": "exchange_quote",
        "exit_price_source": "exchange_quote",
        "entry_price_quality": {"level": "VERIFIED", "is_valid": True},
        "exit_price_quality": {"level": "VERIFIED", "is_valid": True},
        "closed_at": closed_at,
        "decision_context": {"entry_strategy": entry_strategy},
    }


def _config(**overrides):
    cfg = {
        "autonomous_profitability_gate": {
            "enabled": True,
            "min_sample": 3,
            "min_profit_factor": 1.2,
            "min_expectancy_usd": 0.0,
            "max_drawdown_pct": 25.0,
        },
        "autonomous_news_gate": {
            "enabled": True,
            "require_fresh_context": True,
            "min_articles_24h": 1,
            "require_source_urls": False,
            "require_ai_analysis": False,
            "block_conflicting_sentiment": True,
        },
    }
    cfg.update(overrides)
    return cfg


def test_profitability_gate_rejects_insufficient_exact_strategy_history():
    signal = _signal()
    positions = [
        _position(10.0),
        _position(10.0, entry_strategy="momentum"),  # Legacy regime must not validate fade.
        _position(10.0, direction="LONG"),           # Opposite direction must not validate short.
    ]

    result = evaluate_autonomous_performance(signal, positions, _config())

    assert result["allowed"] is False
    assert result["status"] == "insufficient_evidence"
    assert result["metrics"]["sample_size"] == 1


def test_strategy_identity_is_a_defence_in_depth_autonomous_marker():
    assert is_autonomous_candidate({"strategy_type": "SCANNER_AI_LONG"}) is True
    assert is_autonomous_candidate({"strategy_type": "PRE_EARNINGS_AI_SHORT"}) is True
    assert is_autonomous_candidate({"strategy_type": "MANUAL"}) is False


def test_profitability_gate_rejects_negative_net_edge():
    signal = _signal()
    positions = [_position(10.0), _position(-20.0), _position(-10.0)]

    result = evaluate_autonomous_performance(signal, positions, _config())

    assert result["allowed"] is False
    assert result["status"] == "negative_edge"
    assert result["metrics"]["profit_factor"] == pytest.approx(1 / 3)
    assert result["metrics"]["expectancy_usd"] < 0


def test_profitability_gate_allows_only_validated_positive_history():
    signal = _signal()
    positions = [_position(20.0), _position(-10.0), _position(15.0)]

    result = evaluate_autonomous_performance(signal, positions, _config())

    assert result["allowed"] is True
    assert result["status"] == "validated"
    assert result["metrics"]["profit_factor"] == 3.5
    assert result["metrics"]["expectancy_usd"] > 0


def test_profitability_gate_excludes_unverified_execution_history():
    positions = [_position(20.0), _position(-10.0), _position(15.0)]
    for position in positions:
        position.pop("exit_price_quality")

    result = evaluate_autonomous_performance(_signal(), positions, _config())

    assert result["allowed"] is False
    assert result["status"] == "insufficient_evidence"
    assert result["metrics"]["sample_size"] == 0
    assert result["metrics"]["excluded_unverified_execution"] == 3


def test_news_gate_blocks_fresh_directional_conflict_and_keeps_headlines():
    pulse = {
        "score": 82,
        "sentiment_trend": "bullish",
        "24h": {"article_count": 2},
        "7d": {"article_count": 4},
        "top_headlines": [{"title": "Verified catalyst", "impact": "high"}],
    }
    signal = _signal(direction="SHORT")

    result = evaluate_autonomous_news(signal, _config(), news_pulse=pulse)
    attach_news_context(signal, result)

    assert result["allowed"] is False
    assert result["status"] == "direction_conflict"
    assert signal["news_sentiment"] == "bullish"
    assert signal["news_headlines"] == ["Verified catalyst"]


def test_news_gate_blocks_absent_fresh_evidence_without_inventing_sentiment():
    pulse = {
        "score": 0,
        "sentiment_trend": "unknown",
        "24h": {"article_count": 0},
        "7d": {"article_count": 0},
        "top_headlines": [],
    }

    result = evaluate_autonomous_news(_signal(direction="BUY"), _config(), news_pulse=pulse)

    assert result["allowed"] is False
    assert result["status"] == "insufficient_evidence"
    assert "found 0" in result["reason"]


def test_news_gate_does_not_require_news_for_crypto_when_configured_contextual():
    signal = _signal(direction="BUY")
    signal.update({"ticker": "BTC-USD", "asset_class": "CRYPTO_FUTURES"})
    pulse = {"24h": {"article_count": 0}, "7d": {"article_count": 0}, "top_headlines": []}

    result = evaluate_autonomous_news(
        signal,
        _config(autonomous_news_gate={
            "enabled": True,
            "require_fresh_context": True,
            "major_crypto_bases_requiring_news": [],
        }),
        news_pulse=pulse,
    )

    assert result["allowed"] is True
    assert result["status"] == "news_context_missing_price_first"
    assert result["crypto_policy"]["base_asset"] == "BTC"
    assert result["crypto_policy"]["is_major_crypto"] is False
    assert result["context"]["asset_policy"] == "crypto_price_first_news_contextual"


def test_contextual_crypto_still_blocks_verified_directional_conflict():
    signal = _signal(direction="BUY")
    signal.update({"ticker": "BTC-USD", "asset_class": "CRYPTO_FUTURES"})
    pulse = {
        "sentiment_trend": "bearish",
        "24h": {"article_count": 1},
        "7d": {"article_count": 1},
        "top_headlines": [{"title": "Verified bearish catalyst", "source": "Example", "url": "https://example.test/btc"}],
    }

    result = evaluate_autonomous_news(
        signal,
        _config(autonomous_news_gate={
            "enabled": True,
            "require_fresh_context": True,
            "min_articles_24h": 1,
            "require_source_urls": False,
            "require_ai_analysis": False,
            "block_conflicting_sentiment": True,
            "major_crypto_bases_requiring_news": [],
        }),
        news_pulse=pulse,
    )

    assert result["allowed"] is False
    assert result["status"] == "direction_conflict"


def test_news_gate_keeps_news_required_for_major_crypto_and_equities():
    pulse = {"24h": {"article_count": 0}, "7d": {"article_count": 0}, "top_headlines": []}
    btc = _signal(direction="BUY")
    btc.update({"ticker": "BTC-USD", "asset_class": "CRYPTO_FUTURES"})
    stock = _signal(direction="BUY")
    stock.update({"ticker": "DODO-USD", "asset_class": "US_STOCKS"})

    btc_result = evaluate_autonomous_news(btc, _config(), news_pulse=pulse)
    stock_result = evaluate_autonomous_news(stock, _config(), news_pulse=pulse)

    assert btc_result["allowed"] is False
    assert btc_result["status"] == "insufficient_evidence"
    assert btc_result["crypto_policy"]["is_major_crypto"] is True
    assert stock_result["allowed"] is False
    assert stock_result["status"] == "insufficient_evidence"
    assert stock_result["crypto_policy"]["is_crypto"] is False


def test_news_gate_requires_source_backed_ai_analysis_when_enabled():
    pulse = {
        "score": 60,
        "sentiment_trend": "bullish",
        "ai_sentiment_trend": "bullish",
        "24h": {"article_count": 1, "source_backed_count": 1, "ai_analyzed_count": 0},
        "7d": {"article_count": 1},
        "top_headlines": [{"title": "Primary source", "url": "https://example.test/story", "source": "Example"}],
    }
    cfg = _config(autonomous_news_gate={
        "enabled": True,
        "require_fresh_context": True,
        "min_articles_24h": 1,
        "require_source_urls": True,
        "require_ai_analysis": True,
        "min_ai_analyzed_articles_24h": 1,
        "block_conflicting_sentiment": True,
    })

    result = evaluate_autonomous_news(_signal(direction="BUY"), cfg, news_pulse=pulse)

    assert result["allowed"] is False
    assert result["status"] == "ai_analysis_unavailable"


def test_news_gate_uses_ai_sentiment_and_archive_snapshot(monkeypatch):
    pulse = {
        "generated_at": "2026-07-14T10:00:00+00:00",
        "score": 87,
        "sentiment_trend": "neutral",
        "ai_sentiment_trend": "bearish",
        "24h": {"article_count": 1, "source_backed_count": 1, "ai_analyzed_count": 1, "source_backed_ai_analyzed_count": 1},
        "7d": {"article_count": 1},
        "top_headlines": [{
            "article_id": "article-1",
            "title": "Verified catalyst",
            "time": "2026-07-14T09:30:00+00:00",
            "url": "https://example.test/story",
            "source": "Example News",
            "impact": "high",
            "sentiment": "bearish",
            "ai_micro_summary": {"overall_sentiment": "bearish", "rationale": "Source-backed"},
        }],
    }
    cfg = _config(autonomous_news_gate={
        "enabled": True,
        "require_fresh_context": True,
        "min_articles_24h": 1,
        "require_source_urls": True,
        "require_ai_analysis": True,
        "min_ai_analyzed_articles_24h": 1,
        "block_conflicting_sentiment": True,
    })
    decision = evaluate_autonomous_news(_signal(direction="BUY"), cfg, news_pulse=pulse)
    records = []
    monkeypatch.setattr("core.execution_archive.append_execution", records.append)

    record = record_autonomous_news_evidence(_signal(direction="BUY"), decision, evaluation_stage="pre_execution_recheck")

    assert decision["status"] == "direction_conflict"
    assert record is records[0]
    assert record["payload"]["prospective_only"] is True
    headline = record["payload"]["news_context"]["headlines"][0]
    assert headline["url"] == "https://example.test/story"
    assert headline["ai_micro_summary"]["overall_sentiment"] == "bearish"


def test_news_gate_rejects_ai_analysis_not_linked_to_a_source():
    pulse = {
        "score": 70,
        "sentiment_trend": "bullish",
        "ai_sentiment_trend": "bullish",
        "24h": {
            "article_count": 2,
            "source_backed_count": 1,
            "ai_analyzed_count": 1,
            "source_backed_ai_analyzed_count": 0,
        },
        "7d": {"article_count": 2},
        "top_headlines": [
            {"title": "Published item", "time": "2026-07-14T09:00:00+00:00", "url": "https://example.test/story", "source": "Example"},
            {"title": "Unattributed AI item", "ai_micro_summary": {"overall_sentiment": "bullish"}},
        ],
    }
    cfg = _config(autonomous_news_gate={
        "enabled": True,
        "require_fresh_context": True,
        "min_articles_24h": 1,
        "require_source_urls": True,
        "require_ai_analysis": True,
        "min_ai_analyzed_articles_24h": 1,
        "require_source_backed_ai_analysis": True,
        "min_source_backed_ai_analyzed_articles_24h": 1,
        "block_conflicting_sentiment": True,
    })

    result = evaluate_autonomous_news(_signal(direction="BUY"), cfg, news_pulse=pulse)

    assert result["allowed"] is False
    assert result["status"] == "ai_analysis_unverifiable"
