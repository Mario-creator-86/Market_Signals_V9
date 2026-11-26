"""
tests/unit/test_hotlist_scoring.py
====================================
Unit tests for core/hotlist_scoring.py

All functions are pure deterministic — no I/O, no network.
Tests cover:
  - _momentum_score variants
  - compute_news_score
  - _technical_alignment_score
  - _regime_alignment_score
  - _earnings_time_weight + _earnings_time_factor
  - compute_earnings_edge_score — key scenarios
  - compute_hotlist_composite_metrics
  - apply_hotlist_defaults — validators + setdefault logic
  - sort_hotlist_by_priority — ordering
  - hotlist_sort_key — stability
"""

import sys
import os
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

from core.hotlist_scoring import (
    _momentum_score,
    _technical_alignment_score,
    _regime_alignment_score,
    _earnings_time_weight,
    _earnings_time_factor,
    compute_news_score,
    compute_earnings_edge_score,
    compute_hotlist_composite_metrics,
    apply_hotlist_defaults,
    sort_hotlist_by_priority,
    hotlist_sort_key,
    default_news_signals,
    ALLOWED_PLAYS,
    ALLOWED_SETUPS,
    ALLOWED_RISK_LEVELS,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. _momentum_score
# ═══════════════════════════════════════════════════════════════════════════════

class TestMomentumScore:
    def test_both_none_gives_50(self):
        score = _momentum_score({})
        assert score == pytest.approx(50.0)

    def test_only_m1m_fills_m1w(self):
        s = _momentum_score({"momentum_1w": None, "momentum_1m": 5.0})
        assert 50.0 < s < 100.0  # positive momentum → above 50

    def test_only_m1w_fills_m1m(self):
        s = _momentum_score({"momentum_1w": 5.0, "momentum_1m": None})
        assert 50.0 < s < 100.0

    def test_strong_positive_momentum(self):
        s = _momentum_score({"momentum_1w": 10.0, "momentum_1m": 8.0})
        assert s > 50.0

    def test_strong_negative_momentum(self):
        s = _momentum_score({"momentum_1w": -10.0, "momentum_1m": -8.0})
        assert s < 50.0

    def test_clamped_to_0_100(self):
        s = _momentum_score({"momentum_1w": 1000.0, "momentum_1m": 1000.0})
        assert 0.0 <= s <= 100.0

    def test_pct_string_parsed(self):
        s = _momentum_score({"momentum_1w": "5%", "momentum_1m": "3%"})
        assert s > 50.0


# ═══════════════════════════════════════════════════════════════════════════════
# 2. compute_news_score
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputeNewsScore:
    def test_empty_signals_returns_50(self):
        score = compute_news_score(None)
        assert score == pytest.approx(50.0, abs=1.0)

    def test_positive_catalyst_increases_score(self):
        signals = default_news_signals()
        signals["news_catalyst_score"] = 5
        score = compute_news_score(signals)
        assert score > 50.0

    def test_negative_risk_decreases_score(self):
        signals = default_news_signals()
        signals["negative_risk_score"] = 5
        score = compute_news_score(signals)
        assert score < 50.0

    def test_bullish_trend_boosts_score(self):
        signals = default_news_signals()
        signals["sentiment_trend"] = "bullish"
        score = compute_news_score(signals)
        assert score > 50.0

    def test_bearish_trend_reduces_score(self):
        signals = default_news_signals()
        signals["sentiment_trend"] = "bearish"
        score = compute_news_score(signals)
        assert score < 50.0

    def test_clamped_to_0_100(self):
        signals = default_news_signals()
        signals["news_catalyst_score"] = 100
        signals["negative_risk_score"] = 0
        signals["sentiment_trend"] = "bullish"
        signals["counts"]["positive"] = 100
        assert 0.0 <= compute_news_score(signals) <= 100.0

    def test_risk_events_decrease_score(self):
        signals = default_news_signals()
        signals["counts"]["risk_events"] = 3
        score = compute_news_score(signals)
        assert score < 50.0


# ═══════════════════════════════════════════════════════════════════════════════
# 3. _technical_alignment_score
# ═══════════════════════════════════════════════════════════════════════════════

class TestTechnicalAlignmentScore:
    def test_all_bull_high_adx_gives_high_score(self):
        item = {"trend_1d": "BULL", "trend_4h": "BULL", "trend_1h": "BULL", "rsi": 55.0}
        mkt = {"adx": 40.0}
        score = _technical_alignment_score(item, mkt)
        assert score > 70.0

    def test_all_bear_gives_low_score(self):
        item = {"trend_1d": "BEAR", "trend_4h": "BEAR", "trend_1h": "BEAR"}
        score = _technical_alignment_score(item, {})
        assert score < 50.0

    def test_extreme_rsi_penalized(self):
        item = {"rsi": 85.0}
        score_extreme = _technical_alignment_score(item, {})
        item2 = {"rsi": 55.0}
        score_normal = _technical_alignment_score(item2, {})
        assert score_extreme < score_normal

    def test_low_adx_penalizes(self):
        item = {}
        score_low = _technical_alignment_score(item, {"adx": 10.0})
        score_high = _technical_alignment_score(item, {"adx": 35.0})
        assert score_low < score_high

    def test_clamped_to_0_100(self):
        item = {"trend_1d": "BULL", "trend_4h": "BULL", "trend_1h": "BULL", "rsi": 50.0}
        assert 0.0 <= _technical_alignment_score(item, {"adx": 50.0}) <= 100.0


# ═══════════════════════════════════════════════════════════════════════════════
# 4. _earnings_time_weight + _earnings_time_factor
# ═══════════════════════════════════════════════════════════════════════════════

class TestEarningsTiming:
    def test_earnings_today_max_weight(self):
        assert _earnings_time_weight(0) == 100.0

    def test_earnings_tomorrow_high_weight(self):
        assert _earnings_time_weight(1) == 90.0

    def test_earnings_far_future_low_weight(self):
        assert _earnings_time_weight(30) == 12.0

    def test_unknown_earnings_date(self):
        assert _earnings_time_weight(-1) == 12.0

    def test_bmo_factor_gt_amc(self):
        assert _earnings_time_factor("BMO") > _earnings_time_factor("AMC")

    def test_unknown_when_partial_factor(self):
        assert _earnings_time_factor("unknown") == pytest.approx(0.9)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. compute_earnings_edge_score
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputeEarningsEdgeScore:
    def _news(self, pos=0, neg=0, catalyst=0, risk_score=0, trend="unknown"):
        ns = default_news_signals()
        ns["counts"]["positive"] = pos
        ns["counts"]["negative"] = neg
        ns["news_catalyst_score"] = catalyst
        ns["negative_risk_score"] = risk_score
        ns["sentiment_trend"] = trend
        return ns

    def test_score_in_0_100(self):
        result = compute_earnings_edge_score({}, self._news(), {})
        assert 0 <= result["earnings_edge_score"] <= 100

    def test_setup_a_for_high_score(self):
        # Maximize score
        item = {
            "earnings_days": 1,
            "trend_1d": "BULL", "trend_4h": "BULL",
            "momentum_1w": 5.0, "momentum_1m": 3.0,
            "avg_earnings_move_8q": 10.0,
            "atr_percentile": 70.0,
        }
        ns = self._news(pos=10, catalyst=5, trend="bullish")
        result = compute_earnings_edge_score(item, ns, {"adx": 40.0})
        assert result["earnings_setup"] in {"A", "B"}  # high score

    def test_play_avoid_for_high_risk(self):
        item = {
            "earnings_days": 2,
            "trend_1d": "BEAR", "trend_4h": "BEAR",
            "momentum_1w": -10.0, "momentum_1m": -8.0,
        }
        ns = self._news(neg=20, risk_score=15, trend="bearish")
        ns["counts"]["risk_events"] = 3
        result = compute_earnings_edge_score(item, ns, {"adx": 10.0})
        assert result["earnings_play"] == "AVOID"
        assert result["risk_level"] in {"high", "med"}

    def test_returns_all_required_keys(self):
        result = compute_earnings_edge_score({}, self._news(), {})
        for key in ["earnings_edge_score", "earnings_setup", "earnings_play",
                    "earnings_risk_flags", "risk_level", "top_risk"]:
            assert key in result

    def test_earnings_flags_are_unique(self):
        item = {"earnings_days": -1}  # unknown date adds flag
        result = compute_earnings_edge_score(item, self._news(), {})
        flags = result["earnings_risk_flags"]
        assert len(flags) == len(set(flags)), "Flags must be unique"

    def test_allowed_play_values(self):
        for ed in [-1, 0, 1, 3, 7, 20]:
            item = {"earnings_days": ed}
            result = compute_earnings_edge_score(item, self._news(), {})
            assert result["earnings_play"] in ALLOWED_PLAYS

    def test_allowed_setup_values(self):
        result = compute_earnings_edge_score({}, self._news(), {})
        assert result["earnings_setup"] in ALLOWED_SETUPS

    def test_allowed_risk_levels(self):
        result = compute_earnings_edge_score({}, self._news(), {})
        assert result["risk_level"] in ALLOWED_RISK_LEVELS


# ═══════════════════════════════════════════════════════════════════════════════
# 6. compute_hotlist_composite_metrics
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputeHotlistCompositeMetrics:
    def test_returns_all_keys(self):
        result = compute_hotlist_composite_metrics({})
        for key in ["momentum_score", "news_score", "technical_alignment",
                    "regime_alignment", "overall_score", "priority_score",
                    "confidence_score", "risk_penalty", "time_weight", "earnings_time_factor"]:
            assert key in result

    def test_overall_score_in_0_100(self):
        result = compute_hotlist_composite_metrics({"earnings_edge_score": 75})
        assert 0.0 <= result["overall_score"] <= 100.0

    def test_high_risk_gets_penalty(self):
        r1 = compute_hotlist_composite_metrics({"risk_level": "high", "earnings_days": 1})
        r2 = compute_hotlist_composite_metrics({"risk_level": "low", "earnings_days": 1})
        assert r1["priority_score"] < r2["priority_score"]

    def test_earnings_today_boosts_priority(self):
        r_today = compute_hotlist_composite_metrics({"earnings_days": 0})
        r_far = compute_hotlist_composite_metrics({"earnings_days": 30})
        assert r_today["priority_score"] > r_far["priority_score"]


# ═══════════════════════════════════════════════════════════════════════════════
# 7. apply_hotlist_defaults
# ═══════════════════════════════════════════════════════════════════════════════

class TestApplyHotlistDefaults:
    def test_empty_item_gets_all_defaults(self):
        result = apply_hotlist_defaults({})
        assert result["earnings_edge_score"] == 50
        assert result["earnings_play"] == "WAIT"
        assert result["earnings_setup"] == "C"
        assert result["risk_level"] == "med"

    def test_invalid_play_corrected(self):
        result = apply_hotlist_defaults({"earnings_play": "INVALID"})
        assert result["earnings_play"] == "WAIT"

    def test_valid_play_preserved(self):
        result = apply_hotlist_defaults({"earnings_play": "PLAY_PRE"})
        assert result["earnings_play"] == "PLAY_PRE"

    def test_invalid_setup_corrected(self):
        result = apply_hotlist_defaults({"earnings_setup": "X"})
        assert result["earnings_setup"] == "C"

    def test_edge_score_clamped_to_0_100(self):
        r1 = apply_hotlist_defaults({"earnings_edge_score": -50})
        r2 = apply_hotlist_defaults({"earnings_edge_score": 999})
        assert r1["earnings_edge_score"] == 0
        assert r2["earnings_edge_score"] == 100

    def test_atr_percentile_set_to_none_when_missing(self):
        result = apply_hotlist_defaults({})
        assert result["atr_percentile"] is None
        assert result["ATR_percentile"] is None

    def test_atr_percentile_clamped_to_0_100(self):
        result = apply_hotlist_defaults({"atr_percentile": 150.0})
        assert result["atr_percentile"] == pytest.approx(100.0)

    def test_momentum_score_defaults_to_50(self):
        result = apply_hotlist_defaults({})
        assert result["momentum_score"] == pytest.approx(50.0)

    def test_news_signals_merged_with_defaults(self):
        result = apply_hotlist_defaults({"news_signals": {"sentiment_trend": "bullish"}})
        ns = result["news_signals"]
        assert ns["sentiment_trend"] == "bullish"
        assert "counts" in ns

    def test_expected_move_vs_avg_none_when_missing(self):
        result = apply_hotlist_defaults({})
        emv = result["expected_move_vs_avg"]
        assert emv["expected_move_pct"] is None
        assert emv["avg_move_8q"] is None
        assert emv["edge_pct"] is None

    def test_no_mutation_of_original_item(self):
        original = {"earnings_edge_score": 80}
        apply_hotlist_defaults(original)
        assert list(original.keys()) == ["earnings_edge_score"]


# ═══════════════════════════════════════════════════════════════════════════════
# 8. sort_hotlist_by_priority + hotlist_sort_key
# ═══════════════════════════════════════════════════════════════════════════════

class TestSortHotlist:
    def test_empty_list(self):
        assert sort_hotlist_by_priority([]) == []

    def test_sorts_by_priority_score_desc(self):
        items = [
            {"priority_score": 10.0, "overall_score": 50.0, "earnings_edge_score": 50, "ticker": "B"},
            {"priority_score": 30.0, "overall_score": 80.0, "earnings_edge_score": 70, "ticker": "A"},
        ]
        result = sort_hotlist_by_priority(items)
        assert result[0]["ticker"] == "A"

    def test_filters_non_dict_items(self):
        result = sort_hotlist_by_priority([None, "bad", {"ticker": "OK"}])
        assert len(result) == 1
        assert result[0]["ticker"] == "OK"

    def test_sort_key_prefers_high_priority(self):
        k_high = hotlist_sort_key({"priority_score": 100.0, "overall_score": 90.0, "earnings_edge_score": 80, "ticker": "A"})
        k_low = hotlist_sort_key({"priority_score": 10.0, "overall_score": 20.0, "earnings_edge_score": 30, "ticker": "B"})
        assert k_high < k_low  # lower sort tuple = higher priority

    def test_sort_by_edge_when_priority_equal(self):
        # With same priority_score (0.0 default), tiebreak is by overall_score then edge_score
        items = [
            {"ticker": "A", "earnings_days": 5, "earnings_edge_score": 80, "priority_score": 0.0},
            {"ticker": "B", "earnings_days": 1, "earnings_edge_score": 60, "priority_score": 0.0},
        ]
        result = sort_hotlist_by_priority(items)
        # Same priority=0 → tiebreak: overall_score (both 0.0) → edge desc → A (80) before B (60)
        assert result[0]["ticker"] == "A"
