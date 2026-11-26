"""§53 Signal Horizons — SHORT_TERM / SWING / MEDIUM_TERM.

Targets/features must align with horizon (§53). The existing V7 per-mode
strings (core/scoring_system.TIMEFRAME_MAP / HOLDING_PERIOD_MAP) are evolved
into this enum via map_mode_to_horizon; the strings are not removed."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Mapping


class SignalHorizon(str, Enum):
    SHORT_TERM = "SHORT_TERM"
    SWING = "SWING"
    MEDIUM_TERM = "MEDIUM_TERM"


@dataclass(frozen=True)
class HorizonDefinition:
    horizon: SignalHorizon
    sessions_min: int
    sessions_max: int
    target_condition_template: str  # §55 seed
    feature_timeframes: FrozenSet[str]

    def target_condition(self) -> str:
        return self.target_condition_template.format(
            n=f"{self.sessions_min}-{self.sessions_max}"
        )


HORIZON_DEFINITIONS: Mapping[SignalHorizon, HorizonDefinition] = {
    SignalHorizon.SHORT_TERM: HorizonDefinition(
        horizon=SignalHorizon.SHORT_TERM, sessions_min=1, sessions_max=5,
        target_condition_template="P(return > 0 over next {n} sessions)",
        feature_timeframes=frozenset({"1m", "5m", "15m"}),
    ),
    SignalHorizon.SWING: HorizonDefinition(
        horizon=SignalHorizon.SWING, sessions_min=5, sessions_max=20,
        target_condition_template="P(return > 0 over next {n} sessions)",
        feature_timeframes=frozenset({"15m", "1h", "4h"}),
    ),
    SignalHorizon.MEDIUM_TERM: HorizonDefinition(
        horizon=SignalHorizon.MEDIUM_TERM, sessions_min=20, sessions_max=60,
        target_condition_template="P(return > 0 over next {n} sessions)",
        feature_timeframes=frozenset({"1h", "4h", "1d"}),
    ),
}

_MODE_TO_HORIZON: Mapping[str, SignalHorizon] = {
    "PULLBACK": SignalHorizon.SHORT_TERM,
    "BREAKOUT": SignalHorizon.SHORT_TERM,
    "MOMENTUM": SignalHorizon.SWING,
    "MEAN_REVERSION": SignalHorizon.SWING,
    "TrendPullback": SignalHorizon.SWING,
    "BreakoutRetest": SignalHorizon.SHORT_TERM,
    "VolSqueeze": SignalHorizon.SWING,
    "MeanReversion": SignalHorizon.SWING,
    "MomentumContinuation": SignalHorizon.SHORT_TERM,
    "FxSessionBreakout": SignalHorizon.SHORT_TERM,
    "LiquiditySweepReversal": SignalHorizon.SHORT_TERM,
    "SuperTrendBos": SignalHorizon.SWING,
}


def map_mode_to_horizon(mode: str) -> SignalHorizon:
    """Map a V7 mode/strategy name to a §53 horizon. Unknown → SWING (fail-safe)."""
    if not isinstance(mode, str):
        return SignalHorizon.SWING
    key = mode.strip()
    if key.upper() in _MODE_TO_HORIZON:
        return _MODE_TO_HORIZON[key.upper()]
    if key in _MODE_TO_HORIZON:
        return _MODE_TO_HORIZON[key]
    return SignalHorizon.SWING


def align_horizon(horizon: SignalHorizon, feature_timeframe: str) -> bool:
    """§53 alignment guard: a feature timeframe must be in the horizon's set."""
    if not isinstance(horizon, SignalHorizon) or not isinstance(feature_timeframe, str):
        return False
    return feature_timeframe.strip().lower() in HORIZON_DEFINITIONS[horizon].feature_timeframes