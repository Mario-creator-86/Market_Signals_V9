from __future__ import annotations

import pytest

from core.strategy_v2.evaluators import SuperTrendBosEvaluator
from core.strategy_v2.registry import build_default_registry
from core.strategy_v2.selector import StrategySelector


def _spec():
    return next(spec for spec in build_default_registry().specs if spec.id == "S8_SUPERTREND_BOS")


def _context(direction: str) -> dict:
    bullish = direction == "LONG"
    price = 100.0
    context = {
        "symbol": "BTC-USD",
        "asset_class": "CRYPTO_FUTURES",
        "price": price,
        "atr": 1.0,
        "atr_pct": 0.01,
        "volume": 2_400_000.0,
        "volume_avg": 2_000_000.0,
        "volume_ratio": 1.2,
        "spread_bps": 2.0,
        "hours_to_event": 999.0,
        "event_risk": False,
        "supertrend_direction": "BULLISH" if bullish else "BEARISH",
        "supertrend_last_flip_direction": "BULLISH" if bullish else "BEARISH",
        "supertrend_flip_age_bars": 3,
        "supertrend_line": 98.0 if bullish else 102.0,
        "supertrend_upper_band": 102.0,
        "supertrend_lower_band": 98.0,
        "supertrend_period": 10,
        "supertrend_multiplier": 3.0,
        "structure_last_event": "BOS",
        "structure_last_event_direction": "BULLISH" if bullish else "BEARISH",
        "structure_last_event_level": 99.5 if bullish else 100.5,
        "structure_event_age_bars": 0,
        "structure_trend": "BULLISH" if bullish else "BEARISH",
        "structural_support": 98.5,
        "structural_resistance": 101.5,
        "structure_last_high_label": "HH" if bullish else "LH",
        "structure_last_low_label": "HL" if bullish else "LL",
    }
    primary_keys = {
        "volume", "volume_ratio", "supertrend_direction", "supertrend_last_flip_direction",
        "supertrend_flip_age_bars", "supertrend_line", "supertrend_upper_band",
        "supertrend_lower_band", "supertrend_period", "supertrend_multiplier",
        "structure_last_event", "structure_last_event_direction", "structure_last_event_level",
        "structure_event_age_bars", "structure_trend", "structural_support",
        "structural_resistance", "structure_last_high_label", "structure_last_low_label",
    }
    primary = {key: value for key, value in context.items() if key in primary_keys}
    primary.update({"timeframe": "30m", "available": True, "bar_count": 120, "volume_average": 2_000_000.0})
    expected = "BULLISH" if bullish else "BEARISH"
    context.update({
        "s8_minimum_timeframe": "30m",
        "s8_primary_timeframe": "30m",
        "s8_confirmation_timeframes": ["1h", "4h"],
        "s8_timeframes": {
            "30m": primary,
            "1h": {"timeframe": "1h", "available": True, "bar_count": 120, "supertrend_direction": expected},
            "4h": {"timeframe": "4h", "available": True, "bar_count": 120, "supertrend_direction": "BEARISH" if bullish else "BULLISH"},
        },
    })
    return context


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_supertrend_bos_opens_only_matching_direction_and_persists_exact_evidence(direction):
    candidate = SuperTrendBosEvaluator().evaluate(_context(direction), _spec(), direction, "TREND")
    assert candidate is not None
    assert candidate.direction == direction
    assert candidate.rr >= 2.0
    evidence = candidate.meta["strategy_evidence"]
    assert evidence["supertrend"]["flip_direction"] == ("BULLISH" if direction == "LONG" else "BEARISH")
    assert evidence["market_structure"]["event"] == "BOS"
    assert [step["id"] for step in evidence["sequence"]] == [
        "supertrend_flip", "break_of_structure", "multi_timeframe_confirmation",
    ]
    assert evidence["primary_timeframe"] == "30m"
    assert evidence["aligned_timeframes"] == ["1h"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("structure_last_event", "CHOCH"),
        ("structure_last_event_direction", "BEARISH"),
        ("supertrend_last_flip_direction", "BEARISH"),
        ("supertrend_flip_age_bars", 9),
        ("structure_event_age_bars", 2),
    ],
)
def test_supertrend_bos_rejects_missing_stale_or_opposing_confirmation(field, value):
    context = _context("LONG")
    context[field] = value
    context["s8_timeframes"]["30m"][field] = value
    assert SuperTrendBosEvaluator().evaluate(context, _spec(), "LONG", "TREND") is None


def test_supertrend_bos_rejects_bos_that_happened_before_flip():
    context = _context("LONG")
    context["supertrend_flip_age_bars"] = 0
    context["structure_event_age_bars"] = 1
    context["s8_timeframes"]["30m"]["supertrend_flip_age_bars"] = 0
    context["s8_timeframes"]["30m"]["structure_event_age_bars"] = 1
    assert SuperTrendBosEvaluator().evaluate(context, _spec(), "LONG", "TREND") is None


def test_supertrend_bos_requires_30m_primary_and_both_higher_timeframes_to_be_evaluated():
    context = _context("LONG")
    context["s8_primary_timeframe"] = "15m"
    assert SuperTrendBosEvaluator().evaluate(context, _spec(), "LONG", "TREND") is None

    context = _context("LONG")
    context["s8_timeframes"]["4h"]["available"] = False
    assert SuperTrendBosEvaluator().evaluate(context, _spec(), "LONG", "TREND") is None


def test_supertrend_bos_requires_at_least_one_aligned_higher_timeframe():
    context = _context("LONG")
    context["s8_timeframes"]["1h"]["supertrend_direction"] = "BEARISH"
    assert SuperTrendBosEvaluator().evaluate(context, _spec(), "LONG", "TREND") is None


def test_registry_scopes_s8_to_requested_asset_classes():
    registry = build_default_registry()
    assert all(
        "S8_SUPERTREND_BOS" in {spec.id for spec in registry.get_specs_for_asset(asset)}
        for asset in ("US_STOCKS", "CRYPTO_FUTURES", "COMMODITIES")
    )
    assert "S8_SUPERTREND_BOS" not in {
        spec.id for spec in registry.get_specs_for_asset("FOREX_MAJORS")
    }


def test_selector_surfaces_unvalidated_s8_with_paper_research_provenance():
    selector = StrategySelector(
        config={
            "min_rr": 1.6,
            "min_sample_size": 24,
            "min_validation_score": 48,
            "allow_low_sample_fallback": False,
            "allow_low_validation_fallback": False,
            "require_profitable_performance": True,
            "emit_unvalidated_research_candidates": True,
            "research_only_strategy_ids": ["S8_SUPERTREND_BOS"],
            "enabled_strategies_by_asset_class": {
                "CRYPTO_FUTURES": ["S8_SUPERTREND_BOS"],
            },
            "quality_thresholds": {
                "CRYPTO_FUTURES": {
                    "min_volume": 1_000.0,
                    "max_spread_bps": 40.0,
                    "min_atr_pct": 0.001,
                },
            },
        }
    )
    selector.regime_engine.detect = lambda _context: {"regime": "TREND"}
    result = selector.select("BTC-USD", "CRYPTO_FUTURES", _context("LONG"))
    assert result["best_candidate"] is None
    assert result["research_candidates_considered"] == 1
    candidate = result["research_candidates"][0]
    assert candidate["strategy_id"] == "S8_SUPERTREND_BOS"
    assert candidate["meta"]["research_only"] is True
    assert candidate["meta"]["strategy_evidence"]["market_structure"]["event"] == "BOS"
