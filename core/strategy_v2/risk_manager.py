from __future__ import annotations

from typing import Any, Dict, Tuple

from .types import DIRECTION_LONG


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


class RiskManagerV2:
    def __init__(self, config: Dict[str, Any] | None = None):
        cfg = config or {}
        self.min_rr = _f(cfg.get("min_rr"), 2.0)
        self.default_sl_atr_mult = _f(cfg.get("default_sl_atr_mult"), 1.25)
        self.default_trail_atr_mult = _f(cfg.get("default_trail_atr_mult"), 2.2)

    def compute_levels(
        self,
        direction: str,
        entry: float,
        atr: float,
        swing_low: float | None = None,
        swing_high: float | None = None,
        min_rr: float | None = None,
        sl_atr_mult: float | None = None,
    ) -> Tuple[float, float, float]:
        d = str(direction or "").upper()
        entry = _f(entry, 0.0)
        atr = max(_f(atr, 0.0), entry * 0.002)
        sl_mult = _f(sl_atr_mult, self.default_sl_atr_mult)
        rr_target = max(_f(min_rr, self.min_rr), self.min_rr)

        if entry <= 0:
            return 0.0, 0.0, 0.0

        if d == DIRECTION_LONG:
            struct_sl = _f(swing_low, entry - atr * sl_mult)
            atr_sl = entry - (atr * sl_mult)
            sl = min(struct_sl if struct_sl > 0 else atr_sl, atr_sl)
            sl = min(sl, entry * 0.998)
            risk = max(entry - sl, entry * 0.002)
            tp1 = entry + (risk * rr_target)
            rr = (tp1 - entry) / max(risk, 1e-9)
            return sl, tp1, rr

        struct_sl = _f(swing_high, entry + atr * sl_mult)
        atr_sl = entry + (atr * sl_mult)
        sl = max(struct_sl if struct_sl > 0 else atr_sl, atr_sl)
        sl = max(sl, entry * 1.002)
        risk = max(sl - entry, entry * 0.002)
        tp1 = entry - (risk * rr_target)
        rr = (entry - tp1) / max(risk, 1e-9)
        return sl, tp1, rr

    def trailing_rule(self, asset_class: str, regime: str) -> str:
        ac = str(asset_class or "").upper()
        rg = str(regime or "").upper()
        if ac == "CRYPTO_FUTURES" or rg == "HIGH_VOL":
            return f"ATR_TRAIL x{self.default_trail_atr_mult:.2f} after TP1"
        if ac == "FOREX_MAJORS":
            return "STRUCTURE_TRAIL lower-highs/higher-lows after TP1"
        return f"ATR_TRAIL x{max(1.8, self.default_trail_atr_mult - 0.3):.2f} after TP1"

    @staticmethod
    def invalidation_condition(direction: str, level: float) -> str:
        d = str(direction or "").upper()
        if d == DIRECTION_LONG:
            return f"Invalidate if close < {level:.6f}"
        return f"Invalidate if close > {level:.6f}"

