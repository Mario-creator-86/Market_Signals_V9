from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .types import CandidateSignal


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


class QualityGates:
    DEFAULTS = {
        "US_STOCKS": {"min_volume": 250_000.0, "max_spread_bps": 30.0, "min_atr_pct": 0.003},
        "CRYPTO_FUTURES": {"min_volume": 1_500_000.0, "max_spread_bps": 18.0, "min_atr_pct": 0.004},
        "FOREX_MAJORS": {"min_volume": 150_000.0, "max_spread_bps": 6.0, "min_atr_pct": 0.0018},
        "COMMODITIES": {"min_volume": 100_000.0, "max_spread_bps": 22.0, "min_atr_pct": 0.002},
    }

    def __init__(self, config: Dict[str, Any] | None = None):
        self.config = config or {}

    def evaluate(
        self,
        context: Dict[str, Any],
        candidate: CandidateSignal,
    ) -> Tuple[bool, List[str]]:
        reasons: List[str] = []
        asset_class = str(candidate.asset_class or "").upper()
        thresholds = dict(self.DEFAULTS.get(asset_class, self.DEFAULTS["US_STOCKS"]))
        thresholds.update((self.config.get("quality_thresholds") or {}).get(asset_class, {}))

        volume = _f(context.get("volume"), 0.0)
        spread_bps = _f(context.get("spread_bps"), 0.0)
        atr_pct = _f(context.get("atr_pct"), 0.0)
        hours_to_event = _f(context.get("hours_to_event"), 999.0)
        event_risk = bool(context.get("event_risk", False))

        # Admission is configurable, but a strategy may never enter with a
        # reward/risk profile below the positive-edge safety floor.
        min_rr = max(_f(self.config.get("min_rr"), 2.0), 1.2)
        event_block_hours = max(_f(self.config.get("event_block_hours"), 6.0), 0.0)

        if candidate.rr < min_rr:
            reasons.append(f"rr_below_min:{candidate.rr:.2f}<{min_rr:.2f}")

        if volume < _f(thresholds.get("min_volume"), 0.0):
            reasons.append("low_liquidity")

        if spread_bps > _f(thresholds.get("max_spread_bps"), 9999.0):
            reasons.append("spread_too_wide")

        if atr_pct < _f(thresholds.get("min_atr_pct"), 0.0):
            reasons.append("noise_block_low_atr")

        if event_risk and hours_to_event <= event_block_hours:
            reasons.append(f"event_block:{hours_to_event:.1f}h")

        return (len(reasons) == 0), reasons
