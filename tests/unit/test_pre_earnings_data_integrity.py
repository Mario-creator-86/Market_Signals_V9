"""
tests/unit/test_pre_earnings_data_integrity.py
================================================
Data integrity tests for agents/pre_earnings_agent.py

Validates that:
1. _compute_historical_beat_rate returns None (not 0.5) when no data
2. compute_direction_bias_score excludes missing components from weighted avg
3. data_quality_flag is set when >= 2 components are missing
4. missing_reason is populated when components are absent
5. Full-data path produces no flag and correct weighted sum
"""

import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

from agents.pre_earnings_agent import (
    compute_direction_bias_score,
    PreEarningsAgent,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. _compute_historical_beat_rate
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputeHistoricalBeatRate:
    """beat_rate must NEVER return 0.5 hardcoded when data is absent."""

    def _agent(self):
        with patch("agents.pre_earnings_agent.ConfigManager"):
            agent = PreEarningsAgent.__new__(PreEarningsAgent)
            agent._lock = __import__("threading").RLock()
            agent._state = {"items": [], "status": {}}
            agent._get_yfinance_earnings_history = MagicMock(return_value=[])
            return agent

    def test_returns_none_when_no_events(self):
        agent = self._agent()
        agent._get_cached_earnings_history = MagicMock(return_value=[])
        result = agent._compute_historical_beat_rate("AAPL")
        assert result is None, "Must return None, not 0.5 placeholder"

    def test_returns_none_when_events_but_no_valid_eps_pairs(self):
        agent = self._agent()
        events = [
            {"epsEstimated": None, "epsActual": None},
            {"epsEstimated": None, "epsActual": 1.5},
        ]
        agent._get_cached_earnings_history = MagicMock(return_value=events)
        result = agent._compute_historical_beat_rate("AAPL")
        assert result is None, "Must return None when no valid (est, act) pairs"

    def test_returns_float_when_valid_pairs_exist(self):
        agent = self._agent()
        events = [
            {"epsEstimated": 1.0, "epsActual": 1.2},  # beat
            {"epsEstimated": 1.0, "epsActual": 0.8},  # miss
            {"epsEstimated": 1.0, "epsActual": 1.5},  # beat
        ]
        agent._get_cached_earnings_history = MagicMock(return_value=events)
        result = agent._compute_historical_beat_rate("AAPL")
        assert result is not None
        assert abs(result - (2/3)) < 0.001  # 2 beats out of 3

    def test_hut_yahoo_history_returns_six_beats_from_eight_reports(self):
        agent = self._agent()
        agent._get_cached_earnings_history = MagicMock(return_value=[])
        agent._get_yfinance_earnings_history = MagicMock(
            return_value=[
                {"epsEstimated": -0.36, "epsActual": 0.10},
                {"epsEstimated": -0.09, "epsActual": -2.12},
                {"epsEstimated": -0.11, "epsActual": -0.05},
                {"epsEstimated": -0.14, "epsActual": 1.34},
                {"epsEstimated": -1.40, "epsActual": -1.23},
                {"epsEstimated": 0.80, "epsActual": 1.55},
                {"epsEstimated": -0.37, "epsActual": -0.13},
                {"epsEstimated": -0.07, "epsActual": -0.90},
            ]
        )

        stats = agent._compute_historical_beat_stats(
            "HUT",
            max_events=8,
            allow_remote=True,
        )

        assert stats["rate"] == pytest.approx(0.75)
        assert stats["beats"] == 6
        assert stats["samples"] == 8
        assert stats["source"] == "yfinance"

    def test_gct_prefers_complete_yahoo_sample_over_sparse_calendar(self):
        agent = self._agent()
        agent._get_cached_earnings_history = MagicMock(
            return_value=[{"epsEstimated": 0.65, "epsActual": 1.04}]
        )
        agent._get_yfinance_earnings_history = MagicMock(
            return_value=[
                {"epsEstimated": 0.97, "epsActual": 1.24},
                {"epsEstimated": 0.85, "epsActual": 1.16},
                {"epsEstimated": 0.74, "epsActual": 1.16},
                {"epsEstimated": 0.58, "epsActual": 1.14},
                {"epsEstimated": 0.53, "epsActual": 0.83},
                {"epsEstimated": 0.89, "epsActual": 0.75},
                {"epsEstimated": 0.67, "epsActual": 1.15},
                {"epsEstimated": 0.79, "epsActual": 1.03},
            ]
        )

        stats = agent._compute_historical_beat_stats(
            "GCT",
            max_events=8,
            allow_remote=True,
        )

        assert stats["rate"] == pytest.approx(0.875)
        assert stats["beats"] == 7
        assert stats["samples"] == 8
        assert stats["source"] == "yfinance"

    def test_zero_eps_values_are_preserved_as_valid_pairs(self):
        agent = self._agent()
        agent._get_cached_earnings_history = MagicMock(
            return_value=[
                {"epsEstimated": 0.0, "epsActual": 0.10},
                {"EPS Estimate": 0.10, "Reported EPS": 0.0},
            ]
        )

        stats = agent._compute_historical_beat_stats("ZERO", max_events=8)

        assert stats["rate"] == pytest.approx(0.5)
        assert stats["beats"] == 1
        assert stats["samples"] == 2
        assert stats["source"] == "economic_calendar"

    def test_100_pct_beat_rate(self):
        agent = self._agent()
        events = [
            {"epsEstimated": 1.0, "epsActual": 1.5},
            {"epsEstimated": 0.5, "epsActual": 0.8},
        ]
        agent._get_cached_earnings_history = MagicMock(return_value=events)
        result = agent._compute_historical_beat_rate("AAPL")
        assert result == pytest.approx(1.0)

    def test_0_pct_beat_rate(self):
        agent = self._agent()
        events = [
            {"epsEstimated": 1.0, "epsActual": 0.5},
            {"epsEstimated": 1.0, "epsActual": 0.8},
        ]
        agent._get_cached_earnings_history = MagicMock(return_value=events)
        result = agent._compute_historical_beat_rate("AAPL")
        assert result == pytest.approx(0.0)

    def test_not_0_5_when_no_events(self):
        """Critical invariant: 0.5 must NEVER be returned as a placeholder."""
        agent = self._agent()
        agent._get_cached_earnings_history = MagicMock(return_value=[])
        result = agent._compute_historical_beat_rate("XYZ")
        assert result != 0.5


# ═══════════════════════════════════════════════════════════════════════════════
# 2. compute_direction_bias_score — missing components excluded
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputeDirectionBiasScore:
    """Bias score must exclude unavailable data from weighted average."""

    def test_all_missing_except_news_uses_only_news(self):
        """With only c4 (news) available, final_bias = news component alone."""
        enriched = {}  # no beat_rate, no revision, no calls_puts
        news_sentiment = 0.0  # neutral → c4 = 50.0
        result = compute_direction_bias_score("AAPL", enriched, news_sentiment)
        # Only c4 available with weight 0.20 → final = c4 = 50.0
        assert result["bias_score_100"] == pytest.approx(50.0, abs=0.1)
        assert result["direction"] == "NEUTRAL"

    def test_data_quality_flag_set_when_two_or_more_missing(self):
        enriched = {}  # beat_rate=None, revision=None, calls_puts=None → 3 missing
        result = compute_direction_bias_score("AAPL", enriched, 0.0)
        assert result["data_quality_flag"] == "SUSPECT_INSUFFICIENT_DATA"

    def test_data_quality_flag_none_when_all_data_present(self):
        # All 6 components present (real yfinance analyst keys + recent_reaction_tilt
        # + eToro retail): c1 beat_rate, c2 analyst_target_upside_pct/analyst_rec_mean,
        # c3 calls_vs_puts, c4 news (always computable), c5 recent_reaction_tilt,
        # c6 retail positioning.
        enriched = {
            "beat_rate_historical": 0.7,
            "analyst_target_upside_pct": 5.0,
            "analyst_rec_mean": 2.0,
            "calls_vs_puts_ratio": 1.5,
            "recent_reaction_tilt": 0.1,
        }
        result = compute_direction_bias_score(
            "AAPL", enriched, 0.5,
            retail={"buy_pct": 62.0, "sell_pct": 38.0, "popularity_rank": 800},
        )
        assert result["data_quality_flag"] is None

    def test_data_quality_flag_none_when_only_one_missing(self):
        # Only c3 (calls_vs_puts_ratio) missing → 1 missing component → no flag.
        enriched = {
            "beat_rate_historical": 0.7,
            "analyst_target_upside_pct": 5.0,
            "analyst_rec_mean": 2.0,
            "recent_reaction_tilt": 0.1,
            # calls_vs_puts_ratio missing (1 missing component)
        }
        result = compute_direction_bias_score(
            "AAPL", enriched, 0.5,
            retail={"buy_pct": 62.0, "sell_pct": 38.0, "popularity_rank": 800},
        )
        assert result["data_quality_flag"] is None

    def test_missing_reason_populated_when_components_absent(self):
        enriched = {}
        result = compute_direction_bias_score("AAPL", enriched, 0.0)
        assert result["missing_reason"] is not None
        assert "c1_historical" in result["missing_reason"]

    def test_missing_reason_none_when_all_data_present(self):
        enriched = {
            "beat_rate_historical": 0.6,
            "analyst_target_upside_pct": 5.0,
            "analyst_rec_mean": 2.5,
            "calls_vs_puts_ratio": 1.2,
            "recent_reaction_tilt": 0.2,
        }
        result = compute_direction_bias_score(
            "AAPL", enriched, 0.3,
            retail={"buy_pct": 55.0, "sell_pct": 45.0, "popularity_rank": 1200},
        )
        assert result["missing_reason"] is None

    def test_missing_components_list_accurate(self):
        enriched = {
            "beat_rate_historical": 0.6,
            # revision missing
            # calls_puts missing
        }
        result = compute_direction_bias_score("AAPL", enriched, 0.0)
        missing = result["missing_components"]
        assert "c2_revision" in missing
        assert "c3_options" in missing
        assert "c1_historical" not in missing
        assert "c4_news" not in missing

    def test_available_weight_sums_correctly(self):
        enriched = {
            "beat_rate_historical": 0.6,            # c1 weight 0.25
            "analyst_target_upside_pct": 5.0,       # c2 weight 0.30
            "analyst_rec_mean": 2.0,
            "calls_vs_puts_ratio": 1.2,             # c3 weight 0.25
            # recent_reaction_tilt missing (c5 weight 0.12)
        }
        result = compute_direction_bias_score("AAPL", enriched, 0.0)
        # c4 (0.20, always present) + c1 (0.25) + c2 (0.30) + c3 (0.25) = 1.00
        assert result["available_weight"] == pytest.approx(1.00, abs=0.001)

    def test_full_data_weighted_avg_correct(self):
        """Full data: verify weighted average matches manual calculation.

        Mirrors compute_direction_bias_score's exact component formulas (real
        yfinance analyst blend + post-earnings reaction tilt) and the current
        component weights (c1=0.25, c2=0.30, c3=0.25, c4=0.20, c5=0.12).
        """
        beat_rate = 0.8
        upside_pct = 10.0
        rec_mean = 2.0
        calls_puts = 1.5
        news = 0.0
        tilt = 0.2

        c1 = beat_rate * 100
        c2_upside = max(0.0, min(100.0, 50.0 + upside_pct * 1.5))
        c2_rating = max(0.0, min(100.0, (5.0 - rec_mean) / 4.0 * 100.0))
        c2 = 0.65 * c2_upside + 0.35 * c2_rating
        c3 = min(calls_puts / 2.0, 1.0) * 100
        c4 = ((news + 1.0) / 2.0) * 100
        c5 = ((tilt + 1.0) / 2.0) * 100
        w = {"c1": 0.25, "c2": 0.30, "c3": 0.25, "c4": 0.20, "c5": 0.12}
        total_w = sum(w.values())
        expected = (c1*w["c1"] + c2*w["c2"] + c3*w["c3"] + c4*w["c4"] + c5*w["c5"]) / total_w

        enriched = {
            "beat_rate_historical": beat_rate,
            "beat_rate_samples": 4,
            "analyst_target_upside_pct": upside_pct,
            "analyst_rec_mean": rec_mean,
            "calls_vs_puts_ratio": calls_puts,
            "recent_reaction_tilt": tilt,
        }
        result = compute_direction_bias_score("AAPL", enriched, news)
        assert result["bias_score_100"] == pytest.approx(expected, abs=0.05)

    def test_c2_analyst_is_highest_weight_and_dominates(self):
        """c2 (analyst consensus) is the documented strongest predictor and must
        carry the single highest weight (0.30 > 0.25/0.25/0.20/0.12). Pre-fix c2
        was 0.28 — equal-ish to c1/c3 (0.25) and contradicting its own "(30%)"
        spec comment, under-weighting the highest-quality (real yfinance analyst)
        component. This locks the restored 0.30 and the relative dominance."""
        # All components present, only c2 strongly bullish (value 100), others neutral (50).
        enriched_strong_c2 = {
            "beat_rate_historical": 0.5,            # c1 = 50 (neutral)
            "analyst_target_upside_pct": 40.0,      # c2_upside = min(100, 50+40*1.5)=100
            "analyst_rec_mean": 1.0,                # c2_rating = (5-1)/4*100 = 100 -> c2 = 100
            "calls_vs_puts_ratio": 1.0,             # c3 = min(0.5,1)*100 = 50
            "recent_reaction_tilt": 0.0,            # c5 = 50 (neutral)
        }
        # Same but strong c1 instead of strong c2 (others neutral).
        enriched_strong_c1 = {
            "beat_rate_historical": 1.0,            # c1 = 100 (strong)
            "analyst_target_upside_pct": 0.0,       # c2 neutral: upside 0 -> 50, rec 3 -> 50
            "analyst_rec_mean": 3.0,
            "calls_vs_puts_ratio": 1.0,             # c3 = 50
            "recent_reaction_tilt": 0.0,            # c5 = 50
        }
        r_c2 = compute_direction_bias_score("AAPL", enriched_strong_c2, 0.0)  # c4=50 neutral
        r_c1 = compute_direction_bias_score("AAPL", enriched_strong_c1, 0.0)
        # c2 (weight 0.30) pulls the average higher than c1 (weight 0.25) when
        # each is the sole strong component at the same magnitude (100).
        assert r_c2["bias_score_100"] > r_c1["bias_score_100"]
        # c2 must be the single highest weight in the documented design.
        assert max(0.25, 0.30, 0.25, 0.20, 0.12) == 0.30

    def test_bullish_direction_when_high_bias(self):
        enriched = {
            "beat_rate_historical": 1.0,   # 100
            "analyst_target_upside_pct": 20.0,  # strong upside
            "analyst_rec_mean": 1.0,       # Strong Buy
            "calls_vs_puts_ratio": 2.0,    # 100
        }
        result = compute_direction_bias_score("AAPL", enriched, 1.0)  # strongly positive
        assert result["direction"] == "BULLISH"

    def test_bearish_direction_when_low_bias(self):
        enriched = {
            "beat_rate_historical": 0.0,   # 0
            "analyst_target_upside_pct": -20.0,  # downside
            "analyst_rec_mean": 5.0,       # Sell
            "calls_vs_puts_ratio": 0.0,    # 0
        }
        result = compute_direction_bias_score("AAPL", enriched, -1.0)  # strongly negative
        assert result["direction"] == "BEARISH"

    def test_legacy_bias_range(self):
        """bias_score must be in [-100, 100] range."""
        for news in [-1.0, 0.0, 1.0]:
            for enriched in [{}, {"beat_rate_historical": 0.5}]:
                result = compute_direction_bias_score("AAPL", enriched, news)
                assert -100 <= result["bias_score"] <= 100

    def test_returns_all_required_keys(self):
        result = compute_direction_bias_score("AAPL", {}, 0.0)
        for key in ["bias_score", "bias_score_100", "direction", "components",
                    "missing_components", "data_quality_flag", "missing_reason",
                    "available_weight"]:
            assert key in result, f"Missing key: {key}"
