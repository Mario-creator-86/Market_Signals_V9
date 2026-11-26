"""Reproducible, fixed-configuration out-of-sample replay validation.

This module never selects parameters on the data it scores.  Each test window
is replayed with only its preceding training bars available to the engine, then
only trades opened inside the test interval are aggregated.  It is deliberately
strict about unresolved terminal ``time_exit`` positions and about profit factor
without observed losses.

It validates price/volume based strategy behaviour only.  Historical news is
not inferred because the project has no timestamped news archive; callers must
keep the live fresh-news gate enabled for any autonomous use.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import math
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, List, Optional

import pandas as pd


ReplayRunner = Callable[..., Dict[str, Any]]


class _TrainingWindowPerformanceCache:
    """Ephemeral V2 performance view used inside one walk-forward window.

    Entries are derived exclusively from the preceding training slice.  The
    class intentionally has no persistence methods, so it can never contaminate
    the live strategy performance cache used by Capital.
    """

    def __init__(self, entries: Dict[str, Dict[str, Any]]):
        self._entries = {str(key): dict(value) for key, value in (entries or {}).items()}

    def get_symbol_entries(self, _symbol: str) -> Dict[str, Dict[str, Any]]:
        return {key: dict(value) for key, value in self._entries.items()}

    @staticmethod
    def is_fresh(_entry: Dict[str, Any], _ttl_seconds: int) -> bool:
        # Training-slice records are point-in-time evidence for this replay,
        # not wall-clock research artifacts.  Their time validity is bounded by
        # the window and never exposed to the live selector.
        return True


def _closed_v2_trades_in_range(
    replay: Dict[str, Any],
    *,
    start: Optional[pd.Timestamp],
    end: Optional[pd.Timestamp],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(replay, dict) or start is None or end is None:
        return out
    for raw in replay.get("trade_list", []) or []:
        if not isinstance(raw, dict) or str(raw.get("strategy_source") or "").upper() != "V2":
            continue
        if str(raw.get("reason") or "").lower() == "time_exit":
            continue
        entry_time = _as_timestamp(raw.get("entry_time"))
        exit_time = _as_timestamp(raw.get("exit_time"))
        if entry_time is None or exit_time is None:
            continue
        if entry_time >= start and exit_time <= end:
            out.append(dict(raw))
    return out


def _strategy_metric_entry(
    trades: Iterable[Dict[str, Any]],
    *,
    capital_risk_per_trade_pct: float,
    costs: Dict[str, Any],
) -> Dict[str, Any]:
    metrics = summarize_closed_trades(
        trades,
        capital_risk_per_trade_pct=capital_risk_per_trade_pct,
    )
    return {
        "win_rate": metrics["win_rate"],
        "profit_factor": metrics["profit_factor"],
        "expectancy_R": metrics["expectancy_r"],
        "avg_R": metrics["expectancy_r"],
        # The selector and Capital gate both express drawdown as a Capital
        # percentage, not the sum of independent position notionals.
        "max_drawdown": metrics["max_drawdown_pct"],
        "sample_size": metrics["sample_size"],
        "costs_used": dict(costs or {}),
        "capital_model": {
            "risk_per_trade_pct": metrics["capital_risk_per_trade_pct"],
            "equity_model": "sequential_net_r_compounded_at_configured_risk",
        },
    }


def _v2_replay_config(base_config, *, bootstrap: bool):
    """Return a private ConfigManager-shaped object for one V2 replay phase."""
    v2_config = copy.deepcopy(getattr(base_config, "strategy_v2", {}) or {})
    bot_config = copy.deepcopy(getattr(base_config, "bot", {}) or {})
    v2_config["signal_mode"] = "V2_AUTO"
    if bootstrap:
        # This phase only collects *training* outcomes.  It is never exposed
        # to Capital and must not be mistaken for an approved strategy.
        v2_config.update(
            {
                "require_profitable_performance": False,
                "allow_low_sample_fallback": True,
                "allow_low_validation_fallback": True,
                "min_validation_score": 0.0,
            }
        )
        bot_config["min_edge_score"] = 0.0
    else:
        # The test phase uses the production profitability thresholds.  The
        # only private escape hatch tells StrategySelector that its cache is an
        # ephemeral training-window view, not persisted validation provenance.
        v2_config["require_profitable_performance"] = True
        v2_config["_walk_forward_training_performance"] = True
    return SimpleNamespace(
        signal=getattr(base_config, "signal", {}) or {},
        bot=bot_config,
        strategy_v2=v2_config,
    )


def _enabled_v2_keys(symbol: str, asset_class: str, config: Dict[str, Any]) -> List[tuple[str, str]]:
    from core.strategy_v2.registry import build_default_registry

    enabled_map = (config or {}).get("enabled_strategies_by_asset_class", {}) or {}
    configured = enabled_map.get(str(asset_class).upper(), [])
    configured_ids = {str(item).upper() for item in configured} if isinstance(configured, list) and configured else None
    out: List[tuple[str, str]] = []
    for spec in build_default_registry().get_specs_for_asset(asset_class):
        if configured_ids is not None and spec.id.upper() not in configured_ids:
            continue
        for direction in ("LONG", "SHORT"):
            out.append((spec.id.upper(), direction))
    return out


def _as_timestamp(value: Any) -> Optional[pd.Timestamp]:
    try:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        return timestamp.tz_convert("UTC")
    except (TypeError, ValueError):
        return None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _capital_risk_per_trade_pct(value: Any) -> float:
    """Validate the configured equity risk allocated to one replayed trade."""
    risk_pct = _number(value, float("nan"))
    if not math.isfinite(risk_pct) or risk_pct <= 0.0 or risk_pct > 100.0:
        raise ValueError("capital_risk_per_trade_pct must be a finite value in (0, 100]")
    return risk_pct


def summarize_closed_trades(
    trades: Iterable[Dict[str, Any]],
    *,
    capital_risk_per_trade_pct: float = 1.0,
) -> Dict[str, Any]:
    """Calculate trade statistics and a stated, risk-based Capital equity curve.

    ``net_pct`` remains the additive sum of returns on *trade notional*, so
    existing reports stay comparable.  The drawdown gate, however, must refer
    to Capital rather than to an impossible sum of independent notionals.  We
    therefore compound each closed trade's net R at the explicit risk fraction
    supplied by the caller.  This is a modelled sizing assumption, not a claim
    about realized exchange balance or fills.
    """
    safe_capital_risk_pct = _capital_risk_per_trade_pct(capital_risk_per_trade_pct)
    rows = [dict(row) for row in trades if isinstance(row, dict)]
    rows.sort(key=lambda row: str(row.get("exit_time") or row.get("entry_time") or ""))
    net_pct = [_number(row.get("net_pct"), 0.0) for row in rows]
    net_r = [_number(row.get("r_net", row.get("r")), 0.0) for row in rows]
    wins = [value for value in net_pct if value > 0]
    losses = [value for value in net_pct if value <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss else None

    cumulative = 0.0
    peak = 0.0
    raw_max_drawdown = 0.0
    for value in net_pct:
        cumulative += value
        peak = max(peak, cumulative)
        raw_max_drawdown = max(raw_max_drawdown, peak - cumulative)

    capital_equity = 100.0
    capital_peak = capital_equity
    capital_max_drawdown = 0.0
    for value in net_r:
        capital_return_pct = value * safe_capital_risk_pct
        # Equity cannot be negative.  A loss greater than the configured risk
        # allocation is treated as total modelled capital loss, preserving a
        # fail-closed result instead of allowing an impossible rebound.
        capital_equity = max(0.0, capital_equity * (1.0 + (capital_return_pct / 100.0)))
        capital_peak = max(capital_peak, capital_equity)
        if capital_peak > 0:
            capital_max_drawdown = max(
                capital_max_drawdown,
                ((capital_peak - capital_equity) / capital_peak) * 100.0,
            )

    sample_size = len(rows)
    return {
        "sample_size": sample_size,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / sample_size) if sample_size else 0.0,
        "profit_factor": round(profit_factor, 6) if profit_factor is not None else None,
        "expectancy_r": round(sum(net_r) / sample_size, 6) if sample_size else 0.0,
        "expectancy_pct": round(sum(net_pct) / sample_size, 6) if sample_size else 0.0,
        "net_pct": round(sum(net_pct), 6),
        "raw_max_drawdown_pct": round(raw_max_drawdown, 6),
        "capital_risk_per_trade_pct": safe_capital_risk_pct,
        "capital_equity_start": 100.0,
        "capital_equity_end": round(capital_equity, 6),
        "capital_return_pct": round(capital_equity - 100.0, 6),
        "capital_equity_model": "sequential_net_r_compounded_at_configured_risk",
        "max_drawdown_pct": round(capital_max_drawdown, 6),
    }


def _validation_status(
    metrics: Dict[str, Any],
    *,
    completed_windows: int,
    min_windows: int,
    min_sample: int,
    min_profit_factor: float,
    min_expectancy_r: float,
    max_drawdown_pct: float,
) -> tuple[str, str]:
    if completed_windows < min_windows:
        return "insufficient_windows", f"Need {min_windows} completed OOS windows; observed {completed_windows}"
    if int(metrics.get("sample_size") or 0) < min_sample:
        return "insufficient_evidence", f"Need {min_sample} closed OOS trades; observed {metrics.get('sample_size', 0)}"
    if metrics.get("profit_factor") is None:
        return "unmeasurable_profit_factor", "No losing OOS trade was observed, so finite profit factor is unavailable"
    if _number(metrics.get("profit_factor")) < min_profit_factor:
        return "negative_edge", f"OOS profit factor {metrics['profit_factor']:.2f} is below {min_profit_factor:.2f}"
    if _number(metrics.get("expectancy_r")) <= min_expectancy_r:
        return "negative_edge", f"OOS expectancy R {metrics['expectancy_r']:.3f} is not above {min_expectancy_r:.3f}"
    if _number(metrics.get("max_drawdown_pct")) > max_drawdown_pct:
        return "drawdown_exceeded", f"OOS drawdown {metrics['max_drawdown_pct']:.2f}% exceeds {max_drawdown_pct:.2f}%"
    return "validated", "Fixed-configuration price/volume replay passed all OOS thresholds"


def run_fixed_walk_forward(
    ticker: str,
    df: pd.DataFrame,
    *,
    train_bars: int,
    test_bars: int,
    max_windows: int,
    min_windows: int = 3,
    min_sample: int = 30,
    min_profit_factor: float = 1.2,
    min_expectancy_r: float = 0.0,
    max_drawdown_pct: float = 25.0,
    capital_risk_per_trade_pct: float = 1.0,
    warmup_bars: int = 60,
    replay_runner: Optional[ReplayRunner] = None,
) -> Dict[str, Any]:
    """Run non-overlapping OOS windows with a fixed live replay configuration.

    ``df`` must already have causal indicators prepared by
    :func:`core.strategy_backtester.build_dataframe`.  The replay receives only
    a train+test slice, so it cannot inspect bars after the OOS window.
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        raise ValueError("A non-empty prepared OHLC dataframe is required")
    if train_bars <= warmup_bars or test_bars <= 0 or max_windows <= 0:
        raise ValueError("train_bars must exceed warmup_bars; test_bars and max_windows must be positive")
    safe_capital_risk_pct = _capital_risk_per_trade_pct(capital_risk_per_trade_pct)
    if bool((getattr(df, "attrs", {}) or {}).get("source_truncated")):
        return {
            "ticker": str(ticker).upper(),
            "status": "source_truncated",
            "reason": "Source OHLCV is explicitly marked truncated; OOS windows were not evaluated",
            "windows": [],
            "metrics": summarize_closed_trades([], capital_risk_per_trade_pct=safe_capital_risk_pct),
            "source_truncated": True,
            "limitations": [
                "No timestamped historical news archive is available; news gating was not replayed.",
                "Source OHLCV was truncated, so no OOS conclusion was calculated.",
            ],
        }

    from core.strategy_backtester import backtest

    runner = replay_runner or backtest
    rows = df.sort_index().copy()
    required = train_bars + test_bars
    if len(rows) < required:
        return {
            "ticker": ticker,
            "status": "insufficient_data",
            "reason": f"Need at least {required} bars; received {len(rows)}",
            "windows": [],
            "metrics": summarize_closed_trades([], capital_risk_per_trade_pct=safe_capital_risk_pct),
            "limitations": ["No timestamped historical news archive is available; news gating was not replayed."],
        }

    # Use the most recent complete windows.  Windows do not overlap in OOS
    # territory and no output from a later window can influence an earlier one.
    available_windows = max(0, (len(rows) - train_bars) // test_bars)
    window_count = min(max_windows, available_windows)
    first_start = len(rows) - (train_bars + (window_count * test_bars))
    windows: List[Dict[str, Any]] = []
    oos_trades: List[Dict[str, Any]] = []
    observed_oos_windows = 0
    cost_model: Optional[Dict[str, Any]] = None

    for index in range(window_count):
        start = first_start + (index * test_bars)
        test_start_index = start + train_bars
        end = test_start_index + test_bars
        window = rows.iloc[start:end]
        test_start = _as_timestamp(rows.index[test_start_index])
        test_end = _as_timestamp(rows.index[end - 1])
        replay = runner(ticker, days=max(1, int(len(window) / 1440)), warmup=warmup_bars, df=window)
        if cost_model is None and isinstance(replay, dict) and isinstance(replay.get("cost_model"), dict):
            cost_model = dict(replay["cost_model"])
        raw_trades = replay.get("trade_list", []) if isinstance(replay, dict) else []
        closed_in_window: List[Dict[str, Any]] = []
        for trade in raw_trades if isinstance(raw_trades, list) else []:
            entry_time = _as_timestamp(trade.get("entry_time"))
            exit_time = _as_timestamp(trade.get("exit_time"))
            # A terminal time_exit is an unrealized mark-to-market imposed by
            # the replay boundary, not a completed OOS trade.
            if str(trade.get("reason") or "").lower() == "time_exit":
                continue
            if entry_time is None or exit_time is None or test_start is None or test_end is None:
                continue
            if entry_time >= test_start and exit_time <= test_end:
                closed_in_window.append(dict(trade))

        oos_trades.extend(closed_in_window)
        if closed_in_window:
            observed_oos_windows += 1
        windows.append(
            {
                "train_start": _as_timestamp(rows.index[start]).isoformat(),
                "test_start": test_start.isoformat() if test_start is not None else None,
                "test_end": test_end.isoformat() if test_end is not None else None,
                "bars": {"train": train_bars, "test": test_bars},
                "has_closed_oos_evidence": bool(closed_in_window),
                "metrics": summarize_closed_trades(
                    closed_in_window,
                    capital_risk_per_trade_pct=safe_capital_risk_pct,
                ),
            }
        )

    metrics = summarize_closed_trades(
        oos_trades,
        capital_risk_per_trade_pct=safe_capital_risk_pct,
    )
    status, reason = _validation_status(
        metrics,
        completed_windows=observed_oos_windows,
        min_windows=max(1, min_windows),
        min_sample=max(1, min_sample),
        min_profit_factor=max(0.0, min_profit_factor),
        min_expectancy_r=min_expectancy_r,
        max_drawdown_pct=max(0.0, max_drawdown_pct),
    )
    return {
        "ticker": str(ticker).upper(),
        "method": "fixed_configuration_walk_forward_ohlcv",
        "status": status,
        "reason": reason,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "thresholds": {
            "min_windows": max(1, min_windows),
            "min_sample": max(1, min_sample),
            "min_profit_factor": max(0.0, min_profit_factor),
            "min_expectancy_r": min_expectancy_r,
            "max_drawdown_pct": max(0.0, max_drawdown_pct),
        },
        "capital_model": {
            "risk_per_trade_pct": safe_capital_risk_pct,
            "equity_model": "sequential_net_r_compounded_at_configured_risk",
            "is_realized_capital": False,
        },
        "metrics": metrics,
        "cost_model": cost_model,
        "observed_oos_windows": observed_oos_windows,
        "windows": windows,
        "oos_trades": oos_trades,
        "limitations": [
            "No parameter search or selection was performed inside these windows.",
            "No timestamped historical news archive is available; news gating was not replayed.",
            "Terminal time_exit marks at a test boundary are excluded because they are not closed OOS trades.",
        ],
    }


def run_faithful_v2_technical_walk_forward(
    ticker: str,
    df: pd.DataFrame,
    *,
    config_manager,
    train_bars: int,
    test_bars: int,
    max_windows: int,
    min_windows: int = 3,
    min_sample: int = 30,
    min_profit_factor: float = 1.2,
    min_expectancy_r: float = 0.0,
    max_drawdown_pct: float = 25.0,
    capital_risk_per_trade_pct: float = 1.0,
    warmup_bars: int = 60,
    replay_runner: Optional[ReplayRunner] = None,
) -> Dict[str, Any]:
    """Walk-forward replay of the actual V2 selector, without live-cache leakage.

    Every window has two phases:

    1. bootstrap the exact V2 selector on *only* the training bars to build a
       private, in-memory performance snapshot;
    2. replay train+test bars with the normal V2 profitability thresholds and
       that snapshot, then score only closed V2 trades opened and exited in the
       test interval.

    This is materially more faithful than the legacy QuickBacktester proxy.
    It remains **technical research only** because the project has no complete,
    timestamped historical AI-news archive.  Callers must not persist its
    result as ``validation_status=validated`` or use it to authorize Capital.
    The live Capital gate separately requires current source-backed AI news.
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        raise ValueError("A non-empty prepared OHLC dataframe is required")
    if config_manager is None:
        raise ValueError("config_manager is required for a faithful V2 replay")
    if train_bars <= warmup_bars or test_bars <= 0 or max_windows <= 0:
        raise ValueError("train_bars must exceed warmup_bars; test_bars and max_windows must be positive")

    safe_risk_pct = _capital_risk_per_trade_pct(capital_risk_per_trade_pct)
    symbol = str(ticker or "").upper().strip()
    v2_config = getattr(config_manager, "strategy_v2", {}) or {}
    from core.strategy_v2.context_builder import infer_asset_class
    from core.strategy_v2.performance_cache import build_perf_key
    from core.strategy_backtester import backtest

    asset_class = infer_asset_class(symbol)
    expected_keys = _enabled_v2_keys(symbol, asset_class, v2_config)
    costs = ((v2_config.get("costs_by_asset", {}) or {}).get(asset_class, {}) or {})
    if bool((getattr(df, "attrs", {}) or {}).get("source_truncated")):
        return {
            "ticker": symbol,
            "asset_class": asset_class,
            "method": "faithful_v2_technical_walk_forward",
            "status": "source_truncated",
            "promotion_status": "research_only",
            "reason": "Source OHLCV is explicitly marked truncated; no V2 technical conclusion was calculated",
            "strategy_reports": [],
            "windows": [],
            "limitations": ["Historical AI-news coverage is not available and was not replayed."],
        }

    runner = replay_runner or backtest
    rows = df.sort_index().copy()
    required = train_bars + test_bars
    if len(rows) < required:
        return {
            "ticker": symbol,
            "asset_class": asset_class,
            "method": "faithful_v2_technical_walk_forward",
            "status": "insufficient_data",
            "promotion_status": "research_only",
            "reason": f"Need at least {required} bars; received {len(rows)}",
            "strategy_reports": [],
            "windows": [],
            "limitations": ["Historical AI-news coverage is not available and was not replayed."],
        }

    available_windows = max(0, (len(rows) - train_bars) // test_bars)
    window_count = min(max_windows, available_windows)
    first_start = len(rows) - (train_bars + (window_count * test_bars))
    oos_by_key: Dict[tuple[str, str], List[Dict[str, Any]]] = {key: [] for key in expected_keys}
    training_eligible_windows: Dict[tuple[str, str], int] = {key: 0 for key in expected_keys}
    observed_oos_windows: Dict[tuple[str, str], int] = {key: 0 for key in expected_keys}
    windows: List[Dict[str, Any]] = []

    for index in range(window_count):
        start = first_start + (index * test_bars)
        test_start_index = start + train_bars
        end = test_start_index + test_bars
        train_df = rows.iloc[start:test_start_index].copy()
        replay_df = rows.iloc[start:end].copy()
        test_start = _as_timestamp(rows.index[test_start_index])
        test_end = _as_timestamp(rows.index[end - 1])

        # Phase 1: train-only selector bootstrap.  This can discover V2 setups
        # but cannot approve any strategy or inspect future OOS bars.
        bootstrap = runner(
            symbol,
            days=max(1, int(len(train_df) / 1440)),
            warmup=warmup_bars,
            df=train_df,
            config_manager=_v2_replay_config(config_manager, bootstrap=True),
            v2_only=True,
            # Research-only S7 candidates are intentionally visible live so
            # their outcomes can be measured.  Include them only while
            # constructing this private training snapshot; the test half below
            # still requires the normal V2 profitability gates.
            allow_research_only_candidates=True,
        )
        train_start = _as_timestamp(train_df.index[0])
        train_end = _as_timestamp(train_df.index[-1])
        train_by_key: Dict[tuple[str, str], List[Dict[str, Any]]] = {key: [] for key in expected_keys}
        for trade in _closed_v2_trades_in_range(bootstrap, start=train_start, end=train_end):
            key = (str(trade.get("strategy_id") or "").upper(), str(trade.get("dir") or "").upper())
            if key in train_by_key:
                train_by_key[key].append(trade)

        training_entries: Dict[str, Dict[str, Any]] = {}
        for key, training_trades in train_by_key.items():
            entry = _strategy_metric_entry(
                training_trades,
                capital_risk_per_trade_pct=safe_risk_pct,
                costs=costs,
            )
            # The V2 selector's normal sample/PF/expectancy/DD thresholds must
            # pass before a strategy becomes eligible in the test phase.  This
            # prevents cold entries from being disguised as OOS evidence.
            eligible = (
                int(entry["sample_size"]) >= max(1, min_sample)
                and entry["profit_factor"] is not None
                and _number(entry["profit_factor"]) >= min_profit_factor
                and _number(entry["expectancy_R"]) > min_expectancy_r
                and _number(entry["max_drawdown"]) <= max_drawdown_pct
            )
            if not eligible:
                continue
            strategy_id, direction = key
            entry.update(
                {
                    # Never persisted: this is merely the selector's
                    # point-in-time input for the OOS half of this window.
                    "validation_status": "training_window_only",
                    "validation_method": "faithful_v2_training_window",
                    "validation_as_of": train_end.isoformat() if train_end is not None else None,
                    "last_run": train_end.isoformat() if train_end is not None else None,
                }
            )
            training_entries[build_perf_key(symbol, asset_class, strategy_id, direction)] = entry
            training_eligible_windows[key] += 1

        # Phase 2: same V2 selector + normal profitability thresholds, with a
        # private immutable cache populated only from this window's training
        # phase.  No live cache read/write occurs here.
        replay = runner(
            symbol,
            days=max(1, int(len(replay_df) / 1440)),
            warmup=warmup_bars,
            df=replay_df,
            config_manager=_v2_replay_config(config_manager, bootstrap=False),
            v2_perf_cache=_TrainingWindowPerformanceCache(training_entries),
            v2_only=True,
        )
        test_trades = _closed_v2_trades_in_range(replay, start=test_start, end=test_end)
        per_window_counts: Dict[str, int] = {}
        for trade in test_trades:
            key = (str(trade.get("strategy_id") or "").upper(), str(trade.get("dir") or "").upper())
            if key not in oos_by_key:
                continue
            oos_by_key[key].append(trade)
            label = f"{key[0]}:{key[1]}"
            per_window_counts[label] = per_window_counts.get(label, 0) + 1
        for label in per_window_counts:
            strategy_id, direction = label.rsplit(":", 1)
            key = (strategy_id, direction)
            if key in observed_oos_windows:
                observed_oos_windows[key] += 1

        windows.append(
            {
                "train_start": train_start.isoformat() if train_start is not None else None,
                "train_end": train_end.isoformat() if train_end is not None else None,
                "test_start": test_start.isoformat() if test_start is not None else None,
                "test_end": test_end.isoformat() if test_end is not None else None,
                "bars": {"train": train_bars, "test": test_bars},
                "eligible_training_strategies": sorted(
                    key for key, value in training_entries.items() if value
                ),
                "closed_oos_v2_trades": per_window_counts,
            }
        )

    strategy_reports: List[Dict[str, Any]] = []
    validated_technical = 0
    for strategy_id, direction in expected_keys:
        metrics = _strategy_metric_entry(
            oos_by_key[(strategy_id, direction)],
            capital_risk_per_trade_pct=safe_risk_pct,
            costs=costs,
        )
        technical_status, technical_reason = _validation_status(
            {
                "sample_size": metrics["sample_size"],
                "profit_factor": metrics["profit_factor"],
                "expectancy_r": metrics["expectancy_R"],
                "max_drawdown_pct": metrics["max_drawdown"],
            },
            completed_windows=observed_oos_windows[(strategy_id, direction)],
            min_windows=max(1, min_windows),
            min_sample=max(1, min_sample),
            min_profit_factor=max(0.0, min_profit_factor),
            min_expectancy_r=min_expectancy_r,
            max_drawdown_pct=max(0.0, max_drawdown_pct),
        )
        if technical_status == "validated":
            validated_technical += 1
        strategy_reports.append(
            {
                "strategy_id": strategy_id,
                "direction": direction,
                "technical_status": technical_status,
                "technical_reason": technical_reason,
                "promotion_status": "research_only",
                "metrics": metrics,
                # Backward-compatible field now reflects windows with actual
                # closed OOS evidence, matching _validation_status semantics.
                "eligible_windows": observed_oos_windows[(strategy_id, direction)],
                "eligible_training_windows": training_eligible_windows[(strategy_id, direction)],
                "observed_oos_windows": observed_oos_windows[(strategy_id, direction)],
                "validation_method": "faithful_v2_technical_walk_forward",
            }
        )

    return {
        "ticker": symbol,
        "asset_class": asset_class,
        "method": "faithful_v2_technical_walk_forward",
        "status": "research_only",
        "promotion_status": "research_only",
        "reason": (
            f"{validated_technical} strategy/direction pairs passed technical OOS thresholds, "
            "but none are promotable without timestamped historical AI-news coverage"
        ),
        "as_of": datetime.now(timezone.utc).isoformat(),
        "thresholds": {
            "min_windows": max(1, min_windows),
            "min_sample": max(1, min_sample),
            "min_profit_factor": max(0.0, min_profit_factor),
            "min_expectancy_r": min_expectancy_r,
            "max_drawdown_pct": max(0.0, max_drawdown_pct),
        },
        "capital_model": {
            "risk_per_trade_pct": safe_risk_pct,
            "equity_model": "sequential_net_r_compounded_at_configured_risk",
            "is_realized_capital": False,
        },
        "cost_model": dict(costs),
        "strategy_reports": strategy_reports,
        "windows": windows,
        "limitations": [
            "No timestamped historical AI-news archive with source provenance is available; the live AI-news gate was not replayed.",
            "Training-window performance is ephemeral and is never persisted to the live strategy performance cache.",
            "Capital execution remains separately gated by fresh source-backed AI news and realized performance.",
        ],
    }
