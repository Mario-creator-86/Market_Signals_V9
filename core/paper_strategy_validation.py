"""Fixed-configuration OOS research for the disabled BTC paper strategies.

This module evaluates realised closed trades only.  It is deliberately a
research tool: it cannot promote a price-only result because the project does
not have a timestamped historical news archive to replay the live news gate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from core.paper_btc_strategy import (
    DEFAULT_PAPER_BTC_CONFIG,
    _build_bollinger_short_positions,
    _build_donchian_short_positions,
    _deep_merge,
    _extract_closed_trades,
    _normalize_ohlcv,
)
from core.walk_forward_validation import summarize_closed_trades


PositionBuilder = Callable[[str, pd.DataFrame, pd.DataFrame, Dict[str, object]], Tuple[pd.DataFrame, pd.Series]]


def _timestamp(value: Any) -> Optional[pd.Timestamp]:
    try:
        parsed = pd.Timestamp(value)
        if parsed.tzinfo is None:
            parsed = parsed.tz_localize("UTC")
        return parsed.tz_convert("UTC")
    except (TypeError, ValueError):
        return None


def _bar_duration(index: pd.Index) -> pd.Timedelta:
    if len(index) < 2:
        return pd.Timedelta(0)
    duration = index[-1] - index[-2]
    return duration if isinstance(duration, pd.Timedelta) and duration > pd.Timedelta(0) else pd.Timedelta(0)


def _default_position_builder(
    strategy_id: str,
    bars_1h: pd.DataFrame,
    bars_4h: pd.DataFrame,
    config: Dict[str, object],
) -> Tuple[pd.DataFrame, pd.Series]:
    if strategy_id == "donchian_short_1h":
        return bars_1h, _build_donchian_short_positions(bars_1h, bars_4h, config["donchian"])
    if strategy_id == "bollinger_rsi_short_4h":
        return bars_4h, _build_bollinger_short_positions(bars_4h, config["bollinger"])
    raise ValueError(f"Unsupported paper strategy: {strategy_id}")


def _price_validation_status(
    metrics: Dict[str, Any],
    *,
    completed_windows: int,
    min_windows: int,
    min_sample: int,
    min_profit_factor: float,
    min_expectancy_pct: float,
    max_drawdown_pct: float,
) -> Tuple[str, str]:
    if completed_windows < min_windows:
        return "insufficient_windows", f"Need {min_windows} completed OOS windows; observed {completed_windows}"
    if int(metrics.get("sample_size") or 0) < min_sample:
        return "insufficient_evidence", f"Need {min_sample} closed OOS trades; observed {metrics.get('sample_size', 0)}"
    profit_factor = metrics.get("profit_factor")
    if profit_factor is None:
        return "unmeasurable_profit_factor", "No losing OOS trade was observed, so finite profit factor is unavailable"
    if float(profit_factor) < min_profit_factor:
        return "negative_edge", f"OOS profit factor {float(profit_factor):.2f} is below {min_profit_factor:.2f}"
    if float(metrics.get("expectancy_pct") or 0.0) <= min_expectancy_pct:
        return "negative_edge", f"OOS expectancy {float(metrics.get('expectancy_pct') or 0.0):.3f}% is not above {min_expectancy_pct:.3f}%"
    if float(metrics.get("max_drawdown_pct") or 0.0) > max_drawdown_pct:
        return "drawdown_exceeded", f"OOS drawdown {float(metrics['max_drawdown_pct']):.2f}% exceeds {max_drawdown_pct:.2f}%"
    return "price_validated", "Fixed-configuration OHLCV replay passed the price-only OOS thresholds"


def _serialise_trade(trade: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(trade)
    for key in ("entry_time", "exit_time"):
        parsed = _timestamp(row.get(key))
        row[key] = parsed.isoformat() if parsed is not None else None
    row["net_pct"] = round(float(row.get("net_pct") or 0.0), 6)
    return row


def run_paper_btc_walk_forward(
    bars_1h: pd.DataFrame,
    bars_4h: pd.DataFrame,
    *,
    strategy_id: str,
    config: Optional[Dict[str, object]] = None,
    train_days: int = 30,
    test_days: int = 14,
    max_windows: int = 6,
    min_windows: int = 3,
    min_sample: int = 30,
    min_profit_factor: float = 1.2,
    min_expectancy_pct: float = 0.0,
    max_drawdown_pct: float = 25.0,
    position_builder: Optional[PositionBuilder] = None,
) -> Dict[str, Any]:
    """Replay a fixed paper strategy over non-overlapping price-only OOS windows.

    The builder receives a train+test slice only.  Completed trades must both
    open and close inside its OOS portion; unresolved positions are excluded.
    """
    data_1h = _normalize_ohlcv(bars_1h)
    data_4h = _normalize_ohlcv(bars_4h)
    merged = _deep_merge(DEFAULT_PAPER_BTC_CONFIG, config)
    train_bars = max(1, int(train_days)) * 24
    test_bars = max(1, int(test_days)) * 24

    base = {
        "strategy_id": strategy_id,
        "method": "fixed_configuration_paper_btc_walk_forward_ohlcv",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "promotion_eligible": False,
        "promotion_block_reason": "No timestamped historical news archive exists to replay the required live news gate.",
        "thresholds": {
            "min_windows": max(1, int(min_windows)),
            "min_sample": max(1, int(min_sample)),
            "min_profit_factor": max(0.0, float(min_profit_factor)),
            "min_expectancy_pct": float(min_expectancy_pct),
            "max_drawdown_pct": max(0.0, float(max_drawdown_pct)),
        },
        "limitations": [
            "No parameter search or strategy selection is performed inside OOS windows.",
            "Only completed trades are scored; open positions and terminal mark-to-market values are excluded.",
            "No timestamped historical news archive is available, so the live fresh-news gate was not replayed.",
            "This report is research-only and cannot enable autonomous execution.",
        ],
    }
    if data_1h.empty or data_4h.empty or len(data_1h) < train_bars + test_bars:
        metrics = summarize_closed_trades([])
        return {
            **base,
            "status": "research_only",
            "price_validation_status": "insufficient_data",
            "reason": f"Need at least {train_bars + test_bars} complete 1h bars; observed {len(data_1h)}",
            "metrics": metrics,
            "windows": [],
            "oos_trades": [],
        }

    available_windows = max(0, (len(data_1h) - train_bars) // test_bars)
    window_count = min(max(1, int(max_windows)), available_windows)
    first_start = len(data_1h) - (train_bars + (window_count * test_bars))
    builder = position_builder or _default_position_builder
    oos_trades: List[Dict[str, Any]] = []
    windows: List[Dict[str, Any]] = []
    one_hour = _bar_duration(data_1h.index)

    for number in range(window_count):
        start = first_start + (number * test_bars)
        test_start_index = start + train_bars
        end = test_start_index + test_bars
        context_1h = data_1h.iloc[start:end].copy()
        test_start = _timestamp(data_1h.index[test_start_index])
        test_end = _timestamp(data_1h.index[end - 1])
        if test_end is not None:
            test_end += one_hour
        context_start = _timestamp(context_1h.index[0])
        context_end = _timestamp(context_1h.index[-1])
        if context_end is not None:
            context_end += one_hour
        context_4h = data_4h.loc[
            (data_4h.index >= context_start) & (data_4h.index < context_end)
        ].copy()
        if context_4h.empty or test_start is None or test_end is None:
            windows.append(
                {"number": number + 1, "status": "insufficient_data", "metrics": summarize_closed_trades([])}
            )
            continue

        trading_df, positions = builder(strategy_id, context_1h, context_4h, merged)
        positions = positions.reindex(trading_df.index).fillna(0.0)
        validation_cfg = merged.get("validation", {}).get("default", {}) or {}
        trades = _extract_closed_trades(
            trading_df["close"].astype(float),
            positions,
            float(validation_cfg.get("leverage", 1.0) or 1.0),
            float(validation_cfg.get("cost_bps_per_side", 4.0) or 4.0),
        )
        closed_in_window = []
        for trade in trades:
            entry_time = _timestamp(trade.get("entry_time"))
            exit_time = _timestamp(trade.get("exit_time"))
            if entry_time is not None and exit_time is not None and entry_time >= test_start and exit_time <= test_end:
                closed_in_window.append(_serialise_trade(trade))
        oos_trades.extend(closed_in_window)
        windows.append(
            {
                "number": number + 1,
                "train_start": _timestamp(context_1h.index[0]).isoformat(),
                "test_start": test_start.isoformat(),
                "test_end": test_end.isoformat(),
                "bars": {"train_1h": train_bars, "test_1h": test_bars, "context_4h": len(context_4h)},
                "metrics": summarize_closed_trades(closed_in_window),
            }
        )

    metrics = summarize_closed_trades(oos_trades)
    price_status, price_reason = _price_validation_status(
        metrics,
        completed_windows=len(windows),
        min_windows=max(1, int(min_windows)),
        min_sample=max(1, int(min_sample)),
        min_profit_factor=max(0.0, float(min_profit_factor)),
        min_expectancy_pct=float(min_expectancy_pct),
        max_drawdown_pct=max(0.0, float(max_drawdown_pct)),
    )
    return {
        **base,
        "status": "research_only",
        "price_validation_status": price_status,
        "reason": price_reason,
        "metrics": metrics,
        "windows": windows,
        "oos_trades": oos_trades,
    }
