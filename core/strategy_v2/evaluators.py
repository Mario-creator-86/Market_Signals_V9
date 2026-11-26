from __future__ import annotations

from typing import Any, Dict, Optional

from .registry import StrategySpec
from .risk_manager import RiskManagerV2
from .types import CandidateSignal, DIRECTION_LONG, DIRECTION_SHORT
from .confluence import score_confluence


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _news_catalyst_score(news_sentiment: str, direction: str, news_score: float) -> float:
    sentiment = str(news_sentiment or "neutral").lower()
    base = min(100.0, max(0.0, _f(news_score, 0.0)))
    if direction == DIRECTION_LONG and sentiment == "bullish":
        return min(100.0, 55.0 + base * 0.5)
    if direction == DIRECTION_SHORT and sentiment == "bearish":
        return min(100.0, 55.0 + base * 0.5)
    if sentiment == "neutral":
        return min(70.0, 35.0 + base * 0.3)
    # Runda-6 D5 (sign-flip): this fallback covers OPPOSING sentiment (LONG+bearish,
    # SHORT+bullish). Pre-fix `30.0 + base*0.2` made a STRONGER opposing news
    # magnitude ADD to the catalyst score (bearish headline on a LONG boosted the
    # signal) -- an inverted sign. Opposing news must REDUCE catalyst, scaling
    # with magnitude (0.3 mirrors the neutral branch scale), clamped at 0 (full
    # neutralization of the base-30 floor, hence 0 not 15). Correctness fix: a
    # bearish-news LONG should score LOWER, not higher. Forward-validate the WR
    # effect (fewer bad longs/shorts -> expect higher WR); the direction is
    # unambiguous (opposing news never boosts), so no A/B on the sign itself.
    return max(0.0, 30.0 - base * 0.3)


def _risk_score(context: Dict[str, Any], rr: float) -> float:
    event_pen = 25.0 if bool(context.get("event_risk", False)) else 0.0
    spread_pen = min(25.0, _f(context.get("spread_bps"), 0.0) * 0.9)
    vol_pen = 0.0
    atr_pct = _f(context.get("atr_pct"), 0.0)
    if atr_pct > 0.05:
        vol_pen += 12.0
    elif atr_pct < 0.001:
        vol_pen += 18.0
    rr_bonus = min(18.0, max(0.0, (rr - 2.0) * 8.0))
    raw = 85.0 - event_pen - spread_pen - vol_pen + rr_bonus
    return max(0.0, min(100.0, raw))


class BaseEvaluator:
    def __init__(self):
        self.risk_manager = RiskManagerV2()

    def _build_candidate(
        self,
        context: Dict[str, Any],
        spec: StrategySpec,
        direction: str,
        regime: str,
        setup_score: float,
        reasons: list[str],
        swing_low: Optional[float] = None,
        swing_high: Optional[float] = None,
    ) -> Optional[CandidateSignal]:
        entry = _f(context.get("price"), 0.0)
        atr = _f(context.get("atr"), max(entry * 0.003, 0.01))
        if entry <= 0:
            return None

        sl, tp1, rr = self.risk_manager.compute_levels(
            direction=direction,
            entry=entry,
            atr=atr,
            swing_low=swing_low if swing_low is not None else _f(context.get("swing_low"), 0.0),
            swing_high=swing_high if swing_high is not None else _f(context.get("swing_high"), 0.0),
        )
        if sl <= 0 or tp1 <= 0 or rr < 2.0:
            return None

        catalyst = _news_catalyst_score(
            news_sentiment=str(context.get("news_sentiment", "neutral")),
            direction=direction,
            news_score=_f(context.get("news_pulse_score"), 0.0),
        )
        risk_score = _risk_score(context, rr)
        confluence = score_confluence(context, direction)
        candidate_reasons = list(reasons or [])
        if _f(confluence.get("coverage"), 0.0) > 0:
            candidate_reasons.append(f"confluence_{_f(confluence.get('score'), 50.0):.0f}")

        return CandidateSignal(
            strategy_id=spec.id,
            strategy_name=spec.name,
            symbol=str(context.get("symbol") or "").upper(),
            asset_class=str(context.get("asset_class") or "").upper(),
            direction=direction,
            regime=regime,
            entry=entry,
            sl=sl,
            tp1=tp1,
            rr=rr,
            trailing_rule=self.risk_manager.trailing_rule(str(context.get("asset_class") or ""), regime),
            invalidation_condition=self.risk_manager.invalidation_condition(direction, sl),
            setup_score=max(0.0, min(100.0, setup_score)),
            catalyst_score=max(0.0, min(100.0, catalyst)),
            risk_score=max(0.0, min(100.0, risk_score)),
            confluence_score=max(0.0, min(100.0, _f(confluence.get("score"), 50.0))),
            confidence=max(0.0, min(1.0, setup_score / 100.0)),
            reasons=candidate_reasons,
            meta={
                "ema20": _f(context.get("ema20"), 0.0),
                "ema200": _f(context.get("ema200"), 0.0),
                "adx": _f(context.get("adx"), 0.0),
                "rsi": _f(context.get("rsi"), 0.0),
                "bb_width": _f(context.get("bb_width"), 0.0),
                "atr_pct": _f(context.get("atr_pct"), 0.0),
                "confluence": confluence,
            },
        )


class TrendPullbackEvaluator(BaseEvaluator):
    def evaluate(self, context: Dict[str, Any], spec: StrategySpec, direction: str, regime: str) -> Optional[CandidateSignal]:
        if str(regime).upper() != "TREND":
            return None

        price = _f(context.get("price"), 0.0)
        ema20 = _f(context.get("ema20"), price)
        ema200 = _f(context.get("ema200"), price)
        adx = _f(context.get("adx"), 0.0)
        volume = _f(context.get("volume"), 0.0)
        volume_avg = max(_f(context.get("volume_avg"), 0.0), 1.0)
        candle_dir = str(context.get("candle_dir") or "")

        reasons: list[str] = []
        score = 48.0
        if adx >= 22:
            score += 10
            reasons.append("adx_trend_ok")
        if volume >= volume_avg * 1.05:
            score += 8
            reasons.append("volume_confirm")

        if direction == DIRECTION_LONG:
            if not (price > ema200 and price <= ema20 * 1.012):
                return None
            if candle_dir == "bull":
                score += 10
                reasons.append("bullish_confirmation")
            return self._build_candidate(context, spec, direction, regime, score, reasons)

        if not (price < ema200 and price >= ema20 * 0.988):
            return None
        if candle_dir == "bear":
            score += 10
            reasons.append("bearish_confirmation")
        return self._build_candidate(context, spec, direction, regime, score, reasons)


class BreakoutRetestEvaluator(BaseEvaluator):
    def evaluate(self, context: Dict[str, Any], spec: StrategySpec, direction: str, regime: str) -> Optional[CandidateSignal]:
        price = _f(context.get("price"), 0.0)
        resistance = _f(context.get("resistance"), price * 1.01)
        support = _f(context.get("support"), price * 0.99)
        volume = _f(context.get("volume"), 0.0)
        volume_avg = max(_f(context.get("volume_avg"), 0.0), 1.0)
        adx = _f(context.get("adx"), 0.0)
        reasons: list[str] = []
        score = 44.0

        if volume >= volume_avg * 1.12:
            score += 10
            reasons.append("volume_expansion")
        if adx >= 20:
            score += 8
            reasons.append("trend_energy")

        if direction == DIRECTION_LONG:
            cond = price > resistance and bool(context.get("retest_hold_long", False))
            if not cond:
                return None
            score += 14
            reasons.append("breakout_retest_long")
            return self._build_candidate(context, spec, direction, regime, score, reasons, swing_low=support)

        cond = price < support and bool(context.get("retest_hold_short", False))
        if not cond:
            return None
        score += 14
        reasons.append("breakout_retest_short")
        return self._build_candidate(context, spec, direction, regime, score, reasons, swing_high=resistance)


class VolatilitySqueezeEvaluator(BaseEvaluator):
    def evaluate(self, context: Dict[str, Any], spec: StrategySpec, direction: str, regime: str) -> Optional[CandidateSignal]:
        if not bool(context.get("squeeze_active", False)):
            return None

        reasons: list[str] = ["squeeze_detected"]
        score = 46.0
        if _f(context.get("volume"), 0.0) >= max(_f(context.get("volume_avg"), 1.0) * 1.15, 1.0):
            score += 10
            reasons.append("volume_expansion")

        if direction == DIRECTION_LONG:
            if not bool(context.get("squeeze_breakout_up", False)):
                return None
            score += 15
            reasons.append("breakout_up")
            return self._build_candidate(context, spec, direction, regime, score, reasons)

        if not bool(context.get("squeeze_breakout_down", False)):
            return None
        score += 15
        reasons.append("breakout_down")
        return self._build_candidate(context, spec, direction, regime, score, reasons)


class MeanReversionEvaluator(BaseEvaluator):
    def evaluate(self, context: Dict[str, Any], spec: StrategySpec, direction: str, regime: str) -> Optional[CandidateSignal]:
        if str(regime).upper() != "RANGE":
            return None

        price = _f(context.get("price"), 0.0)
        bb_upper = _f(context.get("bb_upper"), price * 1.01)
        bb_lower = _f(context.get("bb_lower"), price * 0.99)
        rsi = _f(context.get("rsi"), 50.0)
        adx = _f(context.get("adx"), 30.0)
        reasons: list[str] = []
        score = 43.0
        if adx <= 20:
            score += 10
            reasons.append("low_adx_range")

        if direction == DIRECTION_LONG:
            if not (price <= bb_lower * 1.004 and rsi <= 34):
                return None
            score += 16
            reasons.append("range_extreme_long")
            return self._build_candidate(
                context,
                spec,
                direction,
                regime,
                score,
                reasons,
                swing_low=min(_f(context.get("swing_low"), price * 0.99), bb_lower * 0.998),
            )

        if not (price >= bb_upper * 0.996 and rsi >= 66):
            return None
        score += 16
        reasons.append("range_extreme_short")
        return self._build_candidate(
            context,
            spec,
            direction,
            regime,
            score,
            reasons,
            swing_high=max(_f(context.get("swing_high"), price * 1.01), bb_upper * 1.002),
        )


class MomentumContinuationEvaluator(BaseEvaluator):
    def evaluate(self, context: Dict[str, Any], spec: StrategySpec, direction: str, regime: str) -> Optional[CandidateSignal]:
        asset_class = str(context.get("asset_class") or "").upper()
        if asset_class not in {"CRYPTO_FUTURES", "COMMODITIES"}:
            return None
        if str(regime).upper() not in {"HIGH_VOL", "TREND"}:
            return None

        reasons: list[str] = []
        score = 47.0
        if _f(context.get("volume"), 0.0) > _f(context.get("volume_avg"), 1.0) * 1.1:
            score += 8
            reasons.append("volume_confirm")

        if direction == DIRECTION_LONG:
            if not (bool(context.get("impulse_up", False)) and bool(context.get("consolidation_break_up", False))):
                return None
            score += 16
            reasons.append("impulse_flag_break_long")
            return self._build_candidate(context, spec, direction, regime, score, reasons)

        if not (bool(context.get("impulse_down", False)) and bool(context.get("consolidation_break_down", False))):
            return None
        score += 16
        reasons.append("impulse_flag_break_short")
        return self._build_candidate(context, spec, direction, regime, score, reasons)


class LiquiditySweepReversalEvaluator(BaseEvaluator):
    """Trade a causal sequence: sweep/reclaim -> BOS or CHoCH confirmation.

    A liquidity wick alone is intentionally insufficient.  The structural break
    must occur after the reclaim, with volume confirmation, before a candidate
    can be created.
    """

    MAX_SWEEP_TO_STRUCTURE_BARS = 5

    @staticmethod
    def _attach_evidence(
        candidate: Optional[CandidateSignal],
        context: Dict[str, Any],
        *,
        structure_event: str,
        structure_direction: str,
        liquidity_direction: str,
        liquidity_age: float,
        sweep_level: float,
    ) -> Optional[CandidateSignal]:
        """Persist the factual S7 trigger chain for Capital and the AI reviewer.

        This is deliberately structured market data, not generated commentary.  The
        same fields are rendered in Capital and passed to the AI review so the
        explanation can cite the exact sweep, structure confirmation and levels
        that produced the candidate.
        """
        if candidate is None:
            return None

        direction = str(candidate.direction or "").upper()
        pattern_status = str(context.get("chart_pattern_status") or "NONE").upper()
        pattern_name = str(context.get("chart_pattern_name") or "").strip()
        pattern_direction = str(context.get("chart_pattern_direction") or "").upper()
        volume = _f(context.get("volume"), 0.0)
        volume_avg = _f(context.get("volume_average"), _f(context.get("volume_avg"), 0.0))
        volume_ratio = _f(context.get("volume_ratio"), volume / volume_avg if volume_avg > 0 else 0.0)
        support = _f(context.get("structural_support"), 0.0)
        resistance = _f(context.get("structural_resistance"), 0.0)

        sequence = [
            {
                "step": 1,
                "id": "liquidity_sweep_reclaim",
                "status": "confirmed",
                "detail": (
                    f"{liquidity_direction.title()} liquidity sweep reclaimed at "
                    f"{sweep_level:.8f}; confirmation age {int(liquidity_age)} bar(s)."
                ),
            },
            {
                "step": 2,
                "id": "market_structure_confirmation",
                "status": "confirmed",
                "detail": f"{structure_direction.title()} {structure_event} confirmed after the sweep.",
            },
            {
                "step": 3,
                "id": "volume_confirmation",
                "status": "confirmed",
                "detail": f"Volume ratio {volume_ratio:.2f}x versus the rolling average.",
            },
        ]
        acceptance_direction = str(context.get("price_acceptance_direction") or "").upper()
        if acceptance_direction == ("BULLISH" if direction == DIRECTION_LONG else "BEARISH"):
            sequence.append(
                {
                    "step": len(sequence) + 1,
                    "id": "price_acceptance",
                    "status": "confirmed",
                    "detail": (
                        f"Two-close {acceptance_direction.lower()} price acceptance beyond "
                        f"{_f(context.get('price_acceptance_level'), 0.0):.8f}."
                    ),
                }
            )
        if pattern_status == "CONFIRMED" and pattern_direction == direction:
            sequence.append(
                {
                    "step": len(sequence) + 1,
                    "id": "chart_pattern_confirmation",
                    "status": "confirmed",
                    "detail": f"{pattern_name or 'Chart pattern'} confirms the {direction} direction.",
                }
            )

        candidate.meta["strategy_evidence"] = {
            "version": 1,
            "strategy_id": candidate.strategy_id,
            "strategy_name": candidate.strategy_name,
            "direction": direction,
            "sequence": sequence,
            "liquidity": {
                "event": str(context.get("liquidity_last_event") or "SWEEP_RECLAIM"),
                "direction": liquidity_direction,
                "sweep_level": round(sweep_level, 8),
                "age_bars": int(liquidity_age),
            },
            "market_structure": {
                "event": structure_event,
                "direction": structure_direction,
                "trend": str(context.get("structure_trend") or "UNKNOWN").upper(),
                "support": round(support, 8),
                "resistance": round(resistance, 8),
                "last_high_label": str(context.get("structure_last_high_label") or ""),
                "last_low_label": str(context.get("structure_last_low_label") or ""),
            },
            "chart_pattern": {
                "name": pattern_name,
                "status": pattern_status,
                "direction": pattern_direction,
            },
            "volume": {
                "current": round(volume, 8),
                "average": round(volume_avg, 8),
                "ratio": round(volume_ratio, 4),
                "flow_direction": str(context.get("volume_flow_direction") or "NEUTRAL").upper(),
            },
            "price_acceptance": {
                "direction": acceptance_direction,
                "level": round(_f(context.get("price_acceptance_level"), 0.0), 8),
                "age_bars": int(_f(context.get("price_acceptance_age_bars"), -1.0)),
            },
            "confluence": dict(candidate.meta.get("confluence") or {}),
            "risk_plan": {
                "entry": round(_f(candidate.entry), 8),
                "stop": round(_f(candidate.sl), 8),
                "target": round(_f(candidate.tp1), 8),
                "rr": round(_f(candidate.rr), 4),
                "invalidation": str(candidate.invalidation_condition or ""),
            },
        }
        return candidate

    def evaluate(self, context: Dict[str, Any], spec: StrategySpec, direction: str, regime: str) -> Optional[CandidateSignal]:
        liquidity_direction = str(context.get("liquidity_last_event_direction") or "").upper()
        liquidity_age = _f(context.get("liquidity_event_age_bars"), 999.0)
        structure_event = str(context.get("structure_event") or "").upper()
        structure_direction = str(context.get("structure_event_direction") or "").upper()
        expected_event_direction = "BULLISH" if direction == DIRECTION_LONG else "BEARISH"
        if liquidity_direction != expected_event_direction or liquidity_age > self.MAX_SWEEP_TO_STRUCTURE_BARS:
            return None
        if structure_event not in {"BOS", "CHOCH"} or structure_direction != expected_event_direction:
            return None

        volume = _f(context.get("volume"), 0.0)
        volume_avg = max(_f(context.get("volume_avg"), 0.0), 1.0)
        if volume < volume_avg:
            return None

        price = _f(context.get("price"), 0.0)
        atr = max(_f(context.get("atr"), price * 0.003), price * 0.001)
        sweep_level = _f(context.get("liquidity_last_event_level"), 0.0)
        score = 58.0
        reasons = ["liquidity_sweep_reclaim", f"{structure_event.lower()}_confirmation", "volume_confirm"]
        if structure_event == "CHOCH":
            score += 10.0
            reasons.append("trend_character_change")
        if liquidity_age == 0:
            score += 6.0
            reasons.append("fresh_sweep")

        pattern_direction = str(context.get("chart_pattern_direction") or "").upper()
        if (
            str(context.get("chart_pattern_status") or "").upper() == "CONFIRMED"
            and pattern_direction == direction
        ):
            score += 8.0
            reasons.append("pattern_confirm")

        if direction == DIRECTION_LONG:
            swing_low = min(_f(context.get("structural_support"), price), sweep_level - atr * 0.15)
            if swing_low <= 0 or swing_low >= price:
                swing_low = price - atr * 1.2
            candidate = self._build_candidate(
                context, spec, direction, regime, score, reasons, swing_low=swing_low
            )
            return self._attach_evidence(
                candidate,
                context,
                structure_event=structure_event,
                structure_direction=structure_direction,
                liquidity_direction=liquidity_direction,
                liquidity_age=liquidity_age,
                sweep_level=sweep_level,
            )

        swing_high = max(_f(context.get("structural_resistance"), price), sweep_level + atr * 0.15)
        if swing_high <= price:
            swing_high = price + atr * 1.2
        candidate = self._build_candidate(
            context, spec, direction, regime, score, reasons, swing_high=swing_high
        )
        return self._attach_evidence(
            candidate,
            context,
            structure_event=structure_event,
            structure_direction=structure_direction,
            liquidity_direction=liquidity_direction,
            liquidity_age=liquidity_age,
            sweep_level=sweep_level,
        )


class SuperTrendBosEvaluator(BaseEvaluator):
    """Enter on a 30m SuperTrend flip + BOS with independent 1h/4h checks."""

    MAX_FLIP_TO_BOS_BARS = 8
    MAX_BOS_AGE_BARS = 1

    @staticmethod
    def _attach_evidence(
        candidate: Optional[CandidateSignal],
        context: Dict[str, Any],
        *,
        expected_direction: str,
        flip_age: int,
        bos_age: int,
        bos_level: float,
        timeframe_states: Dict[str, Any],
        aligned_timeframes: list[str],
    ) -> Optional[CandidateSignal]:
        if candidate is None:
            return None
        direction = str(candidate.direction or "").upper()
        primary = timeframe_states.get("30m") if isinstance(timeframe_states.get("30m"), dict) else {}
        line = _f(primary.get("supertrend_line"), 0.0)
        support = _f(primary.get("structural_support"), 0.0)
        resistance = _f(primary.get("structural_resistance"), 0.0)
        volume = _f(primary.get("volume"), 0.0)
        volume_avg = _f(primary.get("volume_average"), 0.0)
        volume_ratio = _f(primary.get("volume_ratio"), volume / volume_avg if volume_avg > 0 else 0.0)
        candidate.meta["strategy_evidence"] = {
            "version": 2,
            "strategy_id": candidate.strategy_id,
            "strategy_name": candidate.strategy_name,
            "direction": direction,
            "minimum_timeframe": "30m",
            "primary_timeframe": "30m",
            "confirmation_timeframes": ["1h", "4h"],
            "aligned_timeframes": list(aligned_timeframes),
            "multi_timeframe": {
                "primary": "30m",
                "confirmations": ["1h", "4h"],
                "aligned": list(aligned_timeframes),
                "required_aligned_confirmations": 1,
                "states": {
                    timeframe: dict(state or {})
                    for timeframe, state in timeframe_states.items()
                    if timeframe in {"30m", "1h", "4h"}
                },
            },
            "sequence": [
                {
                    "step": 1,
                    "id": "supertrend_flip",
                    "status": "confirmed",
                    "detail": (
                        f"30m SuperTrend flipped {expected_direction.lower()} {flip_age} bar(s) ago "
                        f"with its line at {line:.8f}."
                    ),
                },
                {
                    "step": 2,
                    "id": "break_of_structure",
                    "status": "confirmed",
                    "detail": (
                        f"30m {expected_direction.title()} BOS confirmed at {bos_level:.8f} "
                        f"{bos_age} bar(s) ago, after the SuperTrend flip."
                    ),
                },
                {
                    "step": 3,
                    "id": "multi_timeframe_confirmation",
                    "status": "confirmed",
                    "detail": (
                        "30m trigger checked against 1h and 4h SuperTrend; "
                        f"aligned higher timeframe(s): {', '.join(aligned_timeframes)}."
                    ),
                },
            ],
            "supertrend": {
                "timeframe": "30m",
                "direction": str(primary.get("supertrend_direction") or "").upper(),
                "flip_direction": str(primary.get("supertrend_last_flip_direction") or "").upper(),
                "flip_age_bars": flip_age,
                "line": round(line, 8),
                "upper_band": round(_f(primary.get("supertrend_upper_band"), 0.0), 8),
                "lower_band": round(_f(primary.get("supertrend_lower_band"), 0.0), 8),
                "period": int(_f(primary.get("supertrend_period"), 10.0)),
                "multiplier": round(_f(primary.get("supertrend_multiplier"), 3.0), 4),
            },
            "market_structure": {
                "timeframe": "30m",
                "event": "BOS",
                "direction": expected_direction,
                "event_level": round(bos_level, 8),
                "event_age_bars": bos_age,
                "trend": str(primary.get("structure_trend") or "UNKNOWN").upper(),
                "support": round(support, 8),
                "resistance": round(resistance, 8),
                "last_high_label": str(primary.get("structure_last_high_label") or ""),
                "last_low_label": str(primary.get("structure_last_low_label") or ""),
            },
            "volume": {
                "timeframe": "30m",
                "current": round(volume, 8),
                "average": round(volume_avg, 8),
                "ratio": round(volume_ratio, 4),
                "role": "score_bonus_not_entry_gate",
            },
            "confluence": dict(candidate.meta.get("confluence") or {}),
            "risk_plan": {
                "entry": round(_f(candidate.entry), 8),
                "stop": round(_f(candidate.sl), 8),
                "target": round(_f(candidate.tp1), 8),
                "rr": round(_f(candidate.rr), 4),
                "invalidation": str(candidate.invalidation_condition or ""),
            },
        }
        return candidate

    def evaluate(self, context: Dict[str, Any], spec: StrategySpec, direction: str, regime: str) -> Optional[CandidateSignal]:
        expected = "BULLISH" if direction == DIRECTION_LONG else "BEARISH"
        primary_timeframe = str(context.get("s8_primary_timeframe") or "").lower()
        minimum_timeframe = str(context.get("s8_minimum_timeframe") or "").lower()
        timeframe_states = context.get("s8_timeframes") if isinstance(context.get("s8_timeframes"), dict) else {}
        primary = timeframe_states.get("30m") if isinstance(timeframe_states.get("30m"), dict) else {}
        confirmations = [
            timeframe
            for timeframe in ("1h", "4h")
            if isinstance(timeframe_states.get(timeframe), dict) and timeframe_states[timeframe].get("available") is True
        ]
        if primary_timeframe != "30m" or minimum_timeframe != "30m" or primary.get("available") is not True:
            return None
        if len(confirmations) != 2:
            return None
        aligned_timeframes = [
            timeframe
            for timeframe in confirmations
            if str(timeframe_states[timeframe].get("supertrend_direction") or "").upper() == expected
        ]
        if not aligned_timeframes:
            return None

        supertrend_direction = str(primary.get("supertrend_direction") or "").upper()
        flip_direction = str(
            primary.get("supertrend_last_flip_direction") or primary.get("supertrend_flip") or ""
        ).upper()
        flip_age = int(_f(primary.get("supertrend_flip_age_bars"), 999.0))
        if supertrend_direction != expected or flip_direction != expected:
            return None
        if flip_age < 0 or flip_age > self.MAX_FLIP_TO_BOS_BARS:
            return None

        structure_event = str(primary.get("structure_last_event") or primary.get("structure_event") or "").upper()
        structure_direction = str(
            primary.get("structure_last_event_direction") or primary.get("structure_event_direction") or ""
        ).upper()
        bos_age = int(_f(primary.get("structure_event_age_bars"), 999.0))
        if structure_event != "BOS" or structure_direction != expected:
            return None
        if bos_age < 0 or bos_age > self.MAX_BOS_AGE_BARS:
            return None
        if bos_age > flip_age:
            return None

        price = _f(context.get("price"), 0.0)
        line = _f(primary.get("supertrend_line"), 0.0)
        if price <= 0 or line <= 0:
            return None
        if direction == DIRECTION_LONG and line >= price:
            return None
        if direction == DIRECTION_SHORT and line <= price:
            return None

        bos_level = _f(primary.get("structure_last_event_level"), price)
        volume = _f(primary.get("volume"), _f(context.get("volume"), 0.0))
        volume_avg = max(_f(primary.get("volume_average"), _f(context.get("volume_avg"), 0.0)), 1.0)
        volume_ratio = _f(primary.get("volume_ratio"), volume / volume_avg)
        score = 70.0 + (5.0 * len(aligned_timeframes))
        reasons = [
            f"supertrend_flip_{expected.lower()}",
            f"bos_{expected.lower()}_after_flip",
            "causal_supertrend_bos_sequence",
            "primary_timeframe_30m",
            f"higher_timeframe_alignment_{'_'.join(aligned_timeframes)}",
        ]
        if flip_age <= 2:
            score += 6.0
            reasons.append("fresh_supertrend_flip")
        if bos_age == 0:
            score += 6.0
            reasons.append("fresh_bos")
        if volume_ratio >= 1.0:
            score += min(8.0, 4.0 + max(0.0, volume_ratio - 1.0) * 4.0)
            reasons.append("volume_supports_break")
        if str(primary.get("price_acceptance_direction") or "").upper() == expected:
            score += 5.0
            reasons.append("price_acceptance_aligned")

        if direction == DIRECTION_LONG:
            structural_support = _f(primary.get("structural_support"), line)
            swing_low = min(value for value in (line, structural_support) if value > 0)
            candidate = self._build_candidate(
                context, spec, direction, regime, score, reasons, swing_low=swing_low
            )
        else:
            structural_resistance = _f(primary.get("structural_resistance"), line)
            swing_high = max(line, structural_resistance)
            candidate = self._build_candidate(
                context, spec, direction, regime, score, reasons, swing_high=swing_high
            )
        return self._attach_evidence(
            candidate,
            context,
            expected_direction=expected,
            flip_age=flip_age,
            bos_age=bos_age,
            bos_level=bos_level,
            timeframe_states=timeframe_states,
            aligned_timeframes=aligned_timeframes,
        )


class FxSessionBreakoutEvaluator(BaseEvaluator):
    def evaluate(self, context: Dict[str, Any], spec: StrategySpec, direction: str, regime: str) -> Optional[CandidateSignal]:
        if str(context.get("asset_class") or "").upper() != "FOREX_MAJORS":
            return None
        if not bool(context.get("session_breakout", False)):
            return None
        if bool(context.get("event_risk", False)):
            return None

        price = _f(context.get("price"), 0.0)
        asian_high = _f(context.get("asian_high"), price * 1.002)
        asian_low = _f(context.get("asian_low"), price * 0.998)
        ema200 = _f(context.get("ema200"), price)
        adx = _f(context.get("adx"), 0.0)
        score = 50.0
        reasons = ["session_breakout"]

        if adx >= 20:
            score += 8
            reasons.append("adx_confirm")

        if direction == DIRECTION_LONG:
            if not (price > asian_high and price >= ema200):
                return None
            score += 14
            reasons.append("london_ny_break_long")
            return self._build_candidate(context, spec, direction, regime, score, reasons, swing_low=asian_low)

        if not (price < asian_low and price <= ema200):
            return None
        score += 14
        reasons.append("london_ny_break_short")
        return self._build_candidate(context, spec, direction, regime, score, reasons, swing_high=asian_high)

