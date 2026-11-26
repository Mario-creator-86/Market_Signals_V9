"""GOLD-052 — §52 catalog names the existing V7 indicators; no indicator is
added (catalog length == 16). GOLD-053 — §53 horizons + target/feature
alignment, deterministic map_mode_to_horizon + align_horizon."""
import pytest
from core.signal_engine import (SignalHorizon, HORIZON_DEFINITIONS, map_mode_to_horizon,
                                 align_horizon, SIGNAL_CATALOG, IndicatorStatus,
                                 get_catalog_index, preserved_indicators)

pytestmark = pytest.mark.golden

SECTION_52_INDICATORS = [
    "trend", "momentum", "rsi", "macd", "moving_averages", "relative_strength",
    "volume", "rvol", "atr", "volatility", "market_regime", "sector",
    "earnings", "news", "sentiment", "ml",
]


def test_gold052_catalog_lists_exactly_section_52():
    assert sorted(s.name for s in SIGNAL_CATALOG) == sorted(SECTION_52_INDICATORS)
    assert len(SIGNAL_CATALOG) == 16  # do not add indicators merely because they exist


def test_gold052_preserved_have_real_sources_deferred_are_honest():
    for spec in preserved_indicators():
        assert spec.source_module and spec.status is IndicatorStatus.PRESERVED
    idx = get_catalog_index()
    assert idx["news"].status is IndicatorStatus.DEFERRED
    assert idx["sentiment"].status is IndicatorStatus.DEFERRED
    assert idx["ml"].status is IndicatorStatus.EVALUATE


def test_gold053_three_horizons_each_with_target_and_features():
    assert {h.value for h in SignalHorizon} == {"SHORT_TERM", "SWING", "MEDIUM_TERM"}
    for h in SignalHorizon:
        d = HORIZON_DEFINITIONS[h]
        assert d.sessions_min < d.sessions_max
        assert "P(" in d.target_condition()
        assert len(d.feature_timeframes) >= 2


def test_gold053_map_and_align_deterministic():
    assert map_mode_to_horizon("MOMENTUM") is SignalHorizon.SWING
    assert map_mode_to_horizon("PULLBACK") is SignalHorizon.SHORT_TERM
    assert map_mode_to_horizon("unknown") is SignalHorizon.SWING
    assert align_horizon(SignalHorizon.SHORT_TERM, "1m") is True
    assert align_horizon(SignalHorizon.SHORT_TERM, "1d") is False
    assert align_horizon(SignalHorizon.MEDIUM_TERM, "1d") is True