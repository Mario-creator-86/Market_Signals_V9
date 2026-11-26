from __future__ import annotations

from core.strategy_v2.confluence import score_confluence


def _bullish_context():
    return {
        "structure_trend": "BULLISH",
        "structure_event": "CHOCH",
        "structure_event_direction": "BULLISH",
        "structure_last_high_label": "HH",
        "structure_last_low_label": "HL",
        "liquidity_last_event": "SWEEP_RECLAIM",
        "liquidity_last_event_direction": "BULLISH",
        "liquidity_event_age_bars": 1,
        "chart_pattern": "DOUBLE_BOTTOM",
        "chart_pattern_status": "CONFIRMED",
        "chart_pattern_direction": "BULLISH",
        "volume_ratio": 1.5,
        "volume_flow_direction": "BULLISH",
        "volume_last_event_direction": "BULLISH",
        "volume_event_age_bars": 1,
        "price_acceptance_direction": "BULLISH",
        "price_acceptance_age_bars": 1,
    }


def test_directional_confluence_is_high_for_aligned_causal_facts():
    result = score_confluence(_bullish_context(), "LONG")
    assert result["score"] >= 80
    assert result["grade"] == "A"
    assert result["coverage"] == 1.0
    assert "price_accepted_beyond_level" in result["reasons"]


def test_directional_confluence_penalizes_opposing_price_acceptance():
    context = _bullish_context()
    context["price_acceptance_direction"] = "BEARISH"
    result = score_confluence(context, "LONG")
    assert result["score"] < score_confluence(_bullish_context(), "LONG")["score"]
    assert "price_acceptance" in result["conflicts"]


def test_missing_ohlcv_facts_are_explicitly_neutral_not_confirmation():
    result = score_confluence({}, "LONG")
    assert result["score"] == 50.0
    assert result["coverage"] == 0.0
