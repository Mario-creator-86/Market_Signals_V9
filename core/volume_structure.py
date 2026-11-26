"""Causal volume and price-acceptance structure for OHLCV analysis.

The module deliberately works from candles only.  It does not infer order-book
liquidity or traded bid/ask delta, neither of which is available consistently
for stocks, commodities and crypto.  ``volume_flow`` is therefore an explicit
close-location proxy, and every event is available only after the candle that
created it has closed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

import pandas as pd


@dataclass(frozen=True)
class VolumeEvent:
    index: int
    kind: str
    direction: str
    level: float
    volume_ratio: float
    close_location: float
    timestamp: Optional[str] = None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if pd.notna(result) else default
    except (TypeError, ValueError):
        return default


class VolumeStructureEngine:
    """Incremental volume expansion and two-close price-acceptance detector.

    A single wick outside a range is deliberately *not* called acceptance.  The
    first close outside the prior range starts a pending breakout; the next
    closed candle must still close beyond the same level before a
    ``PRICE_ACCEPTANCE`` event is emitted.  This makes the signal causal and
    avoids labelling one-bar stop runs as accepted price.
    """

    def __init__(
        self,
        volume_lookback: int = 20,
        acceptance_lookback: int = 12,
        expansion_ratio: float = 1.20,
        acceptance_min_ratio: float = 0.90,
    ) -> None:
        if volume_lookback < 2 or acceptance_lookback < 2:
            raise ValueError("volume and acceptance lookbacks must be at least 2")
        self.volume_lookback = int(volume_lookback)
        self.acceptance_lookback = int(acceptance_lookback)
        self.expansion_ratio = max(float(expansion_ratio), 1.0)
        self.acceptance_min_ratio = max(float(acceptance_min_ratio), 0.0)
        self.events: list[VolumeEvent] = []
        self._opens: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []
        self._volumes: list[float] = []
        self._timestamps: list[Any] = []
        self._flow_values: list[float] = []
        self._pending_breakout: Optional[dict[str, Any]] = None
        self._last_volume_average = 0.0
        self._last_volume_ratio = 0.0

    def update(
        self,
        open_price: Any,
        high: Any,
        low: Any,
        close: Any,
        volume: Any,
        timestamp: Any = None,
    ) -> dict[str, Any]:
        index = len(self._closes)
        open_f = _number(open_price, _number(close))
        high_f = _number(high, open_f)
        low_f = _number(low, open_f)
        close_f = _number(close, open_f)
        volume_f = max(_number(volume), 0.0)
        prior_average = self._prior_volume_average()
        volume_ratio = volume_f / prior_average if prior_average > 0 else 0.0
        prior_high, prior_low = self._prior_range()
        candle_range = max(high_f - low_f, 1e-12)
        close_location = max(-1.0, min(1.0, ((close_f - low_f) / candle_range) * 2.0 - 1.0))
        self._opens.append(open_f)
        self._highs.append(high_f)
        self._lows.append(low_f)
        self._closes.append(close_f)
        self._volumes.append(volume_f)
        self._timestamps.append(timestamp if timestamp is not None else index)
        self._flow_values.append(close_location * volume_f)
        self._last_volume_average = prior_average
        self._last_volume_ratio = volume_ratio

        event = self._detect_event(
            index,
            close_f,
            volume_ratio,
            close_location,
            prior_high=prior_high,
            prior_low=prior_low,
        )
        return self.snapshot(event)

    def _prior_volume_average(self) -> float:
        history = self._volumes[-self.volume_lookback :]
        if not history:
            return 0.0
        return sum(history) / len(history)

    def _prior_range(self) -> tuple[Optional[float], Optional[float]]:
        if len(self._highs) < 2:
            return None, None
        highs = self._highs[-self.acceptance_lookback :]
        lows = self._lows[-self.acceptance_lookback :]
        if not highs or not lows:
            return None, None
        return max(highs), min(lows)

    def _detect_event(
        self,
        index: int,
        close: float,
        volume_ratio: float,
        close_location: float,
        *,
        prior_high: Optional[float],
        prior_low: Optional[float],
    ) -> Optional[VolumeEvent]:
        direction = ""
        level = 0.0
        kind = ""

        pending = self._pending_breakout
        if pending is not None:
            pending_direction = str(pending["direction"])
            pending_level = _number(pending["level"])
            still_beyond = (
                close > pending_level if pending_direction == "BULLISH" else close < pending_level
            )
            if still_beyond and volume_ratio >= self.acceptance_min_ratio:
                direction, level, kind = pending_direction, pending_level, "PRICE_ACCEPTANCE"
                self._pending_breakout = None
            elif not still_beyond:
                self._pending_breakout = None

        if not kind and prior_high is not None and close > prior_high:
            self._pending_breakout = {"direction": "BULLISH", "level": prior_high, "index": index}
        elif not kind and prior_low is not None and close < prior_low:
            self._pending_breakout = {"direction": "BEARISH", "level": prior_low, "index": index}

        if not kind and volume_ratio >= self.expansion_ratio:
            direction = "BULLISH" if close_location >= 0 else "BEARISH"
            level = close
            kind = "VOLUME_EXPANSION"

        if not kind:
            return None
        event = VolumeEvent(
            index=index,
            kind=kind,
            direction=direction,
            level=level,
            volume_ratio=volume_ratio,
            close_location=close_location,
            timestamp=self._format_timestamp(index),
        )
        self.events.append(event)
        return event

    def _format_timestamp(self, index: int) -> str:
        value = self._timestamps[index]
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    def snapshot(self, event: Optional[VolumeEvent] = None) -> dict[str, Any]:
        current_index = len(self._closes) - 1
        average = self._last_volume_average
        volume_ratio = self._last_volume_ratio
        flow_window = self._flow_values[-self.volume_lookback :]
        volume_window = self._volumes[-self.volume_lookback :]
        flow_score = (sum(flow_window) / max(sum(volume_window), 1e-12)) if flow_window else 0.0
        flow_direction = "BULLISH" if flow_score > 0.15 else ("BEARISH" if flow_score < -0.15 else "NEUTRAL")
        last_event = self.events[-1] if self.events else None
        event_age = current_index - last_event.index if last_event is not None else None
        last_acceptance = next((item for item in reversed(self.events) if item.kind == "PRICE_ACCEPTANCE"), None)
        acceptance_age = current_index - last_acceptance.index if last_acceptance is not None else None
        return {
            "volume_average": average if average > 0 else None,
            "volume_ratio": volume_ratio if average > 0 else None,
            "volume_expansion": bool(volume_ratio >= self.expansion_ratio) if average > 0 else False,
            "volume_structure_expansion": bool(volume_ratio >= self.expansion_ratio) if average > 0 else False,
            "volume_flow_score": flow_score,
            "volume_flow_direction": flow_direction,
            "volume_event": event.kind if event else None,
            "volume_event_direction": event.direction if event else None,
            "volume_event_level": event.level if event else None,
            "volume_last_event": last_event.kind if last_event else None,
            "volume_last_event_direction": last_event.direction if last_event else None,
            "volume_event_age_bars": event_age,
            "price_acceptance": event is not None and event.kind == "PRICE_ACCEPTANCE",
            "price_acceptance_direction": last_acceptance.direction if last_acceptance else None,
            "price_acceptance_level": last_acceptance.level if last_acceptance else None,
            "price_acceptance_age_bars": acceptance_age,
        }

    def annotations(self, limit: int = 150) -> dict[str, list[dict[str, Any]]]:
        return {"volume_events": [asdict(item) for item in self.events[-limit:]]}


def precompute_volume_structure(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"open", "high", "low", "close", "volume"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Missing OHLCV columns: {sorted(missing)}")
    engine = VolumeStructureEngine()
    states = [
        engine.update(row.open, row.high, row.low, row.close, row.volume, timestamp=index)
        for index, row in frame[["open", "high", "low", "close", "volume"]].iterrows()
    ]
    return pd.DataFrame(states, index=frame.index)


def analyze_volume_structure(frame: pd.DataFrame) -> dict[str, Any]:
    required = {"open", "high", "low", "close", "volume"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Missing OHLCV columns: {sorted(missing)}")
    engine = VolumeStructureEngine()
    latest: dict[str, Any] = {}
    for index, row in frame[["open", "high", "low", "close", "volume"]].iterrows():
        latest = engine.update(row.open, row.high, row.low, row.close, row.volume, timestamp=index)
    return {**latest, **engine.annotations()}
