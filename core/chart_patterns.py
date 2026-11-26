"""Causal, deterministic chart-pattern annotations built on confirmed pivots."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

import pandas as pd

from core.market_structure import MarketStructureEngine, Pivot
from core.volume_structure import VolumeStructureEngine


@dataclass(frozen=True)
class ChartPattern:
    id: str
    kind: str
    direction: str
    status: str
    first_pivot_index: int
    second_pivot_index: int
    neckline: float
    invalidation: float
    measured_target: float
    confidence: float
    confirmed_index: Optional[int] = None
    timestamp: Optional[str] = None
    available_index: Optional[int] = None
    volume_ratio: float = 0.0
    acceptance_status: str = "PENDING"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if pd.notna(result) else default
    except (TypeError, ValueError):
        return default


def _pattern_from_recent_pivots(
    pivots: list[Pivot],
    atr: float,
    tolerance_bps: float = 20.0,
    volume_ratio: float = 0.0,
    acceptance_direction: str = "",
) -> Optional[ChartPattern]:
    """Return the latest forming double top or bottom, if structurally valid."""
    if len(pivots) < 3:
        return None
    ordered = sorted(pivots, key=lambda item: (item.index, item.kind))
    triples = [ordered[position : position + 3] for position in range(len(ordered) - 2)]
    for first, middle, second in reversed(triples):
        if not (first.index < middle.index < second.index):
            continue
        if first.kind == "HIGH" and middle.kind == "LOW" and second.kind == "HIGH":
            tolerance = max(abs(first.price) * tolerance_bps / 10_000.0, max(atr, 0.0) * 0.2)
            if abs(first.price - second.price) <= tolerance:
                peak = (first.price + second.price) / 2.0
                height = max(peak - middle.price, max(atr, 0.0))
                volume_bonus = 0.10 if volume_ratio >= 1.2 else 0.0
                accepted = str(acceptance_direction).upper() == "BEARISH"
                return ChartPattern(
                    id=f"DOUBLE_TOP:{first.index}:{second.index}",
                    kind="DOUBLE_TOP",
                    direction="BEARISH",
                    status="FORMING",
                    first_pivot_index=first.index,
                    second_pivot_index=second.index,
                    neckline=middle.price,
                    invalidation=max(first.price, second.price) + tolerance,
                    measured_target=middle.price - height,
                    confidence=min(0.95, 0.55 + (0.20 if first.label in {"H", "HH", "LH"} else 0.0) + volume_bonus + (0.10 if accepted else 0.0)),
                    available_index=max(first.confirmed_index, middle.confirmed_index, second.confirmed_index),
                    volume_ratio=volume_ratio,
                    acceptance_status="ACCEPTED" if accepted else "PENDING",
                )
        if first.kind == "LOW" and middle.kind == "HIGH" and second.kind == "LOW":
            tolerance = max(abs(first.price) * tolerance_bps / 10_000.0, max(atr, 0.0) * 0.2)
            if abs(first.price - second.price) <= tolerance:
                trough = (first.price + second.price) / 2.0
                height = max(middle.price - trough, max(atr, 0.0))
                volume_bonus = 0.10 if volume_ratio >= 1.2 else 0.0
                accepted = str(acceptance_direction).upper() == "BULLISH"
                return ChartPattern(
                    id=f"DOUBLE_BOTTOM:{first.index}:{second.index}",
                    kind="DOUBLE_BOTTOM",
                    direction="BULLISH",
                    status="FORMING",
                    first_pivot_index=first.index,
                    second_pivot_index=second.index,
                    neckline=middle.price,
                    invalidation=min(first.price, second.price) - tolerance,
                    measured_target=middle.price + height,
                    confidence=min(0.95, 0.55 + (0.20 if first.label in {"L", "HL", "LL"} else 0.0) + volume_bonus + (0.10 if accepted else 0.0)),
                    available_index=max(first.confirmed_index, middle.confirmed_index, second.confirmed_index),
                    volume_ratio=volume_ratio,
                    acceptance_status="ACCEPTED" if accepted else "PENDING",
                )
    return None


class ChartPatternEngine:
    def __init__(self, left_bars: int = 3, right_bars: int = 3) -> None:
        self.structure = MarketStructureEngine(left_bars, right_bars)
        self.volume = VolumeStructureEngine()
        self.patterns: dict[str, ChartPattern] = {}
        self._latest: Optional[ChartPattern] = None

    def update(
        self,
        high: Any,
        low: Any,
        close: Any,
        atr: Any = 0.0,
        timestamp: Any = None,
        *,
        open_price: Any = None,
        volume: Any = 0.0,
    ) -> dict[str, Any]:
        index = len(self.structure._closes)
        close_f = _number(close)
        self.structure.update(high, low, close_f, timestamp=timestamp)
        volume_state = self.volume.update(
            close_f if open_price is None else open_price,
            high,
            low,
            close_f,
            volume,
            timestamp=timestamp,
        )
        candidate = _pattern_from_recent_pivots(
            self.structure.pivots,
            _number(atr),
            volume_ratio=_number(volume_state.get("volume_ratio"), 0.0),
            acceptance_direction=str(volume_state.get("price_acceptance_direction") or ""),
        )
        emitted: Optional[ChartPattern] = None
        if candidate is not None:
            status = "FORMING"
            if candidate.direction == "BEARISH" and close_f < candidate.neckline:
                status = "CONFIRMED"
            elif candidate.direction == "BULLISH" and close_f > candidate.neckline:
                status = "CONFIRMED"
            pattern = ChartPattern(
                **{
                    **asdict(candidate),
                    "status": status,
                    "confirmed_index": index if status == "CONFIRMED" else None,
                    "timestamp": self._format_timestamp(timestamp, index),
                    "volume_ratio": _number(volume_state.get("volume_ratio"), 0.0),
                    "acceptance_status": (
                        "ACCEPTED"
                        if str(volume_state.get("price_acceptance_direction") or "").upper() == candidate.direction
                        else "BREAKOUT_ONLY" if status == "CONFIRMED" else candidate.acceptance_status
                    ),
                }
            )
            prior = self.patterns.get(pattern.id)
            if prior is None or (prior.status != "CONFIRMED" and pattern.status == "CONFIRMED"):
                self.patterns[pattern.id] = pattern
                emitted = pattern if pattern.status == "CONFIRMED" else None
            self._latest = self.patterns.get(pattern.id, pattern)
        return self.snapshot(emitted)

    @staticmethod
    def _format_timestamp(timestamp: Any, index: int) -> str:
        if timestamp is None:
            return str(index)
        return timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp)

    def snapshot(self, emitted: Optional[ChartPattern] = None) -> dict[str, Any]:
        latest = self._latest
        return {
            "chart_pattern": latest.kind if latest else None,
            "chart_pattern_name": latest.kind if latest else None,
            "chart_pattern_direction": latest.direction if latest else None,
            "chart_pattern_status": latest.status if latest else None,
            "chart_pattern_neckline": latest.neckline if latest else None,
            "chart_pattern_invalidation": latest.invalidation if latest else None,
            "chart_pattern_target": latest.measured_target if latest else None,
            "chart_pattern_confidence": latest.confidence if latest else None,
            "chart_pattern_volume_ratio": latest.volume_ratio if latest else None,
            "chart_pattern_acceptance": latest.acceptance_status if latest else None,
            "chart_pattern_event": emitted.kind if emitted else None,
        }

    def annotations(self, limit: int = 100) -> dict[str, list[dict[str, Any]]]:
        patterns = sorted(self.patterns.values(), key=lambda item: (item.second_pivot_index, item.id))
        return {"chart_patterns": [asdict(item) for item in patterns[-limit:]]}


def precompute_chart_patterns(
    frame: pd.DataFrame, left_bars: int = 3, right_bars: int = 3
) -> pd.DataFrame:
    required = {"high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Missing OHLC columns: {sorted(missing)}")
    atr = pd.to_numeric(frame.get("atr", pd.Series(0.0, index=frame.index)), errors="coerce").fillna(0.0)
    engine = ChartPatternEngine(left_bars=left_bars, right_bars=right_bars)
    has_volume = {"open", "volume"}.issubset(frame.columns)
    columns = ["open", "high", "low", "close", "volume"] if has_volume else ["high", "low", "close"]
    states = [
        engine.update(
            row.high,
            row.low,
            row.close,
            atr.iloc[position],
            timestamp=index,
            open_price=row.open if has_volume else row.close,
            volume=row.volume if has_volume else 0.0,
        )
        for position, (index, row) in enumerate(frame[columns].iterrows())
    ]
    return pd.DataFrame(states, index=frame.index)


def analyze_chart_patterns(
    frame: pd.DataFrame, left_bars: int = 3, right_bars: int = 3
) -> dict[str, Any]:
    required = {"high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Missing OHLC columns: {sorted(missing)}")
    atr = pd.to_numeric(frame.get("atr", pd.Series(0.0, index=frame.index)), errors="coerce").fillna(0.0)
    engine = ChartPatternEngine(left_bars=left_bars, right_bars=right_bars)
    latest: dict[str, Any] = {}
    columns = ["high", "low", "close"]
    has_volume = {"open", "volume"}.issubset(frame.columns)
    if has_volume:
        columns = ["open", "high", "low", "close", "volume"]
    for position, (index, row) in enumerate(frame[columns].iterrows()):
        latest = engine.update(
            row.high,
            row.low,
            row.close,
            atr.iloc[position],
            timestamp=index,
            open_price=row.open if has_volume else row.close,
            volume=row.volume if has_volume else 0.0,
        )
    return {**latest, **engine.annotations()}
