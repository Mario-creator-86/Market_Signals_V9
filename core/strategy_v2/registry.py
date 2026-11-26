from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from .types import CandidateSignal


@dataclass
class StrategySpec:
    id: str
    name: str
    asset_classes: List[str]
    regimes_allowed: List[str]
    params_default_by_asset: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    features_required: List[str] = field(default_factory=list)
    entry_rules: str = ""
    exit_rules: str = ""
    rr_policy: str = "RR>=2.0 mandatory"
    notes: str = ""


class StrategyEvaluator(Protocol):
    def evaluate(
        self,
        context: Dict[str, Any],
        spec: StrategySpec,
        direction: str,
        regime: str,
    ) -> Optional[CandidateSignal]:
        ...


class StrategyRegistry:
    def __init__(self, specs: List[StrategySpec], evaluators: Dict[str, StrategyEvaluator]):
        self.specs = list(specs or [])
        self.evaluators = dict(evaluators or {})

    def get_evaluator(self, strategy_id: str) -> Optional[StrategyEvaluator]:
        return self.evaluators.get(str(strategy_id or "").upper())

    def get_specs_for_asset(self, asset_class: str) -> List[StrategySpec]:
        ac = str(asset_class or "").upper()
        return [s for s in self.specs if ac in {x.upper() for x in s.asset_classes}]


def build_default_registry() -> StrategyRegistry:
    from .evaluators import (
        BreakoutRetestEvaluator,
        FxSessionBreakoutEvaluator,
        MeanReversionEvaluator,
        MomentumContinuationEvaluator,
        LiquiditySweepReversalEvaluator,
        SuperTrendBosEvaluator,
        TrendPullbackEvaluator,
        VolatilitySqueezeEvaluator,
    )

    specs = [
        StrategySpec(
            id="S1_TREND_PULLBACK",
            name="Trend Pullback (HTF bias + LTF trigger)",
            asset_classes=["US_STOCKS", "COMMODITIES", "FOREX_MAJORS"],
            regimes_allowed=["TREND"],
            features_required=["ema20", "ema200", "adx", "atr", "volume"],
            entry_rules="Pullback into EMA20/VWAP + confirmation candle + volume",
            exit_rules="SL below/above pullback swing, TP1 >= 2R then trailing",
        ),
        StrategySpec(
            id="S2_BREAKOUT_RETEST",
            name="Breakout + Retest",
            asset_classes=["US_STOCKS", "CRYPTO_FUTURES", "FOREX_MAJORS"],
            regimes_allowed=["TREND", "RANGE", "SESSION_BREAKOUT"],
            features_required=["support", "resistance", "atr", "volume"],
            entry_rules="Breakout and successful retest hold/reject",
            exit_rules="SL beyond retest level, TP1 >= 2R then trailing",
        ),
        StrategySpec(
            id="S3_VOL_SQUEEZE",
            name="Volatility Squeeze Expansion",
            asset_classes=["US_STOCKS", "CRYPTO_FUTURES"],
            regimes_allowed=["RANGE", "TREND"],
            features_required=["bb_width", "atr_pct", "volume", "resistance", "support"],
            entry_rules="Low-vol squeeze then expansion breakout with volume",
            exit_rules="SL at squeeze base, TP1 >= 2R then ATR trailing",
        ),
        StrategySpec(
            id="S4_MEAN_REVERSION",
            name="Mean Reversion (Range Only)",
            asset_classes=["FOREX_MAJORS", "COMMODITIES", "CRYPTO_FUTURES"],
            regimes_allowed=["RANGE"],
            features_required=["rsi", "bb_upper", "bb_lower", "bb_mid", "atr"],
            entry_rules="Fade BB extremes with RSI extreme + failure swing",
            exit_rules="Tight SL beyond range, TP midline/extension with RR>=2",
        ),
        StrategySpec(
            id="S5_MOMENTUM_CONTINUATION",
            name="Momentum Continuation",
            asset_classes=["CRYPTO_FUTURES", "COMMODITIES"],
            regimes_allowed=["HIGH_VOL", "TREND"],
            features_required=["atr_pct", "volume", "resistance", "support"],
            entry_rules="Impulse + flag consolidation + breakout with volume",
            exit_rules="SL below/above consolidation, TP1 >= 2R, trail runner",
        ),
        StrategySpec(
            id="S6_FX_SESSION_BREAKOUT",
            name="FX London/NY Session Breakout",
            asset_classes=["FOREX_MAJORS"],
            regimes_allowed=["SESSION_BREAKOUT", "TREND"],
            features_required=["asian_high", "asian_low", "ema200", "adx"],
            entry_rules="Asian range breakout aligned with HTF bias",
            exit_rules="SL inside range, TP1 >= 2R, optional trail",
        ),
        StrategySpec(
            id="S7_LIQUIDITY_SWEEP_REVERSAL",
            name="Liquidity Sweep + Structure Reversal",
            asset_classes=["US_STOCKS", "CRYPTO_FUTURES", "FOREX_MAJORS", "COMMODITIES"],
            regimes_allowed=["RANGE", "TREND", "HIGH_VOL", "SESSION_BREAKOUT"],
            features_required=[
                "liquidity_last_event_direction", "liquidity_event_age_bars",
                "structure_event", "structure_event_direction", "volume", "atr",
            ],
            entry_rules="Sweep/reclaim followed within five bars by matching BOS/CHoCH and volume confirmation",
            exit_rules="SL beyond swept liquidity; TP1 >= 2R then trailing",
            notes="Requires causal confirmed pivots; a wick without reclaim/structure break is rejected.",
        ),
        StrategySpec(
            id="S8_SUPERTREND_BOS",
            name="SuperTrend Flip + Break of Structure",
            asset_classes=["US_STOCKS", "CRYPTO_FUTURES", "COMMODITIES"],
            regimes_allowed=["RANGE", "TREND", "HIGH_VOL", "SESSION_BREAKOUT"],
            params_default_by_asset={
                "US_STOCKS": {"period": 10, "multiplier": 3.0, "primary_timeframe": "30m", "confirmation_timeframes": ["1h", "4h"], "max_flip_to_bos_bars": 8},
                "CRYPTO_FUTURES": {"period": 10, "multiplier": 3.0, "primary_timeframe": "30m", "confirmation_timeframes": ["1h", "4h"], "max_flip_to_bos_bars": 8},
                "COMMODITIES": {"period": 10, "multiplier": 3.0, "primary_timeframe": "30m", "confirmation_timeframes": ["1h", "4h"], "max_flip_to_bos_bars": 8},
            },
            features_required=[
                "s8_timeframes", "s8_primary_timeframe", "supertrend_direction",
                "supertrend_last_flip_direction", "supertrend_flip_age_bars",
                "structure_last_event", "structure_last_event_direction",
                "structure_event_age_bars", "atr",
            ],
            entry_rules="30m SuperTrend flip followed by matching causal BOS, checked against 1h and 4h with at least one higher-timeframe alignment",
            exit_rules="SL beyond SuperTrend/structure; TP1 >= 2R then trailing",
            notes="30m is the minimum trigger timeframe. Both 1h and 4h are evaluated; at least one must align. BOS must occur on or after the flip.",
        ),
    ]

    evaluators: Dict[str, StrategyEvaluator] = {
        "S1_TREND_PULLBACK": TrendPullbackEvaluator(),
        "S2_BREAKOUT_RETEST": BreakoutRetestEvaluator(),
        "S3_VOL_SQUEEZE": VolatilitySqueezeEvaluator(),
        "S4_MEAN_REVERSION": MeanReversionEvaluator(),
        "S5_MOMENTUM_CONTINUATION": MomentumContinuationEvaluator(),
        "S6_FX_SESSION_BREAKOUT": FxSessionBreakoutEvaluator(),
        "S7_LIQUIDITY_SWEEP_REVERSAL": LiquiditySweepReversalEvaluator(),
        "S8_SUPERTREND_BOS": SuperTrendBosEvaluator(),
    }
    return StrategyRegistry(specs=specs, evaluators=evaluators)
