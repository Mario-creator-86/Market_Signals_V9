"""Backtest domain types for specification sections 57 through 61.

The immutable types in this module form the stable boundary between legacy
engines and the V9 validation stack.  Backtest statistics are realized
measurements, never predictive probabilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple

IndexRange = Tuple[int, int]


class BacktestExecutionTiming(str, Enum):
    NEXT_BAR_OPEN = "next_bar_open"


class BacktestDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class EvidenceLevel(str, Enum):
    TICKER = "TICKER"
    INDUSTRY = "INDUSTRY"
    SECTOR = "SECTOR"
    UNIVERSE = "UNIVERSE"

    @classmethod
    def ordered(cls) -> tuple["EvidenceLevel", ...]:
        return cls.TICKER, cls.INDUSTRY, cls.SECTOR, cls.UNIVERSE


@dataclass(frozen=True)
class BacktestConfig:
    ticker: str
    asset_class: str
    initial_capital: float
    position_size_pct: float
    tp_pct: float
    sl_pct: float
    max_holding_bars: int
    direction: BacktestDirection | str = "BOTH"
    strategy_id: Optional[str] = None
    commission_pct: Optional[float] = None
    cost_model: Optional[Mapping[str, Any]] = None
    train_range: Optional[IndexRange] = None
    validation_range: Optional[IndexRange] = None
    out_of_sample_range: Optional[IndexRange] = None
    final_holdout_range: Optional[IndexRange] = None
    execution_timing: BacktestExecutionTiming = BacktestExecutionTiming.NEXT_BAR_OPEN
    no_look_ahead: bool = True

    def __post_init__(self) -> None:
        ticker = self.ticker.strip().upper()
        if not ticker:
            raise ValueError("ticker must not be empty")
        object.__setattr__(self, "ticker", ticker)
        if not self.asset_class.strip():
            raise ValueError("asset_class must not be empty")
        numeric = (
            self.initial_capital,
            self.position_size_pct,
            self.tp_pct,
            self.sl_pct,
        )
        if any(not math.isfinite(float(value)) for value in numeric):
            raise ValueError("backtest financial inputs must be finite")
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be greater than zero")
        if not 0 < self.position_size_pct <= 1:
            raise ValueError("position_size_pct must be in (0, 1]")
        if self.tp_pct <= 0 or self.sl_pct <= 0:
            raise ValueError("tp_pct and sl_pct must be greater than zero")
        if self.max_holding_bars <= 0:
            raise ValueError("max_holding_bars must be greater than zero")
        if self.commission_pct is not None:
            if not math.isfinite(float(self.commission_pct)) or self.commission_pct < 0:
                raise ValueError("commission_pct must be finite and non-negative")
        direction = self.direction.value if isinstance(self.direction, BacktestDirection) else str(self.direction).upper()
        if direction not in {"LONG", "SHORT", "BOTH"}:
            raise ValueError("direction must be LONG, SHORT, or BOTH")
        object.__setattr__(self, "direction", direction)
        if self.execution_timing is not BacktestExecutionTiming.NEXT_BAR_OPEN:
            raise ValueError("only next-bar-open execution is permitted")
        if not self.no_look_ahead:
            raise ValueError("no-look-ahead cannot be disabled")
        for name in ("train_range", "validation_range", "out_of_sample_range", "final_holdout_range"):
            value = getattr(self, name)
            if value is not None and (len(value) != 2 or value[0] < 0 or value[0] >= value[1]):
                raise ValueError(f"{name} must be a non-empty half-open range")
        if self.cost_model is not None:
            object.__setattr__(self, "cost_model", MappingProxyType(dict(self.cost_model)))


@dataclass(frozen=True)
class Trade:
    entry_bar: int
    exit_bar: int
    entry_time: Optional[str]
    exit_time: Optional[str]
    direction: BacktestDirection | str
    entry_price: float
    exit_price: float
    sl: Optional[float]
    tp: Optional[float]
    size: float
    gross_pnl: float
    commission: float
    slippage_cost: float
    result: str
    r_multiple: float
    exit_reason: str
    holding_bars: int

    def __post_init__(self) -> None:
        if self.entry_bar < 0 or self.exit_bar < self.entry_bar:
            raise ValueError("trade bars must be ordered and non-negative")
        numeric = (
            self.entry_price,
            self.exit_price,
            self.size,
            self.gross_pnl,
            self.commission,
            self.slippage_cost,
            self.r_multiple,
        )
        if any(not math.isfinite(float(value)) for value in numeric):
            raise ValueError("trade financial values must be finite")
        if any(value is not None and not math.isfinite(float(value)) for value in (self.sl, self.tp)):
            raise ValueError("trade stop and target must be finite when present")
        if self.entry_price < 0 or self.exit_price < 0 or self.size < 0:
            raise ValueError("prices and size must be non-negative")
        if self.commission < 0 or self.slippage_cost < 0:
            raise ValueError("trading costs must be non-negative")
        if self.holding_bars < 0:
            raise ValueError("holding_bars must be non-negative")
        direction = self.direction.value if isinstance(self.direction, BacktestDirection) else str(self.direction).upper()
        if direction not in {"LONG", "SHORT"}:
            raise ValueError("trade direction must be LONG or SHORT")
        object.__setattr__(self, "direction", direction)


@dataclass(frozen=True)
class BacktestMetrics:
    initial_capital: float
    final_capital: float
    net_return_pct: float
    cagr: float
    benchmark_net_return_pct: Optional[float]
    max_drawdown_pct: float
    sharpe: float
    sortino: float
    calmar: float
    win_rate: float
    profit_factor: Optional[float]
    expectancy_r: float
    num_trades: int
    avg_trade_r: float
    median_trade_r: float
    best_trade_r: float
    worst_trade_r: float
    avg_holding_bars: float
    exposure_pct: float
    turnover_pct: float
    fees_total: float
    slippage_total: float
    equity_curve: Tuple[float, ...]
    validation_scope: str
    is_authoritative: bool
    is_not_probability: bool = field(default=True, init=False)

    def __post_init__(self) -> None:
        required_numeric = (
            self.initial_capital,
            self.final_capital,
            self.net_return_pct,
            self.cagr,
            self.max_drawdown_pct,
            self.sharpe,
            self.sortino,
            self.calmar,
            self.win_rate,
            self.expectancy_r,
            self.avg_trade_r,
            self.median_trade_r,
            self.best_trade_r,
            self.worst_trade_r,
            self.avg_holding_bars,
            self.exposure_pct,
            self.turnover_pct,
            self.fees_total,
            self.slippage_total,
        )
        optional_numeric = (self.benchmark_net_return_pct, self.profit_factor)
        if any(not math.isfinite(float(value)) for value in required_numeric):
            raise ValueError("backtest metrics must contain only finite values")
        if any(value is not None and not math.isfinite(float(value)) for value in optional_numeric):
            raise ValueError("optional backtest metrics must be finite when present")
        if self.initial_capital <= 0 or self.final_capital < 0:
            raise ValueError("capital metrics are outside their valid range")
        if self.max_drawdown_pct < 0 or not 0 <= self.win_rate <= 1:
            raise ValueError("drawdown and win_rate metrics are outside their valid range")
        if self.profit_factor is not None and self.profit_factor < 0:
            raise ValueError("profit_factor must be non-negative")
        if self.num_trades < 0 or self.avg_holding_bars < 0:
            raise ValueError("trade count and holding period must be non-negative")
        if not 0 <= self.exposure_pct <= 1 or self.turnover_pct < 0:
            raise ValueError("exposure and turnover metrics are outside their valid range")
        if self.fees_total < 0 or self.slippage_total < 0:
            raise ValueError("cost metrics must be non-negative")
        equity = tuple(float(value) for value in self.equity_curve)
        if not equity or any(not math.isfinite(value) or value < 0 for value in equity):
            raise ValueError("equity_curve must contain finite, non-negative values")
        if not self.validation_scope.strip():
            raise ValueError("validation_scope must not be empty")
        object.__setattr__(self, "equity_curve", equity)


@dataclass(frozen=True)
class BacktestResult:
    config: BacktestConfig
    metrics: BacktestMetrics
    trades: Tuple[Trade, ...]
    evidence_level: EvidenceLevel = EvidenceLevel.TICKER
    no_look_ahead: bool = True
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.no_look_ahead:
            raise ValueError("no-look-ahead cannot be disabled")
        object.__setattr__(self, "trades", tuple(self.trades))
        object.__setattr__(self, "raw", MappingProxyType(dict(self.raw)))
