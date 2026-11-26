"""Pure, deterministic computation of the complete section 58 metric set."""

from __future__ import annotations

import math
import statistics
from dataclasses import replace
from typing import Any, Mapping, Optional, Sequence

from .types import BacktestMetrics, Trade


def _validated_curve(values: Sequence[float], *, name: str) -> tuple[float, ...]:
    curve = tuple(float(value) for value in values)
    if not curve:
        raise ValueError(f"{name} must not be empty")
    if any(not math.isfinite(value) or value < 0 for value in curve):
        raise ValueError(f"{name} must contain finite, non-negative values")
    return curve


def _equity_from_trades(trades: Sequence[Trade], initial_capital: float) -> tuple[float, ...]:
    equity = [initial_capital]
    for trade in trades:
        net_pnl = trade.gross_pnl - trade.commission - trade.slippage_cost
        equity.append(equity[-1] + net_pnl)
    return tuple(equity)


def _max_drawdown_pct(equity: Sequence[float]) -> float:
    peak = float(equity[0])
    maximum = 0.0
    for value in equity:
        peak = max(peak, float(value))
        if peak > 0:
            maximum = max(maximum, (peak - float(value)) / peak)
    return maximum * 100.0


def _cagr_pct(net_return_pct: float, date_span_days: Optional[float]) -> float:
    if date_span_days is None:
        return 0.0
    if not math.isfinite(date_span_days) or date_span_days <= 0:
        raise ValueError("date_span_days must be finite and greater than zero")
    terminal_multiple = 1.0 + net_return_pct / 100.0
    if terminal_multiple <= 0:
        return -100.0
    years = date_span_days / 365.25
    return (terminal_multiple ** (1.0 / years) - 1.0) * 100.0


def _risk_adjusted(r_multiples: Sequence[float]) -> tuple[float, float]:
    if len(r_multiples) < 2:
        return 0.0, 0.0
    mean_return = statistics.fmean(r_multiples)
    volatility = statistics.pstdev(r_multiples)
    sharpe = mean_return / volatility if volatility > 0 else 0.0
    downside_squared = [min(value, 0.0) ** 2 for value in r_multiples]
    downside_deviation = math.sqrt(statistics.fmean(downside_squared))
    sortino = mean_return / downside_deviation if downside_deviation > 0 else 0.0
    return sharpe, sortino


def _benchmark_return_pct(curve: Optional[Sequence[float]]) -> Optional[float]:
    if curve is None:
        return None
    validated = _validated_curve(curve, name="benchmark_equity_curve")
    if len(validated) < 2 or validated[0] == 0:
        return None
    return (validated[-1] / validated[0] - 1.0) * 100.0


def compute_metrics(
    trades: Sequence[Trade],
    *,
    initial_capital: float,
    equity_curve: Optional[Sequence[float]] = None,
    date_span_days: Optional[float] = None,
    benchmark_equity_curve: Optional[Sequence[float]] = None,
    validation_scope: str,
    is_authoritative: bool,
) -> BacktestMetrics:
    """Compute V9 metrics using net equity and auditable trade-level inputs.

    Percent fields use percentage points.  ``exposure_pct`` and
    ``turnover_pct`` retain the legacy ratio convention (0.4 means 40%).
    Undefined profit factor and benchmark values remain ``None``.
    """

    if not math.isfinite(initial_capital) or initial_capital <= 0:
        raise ValueError("initial_capital must be finite and greater than zero")
    if not validation_scope.strip():
        raise ValueError("validation_scope must not be empty")
    trade_list = tuple(trades)
    equity = (
        _validated_curve(equity_curve, name="equity_curve")
        if equity_curve is not None
        else _equity_from_trades(trade_list, float(initial_capital))
    )
    final_capital = equity[-1]
    net_return_pct = (final_capital / initial_capital - 1.0) * 100.0

    r_multiples = [float(trade.r_multiple) for trade in trade_list]
    wins = [trade for trade in trade_list if trade.gross_pnl > 0]
    losses = [trade for trade in trade_list if trade.gross_pnl < 0]
    gross_profit = sum(trade.gross_pnl for trade in wins)
    gross_loss = abs(sum(trade.gross_pnl for trade in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    expectancy_r = statistics.fmean(r_multiples) if r_multiples else 0.0
    max_drawdown_pct = _max_drawdown_pct(equity)
    sharpe, sortino = _risk_adjusted(r_multiples)

    last_observed_bar = max((trade.exit_bar for trade in trade_list), default=0)
    holding_bars = sum(trade.holding_bars for trade in trade_list)
    exposure = min(1.0, holding_bars / last_observed_bar) if last_observed_bar else 0.0
    entry_notional = sum(abs(trade.size * trade.entry_price) for trade in trade_list)
    turnover = entry_notional / initial_capital
    cagr = _cagr_pct(net_return_pct, date_span_days)

    return BacktestMetrics(
        initial_capital=float(initial_capital),
        final_capital=final_capital,
        net_return_pct=net_return_pct,
        cagr=cagr,
        benchmark_net_return_pct=_benchmark_return_pct(benchmark_equity_curve),
        max_drawdown_pct=max_drawdown_pct,
        sharpe=sharpe,
        sortino=sortino,
        calmar=cagr / max_drawdown_pct if max_drawdown_pct > 0 else 0.0,
        win_rate=len(wins) / len(trade_list) if trade_list else 0.0,
        profit_factor=profit_factor,
        expectancy_r=expectancy_r,
        num_trades=len(trade_list),
        avg_trade_r=expectancy_r,
        median_trade_r=statistics.median(r_multiples) if r_multiples else 0.0,
        best_trade_r=max(r_multiples, default=0.0),
        worst_trade_r=min(r_multiples, default=0.0),
        avg_holding_bars=statistics.fmean(trade.holding_bars for trade in trade_list) if trade_list else 0.0,
        exposure_pct=exposure,
        turnover_pct=turnover,
        fees_total=sum(trade.commission for trade in trade_list),
        slippage_total=sum(trade.slippage_cost for trade in trade_list),
        equity_curve=equity,
        validation_scope=validation_scope,
        is_authoritative=bool(is_authoritative),
    )


def _finite_number(raw: Mapping[str, Any], keys: Sequence[str], default: Any) -> Any:
    for key in keys:
        if key not in raw or raw[key] is None:
            continue
        value = raw[key]
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return value
    return default


def merge_with_legacy(
    raw_metrics: Mapping[str, Any],
    trades: Sequence[Trade],
    *,
    initial_capital: float,
    date_span_days: Optional[float] = None,
    validation_scope: str,
    is_authoritative: bool,
) -> BacktestMetrics:
    """Preserve trustworthy legacy metrics and deterministically fill its gaps."""

    legacy_curve = raw_metrics.get("equity_curve")
    computed = compute_metrics(
        trades,
        initial_capital=initial_capital,
        equity_curve=legacy_curve,
        date_span_days=date_span_days,
        validation_scope=validation_scope,
        is_authoritative=is_authoritative,
    )
    return replace(
        computed,
        initial_capital=float(_finite_number(raw_metrics, ("initial_capital",), computed.initial_capital)),
        final_capital=float(_finite_number(raw_metrics, ("final_capital",), computed.final_capital)),
        net_return_pct=float(
            _finite_number(raw_metrics, ("total_return_pct", "net_return_pct"), computed.net_return_pct)
        ),
        max_drawdown_pct=float(
            _finite_number(raw_metrics, ("max_drawdown_pct",), computed.max_drawdown_pct)
        ),
        sharpe=float(_finite_number(raw_metrics, ("sharpe_ratio", "sharpe"), computed.sharpe)),
        sortino=float(_finite_number(raw_metrics, ("sortino_ratio", "sortino"), computed.sortino)),
        calmar=float(_finite_number(raw_metrics, ("calmar_ratio", "calmar"), computed.calmar)),
        win_rate=float(_finite_number(raw_metrics, ("win_rate",), computed.win_rate)),
        profit_factor=_finite_number(raw_metrics, ("profit_factor",), computed.profit_factor),
        num_trades=int(_finite_number(raw_metrics, ("total_trades", "num_trades"), computed.num_trades)),
        avg_holding_bars=float(
            _finite_number(raw_metrics, ("avg_holding_bars",), computed.avg_holding_bars)
        ),
    )
