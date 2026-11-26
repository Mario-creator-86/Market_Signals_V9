"""Immutable capital, portfolio, risk, accounting, and position contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from numbers import Real
from types import MappingProxyType
from typing import Any, Mapping, Optional


def _finite(value: Any, name: str, *, minimum: Optional[float] = None) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return result


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported immutable value: {type(value).__name__}")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _utc_iso(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("assessed_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("assessed_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


class CapitalMethod(str, Enum):
    FIXED_AMOUNT = "FIXED_AMOUNT"
    FIXED_PERCENTAGE = "FIXED_PERCENTAGE"
    FIXED_RISK = "FIXED_RISK"
    INVERSE_VOLATILITY = "INVERSE_VOLATILITY"
    VOLATILITY_TARGETING = "VOLATILITY_TARGETING"


class PositionAction(str, Enum):
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"


@dataclass(frozen=True)
class SizingRequest:
    method: CapitalMethod
    equity_usd: float
    fixed_amount_usd: Optional[float] = None
    percentage_of_equity: Optional[float] = None
    risk_per_trade_pct: Optional[float] = None
    stop_loss_fraction: Optional[float] = None
    risk_budget_pct: Optional[float] = None
    asset_volatility: Optional[float] = None
    allocation_pct: Optional[float] = None
    target_volatility: Optional[float] = None

    def __post_init__(self) -> None:
        method = self.method if isinstance(self.method, CapitalMethod) else CapitalMethod(str(self.method).upper())
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "equity_usd", _finite(self.equity_usd, "equity_usd", minimum=0.0))
        for name in (
            "fixed_amount_usd", "percentage_of_equity", "risk_per_trade_pct",
            "stop_loss_fraction", "risk_budget_pct", "asset_volatility",
            "allocation_pct", "target_volatility",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _finite(value, name, minimum=0.0))


@dataclass(frozen=True)
class SizingResult:
    method: CapitalMethod
    notional_usd: float
    formula: str
    inputs: Mapping[str, Any]
    is_authoritative: bool = True

    def __post_init__(self) -> None:
        method = self.method if isinstance(self.method, CapitalMethod) else CapitalMethod(str(self.method).upper())
        formula = str(self.formula or "").strip()
        if not formula or not isinstance(self.inputs, Mapping):
            raise ValueError("formula and inputs are required")
        if self.is_authoritative is not True:
            raise ValueError("application sizing results must be authoritative")
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "notional_usd", _finite(self.notional_usd, "notional_usd", minimum=0.0))
        object.__setattr__(self, "formula", formula)
        object.__setattr__(self, "inputs", _freeze(self.inputs))


@dataclass(frozen=True)
class PortfolioPosition:
    symbol: str
    sector: str
    notional_usd: float
    leverage: float
    loss_at_stop_usd: float

    def __post_init__(self) -> None:
        symbol = str(self.symbol or "").strip().upper()
        sector = str(self.sector or "").strip().upper()
        if not symbol or not sector:
            raise ValueError("position symbol and sector are required")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "sector", sector)
        object.__setattr__(self, "notional_usd", _finite(self.notional_usd, "notional_usd", minimum=0.0))
        object.__setattr__(self, "leverage", _finite(self.leverage, "leverage", minimum=1.0))
        object.__setattr__(self, "loss_at_stop_usd", _finite(self.loss_at_stop_usd, "loss_at_stop_usd", minimum=0.0))


@dataclass(frozen=True)
class PortfolioSnapshot:
    equity_usd: float
    cash_usd: float
    positions: tuple[PortfolioPosition, ...] = ()
    daily_realized_pnl_usd: float = 0.0
    peak_equity_usd: Optional[float] = None
    correlations: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        equity = _finite(self.equity_usd, "equity_usd", minimum=0.0)
        if equity <= 0:
            raise ValueError("equity_usd must be > 0")
        cash = _finite(self.cash_usd, "cash_usd", minimum=0.0)
        positions = tuple(self.positions)
        if any(not isinstance(position, PortfolioPosition) for position in positions):
            raise TypeError("positions must contain PortfolioPosition values")
        daily = _finite(self.daily_realized_pnl_usd, "daily_realized_pnl_usd")
        peak = equity if self.peak_equity_usd is None else _finite(
            self.peak_equity_usd, "peak_equity_usd", minimum=0.0
        )
        if peak < equity:
            raise ValueError("peak_equity_usd cannot be below current equity")
        if not isinstance(self.correlations, Mapping):
            raise TypeError("correlations must be a mapping")
        correlations = {}
        for symbol, value in self.correlations.items():
            correlation = _finite(value, f"correlation[{symbol}]")
            if not -1.0 <= correlation <= 1.0:
                raise ValueError("correlations must be between -1 and 1")
            correlations[str(symbol).strip().upper()] = correlation
        object.__setattr__(self, "equity_usd", equity)
        object.__setattr__(self, "cash_usd", cash)
        object.__setattr__(self, "positions", positions)
        object.__setattr__(self, "daily_realized_pnl_usd", daily)
        object.__setattr__(self, "peak_equity_usd", peak)
        object.__setattr__(self, "correlations", _freeze(correlations))


@dataclass(frozen=True)
class OrderProposal:
    symbol: str
    sector: str
    notional_usd: float
    margin_required_usd: float
    fees_usd: float
    leverage: float
    loss_at_stop_usd: float
    average_daily_volume_usd: float
    data_age_seconds: float
    has_data_conflict: bool
    strategy_status: str
    is_live_autonomous: bool = False
    live_auto_owner_enabled: bool = False
    kill_switch_active: bool = False

    def __post_init__(self) -> None:
        symbol = str(self.symbol or "").strip().upper()
        sector = str(self.sector or "").strip().upper()
        status = str(self.strategy_status or "").strip().upper()
        if not symbol or not sector or not status:
            raise ValueError("proposal symbol, sector, and strategy_status are required")
        for name in (
            "has_data_conflict", "is_live_autonomous",
            "live_auto_owner_enabled", "kill_switch_active",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "sector", sector)
        object.__setattr__(self, "strategy_status", status)
        for name, minimum in (
            ("notional_usd", 0.0), ("margin_required_usd", 0.0),
            ("fees_usd", 0.0), ("leverage", 1.0),
            ("loss_at_stop_usd", 0.0), ("average_daily_volume_usd", 0.0),
            ("data_age_seconds", 0.0),
        ):
            object.__setattr__(self, name, _finite(getattr(self, name), name, minimum=minimum))
        if self.notional_usd <= 0:
            raise ValueError("notional_usd must be > 0")
        if self.margin_required_usd > self.notional_usd:
            raise ValueError("margin_required_usd cannot exceed notional_usd")


@dataclass(frozen=True)
class ConstraintCheck:
    name: str
    passed: bool
    actual: Any = None
    limit: Any = None
    detail: str = ""

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        if not name:
            raise ValueError("constraint check name is required")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "actual", _freeze(self.actual))
        object.__setattr__(self, "limit", _freeze(self.limit))
        object.__setattr__(self, "detail", str(self.detail or ""))


@dataclass(frozen=True)
class ConstraintDecision:
    allowed: bool
    checks: tuple[ConstraintCheck, ...]
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        checks = tuple(self.checks)
        if any(not isinstance(check, ConstraintCheck) for check in checks):
            raise TypeError("checks must contain ConstraintCheck values")
        reasons = tuple(str(reason or "").strip() for reason in self.reasons if str(reason or "").strip())
        all_passed = bool(checks) and all(check.passed for check in checks)
        if bool(self.allowed) != all_passed:
            raise ValueError("allowed must equal all constraint checks")
        if self.allowed and reasons:
            raise ValueError("allowed decision cannot have reasons")
        if not self.allowed and not reasons:
            raise ValueError("rejected decision requires reasons")
        object.__setattr__(self, "checks", checks)
        object.__setattr__(self, "reasons", reasons)


@dataclass(frozen=True)
class OpenTradeAccounting:
    direction: str
    starting_cash_usd: float
    entry_price: float
    quantity: float
    notional_usd: float
    leverage: float
    margin_usd: float
    entry_fee_usd: float
    cash_after_open_usd: float

    def __post_init__(self) -> None:
        direction = str(self.direction or "").strip().upper()
        if direction not in {"LONG", "SHORT"}:
            raise ValueError("direction must be LONG or SHORT")
        object.__setattr__(self, "direction", direction)
        for name, minimum in (
            ("starting_cash_usd", 0.0), ("entry_price", 0.0), ("quantity", 0.0),
            ("notional_usd", 0.0), ("leverage", 1.0), ("margin_usd", 0.0),
            ("entry_fee_usd", 0.0), ("cash_after_open_usd", 0.0),
        ):
            object.__setattr__(self, name, _finite(getattr(self, name), name, minimum=minimum))
        if self.entry_price <= 0 or self.quantity <= 0 or self.notional_usd <= 0:
            raise ValueError("entry price, quantity, and notional must be > 0")
        if not math.isclose(
            self.notional_usd,
            self.entry_price * self.quantity,
            rel_tol=1e-9,
            abs_tol=1e-6,
        ):
            raise ValueError("notional_usd must equal entry_price * quantity")
        if not math.isclose(
            self.margin_usd,
            self.notional_usd / self.leverage,
            rel_tol=1e-9,
            abs_tol=1e-6,
        ):
            raise ValueError("margin_usd must equal notional_usd / leverage")
        expected_cash = self.starting_cash_usd - self.margin_usd - self.entry_fee_usd
        if expected_cash < 0 or not math.isclose(
            self.cash_after_open_usd, expected_cash, rel_tol=1e-9, abs_tol=1e-6
        ):
            raise ValueError("cash_after_open_usd violates the open conservation identity")


@dataclass(frozen=True)
class TradeSettlement:
    opened: OpenTradeAccounting
    exit_price: float
    exit_fee_usd: float
    gross_pnl_usd: float
    net_pnl_usd: float
    ending_cash_usd: float

    def __post_init__(self) -> None:
        if not isinstance(self.opened, OpenTradeAccounting):
            raise TypeError("opened must be OpenTradeAccounting")
        for name, minimum in (
            ("exit_price", 0.0), ("exit_fee_usd", 0.0), ("ending_cash_usd", 0.0),
        ):
            object.__setattr__(self, name, _finite(getattr(self, name), name, minimum=minimum))
        for name in ("gross_pnl_usd", "net_pnl_usd"):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        signed_move = (
            self.exit_price - self.opened.entry_price
            if self.opened.direction == "LONG"
            else self.opened.entry_price - self.exit_price
        )
        expected_gross = signed_move * self.opened.quantity
        expected_net = expected_gross - self.opened.entry_fee_usd - self.exit_fee_usd
        expected_cash = self.opened.starting_cash_usd + expected_net
        if not math.isclose(self.gross_pnl_usd, expected_gross, rel_tol=1e-9, abs_tol=1e-6):
            raise ValueError("gross_pnl_usd violates the price/quantity identity")
        if not math.isclose(self.net_pnl_usd, expected_net, rel_tol=1e-9, abs_tol=1e-6):
            raise ValueError("net_pnl_usd violates the fee identity")
        if expected_cash < 0 or not math.isclose(
            self.ending_cash_usd, expected_cash, rel_tol=1e-9, abs_tol=1e-6
        ):
            raise ValueError("ending_cash_usd violates the close conservation identity")


@dataclass(frozen=True)
class PositionEvidence:
    entry_thesis: str
    supporting_evidence: tuple[str, ...]
    opposing_evidence: tuple[str, ...]
    strategy: str
    signal: Mapping[str, Any]
    ai_model: Optional[str]
    invalidation_rules: tuple[str, ...]
    risk_state: Mapping[str, Any]

    def __post_init__(self) -> None:
        thesis = str(self.entry_thesis or "").strip()
        strategy = str(self.strategy or "").strip().upper()
        if not thesis or not strategy:
            raise ValueError("entry_thesis and strategy are required")
        if not isinstance(self.signal, Mapping) or not isinstance(self.risk_state, Mapping):
            raise TypeError("signal and risk_state must be mappings")
        supporting = tuple(str(value).strip() for value in self.supporting_evidence if str(value).strip())
        opposing = tuple(str(value).strip() for value in self.opposing_evidence if str(value).strip())
        invalidations = tuple(str(value).strip() for value in self.invalidation_rules if str(value).strip())
        if not invalidations:
            raise ValueError("at least one invalidation rule is required")
        object.__setattr__(self, "entry_thesis", thesis)
        object.__setattr__(self, "strategy", strategy)
        object.__setattr__(self, "supporting_evidence", supporting)
        object.__setattr__(self, "opposing_evidence", opposing)
        object.__setattr__(self, "signal", _freeze(self.signal))
        object.__setattr__(self, "ai_model", str(self.ai_model).strip() if self.ai_model else None)
        object.__setattr__(self, "invalidation_rules", invalidations)
        object.__setattr__(self, "risk_state", _freeze(self.risk_state))

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_thesis": self.entry_thesis,
            "supporting_evidence": list(self.supporting_evidence),
            "opposing_evidence": list(self.opposing_evidence),
            "strategy": self.strategy,
            "signal": _thaw(self.signal),
            "ai_model": self.ai_model,
            "invalidation_rules": list(self.invalidation_rules),
            "risk_state": _thaw(self.risk_state),
        }


@dataclass(frozen=True)
class PositionAssessment:
    action: PositionAction
    reasons: tuple[str, ...]
    triggered_rules: tuple[str, ...]
    assessed_at: str

    def __post_init__(self) -> None:
        action = self.action if isinstance(self.action, PositionAction) else PositionAction(str(self.action).upper())
        reasons = tuple(str(value).strip() for value in self.reasons if str(value).strip())
        rules = tuple(str(value).strip() for value in self.triggered_rules if str(value).strip())
        if not reasons:
            raise ValueError("position assessment requires reasons")
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(self, "triggered_rules", rules)
        object.__setattr__(self, "assessed_at", _utc_iso(self.assessed_at))


__all__ = [
    "CapitalMethod", "ConstraintCheck", "ConstraintDecision", "OpenTradeAccounting",
    "OrderProposal", "PortfolioPosition", "PortfolioSnapshot", "PositionAction",
    "PositionAssessment", "PositionEvidence", "SizingRequest", "SizingResult",
    "TradeSettlement",
]
