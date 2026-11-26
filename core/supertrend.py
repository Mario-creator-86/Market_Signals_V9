"""Causal SuperTrend indicator for live analysis, replay, and charting.

The implementation uses Wilder's ATR smoothing and updates bands one candle at
a time.  Every row is therefore computed exclusively from that row and earlier
rows; future candles never change an already-produced value.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

import pandas as pd


@dataclass(frozen=True)
class SuperTrendEvent:
    index: int
    direction: str
    line: float
    close: float
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


def precompute_supertrend(
    frame: pd.DataFrame,
    period: int = 10,
    multiplier: float = 3.0,
) -> pd.DataFrame:
    """Return one causal SuperTrend state for every OHLC input row."""
    required = {"high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Missing OHLC columns: {sorted(missing)}")
    if int(period) < 2:
        raise ValueError("period must be at least 2")
    if float(multiplier) <= 0:
        raise ValueError("multiplier must be positive")

    period = int(period)
    multiplier = float(multiplier)
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    close = pd.to_numeric(frame["close"], errors="coerce")
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            (high - low).abs(),
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    # Wilder's RMA is the ATR convention used by the standard SuperTrend.
    atr = true_range.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    midpoint = (high + low) / 2.0
    basic_upper = midpoint + multiplier * atr
    basic_lower = midpoint - multiplier * atr

    row_count = len(frame)
    upper: list[Optional[float]] = [None] * row_count
    lower: list[Optional[float]] = [None] * row_count
    line: list[Optional[float]] = [None] * row_count
    directions: list[Optional[str]] = [None] * row_count
    flips: list[Optional[str]] = [None] * row_count
    last_flip_directions: list[Optional[str]] = [None] * row_count
    flip_ages: list[Optional[int]] = [None] * row_count
    last_flip_direction: Optional[str] = None
    last_flip_index: Optional[int] = None
    previous_valid_index: Optional[int] = None

    for position in range(row_count):
        if pd.isna(atr.iloc[position]) or pd.isna(close.iloc[position]):
            continue

        current_upper = float(basic_upper.iloc[position])
        current_lower = float(basic_lower.iloc[position])
        if previous_valid_index is None:
            upper[position] = current_upper
            lower[position] = current_lower
            # Initialisation establishes state but is not a tradable flip.
            prior = close.iloc[position - 1] if position > 0 else close.iloc[position]
            direction = "BULLISH" if close.iloc[position] >= prior else "BEARISH"
        else:
            previous = previous_valid_index
            previous_upper = float(upper[previous])
            previous_lower = float(lower[previous])
            prior_close = float(close.iloc[position - 1]) if position > 0 and pd.notna(close.iloc[position - 1]) else float(close.iloc[previous])
            upper[position] = (
                current_upper
                if current_upper < previous_upper or prior_close > previous_upper
                else previous_upper
            )
            lower[position] = (
                current_lower
                if current_lower > previous_lower or prior_close < previous_lower
                else previous_lower
            )

            previous_direction = directions[previous] or "BULLISH"
            if float(close.iloc[position]) > float(upper[position]):
                direction = "BULLISH"
            elif float(close.iloc[position]) < float(lower[position]):
                direction = "BEARISH"
            else:
                direction = previous_direction
                if direction == "BULLISH" and lower[position] is not None:
                    lower[position] = max(float(lower[position]), previous_lower)
                elif direction == "BEARISH" and upper[position] is not None:
                    upper[position] = min(float(upper[position]), previous_upper)

            if direction != previous_direction:
                flips[position] = direction
                last_flip_direction = direction
                last_flip_index = position

        directions[position] = direction
        line[position] = float(lower[position]) if direction == "BULLISH" else float(upper[position])
        last_flip_directions[position] = last_flip_direction
        flip_ages[position] = position - last_flip_index if last_flip_index is not None else None
        previous_valid_index = position

    return pd.DataFrame(
        {
            "supertrend_line": line,
            "supertrend_upper_band": upper,
            "supertrend_lower_band": lower,
            "supertrend_direction": directions,
            "supertrend_flip": flips,
            "supertrend_last_flip_direction": last_flip_directions,
            "supertrend_flip_age_bars": flip_ages,
            "supertrend_atr": atr,
        },
        index=frame.index,
    )


def analyze_supertrend(
    frame: pd.DataFrame,
    period: int = 10,
    multiplier: float = 3.0,
) -> dict[str, Any]:
    """Return the latest state plus chart-ready causal points and flip events."""
    states = precompute_supertrend(frame, period=period, multiplier=multiplier)
    points: list[dict[str, Any]] = []
    events: list[SuperTrendEvent] = []
    for position, (_, row) in enumerate(states.iterrows()):
        line = row.get("supertrend_line")
        direction = row.get("supertrend_direction")
        if pd.isna(line) or not direction:
            continue
        point = {
            "index": position,
            "timestamp": _timestamp(frame.index, position),
            "line": round(float(line), 8),
            "direction": str(direction),
        }
        points.append(point)
        flip = row.get("supertrend_flip")
        if isinstance(flip, str) and flip:
            events.append(
                SuperTrendEvent(
                    index=position,
                    direction=flip,
                    line=float(line),
                    close=_number(frame["close"].iloc[position]),
                    timestamp=_timestamp(frame.index, position),
                )
            )

    latest = states.iloc[-1].to_dict() if not states.empty else {}
    latest_state = {
        key: (None if pd.isna(value) else value)
        for key, value in latest.items()
    }
    return {
        **latest_state,
        "supertrend_period": period,
        "supertrend_multiplier": multiplier,
        "supertrend_points": points,
        "supertrend_events": [asdict(event) for event in events],
    }
