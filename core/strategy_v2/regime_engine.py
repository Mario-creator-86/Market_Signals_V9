from __future__ import annotations

from typing import Any, Dict

from .types import (
    REGIME_EVENT_RISK,
    REGIME_HIGH_VOL,
    REGIME_RANGE,
    REGIME_SESSION_BREAKOUT,
    REGIME_TREND,
)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


class RegimeEngine:
    def __init__(self, config: Dict[str, Any] | None = None):
        cfg = config or {}
        self.adx_trend_min = _f(cfg.get("adx_trend_min"), 24.0)
        self.adx_range_max = _f(cfg.get("adx_range_max"), 18.0)
        self.atr_pct_high = _f(cfg.get("atr_pct_high"), 0.025)
        self.bb_width_high = _f(cfg.get("bb_width_high"), 0.065)

    def detect(self, context: Dict[str, Any]) -> Dict[str, Any]:
        event_risk = bool(context.get("event_risk", False))
        asset_class = str(context.get("asset_class") or "").upper()
        session_breakout = bool(context.get("session_breakout", False))

        adx = _f(context.get("adx"), 0.0)
        ema_slope = _f(context.get("ema_slope"), 0.0)
        macd_hist = _f(context.get("macd_hist"), 0.0)
        atr_pct = _f(context.get("atr_pct"), 0.0)
        bb_width = _f(context.get("bb_width"), 0.0)

        trend_components = 0
        if adx >= self.adx_trend_min:
            trend_components += 1
        if abs(ema_slope) > 0.0008:
            trend_components += 1
        if abs(macd_hist) > 0.01:
            trend_components += 1

        range_components = 0
        if adx <= self.adx_range_max:
            range_components += 1
        if abs(macd_hist) < 0.008:
            range_components += 1
        if abs(ema_slope) < 0.0006:
            range_components += 1

        if event_risk:
            return {
                "regime": REGIME_EVENT_RISK,
                "confidence": 0.92,
                "sub_scores": {
                    "trend_strength": trend_components / 3.0,
                    "range_likelihood": range_components / 3.0,
                    "volatility": max(atr_pct, bb_width),
                },
            }

        if asset_class == "FOREX_MAJORS" and session_breakout:
            return {
                "regime": REGIME_SESSION_BREAKOUT,
                "confidence": 0.78,
                "sub_scores": {
                    "trend_strength": trend_components / 3.0,
                    "range_likelihood": range_components / 3.0,
                    "volatility": max(atr_pct, bb_width),
                },
            }

        if atr_pct >= self.atr_pct_high or bb_width >= self.bb_width_high:
            conf = min(0.9, 0.55 + max(atr_pct / max(self.atr_pct_high, 1e-6), bb_width / max(self.bb_width_high, 1e-6)) * 0.08)
            return {
                "regime": REGIME_HIGH_VOL,
                "confidence": conf,
                "sub_scores": {
                    "trend_strength": trend_components / 3.0,
                    "range_likelihood": range_components / 3.0,
                    "volatility": max(atr_pct, bb_width),
                },
            }

        if trend_components >= 2 and adx > self.adx_range_max:
            conf = min(0.9, 0.52 + (trend_components * 0.12))
            return {
                "regime": REGIME_TREND,
                "confidence": conf,
                "sub_scores": {
                    "trend_strength": trend_components / 3.0,
                    "range_likelihood": range_components / 3.0,
                    "volatility": max(atr_pct, bb_width),
                },
            }

        conf = min(0.88, 0.48 + (range_components * 0.13))
        return {
            "regime": REGIME_RANGE,
            "confidence": conf,
            "sub_scores": {
                "trend_strength": trend_components / 3.0,
                "range_likelihood": range_components / 3.0,
                "volatility": max(atr_pct, bb_width),
            },
        }

