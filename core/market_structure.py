"""Causal market-structure primitives derived from OHLCV bars.

Pivots become visible only after ``right_bars`` later candles have closed.  The
engine therefore produces the same state in live evaluation and historical
replay and never labels a turning point using future information prematurely.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Optional

import pandas as pd


@dataclass(frozen=True)
class Pivot:
    index: int
    confirmed_index: int
    kind: str
    price: float
    label: str
    timestamp: Optional[str] = None


@dataclass(frozen=True)
class StructureEvent:
    index: int
    kind: str
    direction: str
    level: float
    pivot_index: int
    timestamp: Optional[str] = None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if pd.notna(result) else default
    except (TypeError, ValueError):
        return default


def _timestamp(index: pd.Index, position: int) -> Optional[str]:
    if position < 0 or position >= len(index):
        return None
    value = index[position]
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


class MarketStructureEngine:
    """Incremental confirmed-pivot and structure-break detector."""

    def __init__(self, left_bars: int = 3, right_bars: int = 3) -> None:
        if left_bars < 1 or right_bars < 1:
            raise ValueError("left_bars and right_bars must be positive")
        self.left_bars = int(left_bars)
        self.right_bars = int(right_bars)
        self.pivots: list[Pivot] = []
        self.events: list[StructureEvent] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []
        self._index: list[Any] = []
        self._last_high: Optional[Pivot] = None
        self._last_low: Optional[Pivot] = None
        self._broken_highs: set[int] = set()
        self._broken_lows: set[int] = set()
        self.trend = "RANGE"

    def update(self, high: Any, low: Any, close: Any, timestamp: Any = None) -> dict[str, Any]:
        bar_index = len(self._closes)
        self._highs.append(_number(high))
        self._lows.append(_number(low))
        self._closes.append(_number(close))
        self._index.append(timestamp if timestamp is not None else bar_index)
        self._confirm_candidate(bar_index)
        self._detect_break(bar_index)
        return self.snapshot()

    def _confirm_candidate(self, current_index: int) -> None:
        candidate = current_index - self.right_bars
        start = candidate - self.left_bars
        end = candidate + self.right_bars
        if start < 0 or end >= len(self._closes):
            return

        candidate_high = self._highs[candidate]
        candidate_low = self._lows[candidate]
        other_highs = self._highs[start:candidate] + self._highs[candidate + 1 : end + 1]
        other_lows = self._lows[start:candidate] + self._lows[candidate + 1 : end + 1]
        is_high = bool(other_highs) and candidate_high > max(other_highs)
        is_low = bool(other_lows) and candidate_low < min(other_lows)

        # An outside bar can technically be both. Preserve both structural facts.
        if is_high:
            label = "H" if self._last_high is None else ("HH" if candidate_high > self._last_high.price else "LH")
            pivot = Pivot(
                index=candidate,
                confirmed_index=current_index,
                kind="HIGH",
                price=candidate_high,
                label=label,
                timestamp=self._format_timestamp(candidate),
            )
            self.pivots.append(pivot)
            self._last_high = pivot
        if is_low:
            label = "L" if self._last_low is None else ("HL" if candidate_low > self._last_low.price else "LL")
            pivot = Pivot(
                index=candidate,
                confirmed_index=current_index,
                kind="LOW",
                price=candidate_low,
                label=label,
                timestamp=self._format_timestamp(candidate),
            )
            self.pivots.append(pivot)
            self._last_low = pivot
        self._update_trend_from_pivots()

    def _update_trend_from_pivots(self) -> None:
        if self._last_high is None or self._last_low is None:
            return
        if self._last_high.label == "HH" and self._last_low.label == "HL":
            self.trend = "BULLISH"
        elif self._last_high.label == "LH" and self._last_low.label == "LL":
            self.trend = "BEARISH"

    def _detect_break(self, index: int) -> None:
        close = self._closes[index]
        previous_close = self._closes[index - 1] if index > 0 else close
        event: Optional[StructureEvent] = None
        if (
            self._last_high is not None
            and self._last_high.index not in self._broken_highs
            and previous_close <= self._last_high.price < close
        ):
            kind = "CHOCH" if self.trend == "BEARISH" else "BOS"
            event = StructureEvent(
                index=index,
                kind=kind,
                direction="BULLISH",
                level=self._last_high.price,
                pivot_index=self._last_high.index,
                timestamp=self._format_timestamp(index),
            )
            self._broken_highs.add(self._last_high.index)
            self.trend = "BULLISH"
        elif (
            self._last_low is not None
            and self._last_low.index not in self._broken_lows
            and previous_close >= self._last_low.price > close
        ):
            kind = "CHOCH" if self.trend == "BULLISH" else "BOS"
            event = StructureEvent(
                index=index,
                kind=kind,
                direction="BEARISH",
                level=self._last_low.price,
                pivot_index=self._last_low.index,
                timestamp=self._format_timestamp(index),
            )
            self._broken_lows.add(self._last_low.index)
            self.trend = "BEARISH"
        if event is not None:
            self.events.append(event)

    def _format_timestamp(self, position: int) -> Optional[str]:
        value = self._index[position]
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    def snapshot(self) -> dict[str, Any]:
        last_event = self.events[-1] if self.events else None
        current_index = len(self._closes) - 1
        event_age = (current_index - last_event.index) if last_event is not None else None
        return {
            "structure_trend": self.trend,
            "structure_last_high": self._last_high.price if self._last_high else None,
            "structure_last_low": self._last_low.price if self._last_low else None,
            "structure_last_high_label": self._last_high.label if self._last_high else None,
            "structure_last_low_label": self._last_low.label if self._last_low else None,
            "structure_event": last_event.kind if last_event and last_event.index == len(self._closes) - 1 else None,
            "structure_event_direction": last_event.direction if last_event and last_event.index == len(self._closes) - 1 else None,
            "structure_event_level": last_event.level if last_event and last_event.index == len(self._closes) - 1 else None,
            "structure_last_event": last_event.kind if last_event else None,
            "structure_last_event_direction": last_event.direction if last_event else None,
            "structure_last_event_level": last_event.level if last_event else None,
            "structure_event_age_bars": event_age,
            "confirmed_pivot_count": len(self.pivots),
        }

    def annotations(self, limit: int = 100) -> dict[str, list[dict[str, Any]]]:
        return {
            "pivots": [asdict(item) for item in self.pivots[-limit:]],
            "structure_events": [asdict(item) for item in self.events[-limit:]],
        }


def precompute_market_structure(
    frame: pd.DataFrame, left_bars: int = 3, right_bars: int = 3
) -> pd.DataFrame:
    """Return one causal structure snapshot per input bar."""
    required = {"high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Missing OHLC columns: {sorted(missing)}")
    engine = MarketStructureEngine(left_bars=left_bars, right_bars=right_bars)
    snapshots = [
        engine.update(row.high, row.low, row.close, timestamp=index)
        for index, row in frame[["high", "low", "close"]].iterrows()
    ]
    return pd.DataFrame(snapshots, index=frame.index)


def analyze_market_structure(
    frame: pd.DataFrame, left_bars: int = 3, right_bars: int = 3
) -> dict[str, Any]:
    """Analyze the available history and return the latest state + annotations."""
    required = {"high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Missing OHLC columns: {sorted(missing)}")
    engine = MarketStructureEngine(left_bars=left_bars, right_bars=right_bars)
    latest: dict[str, Any] = engine.snapshot()
    for index, row in frame[["high", "low", "close"]].iterrows():
        latest = engine.update(row.high, row.low, row.close, timestamp=index)
    return {**latest, **engine.annotations()}
