from datetime import datetime, timezone

from core.autonomous_candidates import (
    assess_pre_earnings_candidate,
    build_pre_earnings_candidate_signal,
    build_scanner_candidate_signal,
)


def test_dao_recommendation_eligible_is_not_execution_eligible_below_gap_threshold():
    item = {
        "symbol": "DAO",
        "event_status": "UPCOMING",
        "recommendation_eligible": True,
        "gap_risk": 48,
        "direction_bias_score": -26,
        "direction_label": "SHORT",
        "coverage_score": 77,
        "data_confidence": "MEDIUM",
    }
    cfg = {
        "pre_earnings_min_gap_risk": 65,
        "pre_earnings_min_abs_bias": 20,
        "pre_earnings_min_coverage_score": 50,
        "pre_earnings_min_score": 78.0,
    }

    assessment = assess_pre_earnings_candidate(item, 17.0, cfg)

    assert assessment["eligible"] is False
    assert assessment["first_blocker"]["code"] == "gap_risk_below_minimum"
    assert assessment["first_blocker"]["actual"] == 48
    assert assessment["first_blocker"]["required"] == 65
    assert build_pre_earnings_candidate_signal(item, 17.0, cfg) is None


def test_explicitly_ineligible_recommendation_fails_closed_for_autonomous_entry():
    item = {
        "symbol": "SAFE",
        "event_status": "UPCOMING",
        "recommendation_eligible": False,
        "recommendation_blockers": ["ai_news_refresh_pending"],
        "gap_risk": 90,
        "direction_bias_score": 55,
        "direction_label": "LONG",
        "coverage_score": 90,
        "data_confidence": "HIGH",
    }

    assessment = assess_pre_earnings_candidate(item, 100.0, {})

    assert assessment["eligible"] is False
    assert assessment["first_blocker"]["code"] == "recommendation_ineligible"
    assert build_pre_earnings_candidate_signal(item, 100.0, {}) is None


def test_pre_earnings_direction_calibration_blocks_losing_side():
    item = {
        "symbol": "CAL",
        "event_status": "UPCOMING",
        "recommendation_eligible": True,
        "gap_risk": 90,
        "direction_bias_score": -40,
        "direction_label": "SHORT",
        "coverage_score": 90,
        "data_confidence": "HIGH",
        "direction_track_record": {"n": 392, "hits": 180, "hit_rate": 45.9},
    }
    cfg = {
        "pre_earnings_require_directional_edge": True,
        "pre_earnings_min_directional_sample": 100,
        "pre_earnings_min_directional_hit_rate": 50.0,
    }

    assessment = assess_pre_earnings_candidate(item, 100.0, cfg)

    assert assessment["eligible"] is False
    assert any(
        blocker["code"] == "directional_track_record_below_minimum"
        for blocker in assessment["blockers"]
    )


def test_pre_earnings_canonical_decision_must_match_structured_direction():
    item = {
        "symbol": "DEC",
        "event_status": "UPCOMING",
        "recommendation_eligible": True,
        "gap_risk": 90,
        "direction_bias_score": 40,
        "direction_label": "LONG",
        "coverage_score": 90,
        "data_confidence": "HIGH",
        "pre_earnings_decision": {"available": True, "action": "OPEN_SHORT"},
    }

    assessment = assess_pre_earnings_candidate(
        item, 100.0, {"pre_earnings_require_canonical_decision": True}
    )

    assert assessment["eligible"] is False
    assert assessment["first_blocker"]["code"] == "canonical_direction_mismatch"


def test_bias_49_clears_minimum_20_and_reports_canonical_watch_truthfully():
    item = {
        "symbol": "HPE",
        "event_status": "UPCOMING",
        "recommendation_eligible": True,
        "gap_risk": 90,
        "direction_bias_score": 49,
        "direction_label": "LONG",
        "coverage_score": 93,
        "data_confidence": "HIGH",
        "pre_earnings_decision": {
            "available": True,
            "action": "WATCH",
        },
    }
    cfg = {
        "pre_earnings_min_abs_bias": 20,
        "pre_earnings_min_gap_risk": 65,
        "pre_earnings_min_coverage_score": 50,
        "pre_earnings_min_score": 78,
        "pre_earnings_require_canonical_decision": True,
    }

    assessment = assess_pre_earnings_candidate(item, 100.0, cfg)

    assert assessment["eligible"] is False
    assert assessment["first_blocker"]["code"] == "canonical_decision_not_open"
    assert not any(
        blocker["code"] == "directional_edge_below_minimum"
        for blocker in assessment["blockers"]
    )
    assert assessment["metrics"]["directional_bias_abs"] == 49
    assert assessment["thresholds"]["directional_bias_abs"] == 20


def test_ranking_eligibility_is_distinct_from_current_execution_window():
    item = {
        "symbol": "SNOW",
        "earnings_time_utc": "2026-09-02T20:00:00+00:00",
        "session": "AMC",
        "event_status": "UPCOMING",
        "recommendation_eligible": True,
        "gap_risk": 85,
        "direction_bias_score": 35,
        "direction_label": "LONG",
        "coverage_score": 90,
        "data_confidence": "HIGH",
    }
    now = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)

    ranking = assess_pre_earnings_candidate(item, 100.0, {}, now=now)
    execution = assess_pre_earnings_candidate(
        item, 100.0, {}, require_entry_window=True, now=now
    )

    assert ranking["eligible"] is True
    assert ranking["ranking_eligible"] is True
    assert ranking["executable_now"] is False
    assert ranking["eligibility_scope"] == "ranking"
    assert execution["eligible"] is False
    assert execution["ranking_eligible"] is True
    assert execution["first_blocker"]["code"] == "outside_pre_earnings_window"


def test_scanner_candidate_builds_long_crypto_signal():
    signal = build_scanner_candidate_signal(
        {
            "symbol": "BTCUSDT",
            "price": 65000,
            "change_pct": 6.2,
            "quoteVolume": 250000000,
            "source": "binance",
        },
        {
            "scanner_min_abs_change_pct": 5.0,
            "scanner_min_volume_24h": 1000000,
            "scanner_entry_mode": "momentum",
            "scanner_crypto_stop_pct": 0.03,
            "scanner_rr_ratio": 2.0,
            "scanner_min_score": 78.0,
        },
    )

    assert signal is not None
    assert signal["ticker"] == "BTC-USD"
    assert signal["action"] == "BUY"
    assert signal["autonomous_source"] == "scanner"
    assert signal["strategy_source"] == "SCANNER_PRICE_FIRST_V1"
    assert signal["strategy_status"] == "RESEARCH"
    assert signal["strategy_evidence"]["lifecycle_status"] == "RESEARCH"
    assert signal["average_daily_volume_usd"] == 250000000
    assert signal["sl"] < signal["entry"] < signal["tp"]
    assert signal["risk_reward_ratio"] == 2.0


def test_scanner_candidate_builds_short_stock_signal():
    signal = build_scanner_candidate_signal(
        {
            "symbol": "AAPL",
            "price": 180,
            "change_pct": -7.0,
            "volume": 12000000,
            "source": "yahoo_losers",
        },
        {
            "scanner_min_abs_change_pct": 5.0,
            "scanner_min_volume_24h": 1000000,
            "scanner_entry_mode": "momentum",
            "scanner_stop_pct": 0.025,
            "scanner_rr_ratio": 2.0,
            "scanner_min_score": 78.0,
        },
    )

    assert signal is not None
    assert signal["ticker"] == "AAPL"
    assert signal["action"] == "SHORT"
    assert signal["tp"] < signal["entry"] < signal["sl"]


def test_scanner_candidate_rejects_thin_stock_by_dollar_volume():
    signal = build_scanner_candidate_signal(
        {"symbol": "THIN", "price": 2.0, "change_pct": 9.0, "volume": 1_000_000, "source": "yahoo_gainers"},
        {
            "scanner_min_abs_change_pct": 5.0,
            "scanner_min_volume_24h": 1_000_000,
            "scanner_min_dollar_volume": 10_000_000,
            "scanner_entry_mode": "momentum",
            "scanner_stop_pct": 0.04,
            "scanner_rr_ratio": 1.8,
            "scanner_min_score": 78.0,
        },
    )
    assert signal is None


def test_scanner_candidate_skips_unconfirmed_late_momentum():
    signal = build_scanner_candidate_signal(
        {
            "symbol": "SMMT",
            "price": 28.4,
            "change_pct": 24.0,
            "volume": 18_000_000,
            "source": "yahoo_gainers",
        },
        {
            "scanner_min_abs_change_pct": 5.0,
            "scanner_min_volume_24h": 1_000_000,
            "scanner_entry_mode": "momentum",
            "scanner_max_unconfirmed_abs_change_pct": 12.0,
            "scanner_require_late_momentum_confirmation": True,
            "scanner_min_score": 78.0,
        },
    )

    assert signal is None


def test_scanner_candidate_allows_late_momentum_with_pullback_confirmation():
    signal = build_scanner_candidate_signal(
        {
            "symbol": "SMMT",
            "price": 28.4,
            "change_pct": 24.0,
            "volume": 18_000_000,
            "pullback_pct": 2.4,
            "source": "yahoo_gainers",
        },
        {
            "scanner_min_abs_change_pct": 5.0,
            "scanner_min_volume_24h": 1_000_000,
            "scanner_entry_mode": "momentum",
            "scanner_max_unconfirmed_abs_change_pct": 12.0,
            "scanner_require_late_momentum_confirmation": True,
            "scanner_late_min_pullback_pct": 0.6,
            "scanner_late_max_pullback_pct": 6.0,
            "scanner_min_score": 78.0,
        },
    )

    assert signal is not None
    assert signal["scanner_context"]["late_momentum"] is True
    assert signal["scanner_context"]["late_confirmation"] == ["pullback=2.40%"]


def test_scanner_candidate_allows_late_crypto_with_liquidity_confirmation():
    signal = build_scanner_candidate_signal(
        {
            "symbol": "STORJUSDT",
            "price": 0.125,
            "change_pct": 23.7,
            "quoteVolume": 20_000_000,
            "source": "binance",
        },
        {
            "scanner_min_abs_change_pct": 5.0,
            "scanner_min_volume_24h": 1_000_000,
            "scanner_entry_mode": "momentum",
            "scanner_max_unconfirmed_abs_change_pct": 12.0,
            "scanner_require_late_momentum_confirmation": True,
            "scanner_min_score": 78.0,
        },
    )

    assert signal is not None
    assert signal["ticker"] == "STORJ-USD"
    assert signal["scanner_context"]["late_momentum"] is True
    assert signal["scanner_context"]["late_confirmation"] == ["crypto_liquidity=$20,000,000"]


def _fade_cfg(**over):
    cfg = {
        "scanner_min_abs_change_pct": 5.0,
        "scanner_min_volume_24h": 1_000_000,
        "scanner_crypto_stop_pct": 0.03,
        "scanner_min_score": 78.0,
        "scanner_entry_mode": "fade",
        "scanner_fade_rr_ratio": 1.0,
        "scanner_fade_min_move_pct": 6.0,
        "scanner_fade_max_move_pct": 40.0,
    }
    cfg.update(over)
    return cfg


def test_fade_mode_shorts_an_up_move():
    # Mean-reversion: a +10% over-extension is faded SHORT with a tight 1:1 target.
    signal = build_scanner_candidate_signal(
        {"symbol": "SOLUSDT", "price": 150.0, "change_pct": 10.0, "quoteVolume": 50_000_000, "source": "binance"},
        _fade_cfg(),
    )
    assert signal is not None
    assert signal["action"] == "SHORT"
    assert signal["tp"] < signal["entry"] < signal["sl"]
    assert signal["risk_reward_ratio"] == 1.0


def test_fade_mode_buys_a_down_move():
    signal = build_scanner_candidate_signal(
        {"symbol": "SOLUSDT", "price": 150.0, "change_pct": -10.0, "quoteVolume": 50_000_000, "source": "binance"},
        _fade_cfg(),
    )
    assert signal is not None
    assert signal["action"] == "BUY"
    assert signal["sl"] < signal["entry"] < signal["tp"]


def test_fade_mode_skips_below_band():
    # 4% move is not exhausted enough to fade.
    signal = build_scanner_candidate_signal(
        {"symbol": "SOLUSDT", "price": 150.0, "change_pct": 4.0, "quoteVolume": 50_000_000, "source": "binance"},
        _fade_cfg(),
    )
    assert signal is None


def test_fade_mode_skips_above_band():
    # 45% is a likely genuine trend/news move — do not fade it.
    signal = build_scanner_candidate_signal(
        {"symbol": "SOLUSDT", "price": 150.0, "change_pct": 45.0, "quoteVolume": 50_000_000, "source": "binance"},
        _fade_cfg(),
    )
    assert signal is None


def test_entry_mode_off_disables_scanner_entries():
    signal = build_scanner_candidate_signal(
        {"symbol": "SOLUSDT", "price": 150.0, "change_pct": 10.0, "quoteVolume": 50_000_000, "source": "binance"},
        _fade_cfg(scanner_entry_mode="off"),
    )
    assert signal is None


def test_pre_earnings_candidate_skips_neutral_bias():
    signal = build_pre_earnings_candidate_signal(
        {
            "symbol": "GEHC",
            "gap_risk": 80,
            "direction_bias_score": -2,
            "direction_label": "NEUTRAL",
            "coverage_score": 60,
        },
        70.0,
        {
            "pre_earnings_min_gap_risk": 65,
            "pre_earnings_min_abs_bias": 20,
            "pre_earnings_min_coverage_score": 50,
        },
    )

    assert signal is None


def test_pre_earnings_candidate_builds_directional_short_signal():
    signal = build_pre_earnings_candidate_signal(
        {
            "symbol": "MSFT",
            "earnings_time_utc": "2026-05-01T20:00:00+00:00",
            "session": "AMC",
            "gap_risk": 82,
            "gap_risk_level": "high",
            "direction_bias_score": -35,
            "direction_label": "SHORT",
            "coverage_score": 80,
            "coverage_label": "HIGH",
            "data_confidence": "MEDIUM",
            "expected_move_pct": 10,
            "avg_volume": 2_000_000,
            "top_news": [{"title": "Microsoft faces margin pressure before earnings"}],
        },
        100.0,
        {
            "pre_earnings_min_gap_risk": 65,
            "pre_earnings_min_abs_bias": 20,
            "pre_earnings_min_coverage_score": 50,
            "pre_earnings_stop_pct_min": 0.025,
            "pre_earnings_stop_pct_max": 0.08,
            "pre_earnings_rr_ratio": 1.8,
            "pre_earnings_min_score": 78.0,
        },
    )

    assert signal is not None
    assert signal["ticker"] == "MSFT"
    assert signal["action"] == "SHORT"
    assert signal["autonomous_source"] == "pre_earnings"
    assert signal["strategy_status"] == "RESEARCH"
    assert signal["average_daily_volume_usd"] == 200_000_000
    assert signal["tp"] < signal["entry"] < signal["sl"]
    assert signal["news_headlines"] == ["Microsoft faces margin pressure before earnings"]


def test_scanner_candidate_propagates_news_context_for_ai_review():
    """A1: the scanner autonomous path must attach news_score/news_sentiment/
    top_headlines so _compact_signal (ollama_decision) can pass them to the AI
    reviewer. Pre-fix this path carried ZERO news -- the model decided news-blind
    on scanner momentum candidates (contra the AI-integration directive)."""
    signal = build_scanner_candidate_signal(
        {
            "symbol": "AAPL",
            "price": 180,
            "change_pct": 7.0,
            "volume": 12000000,
            "source": "yahoo_gainers",
            "news_score": 74.0,
            "news_sentiment_trend": "bullish",
            "top_news": [
                {"title": "AAPL unveils new AI chip line"},
                {"title": "Analysts upgrade AAPL price target"},
            ],
        },
        {
            "scanner_min_abs_change_pct": 5.0,
            "scanner_min_volume_24h": 1000000,
            "scanner_stop_pct": 0.025,
            "scanner_rr_ratio": 2.0,
            "scanner_min_score": 78.0,
        },
    )
    assert signal is not None
    # News pulse context reaches the candidate signal (A1 additive keys).
    assert signal["news_score"] == 74.0
    assert signal["news_sentiment"] == "bullish"
    assert signal["news_headlines"] == ["AAPL unveils new AI chip line", "Analysts upgrade AAPL price target"]
    assert signal["top_headlines"] == ["AAPL unveils new AI chip line", "Analysts upgrade AAPL price target"]


def test_scanner_candidate_news_defaults_when_item_lacks_news():
    """A1: a scanner item with no news still gets safe defaults (score 0.0,
    sentiment 'unknown', empty headlines) -- never raises, never synthesizes
    fake headlines the model would treat as real signal."""
    signal = build_scanner_candidate_signal(
        {
            "symbol": "AAPL",
            "price": 180,
            "change_pct": 7.0,
            "volume": 12000000,
            "source": "yahoo_gainers",
        },
        {
            "scanner_min_abs_change_pct": 5.0,
            "scanner_min_volume_24h": 1000000,
            "scanner_stop_pct": 0.025,
            "scanner_rr_ratio": 2.0,
            "scanner_min_score": 78.0,
        },
    )
    assert signal is not None
    assert signal["news_score"] == 0.0
    assert signal["news_sentiment"] == "unknown"
    assert signal["news_headlines"] == []
    assert signal["top_headlines"] == []


def test_pre_earnings_candidate_propagates_news_pulse_for_ai_review():
    """A2: the pre-earnings path must attach the numeric news pulse score +
    sentiment trend + top_headlines (not just headline text), so the AI reviewer
    sees the full news set -- pre-fix it carried headline text only."""
    signal = build_pre_earnings_candidate_signal(
        {
            "symbol": "MSFT",
            "earnings_time_utc": "2026-05-01T20:00:00+00:00",
            "session": "AMC",
            "gap_risk": 82,
            "gap_risk_level": "high",
            "direction_bias_score": -35,
            "direction_label": "SHORT",
            "coverage_score": 80,
            "coverage_label": "HIGH",
            "data_confidence": "MEDIUM",
            "expected_move_pct": 10,
            "top_news": [{"title": "Microsoft faces margin pressure before earnings"}],
            "news_score": 69.0,
            "news_sentiment_trend": "bearish",
        },
        100.0,
        {
            "pre_earnings_min_gap_risk": 65,
            "pre_earnings_min_abs_bias": 20,
            "pre_earnings_min_coverage_score": 50,
            "pre_earnings_stop_pct_min": 0.025,
            "pre_earnings_stop_pct_max": 0.08,
            "pre_earnings_rr_ratio": 1.8,
            "pre_earnings_min_score": 78.0,
        },
    )
    assert signal is not None
    # A2 additive keys: numeric pulse + sentiment + top_headlines whitelist key.
    assert signal["news_score"] == 69.0
    assert signal["news_sentiment"] == "bearish"
    assert signal["top_headlines"] == ["Microsoft faces margin pressure before earnings"]
