from core.signal_engine.horizons import (
    SignalHorizon, HORIZON_DEFINITIONS, map_mode_to_horizon, align_horizon,
)


def test_horizons_enum():
    assert {h.value for h in SignalHorizon} == {"SHORT_TERM", "SWING", "MEDIUM_TERM"}


def test_map_mode_to_horizon_fail_safe():
    assert map_mode_to_horizon("MOMENTUM") is SignalHorizon.SWING
    assert map_mode_to_horizon("PULLBACK") is SignalHorizon.SHORT_TERM
    assert map_mode_to_horizon("nope") is SignalHorizon.SWING
    assert map_mode_to_horizon(None) is SignalHorizon.SWING


def test_align_horizon():
    assert align_horizon(SignalHorizon.SHORT_TERM, "1m") is True
    assert align_horizon(SignalHorizon.SHORT_TERM, "1d") is False
    assert align_horizon(SignalHorizon.MEDIUM_TERM, "1d") is True

from core.signal_engine.catalog import (
    IndicatorStatus, SIGNAL_CATALOG, get_catalog_index, preserved_indicators,
)

SECTION_52_INDICATORS = [
    "trend", "momentum", "rsi", "macd", "moving_averages", "relative_strength",
    "volume", "rvol", "atr", "volatility", "market_regime", "sector",
    "earnings", "news", "sentiment", "ml",
]


def test_catalog_lists_section_52_exactly():
    assert sorted(s.name for s in SIGNAL_CATALOG) == sorted(SECTION_52_INDICATORS)
    assert len(SIGNAL_CATALOG) == 16


def test_preserved_have_source_modules():
    for spec in preserved_indicators():
        assert spec.source_module
        assert spec.status is IndicatorStatus.PRESERVED


def test_news_sentiment_deferred_ml_evaluate():
    idx = get_catalog_index()
    assert idx["news"].status is IndicatorStatus.DEFERRED
    assert idx["sentiment"].status is IndicatorStatus.DEFERRED
    assert idx["ml"].status is IndicatorStatus.EVALUATE


from core.signal_engine.regime import (
    MarketRegimeEngine, detect_macro_regime, MARKET_RISK_ON, MARKET_RISK_OFF, MARKET_MIXED,
)


def test_market_regime_risk_on():
    engine = MarketRegimeEngine(universe_min=5)
    regimes = [{"regime": "TREND"}] * 7 + [{"regime": "RANGE"}] * 3
    res = engine.detect_market(regimes)
    assert res.label == MARKET_RISK_ON
    assert res.is_not_probability is True
    assert res.sample_size == 10


def test_market_regime_insufficient_is_stale():
    engine = MarketRegimeEngine(universe_min=10)
    res = engine.detect_market([{"regime": "TREND"}])
    assert res.is_stale is True
    assert res.label == MARKET_MIXED


def test_macro_regime_is_honest_stub():
    m = detect_macro_regime()
    assert m["available"] is False
    assert "deferred" in m["reason"]


from core.signal_engine import Signal, SignalHorizon


def test_signal_from_dict_roundtrip():
    raw = {"symbol": "NVDA", "direction": "LONG", "signal_score": 72.0,
           "mode": "MOMENTUM", "signal_type": "v1_logistic", "source": "core.scoring_system"}
    sig = Signal.from_dict(raw)
    assert sig.ticker == "NVDA"
    assert sig.horizon is SignalHorizon.SWING
    assert sig.is_not_probability is True
    out = sig.to_dict()
    assert out["horizon"] == "SWING"
    assert out["signal_horizon"] == "SWING"
    assert out["symbol"] == "NVDA"


def test_ml_and_signal_engine_config_present():
    # §54/§49 — DEFAULT_CONFIG carries the additive `ml` + `signal_engine` blocks
    # (registry/qlib_enabled/baseline_feature_version + market_regime_universe_min).
    from core.config import DEFAULT_CONFIG
    assert "ml" in DEFAULT_CONFIG
    ml = DEFAULT_CONFIG["ml"]
    assert "registry" in ml and ml["registry"] == []
    assert "qlib_enabled" in ml and ml["qlib_enabled"] is False
    assert "baseline_feature_version" in ml
    assert "signal_engine" in DEFAULT_CONFIG
    assert DEFAULT_CONFIG["signal_engine"]["market_regime_universe_min"] == 10
