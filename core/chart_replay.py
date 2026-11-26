"""Look-ahead-safe state snapshots and annotation filtering for chart replay."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict

import pandas as pd

from core.chart_patterns import precompute_chart_patterns
from core.liquidity_engine import precompute_liquidity
from core.market_structure import precompute_market_structure
from core.supertrend import precompute_supertrend
from core.volume_structure import precompute_volume_structure


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "item"):
        try:
            return _json_value(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, (bool, str, int)):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, float):
        return round(value, 8)
    return value


def build_replay_states(frame: pd.DataFrame) -> list[Dict[str, Any]]:
    """Build one causal, compact state per bar from OHLCV.

    Each precompute engine updates incrementally, so entry ``i`` uses only bars
    ``0..i``.  The output is intentionally independent of a strategy or trade
    so the UI can replay the exact facts behind a future paper decision.
    """
    required = {"open", "high", "low", "close", "volume"}
    if frame.empty or not required.issubset(frame.columns):
        return []
    structure = precompute_market_structure(frame)
    liquidity = precompute_liquidity(frame)
    patterns = precompute_chart_patterns(frame)
    volume = precompute_volume_structure(frame)
    supertrend = precompute_supertrend(frame)
    combined = pd.concat([structure, liquidity, patterns, volume, supertrend], axis=1)
    # Duplicate names such as volume_expansion resolve to the newest explicit
    # volume-structure value, while keeping a stable JSON schema per frame.
    combined = combined.loc[:, ~combined.columns.duplicated(keep="last")]
    return [
        {str(key): _json_value(value) for key, value in row.items()}
        for _, row in combined.iterrows()
    ]


def _annotation_available_at(annotation: Dict[str, Any], cursor_index: int, kind: str) -> bool:
    if kind == "pivots":
        return int(annotation.get("confirmed_index", annotation.get("index", 10**9)) or 10**9) <= cursor_index
    if kind == "liquidity_pools":
        return int(annotation.get("confirmed_index", annotation.get("last_pivot_index", 10**9)) or 10**9) <= cursor_index
    if kind == "chart_patterns":
        available = annotation.get("confirmed_index")
        if available is None:
            available = annotation.get("available_index", annotation.get("second_pivot_index", 10**9))
        return int(available or 10**9) <= cursor_index
    return int(annotation.get("index", 10**9) or 10**9) <= cursor_index


def apply_visual_replay(payload: Dict[str, Any], replay_index: int) -> Dict[str, Any]:
    """Return a chart payload as it could have been known at ``replay_index``."""
    source = deepcopy(payload if isinstance(payload, dict) else {})
    bars = source.get("bars") if isinstance(source.get("bars"), list) else []
    if not bars:
        source["replay"] = {"cursor_index": 0, "bar_count": 0, "lookahead_safe": True}
        return source
    cursor = max(0, min(int(replay_index), len(bars) - 1))
    annotations = source.get("annotations") if isinstance(source.get("annotations"), dict) else {}
    source["bars"] = bars[: cursor + 1]
    source["annotations"] = {
        key: [
            item for item in values
            if isinstance(item, dict) and _annotation_available_at(item, cursor, key)
        ]
        for key, values in annotations.items()
        if isinstance(values, list)
    }
    indicators = source.get("indicators") if isinstance(source.get("indicators"), dict) else {}
    source["indicators"] = {
        key: [
            item for item in values
            if isinstance(item, dict) and int(item.get("index", 10**9)) <= cursor
        ]
        for key, values in indicators.items()
        if isinstance(values, list)
    }
    states = source.get("replay_states") if isinstance(source.get("replay_states"), list) else []
    if states and cursor < len(states) and isinstance(states[cursor], dict):
        source["state"] = states[cursor]
    source["replay"] = {"cursor_index": cursor, "bar_count": len(bars), "lookahead_safe": True}
    return source
