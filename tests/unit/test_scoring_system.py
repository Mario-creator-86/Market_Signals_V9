"""
tests/unit/test_scoring_system.py
==================================
Unit tests for core/scoring_system.py

All functions under test are pure arithmetic — no I/O, no network, no FMP.
We test:
  - Utility helpers: _clamp, _sigmoid, _merge_cfg, calculate_distance_in_atr
  - calculate_percentile_rank
  - calculate_mtf_consensus_count, calculate_mtf_alignment
  - detect_market_regime, allowed_modes_for_regime
  - _score_rsi_zone        — PULLBACK / BREAKOUT / CONTRA × LONG/SHORT
  - _score_vwap_distance   — PULLBACK / BREAKOUT / CONTRA × direction
  - _score_ema_distance    — PULLBACK / BREAKOUT / CONTRA × direction
  - _score_macd_momentum   — strong/cross/hist × direction × mode
  - _score_price_action    — body ratio, shadows × direction × mode
  - _score_mtf_alignment   — per profile × CONTRA mode
  - SCORE_WEIGHTS sum invariant
"""

import sys
import os
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")


# ── lazy import after env setup ───────────────────────────────────────────────
from core.scoring_system import (
    SCORE_WEIGHTS,
    SCORE_THRESHOLDS,
    _clamp,
    _sigmoid,
    _merge_cfg,
    calculate_distance_in_atr,
    calculate_percentile_rank,
    calculate_mtf_consensus_count,
    calculate_mtf_alignment,
    detect_market_regime,
    allowed_modes_for_regime,
    _score_rsi_zone,
    _score_vwap_distance,
    _score_ema_distance,
    _score_macd_momentum,
    _score_price_action,
    _score_mtf_alignment,
    RowContext,
)


# ── helper: build a minimal RowContext ───────────────────────────────────────

def _row(
    close=100.0, open_=99.0, high=101.0, low=98.0, volume=10_000.0,
    rsi=50.0, macd=0.005, macd_signal=0.003, macd_hist=0.002,
    ema20=99.5, vwap=100.0, atr=1.0,
    adx=25.0, vol_sma=9_000.0,
    adx_5m=25.0, bb_width_5m=0.02, atr_percentile_5m=50.0,
    market_regime="MIXED",
    trend_1d="BULL", trend_4h="BULL", trend_1h="BULL",
    trend_30m="BULL", trend_15m="BULL", trend_5m="BULL", trend_1m="BULL",
    dist_vwap_atr=0.0, dist_ema_atr=0.0,
    cooldown_active=False, cooldown_bars_remaining=0,
) -> RowContext:
    return RowContext(
        open=open_, high=high, low=low, close=close, volume=volume,
        rsi=rsi, macd=macd, macd_signal=macd_signal, macd_hist=macd_hist,
        ema20=ema20, vwap=vwap, atr=atr,
        adx=adx, vol_sma=vol_sma,
        adx_5m=adx_5m, bb_width_5m=bb_width_5m, atr_percentile_5m=atr_percentile_5m,
        market_regime=market_regime,
        trend_1d=trend_1d, trend_4h=trend_4h, trend_1h=trend_1h,
        trend_30m=trend_30m, trend_15m=trend_15m, trend_5m=trend_5m, trend_1m=trend_1m,
        dist_vwap_atr=dist_vwap_atr, dist_ema_atr=dist_ema_atr,
        cooldown_active=cooldown_active, cooldown_bars_remaining=cooldown_bars_remaining,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Config / constants sanity
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfigConstants:
    def test_score_weights_sum_to_100(self):
        assert sum(SCORE_WEIGHTS.values()) == 100

    def test_thresholds_in_range(self):
        for name, val in SCORE_THRESHOLDS.items():
            assert 0 < val < 100, f"{name} threshold out of range"

    def test_strong_threshold_greater_than_medium(self):
        assert SCORE_THRESHOLDS["STRONG"] > SCORE_THRESHOLDS["MEDIUM"]

    def test_medium_threshold_greater_than_relax(self):
        assert SCORE_THRESHOLDS["MEDIUM"] > SCORE_THRESHOLDS["RELAX"]


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Utility helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestUtilHelpers:
    def test_clamp_below_min(self):
        assert _clamp(0.0, 1.0, -5.0) == 0.0

    def test_clamp_above_max(self):
        assert _clamp(0.0, 1.0, 5.0) == 1.0

    def test_clamp_within_range(self):
        assert _clamp(0.0, 10.0, 3.5) == pytest.approx(3.5)

    def test_sigmoid_zero(self):
        assert _sigmoid(0.0) == pytest.approx(0.5, abs=1e-6)

    def test_sigmoid_large_positive_approaches_one(self):
        assert _sigmoid(10.0) > 0.99

    def test_sigmoid_large_negative_approaches_zero(self):
        assert _sigmoid(-10.0) < 0.01

    def test_sigmoid_clamped_at_plus_12(self):
        # Values above 12 → same as 12
        assert _sigmoid(100.0) == pytest.approx(_sigmoid(12.0), abs=1e-6)

    def test_sigmoid_clamped_at_minus_12(self):
        assert _sigmoid(-100.0) == pytest.approx(_sigmoid(-12.0), abs=1e-6)

    def test_merge_cfg_no_override(self):
        result = _merge_cfg({"a": 1, "b": 2}, None)
        assert result == {"a": 1, "b": 2}

    def test_merge_cfg_flat_override(self):
        result = _merge_cfg({"a": 1, "b": 2}, {"b": 99})
        assert result["b"] == 99
        assert result["a"] == 1

    def test_merge_cfg_nested_dict_merge(self):
        defaults = {"nested": {"x": 1, "y": 2}}
        override = {"nested": {"y": 99}}
        result = _merge_cfg(defaults, override)
        assert result["nested"]["x"] == 1
        assert result["nested"]["y"] == 99

    def test_merge_cfg_does_not_mutate_defaults(self):
        defaults = {"a": 1}
        _merge_cfg(defaults, {"a": 2})
        assert defaults["a"] == 1

    def test_calculate_distance_in_atr_positive(self):
        assert calculate_distance_in_atr(102.0, 100.0, 1.0) == pytest.approx(2.0)

    def test_calculate_distance_in_atr_absolute(self):
        # distance is always positive
        assert calculate_distance_in_atr(98.0, 100.0, 2.0) == pytest.approx(1.0)

    def test_calculate_distance_in_atr_zero_atr(self):
        # zero ATR → returns 0.0 (no division)
        assert calculate_distance_in_atr(102.0, 100.0, 0.0) == 0.0

    def test_calculate_percentile_rank_basic(self):
        # current=5 in [1,2,3,4,5,6,7,8,9,10] → 50th percentile
        val = calculate_percentile_rank([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 5)
        assert val == pytest.approx(50.0)

    def test_calculate_percentile_rank_empty(self):
        assert calculate_percentile_rank([], 5.0) == 0.0

    def test_calculate_percentile_rank_max(self):
        val = calculate_percentile_rank([1, 2, 3, 4, 5], 5)
        assert val == pytest.approx(100.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. MTF consensus / alignment helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestMtfHelpers:
    def test_consensus_all_bull(self):
        row = _row()  # all trends = BULL
        assert calculate_mtf_consensus_count(row) == 6

    def test_consensus_all_bear(self):
        row = _row(trend_4h="BEAR", trend_1h="BEAR", trend_30m="BEAR",
                   trend_15m="BEAR", trend_5m="BEAR", trend_1m="BEAR")
        assert calculate_mtf_consensus_count(row) == 6

    def test_consensus_mixed(self):
        # 3 BULL, 3 BEAR → max=3
        row = _row(trend_4h="BULL", trend_1h="BULL", trend_30m="BULL",
                   trend_15m="BEAR", trend_5m="BEAR", trend_1m="BEAR")
        assert calculate_mtf_consensus_count(row) == 3

    def test_mtf_alignment_long_pullback_all_bull(self):
        row = _row()  # all BULL
        aligned, total = calculate_mtf_alignment(row, "LONG", "PULLBACK")
        assert aligned == 6
        assert total == 6

    def test_mtf_alignment_short_pullback_all_bear(self):
        row = _row(trend_4h="BEAR", trend_1h="BEAR", trend_30m="BEAR",
                   trend_15m="BEAR", trend_5m="BEAR", trend_1m="BEAR")
        aligned, total = calculate_mtf_alignment(row, "SHORT", "PULLBACK")
        assert aligned == 6
        assert total == 6

    def test_mtf_alignment_contra_logic(self):
        # CONTRA LONG: big TFs should be BEAR (opposite), small TFs BULL (expected)
        row = _row(
            trend_4h="BEAR", trend_1h="BEAR", trend_30m="BEAR", trend_15m="BEAR",  # 4 BEAR
            trend_5m="BULL", trend_1m="BULL",  # small BULL → confirmare
        )
        aligned, total = calculate_mtf_alignment(row, "LONG", "CONTRA")
        # 4 big tfs match opposite (BEAR) + 2 small match expected (BULL) = 6
        assert aligned == 6
        assert total == 6

    def test_mtf_alignment_partial(self):
        row = _row(trend_4h="BULL", trend_1h="BULL", trend_30m="BEAR",
                   trend_15m="BEAR", trend_5m="BEAR", trend_1m="BEAR")
        aligned, _ = calculate_mtf_alignment(row, "LONG", "PULLBACK")
        assert aligned == 2  # only 4h and 1h are BULL


# ═══════════════════════════════════════════════════════════════════════════════
# 4. detect_market_regime
# ═══════════════════════════════════════════════════════════════════════════════

class TestDetectMarketRegime:
    def test_low_vol_regime(self):
        row = _row(atr_percentile_5m=10.0, adx_5m=10.0, bb_width_5m=0.01)
        regime, details = detect_market_regime(row)
        assert regime == "LOW_VOL"

    def test_trend_regime(self):
        row = _row(atr_percentile_5m=50.0, adx_5m=25.0, bb_width_5m=0.05,
                   trend_4h="BULL", trend_1h="BULL", trend_30m="BULL",
                   trend_15m="BULL", trend_5m="BULL", trend_1m="BULL")
        regime, _ = detect_market_regime(row)
        assert regime == "TREND"

    def test_range_regime(self):
        # Low ADX + low BB width + no strong MTF consensus
        row = _row(atr_percentile_5m=50.0, adx_5m=10.0, bb_width_5m=0.02,
                   trend_4h="BEAR", trend_1h="BULL", trend_30m="BEAR",
                   trend_15m="BULL", trend_5m="BEAR", trend_1m="BULL")
        regime, _ = detect_market_regime(row)
        assert regime == "RANGE"

    def test_volatile_regime(self):
        row = _row(atr_percentile_5m=90.0, adx_5m=15.0, bb_width_5m=0.08,
                   trend_4h="BEAR", trend_1h="BULL", trend_30m="BEAR",
                   trend_15m="BULL", trend_5m="BEAR", trend_1m="BULL")
        regime, _ = detect_market_regime(row)
        assert regime == "VOLATILE"

    def test_mixed_regime_fallback(self):
        # ADX moderate, BB wide, no majority consensus → MIXED
        row = _row(atr_percentile_5m=40.0, adx_5m=17.0, bb_width_5m=0.06,
                   trend_4h="BEAR", trend_1h="BULL", trend_30m="BEAR",
                   trend_15m="BULL", trend_5m="BEAR", trend_1m="BULL")
        regime, _ = detect_market_regime(row)
        assert regime == "MIXED"

    def test_detect_regime_returns_details_dict(self):
        row = _row()
        _, details = detect_market_regime(row)
        assert "adx" in details
        assert "bb_width" in details
        assert "atr_percentile" in details
        assert "mtf_consensus" in details


# ═══════════════════════════════════════════════════════════════════════════════
# 5. allowed_modes_for_regime
# ═══════════════════════════════════════════════════════════════════════════════

class TestAllowedModesForRegime:
    def test_trend_allows_contra_only(self):
        modes = allowed_modes_for_regime("TREND")
        assert modes == ["CONTRA"]

    def test_range_allows_contra_only(self):
        modes = allowed_modes_for_regime("RANGE")
        assert modes == ["CONTRA"]

    def test_low_vol_allows_nothing(self):
        modes = allowed_modes_for_regime("LOW_VOL")
        assert modes == []

    def test_volatile_allows_pullback_breakout(self):
        modes = allowed_modes_for_regime("VOLATILE")
        assert "PULLBACK" in modes
        assert "BREAKOUT" in modes
        assert "CONTRA" not in modes

    def test_volatile_with_disallow(self):
        modes = allowed_modes_for_regime("VOLATILE", {"allow_volatile": False})
        assert modes == []

    def test_mixed_allows_all(self):
        modes = allowed_modes_for_regime("MIXED")
        assert "PULLBACK" in modes
        assert "BREAKOUT" in modes
        assert "CONTRA" in modes


# ═══════════════════════════════════════════════════════════════════════════════
# 6. _score_rsi_zone
# ═══════════════════════════════════════════════════════════════════════════════

class TestScoreRsiZone:
    MAX = SCORE_WEIGHTS["rsi_zone"]

    # ── PULLBACK ──
    def test_pullback_long_optimal(self):
        score, desc = _score_rsi_zone(52.0, "LONG", "PULLBACK", "MEDIUM")
        assert score == self.MAX

    def test_pullback_long_acceptable(self):
        score, desc = _score_rsi_zone(65.0, "LONG", "PULLBACK", "MEDIUM")
        assert score == self.MAX * 2 // 3

    def test_pullback_long_marginal(self):
        score, desc = _score_rsi_zone(36.0, "LONG", "PULLBACK", "MEDIUM")
        assert score == self.MAX // 3

    def test_pullback_long_extreme_zero(self):
        score, _ = _score_rsi_zone(10.0, "LONG", "PULLBACK", "MEDIUM")
        assert score == 0

    def test_pullback_short_optimal(self):
        score, _ = _score_rsi_zone(48.0, "SHORT", "PULLBACK", "MEDIUM")
        assert score == self.MAX

    def test_pullback_short_extreme_zero(self):
        # RSI 85 is too high for short pullback (should be < 65 for marginal)
        score, _ = _score_rsi_zone(85.0, "SHORT", "PULLBACK", "MEDIUM")
        assert score == 0

    # ── BREAKOUT ──
    def test_breakout_long_optimal(self):
        score, _ = _score_rsi_zone(65.0, "LONG", "BREAKOUT", "MEDIUM")
        assert score == self.MAX

    def test_breakout_long_overbought_partial(self):
        # RSI > 85 → risc retragere → partial score
        score, _ = _score_rsi_zone(88.0, "LONG", "BREAKOUT", "MEDIUM")
        assert score == self.MAX // 3

    def test_breakout_short_optimal(self):
        score, _ = _score_rsi_zone(35.0, "SHORT", "BREAKOUT", "MEDIUM")
        assert score == self.MAX

    def test_breakout_short_extreme_oversold_partial(self):
        # RSI < 20 → risc bounce
        score, _ = _score_rsi_zone(15.0, "SHORT", "BREAKOUT", "MEDIUM")
        assert score == self.MAX // 3

    def test_breakout_long_low_rsi_zero(self):
        score, _ = _score_rsi_zone(30.0, "LONG", "BREAKOUT", "MEDIUM")
        assert score == 0

    # ── CONTRA ──
    def test_contra_long_extreme_oversold(self):
        score, _ = _score_rsi_zone(18.0, "LONG", "CONTRA", "MEDIUM")
        assert score == self.MAX

    def test_contra_long_near_oversold(self):
        score, _ = _score_rsi_zone(23.0, "LONG", "CONTRA", "MEDIUM")
        assert score == self.MAX * 2 // 3

    def test_contra_long_approaching_oversold(self):
        score, _ = _score_rsi_zone(28.0, "LONG", "CONTRA", "MEDIUM")
        assert score == self.MAX // 3

    def test_contra_long_too_high(self):
        score, _ = _score_rsi_zone(50.0, "LONG", "CONTRA", "MEDIUM")
        assert score == 0

    def test_contra_short_extreme_overbought(self):
        score, _ = _score_rsi_zone(82.0, "SHORT", "CONTRA", "MEDIUM")
        assert score == self.MAX

    def test_contra_short_near_overbought(self):
        score, _ = _score_rsi_zone(77.0, "SHORT", "CONTRA", "MEDIUM")
        assert score == self.MAX * 2 // 3

    def test_contra_short_too_low(self):
        score, _ = _score_rsi_zone(50.0, "SHORT", "CONTRA", "MEDIUM")
        assert score == 0

    def test_returns_tuple_with_description(self):
        result = _score_rsi_zone(50.0, "LONG", "PULLBACK", "MEDIUM")
        assert isinstance(result, tuple) and len(result) == 2
        assert isinstance(result[1], str)


# ═══════════════════════════════════════════════════════════════════════════════
# 7. _score_vwap_distance
# ═══════════════════════════════════════════════════════════════════════════════

class TestScoreVwapDistance:
    MAX = SCORE_WEIGHTS["vwap_distance"]

    def test_pullback_very_close(self):
        score, _ = _score_vwap_distance(0.2, "LONG", 100.1, 100.0, "PULLBACK", "MEDIUM")
        assert score == self.MAX

    def test_pullback_close(self):
        score, _ = _score_vwap_distance(0.4, "LONG", 100.4, 100.0, "PULLBACK", "MEDIUM")
        assert score == self.MAX * 3 // 4

    def test_pullback_moderate(self):
        score, _ = _score_vwap_distance(0.8, "LONG", 100.8, 100.0, "PULLBACK", "MEDIUM")
        assert score == self.MAX // 2

    def test_pullback_far(self):
        score, _ = _score_vwap_distance(1.2, "LONG", 101.2, 100.0, "PULLBACK", "MEDIUM")
        assert score == self.MAX // 4

    def test_pullback_too_far_zero(self):
        score, _ = _score_vwap_distance(2.0, "LONG", 102.0, 100.0, "PULLBACK", "MEDIUM")
        assert score == 0

    def test_breakout_long_above_vwap_close(self):
        score, _ = _score_vwap_distance(0.5, "LONG", 100.5, 100.0, "BREAKOUT", "MEDIUM")
        assert score == self.MAX

    def test_breakout_long_above_vwap_far(self):
        score, _ = _score_vwap_distance(1.3, "LONG", 101.3, 100.0, "BREAKOUT", "MEDIUM")
        assert score == self.MAX * 2 // 3

    def test_breakout_long_above_vwap_very_far_zero(self):
        score, _ = _score_vwap_distance(2.5, "LONG", 102.5, 100.0, "BREAKOUT", "MEDIUM")
        assert score == 0

    def test_breakout_short_below_vwap(self):
        score, _ = _score_vwap_distance(0.5, "SHORT", 99.5, 100.0, "BREAKOUT", "MEDIUM")
        assert score == self.MAX

    def test_breakout_wrong_direction_partial(self):
        # LONG but below VWAP → partial (counter-VWAP)
        score, _ = _score_vwap_distance(0.5, "LONG", 99.5, 100.0, "BREAKOUT", "MEDIUM")
        assert score == self.MAX // 4

    def test_contra_extreme_far(self):
        score, _ = _score_vwap_distance(3.0, "LONG", 103.0, 100.0, "CONTRA", "MEDIUM")
        assert score == self.MAX

    def test_contra_far(self):
        score, _ = _score_vwap_distance(2.2, "LONG", 102.2, 100.0, "CONTRA", "MEDIUM")
        assert score == self.MAX * 2 // 3

    def test_contra_moderate_far(self):
        score, _ = _score_vwap_distance(1.7, "LONG", 101.7, 100.0, "CONTRA", "MEDIUM")
        assert score == self.MAX // 3

    def test_contra_too_close_zero(self):
        score, _ = _score_vwap_distance(0.5, "LONG", 100.5, 100.0, "CONTRA", "MEDIUM")
        assert score == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 8. _score_ema_distance
# ═══════════════════════════════════════════════════════════════════════════════

class TestScoreEmaDistance:
    MAX = SCORE_WEIGHTS["ema_distance"]

    def test_pullback_very_close(self):
        score, _ = _score_ema_distance(0.1, "LONG", 100.1, 100.0, "PULLBACK", "MEDIUM")
        assert score == self.MAX

    def test_pullback_close(self):
        score, _ = _score_ema_distance(0.3, "LONG", 100.3, 100.0, "PULLBACK", "MEDIUM")
        assert score == self.MAX * 3 // 4

    def test_pullback_moderate(self):
        score, _ = _score_ema_distance(0.6, "LONG", 100.6, 100.0, "PULLBACK", "MEDIUM")
        assert score == self.MAX // 2

    def test_pullback_far(self):
        score, _ = _score_ema_distance(1.0, "LONG", 101.0, 100.0, "PULLBACK", "MEDIUM")
        assert score == self.MAX // 4

    def test_pullback_too_far_zero(self):
        score, _ = _score_ema_distance(1.5, "LONG", 101.5, 100.0, "PULLBACK", "MEDIUM")
        assert score == 0

    def test_breakout_long_above_ema(self):
        score, _ = _score_ema_distance(0.5, "LONG", 100.5, 100.0, "BREAKOUT", "MEDIUM")
        assert score == self.MAX

    def test_breakout_long_above_ema_moderate(self):
        score, _ = _score_ema_distance(1.0, "LONG", 101.0, 100.0, "BREAKOUT", "MEDIUM")
        assert score == self.MAX * 2 // 3

    def test_breakout_short_below_ema(self):
        score, _ = _score_ema_distance(0.5, "SHORT", 99.5, 100.0, "BREAKOUT", "MEDIUM")
        assert score == self.MAX

    def test_breakout_wrong_direction_partial(self):
        # LONG but below EMA
        score, _ = _score_ema_distance(0.5, "LONG", 99.5, 100.0, "BREAKOUT", "MEDIUM")
        assert score == self.MAX // 4

    def test_contra_extreme_far(self):
        score, _ = _score_ema_distance(2.5, "LONG", 102.5, 100.0, "CONTRA", "MEDIUM")
        assert score == self.MAX

    def test_contra_far(self):
        score, _ = _score_ema_distance(1.7, "LONG", 101.7, 100.0, "CONTRA", "MEDIUM")
        assert score == self.MAX * 2 // 3

    def test_contra_moderate(self):
        score, _ = _score_ema_distance(1.1, "LONG", 101.1, 100.0, "CONTRA", "MEDIUM")
        assert score == self.MAX // 3

    def test_contra_too_close_zero(self):
        score, _ = _score_ema_distance(0.5, "LONG", 100.5, 100.0, "CONTRA", "MEDIUM")
        assert score == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 9. _score_macd_momentum
# ═══════════════════════════════════════════════════════════════════════════════

class TestScoreMacdMomentum:
    MAX = SCORE_WEIGHTS["macd_momentum"]

    # ── PULLBACK/BREAKOUT LONG ──
    def test_long_strong_positive_macd(self):
        score, _ = _score_macd_momentum(0.002, 0.005, 0.003, "LONG", "PULLBACK", "MEDIUM")
        assert score == self.MAX  # hist>0.001 and cross_up

    def test_long_weak_positive_macd(self):
        # hist > 0 but < 0.001
        score, _ = _score_macd_momentum(0.0005, 0.005, 0.003, "LONG", "PULLBACK", "MEDIUM")
        assert score == self.MAX * 2 // 3

    def test_long_cross_up_only(self):
        # macd > signal but hist <= 0
        score, _ = _score_macd_momentum(-0.0001, 0.005, 0.003, "LONG", "PULLBACK", "MEDIUM")
        assert score == self.MAX // 2

    def test_long_positive_hist_no_cross(self):
        # hist > 0, macd < signal
        score, _ = _score_macd_momentum(0.002, 0.002, 0.005, "LONG", "PULLBACK", "MEDIUM")
        assert score == self.MAX // 3

    def test_long_negative_macd_zero(self):
        score, _ = _score_macd_momentum(-0.002, 0.001, 0.005, "LONG", "PULLBACK", "MEDIUM")
        assert score == 0

    # ── PULLBACK/BREAKOUT SHORT ──
    def test_short_strong_negative_macd(self):
        score, _ = _score_macd_momentum(-0.002, 0.003, 0.005, "SHORT", "PULLBACK", "MEDIUM")
        assert score == self.MAX  # hist < -0.001 and cross_down

    def test_short_weak_negative_macd(self):
        score, _ = _score_macd_momentum(-0.0005, 0.003, 0.005, "SHORT", "PULLBACK", "MEDIUM")
        assert score == self.MAX * 2 // 3

    def test_short_cross_down_only(self):
        score, _ = _score_macd_momentum(0.0001, 0.003, 0.005, "SHORT", "PULLBACK", "MEDIUM")
        assert score == self.MAX // 2

    def test_short_positive_macd_zero(self):
        score, _ = _score_macd_momentum(0.005, 0.008, 0.003, "SHORT", "PULLBACK", "MEDIUM")
        assert score == 0

    # ── CONTRA LONG ──
    def test_contra_long_bullish_cross_from_negative(self):
        # hist < 0, macd > signal (bullish cross)
        score, _ = _score_macd_momentum(-0.001, 0.005, 0.003, "LONG", "CONTRA", "MEDIUM")
        assert score == self.MAX

    def test_contra_long_near_cross(self):
        # hist < 0 AND cross_up → first branch → MAX
        # macd(0.005) > signal(0.003) → cross_up=True; hist=-0.00005 < 0
        score, _ = _score_macd_momentum(-0.00005, 0.005, 0.003, "LONG", "CONTRA", "MEDIUM")
        assert score == self.MAX

    def test_contra_long_very_negative_partial(self):
        # hist < -0.001 but no cross
        score, _ = _score_macd_momentum(-0.005, 0.001, 0.005, "LONG", "CONTRA", "MEDIUM")
        assert score == self.MAX // 3

    def test_contra_long_no_reversal_zero(self):
        # hist > 0, no cross_up (macd < signal), hist not < -0.001 → zero
        # macd(0.001) < signal(0.003) → cross_up=False
        # hist=0.002 > 0, not < 0 for first two branches
        # third: hist < -0.001? No. → 0
        score, _ = _score_macd_momentum(0.002, 0.001, 0.003, "LONG", "CONTRA", "MEDIUM")
        assert score == 0

    # ── CONTRA SHORT ──
    def test_contra_short_bearish_cross_from_positive(self):
        # hist > 0, macd < signal (bearish cross)
        score, _ = _score_macd_momentum(0.001, 0.003, 0.005, "SHORT", "CONTRA", "MEDIUM")
        assert score == self.MAX

    def test_contra_short_very_positive_partial(self):
        # hist > 0.001 but no cross
        score, _ = _score_macd_momentum(0.005, 0.008, 0.003, "SHORT", "CONTRA", "MEDIUM")
        assert score == self.MAX // 3

    # ── BREAKOUT mode same as PULLBACK ──
    def test_breakout_same_logic_as_pullback(self):
        score_pb, _ = _score_macd_momentum(0.002, 0.005, 0.003, "LONG", "PULLBACK", "MEDIUM")
        score_bo, _ = _score_macd_momentum(0.002, 0.005, 0.003, "LONG", "BREAKOUT", "MEDIUM")
        assert score_pb == score_bo


# ═══════════════════════════════════════════════════════════════════════════════
# 10. _score_price_action
# ═══════════════════════════════════════════════════════════════════════════════

class TestScorePriceAction:
    MAX = SCORE_WEIGHTS["price_action"]

    def test_long_strong_bullish_candle(self):
        # body > 60%: close-open=0.8, range=1.0
        score, _ = _score_price_action(99.0, 100.0, 98.9, 99.8, "LONG", "PULLBACK", "MEDIUM")
        # body = 0.8, total = 1.1, ratio ≈ 0.73 > 0.6
        assert score == self.MAX

    def test_long_moderate_bullish_candle(self):
        # body ratio ~0.5 (40<ratio<60)
        score, _ = _score_price_action(99.0, 101.0, 98.5, 99.5, "LONG", "PULLBACK", "MEDIUM")
        # body=0.5, range=2.5, ratio=0.2 → slabă (< 0.4)
        # but close > open so is_bullish
        assert score == self.MAX // 2

    def test_long_bearish_candle_zero(self):
        score, _ = _score_price_action(100.0, 101.0, 98.0, 99.0, "LONG", "PULLBACK", "MEDIUM")
        assert score == 0

    def test_short_strong_bearish_candle(self):
        # body > 60% bearish
        score, _ = _score_price_action(100.0, 100.1, 98.9, 99.2, "SHORT", "PULLBACK", "MEDIUM")
        # body=0.8, range=1.2, ratio≈0.67 > 0.6
        assert score == self.MAX

    def test_short_bullish_candle_zero(self):
        score, _ = _score_price_action(99.0, 101.0, 98.0, 100.0, "SHORT", "PULLBACK", "MEDIUM")
        assert score == 0

    def test_zero_range_candle_partial(self):
        # total_range = 0 → returns max // 2
        score, _ = _score_price_action(100.0, 100.0, 100.0, 100.0, "LONG", "PULLBACK", "MEDIUM")
        assert score == self.MAX // 2

    def test_long_shadow_support(self):
        # bearish candle but lower_shadow > upper_shadow → partial support
        score, _ = _score_price_action(100.0, 100.5, 97.0, 99.5, "LONG", "PULLBACK", "MEDIUM")
        # close(99.5) < open(100.0) → bearish
        # lower_shadow = min(99.5,100.0) - 97 = 2.5
        # upper_shadow = 100.5 - max(99.5,100.0) = 0.5
        # lower > upper → support
        assert score == self.MAX // 3

    def test_short_shadow_resistance(self):
        # bullish candle but upper_shadow > lower_shadow → partial resistance
        score, _ = _score_price_action(99.0, 102.0, 98.5, 99.5, "SHORT", "PULLBACK", "MEDIUM")
        # close(99.5) > open(99.0) → bullish 
        # upper_shadow = 102 - 99.5 = 2.5
        # lower_shadow = 99 - 98.5 = 0.5
        # upper > lower → resistance
        assert score == self.MAX // 3

    def test_breakout_same_as_pullback(self):
        score_pb, _ = _score_price_action(99.0, 100.0, 98.9, 99.9, "LONG", "PULLBACK", "MEDIUM")
        score_bo, _ = _score_price_action(99.0, 100.0, 98.9, 99.9, "LONG", "BREAKOUT", "MEDIUM")
        assert score_pb == score_bo


# ═══════════════════════════════════════════════════════════════════════════════
# 11. _score_mtf_alignment (with profiles)
# ═══════════════════════════════════════════════════════════════════════════════

class TestScoreMtfAlignment:
    MAX = SCORE_WEIGHTS["mtf_alignment"]

    def _row_aligned(self, n_aligned: int, direction="LONG") -> RowContext:
        """Build a row with n_aligned TFs in the right direction (BULL for LONG)."""
        tfs = ["BULL"] * n_aligned + ["BEAR"] * (6 - n_aligned)
        return _row(
            trend_4h=tfs[0], trend_1h=tfs[1], trend_30m=tfs[2],
            trend_15m=tfs[3], trend_5m=tfs[4], trend_1m=tfs[5],
            volume=10_000.0, vol_sma=9_000.0
        )

    # --- MEDIUM profile ---
    def test_medium_6_aligned_full_score(self):
        row = self._row_aligned(6)
        score, _, _ = _score_mtf_alignment(row, "LONG", "PULLBACK", "MEDIUM")
        assert score == self.MAX

    def test_medium_3_aligned_half_score(self):
        row = self._row_aligned(3)
        score, _, _ = _score_mtf_alignment(row, "LONG", "PULLBACK", "MEDIUM")
        assert score == self.MAX // 2

    def test_medium_2_aligned_zero(self):
        row = self._row_aligned(2)
        score, _, _ = _score_mtf_alignment(row, "LONG", "PULLBACK", "MEDIUM")
        assert score == 0

    # --- STRONG profile ---
    def test_strong_5_aligned_full_score(self):
        row = self._row_aligned(5)
        score, _, _ = _score_mtf_alignment(row, "LONG", "BREAKOUT", "STRONG")
        assert score == self.MAX

    def test_strong_4_aligned_half_score(self):
        row = self._row_aligned(4)
        score, _, _ = _score_mtf_alignment(row, "LONG", "BREAKOUT", "STRONG")
        assert score == self.MAX // 2

    def test_strong_3_aligned_zero(self):
        row = self._row_aligned(3)
        score, _, _ = _score_mtf_alignment(row, "LONG", "BREAKOUT", "STRONG")
        assert score == 0

    # --- PRO profile ---
    def test_pro_5_aligned_full_score(self):
        row = self._row_aligned(5)
        score, _, _ = _score_mtf_alignment(row, "LONG", "PULLBACK", "PRO")
        assert score == self.MAX

    def test_pro_4_aligned_partial(self):
        row = self._row_aligned(4)
        score, _, _ = _score_mtf_alignment(row, "LONG", "PULLBACK", "PRO")
        assert score == int(self.MAX * 0.8)

    def test_pro_3_aligned_minimal(self):
        row = self._row_aligned(3)
        score, _, _ = _score_mtf_alignment(row, "LONG", "PULLBACK", "PRO")
        assert score == int(self.MAX * 0.66)

    def test_pro_2_aligned_zero(self):
        row = self._row_aligned(2)
        score, _, _ = _score_mtf_alignment(row, "LONG", "PULLBACK", "PRO")
        assert score == 0

    # --- RELAX profile ---
    def test_relax_5m_and_1m_aligned_full(self):
        row = _row(trend_4h="BEAR", trend_1h="BEAR", trend_30m="BEAR",
                   trend_15m="BEAR", trend_5m="BULL", trend_1m="BULL")
        score, _, _ = _score_mtf_alignment(row, "LONG", "PULLBACK", "RELAX")
        assert score == self.MAX

    def test_relax_only_1m_with_context_partial(self):
        # Only 1m aligned + at least 1 higher TF aligned
        row = _row(trend_4h="BULL", trend_1h="BEAR", trend_30m="BEAR",
                   trend_15m="BEAR", trend_5m="BEAR", trend_1m="BULL")
        score, _, _ = _score_mtf_alignment(row, "LONG", "PULLBACK", "RELAX")
        assert score == self.MAX * 2 // 3

    def test_relax_1m_not_aligned_zero(self):
        row = _row(trend_4h="BULL", trend_1h="BULL", trend_30m="BULL",
                   trend_15m="BULL", trend_5m="BULL", trend_1m="BEAR")
        score, _, _ = _score_mtf_alignment(row, "LONG", "PULLBACK", "RELAX")
        assert score == 0

    # --- Volume penalty ---
    def test_volume_penalty_applied(self):
        # Low volume → score reduced by 20%
        row = _row(
            trend_4h="BULL", trend_1h="BULL", trend_30m="BULL",
            trend_15m="BULL", trend_5m="BULL", trend_1m="BULL",
            volume=5_000.0, vol_sma=10_000.0  # volume < 80% of SMA
        )
        score, _, warnings = _score_mtf_alignment(row, "LONG", "PULLBACK", "MEDIUM")
        assert score < self.MAX
        assert any("Volume" in w for w in warnings)

    def test_returns_tuple_three_elements(self):
        row = self._row_aligned(6)
        result = _score_mtf_alignment(row, "LONG", "PULLBACK", "MEDIUM")
        assert len(result) == 3
        score, desc, warnings = result
        assert isinstance(score, int)
        assert isinstance(desc, str)
        assert isinstance(warnings, list)

    # --- CONTRA mode MTF ---
    def test_contra_strong_structure(self):
        # LONG contra: 4 big TFs BEAR + 5m+1m BULL
        row = _row(
            trend_4h="BEAR", trend_1h="BEAR", trend_30m="BEAR", trend_15m="BEAR",
            trend_5m="BULL", trend_1m="BULL",
            volume=10_000.0, vol_sma=9_000.0
        )
        score, _, _ = _score_mtf_alignment(row, "LONG", "CONTRA", "MEDIUM")
        assert score == self.MAX

    def test_contra_5m_still_bear_rejected(self):
        # CONTRA LONG: 5m is still BEAR → score = 0
        row = _row(
            trend_4h="BEAR", trend_1h="BEAR", trend_30m="BEAR", trend_15m="BEAR",
            trend_5m="BEAR", trend_1m="BULL",
        )
        score, _, _ = _score_mtf_alignment(row, "LONG", "CONTRA", "MEDIUM")
        assert score == 0

    def test_contra_no_structure_zero(self):
        # All BULL for LONG contra → not a contra setup
        row = _row()  # all BULL
        score, desc, _ = _score_mtf_alignment(row, "LONG", "CONTRA", "MEDIUM")
        assert score == 0

    # --- Conflict detection warning ---
    def test_conflict_warning_when_5m_and_1d_contradict_4h(self):
        row = _row(
            trend_1d="BEAR", trend_4h="BULL", trend_1h="BULL",
            trend_30m="BULL", trend_15m="BULL", trend_5m="BEAR", trend_1m="BULL"
        )
        _, _, warnings = _score_mtf_alignment(row, "LONG", "PULLBACK", "MEDIUM")
        assert any("CONFLICT" in w for w in warnings)
