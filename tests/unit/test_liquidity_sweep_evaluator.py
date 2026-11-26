from __future__ import annotations

from core.strategy_v2.evaluators import LiquiditySweepReversalEvaluator
from core.strategy_v2.registry import StrategySpec
from core.strategy_v2.types import DIRECTION_LONG, DIRECTION_SHORT


SPEC = StrategySpec(
    id="S7_LIQUIDITY_SWEEP_REVERSAL",
    name="Liquidity Sweep + Structure Reversal",
    asset_classes=["CRYPTO_FUTURES"],
    regimes_allowed=["RANGE"],
)


def _context(**overrides):
    base = {
        "symbol": "BTCUSDT", "asset_class": "CRYPTO_FUTURES", "price": 100.0,
        "atr": 2.0, "atr_pct": 0.02, "volume": 2_000_000.0,
        "volume_avg": 1_000_000.0, "spread_bps": 2.0, "news_sentiment": "neutral",
        "news_pulse_score": 0.0, "liquidity_last_event": "SWEEP_RECLAIM",
        "liquidity_last_event_direction": "BULLISH", "liquidity_last_event_level": 96.0,
        "liquidity_event_age_bars": 1, "structure_event": "CHOCH",
        "structure_event_direction": "BULLISH", "structural_support": 95.0,
        "structural_resistance": 105.0,
    }
    return {**base, **overrides}


def test_emits_only_for_matching_sweep_and_structure_confirmation():
    candidate = LiquiditySweepReversalEvaluator().evaluate(
        _context(), SPEC, DIRECTION_LONG, "RANGE"
    )
    assert candidate is not None
    assert candidate.direction == DIRECTION_LONG
    assert "liquidity_sweep_reclaim" in candidate.reasons
    assert "choch_confirmation" in candidate.reasons
    evidence = candidate.meta["strategy_evidence"]
    assert evidence["liquidity"]["sweep_level"] == 96.0
    assert evidence["market_structure"]["event"] == "CHOCH"
    assert [step["id"] for step in evidence["sequence"]] == [
        "liquidity_sweep_reclaim",
        "market_structure_confirmation",
        "volume_confirmation",
    ]


def test_rejects_wick_sequence_without_bos_or_choch():
    candidate = LiquiditySweepReversalEvaluator().evaluate(
        _context(structure_event=None, structure_event_direction=None), SPEC, DIRECTION_LONG, "RANGE"
    )
    assert candidate is None


def test_rejects_stale_or_opposite_liquidity_event():
    evaluator = LiquiditySweepReversalEvaluator()
    assert evaluator.evaluate(_context(liquidity_event_age_bars=6), SPEC, DIRECTION_LONG, "RANGE") is None
    assert evaluator.evaluate(_context(liquidity_last_event_direction="BEARISH"), SPEC, DIRECTION_LONG, "RANGE") is None


def test_short_sequence_uses_bearish_sweep_and_break():
    candidate = LiquiditySweepReversalEvaluator().evaluate(
        _context(
            liquidity_last_event_direction="BEARISH",
            liquidity_last_event_level=104.0,
            structure_event="BOS",
            structure_event_direction="BEARISH",
        ),
        SPEC,
        DIRECTION_SHORT,
        "RANGE",
    )
    assert candidate is not None
    assert candidate.direction == DIRECTION_SHORT
