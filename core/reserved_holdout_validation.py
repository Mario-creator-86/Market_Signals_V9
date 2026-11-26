"""Strict development/holdout validation for pre-defined replay candidates.

The final holdout is one contiguous period at the end of the supplied OHLCV.
It is never used to choose parameters.  This module only assesses a fixed
runner; it deliberately cannot promote a strategy to autonomous execution.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

import pandas as pd

from core.walk_forward_validation import _capital_risk_per_trade_pct, summarize_closed_trades


ReplayRunner = Callable[..., Dict[str, Any]]


def _timestamp(value: Any) -> Optional[pd.Timestamp]:
    try:
        result = pd.Timestamp(value)
        if result.tzinfo is None:
            result = result.tz_localize("UTC")
        return result.tz_convert("UTC")
    except (TypeError, ValueError):
        return None


def _closed_in_range(
    trades: Iterable[Dict[str, Any]],
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    for raw_trade in trades:
        if not isinstance(raw_trade, dict):
            continue
        if str(raw_trade.get("reason") or "").lower() == "time_exit":
            continue
        entry_time = _timestamp(raw_trade.get("entry_time"))
        exit_time = _timestamp(raw_trade.get("exit_time"))
        if entry_time is None or exit_time is None:
            continue
        if entry_time >= start and exit_time <= end:
            selected.append(dict(raw_trade))
    return selected


def _status(
    metrics: Dict[str, Any],
    *,
    min_sample: int,
    min_profit_factor: float,
    min_expectancy_r: float,
    max_drawdown_pct: float,
) -> tuple[str, str]:
    sample = int(metrics.get("sample_size") or 0)
    if sample < min_sample:
        return "insufficient_evidence", f"Need {min_sample} closed holdout trades; observed {sample}"
    profit_factor = metrics.get("profit_factor")
    if profit_factor is None:
        return "unmeasurable_profit_factor", "No losing holdout trade was observed, so finite profit factor is unavailable"
    if float(profit_factor) < min_profit_factor:
        return "negative_edge", f"Holdout profit factor {float(profit_factor):.2f} is below {min_profit_factor:.2f}"
    if float(metrics.get("expectancy_r") or 0.0) <= min_expectancy_r:
        return "negative_edge", "Holdout expectancy R did not clear the required threshold"
    if float(metrics.get("max_drawdown_pct") or 0.0) > max_drawdown_pct:
        return "drawdown_exceeded", "Holdout drawdown exceeded the required threshold"
    return "validated_price_only", "Fixed candidate passed the untouched price/volume holdout"


def run_reserved_holdout(
    ticker: str,
    df: pd.DataFrame,
    *,
    holdout_bars: int,
    warmup_bars: int,
    replay_runner: ReplayRunner,
    min_sample: int = 30,
    min_profit_factor: float = 1.2,
    min_expectancy_r: float = 0.0,
    max_drawdown_pct: float = 25.0,
    capital_risk_per_trade_pct: float = 1.0,
) -> Dict[str, Any]:
    """Evaluate a fixed replay runner on development and untouched final holdout.

    The development section is reported for audit only.  Its metrics do not
    modify the runner or the holdout thresholds.
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        raise ValueError("A non-empty prepared OHLC dataframe is required")
    if bool((getattr(df, "attrs", {}) or {}).get("source_truncated")):
        return {
            "ticker": str(ticker).upper(),
            "status": "source_truncated",
            "reason": "Source OHLCV is explicitly marked truncated; final holdout was not evaluated",
            "source_truncated": True,
            "promotion_eligible": False,
        }
    safe_capital_risk_pct = _capital_risk_per_trade_pct(capital_risk_per_trade_pct)
    rows = df.sort_index().copy()
    safe_holdout_bars = max(1, int(holdout_bars))
    safe_warmup_bars = max(1, int(warmup_bars))
    minimum_rows = safe_warmup_bars + safe_holdout_bars + 1
    if len(rows) < minimum_rows:
        return {
            "ticker": str(ticker).upper(),
            "status": "insufficient_data",
            "reason": f"Need at least {minimum_rows} bars; received {len(rows)}",
            "promotion_eligible": False,
        }

    split_index = len(rows) - safe_holdout_bars
    development = rows.iloc[:split_index]
    development_start = _timestamp(development.index[safe_warmup_bars - 1])
    development_end = _timestamp(development.index[-1])
    holdout_start = _timestamp(rows.index[split_index])
    holdout_end = _timestamp(rows.index[-1])
    if None in (development_start, development_end, holdout_start, holdout_end):
        raise ValueError("OHLC dataframe index must be timestamp-like")

    development_replay = replay_runner(
        ticker,
        days=max(1, int(len(development) / 1440)),
        warmup=safe_warmup_bars,
        df=development,
    )
    holdout_replay = replay_runner(
        ticker,
        days=max(1, int(len(rows) / 1440)),
        warmup=safe_warmup_bars,
        df=rows,
    )
    development_trades = _closed_in_range(
        (development_replay or {}).get("trade_list", []),
        start=development_start,
        end=development_end,
    )
    holdout_trades = _closed_in_range(
        (holdout_replay or {}).get("trade_list", []),
        start=holdout_start,
        end=holdout_end,
    )
    development_metrics = summarize_closed_trades(
        development_trades,
        capital_risk_per_trade_pct=safe_capital_risk_pct,
    )
    holdout_metrics = summarize_closed_trades(
        holdout_trades,
        capital_risk_per_trade_pct=safe_capital_risk_pct,
    )
    status, reason = _status(
        holdout_metrics,
        min_sample=max(1, int(min_sample)),
        min_profit_factor=max(0.0, float(min_profit_factor)),
        min_expectancy_r=float(min_expectancy_r),
        max_drawdown_pct=max(0.0, float(max_drawdown_pct)),
    )

    cost_model = (holdout_replay or {}).get("cost_model")
    return {
        "ticker": str(ticker).upper(),
        "method": "fixed_candidate_reserved_contiguous_holdout",
        "status": status,
        "reason": reason,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "split": {
            "development_start": development_start.isoformat(),
            "development_end": development_end.isoformat(),
            "holdout_start": holdout_start.isoformat(),
            "holdout_end": holdout_end.isoformat(),
            "development_bars": len(development),
            "holdout_bars": safe_holdout_bars,
            "warmup_bars": safe_warmup_bars,
            "overlap": False,
        },
        "thresholds": {
            "min_sample": max(1, int(min_sample)),
            "min_profit_factor": max(0.0, float(min_profit_factor)),
            "min_expectancy_r": float(min_expectancy_r),
            "max_drawdown_pct": max(0.0, float(max_drawdown_pct)),
        },
        "capital_model": {
            "risk_per_trade_pct": safe_capital_risk_pct,
            "equity_model": "sequential_net_r_compounded_at_configured_risk",
            "is_realized_capital": False,
        },
        "development_metrics": development_metrics,
        "holdout_metrics": holdout_metrics,
        "holdout_trades": holdout_trades,
        "cost_model": cost_model if isinstance(cost_model, dict) else None,
        "promotion_eligible": False,
        "promotion_blockers": [
            "A price-only holdout does not replay timestamped historical news.",
            "The candidate is not approved for autonomous execution by this report.",
            "Matching realized Capital trades remain required.",
        ],
    }
