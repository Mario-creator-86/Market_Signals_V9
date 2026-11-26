"""Owner-confirmed Risk Constitution and complete portfolio constraint gate."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from numbers import Real
from typing import Any, Mapping

from .types import (
    ConstraintCheck,
    ConstraintDecision,
    OrderProposal,
    PortfolioSnapshot,
)


class RiskConstitutionError(ValueError):
    """Raised when protected owner values are absent, malformed, or unconfirmed."""


def resolve_confirmed_risk_constitution(
    config: Mapping[str, Any] | None,
) -> "RiskConstitution | None":
    """Resolve an explicitly confirmed constitution, preserving the V7 path otherwise."""

    if config is None:
        return None
    if not isinstance(config, Mapping):
        raise RiskConstitutionError("application config must be a mapping")
    raw = config.get("risk_constitution")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise RiskConstitutionError("risk_constitution must be a mapping")
    confirmation = raw.get("owner_confirmed")
    if confirmation is False or confirmation is None:
        return None
    if confirmation is not True:
        raise RiskConstitutionError("owner_confirmed must be boolean")
    return RiskConstitution.from_mapping(raw)


def _number(value: Any, name: str, *, minimum: float, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise RiskConstitutionError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise RiskConstitutionError(f"{name} must be finite and >= {minimum}")
    if maximum is not None and result > maximum:
        raise RiskConstitutionError(f"{name} must be <= {maximum}")
    return result


@dataclass(frozen=True)
class RiskConstitution:
    owner_confirmed: bool
    max_risk_per_trade_pct: float
    max_position_size_pct: float
    max_total_exposure_pct: float
    max_leverage: float
    minimum_cash_pct: float
    max_sector_exposure_pct: float
    correlation_threshold: float
    max_correlated_exposure_pct: float
    max_positions: int
    max_liquidity_participation_pct: float
    max_daily_loss_pct: float
    max_daily_loss_trades: int
    max_drawdown_pct: float
    max_data_age_seconds: int
    block_data_conflicts: bool
    require_approved_strategy: bool

    def __post_init__(self) -> None:
        if self.owner_confirmed is not True:
            raise RiskConstitutionError("Risk Constitution must be owner-confirmed")
        for name in ("block_data_conflicts", "require_approved_strategy"):
            if not isinstance(getattr(self, name), bool):
                raise RiskConstitutionError(f"{name} must be boolean")
        percentage_names = (
            "max_risk_per_trade_pct", "max_position_size_pct",
            "max_total_exposure_pct", "minimum_cash_pct",
            "max_sector_exposure_pct", "max_correlated_exposure_pct",
            "max_liquidity_participation_pct", "max_daily_loss_pct",
            "max_drawdown_pct",
        )
        for name in percentage_names:
            object.__setattr__(
                self,
                name,
                _number(getattr(self, name), name, minimum=0.0, maximum=100.0),
            )
        object.__setattr__(
            self,
            "max_leverage",
            _number(self.max_leverage, "max_leverage", minimum=1.0),
        )
        object.__setattr__(
            self,
            "correlation_threshold",
            _number(self.correlation_threshold, "correlation_threshold", minimum=0.0, maximum=1.0),
        )
        if isinstance(self.max_positions, bool) or not isinstance(self.max_positions, int) or self.max_positions <= 0:
            raise RiskConstitutionError("max_positions must be a positive integer")
        if isinstance(self.max_daily_loss_trades, bool) or not isinstance(self.max_daily_loss_trades, int) or self.max_daily_loss_trades <= 0:
            raise RiskConstitutionError("max_daily_loss_trades must be a positive integer")
        if (
            isinstance(self.max_data_age_seconds, bool)
            or not isinstance(self.max_data_age_seconds, int)
            or self.max_data_age_seconds <= 0
        ):
            raise RiskConstitutionError("max_data_age_seconds must be a positive integer")

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RiskConstitution":
        if not isinstance(value, Mapping):
            raise RiskConstitutionError("risk_constitution must be a mapping")
        required = set(cls.__dataclass_fields__)
        missing = sorted(required - set(value))
        if missing:
            raise RiskConstitutionError(
                "missing Risk Constitution fields: " + ", ".join(missing)
            )
        try:
            return cls(**{name: value[name] for name in required})
        except TypeError as exc:
            raise RiskConstitutionError(str(exc)) from exc


def _pct(amount: float, equity: float) -> float:
    # Equity is adapter-validated > 0, but a degenerate runtime state must never
    # leak inf/NaN into audit records (§99: non-finite values are not JSON and
    # would 500 any consumer of the checks).
    if equity == 0:
        return 0.0
    return amount / equity * 100.0


def _finite_actual(value):
    """Coerce a check's `actual` to a JSON-safe number (0.0 for non-finite)."""
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else 0.0
    except (TypeError, ValueError):
        return value


def evaluate_portfolio_constraints(
    snapshot: PortfolioSnapshot,
    proposal: OrderProposal,
    constitution: RiskConstitution,
) -> ConstraintDecision:
    """Evaluate every mandatory constraint without short-circuiting audit evidence."""

    if not isinstance(snapshot, PortfolioSnapshot):
        raise TypeError("snapshot must be a PortfolioSnapshot")
    if not isinstance(proposal, OrderProposal):
        raise TypeError("proposal must be an OrderProposal")
    if not isinstance(constitution, RiskConstitution):
        raise TypeError("constitution must be a RiskConstitution")

    checks: list[ConstraintCheck] = []

    def add(name: str, passed: bool, actual: Any, limit: Any, detail: str = "") -> None:
        checks.append(ConstraintCheck(name, bool(passed), _finite_actual(actual), _finite_actual(limit), detail))

    add("constitution_confirmed", constitution.owner_confirmed, True, True)
    add("kill_switch", not proposal.kill_switch_active, proposal.kill_switch_active, False)
    strategy_ok = (
        not constitution.require_approved_strategy
        or proposal.strategy_status in {"APPROVED", "CHAMPION"}
    )
    add(
        "strategy_evidence",
        strategy_ok,
        proposal.strategy_status,
        "APPROVED_OR_CHAMPION" if constitution.require_approved_strategy else "ANY",
    )
    add(
        "data_freshness",
        proposal.data_age_seconds <= constitution.max_data_age_seconds,
        proposal.data_age_seconds,
        constitution.max_data_age_seconds,
    )
    conflict_ok = not constitution.block_data_conflicts or not proposal.has_data_conflict
    add("data_conflict", conflict_ok, proposal.has_data_conflict, False)
    add(
        "max_leverage",
        proposal.leverage <= constitution.max_leverage,
        proposal.leverage,
        constitution.max_leverage,
    )

    existing_symbols = {position.symbol for position in snapshot.positions}
    projected_positions = len(snapshot.positions) + (0 if proposal.symbol in existing_symbols else 1)
    add(
        "max_positions",
        projected_positions <= constitution.max_positions,
        projected_positions,
        constitution.max_positions,
    )

    symbol_notional = sum(
        position.notional_usd for position in snapshot.positions
        if position.symbol == proposal.symbol
    ) + proposal.notional_usd
    symbol_pct = _pct(symbol_notional, snapshot.equity_usd)
    add(
        "max_position",
        symbol_pct <= constitution.max_position_size_pct,
        symbol_pct,
        constitution.max_position_size_pct,
    )

    total_notional = sum(position.notional_usd for position in snapshot.positions) + proposal.notional_usd
    total_pct = _pct(total_notional, snapshot.equity_usd)
    add(
        "max_total_exposure",
        total_pct <= constitution.max_total_exposure_pct,
        total_pct,
        constitution.max_total_exposure_pct,
    )

    remaining_cash = snapshot.cash_usd - proposal.margin_required_usd - proposal.fees_usd
    minimum_cash = snapshot.equity_usd * constitution.minimum_cash_pct / 100.0
    add("minimum_cash", remaining_cash >= minimum_cash, remaining_cash, minimum_cash)

    sector_notional = sum(
        position.notional_usd for position in snapshot.positions
        if position.sector == proposal.sector
    ) + proposal.notional_usd
    sector_pct = _pct(sector_notional, snapshot.equity_usd)
    add(
        "max_sector_exposure",
        sector_pct <= constitution.max_sector_exposure_pct,
        sector_pct,
        constitution.max_sector_exposure_pct,
    )

    missing_correlation = sorted(
        position.symbol for position in snapshot.positions
        if position.symbol != proposal.symbol and position.symbol not in snapshot.correlations
    )
    add("correlation_data", not missing_correlation, missing_correlation, "COMPLETE")
    correlated_notional = proposal.notional_usd
    for position in snapshot.positions:
        correlation = 1.0 if position.symbol == proposal.symbol else snapshot.correlations.get(position.symbol)
        if correlation is not None and abs(correlation) >= constitution.correlation_threshold:
            correlated_notional += position.notional_usd
    correlated_pct = _pct(correlated_notional, snapshot.equity_usd)
    add(
        "max_correlated_exposure",
        not missing_correlation and correlated_pct <= constitution.max_correlated_exposure_pct,
        correlated_pct,
        constitution.max_correlated_exposure_pct,
    )

    risk_pct = _pct(proposal.loss_at_stop_usd, snapshot.equity_usd)
    add(
        "risk_per_trade",
        risk_pct <= constitution.max_risk_per_trade_pct,
        risk_pct,
        constitution.max_risk_per_trade_pct,
    )

    adv_unavailable = proposal.average_daily_volume_usd <= 0
    participation_pct = (
        None  # unevaluable, never inf (non-finite breaks §99 JSON consumers)
        if adv_unavailable
        else _pct(proposal.notional_usd, proposal.average_daily_volume_usd)
    )
    add(
        "liquidity",
        # Conservative: unknown ADV cannot prove liquidity headroom -> reject.
        (False if adv_unavailable
         else participation_pct <= constitution.max_liquidity_participation_pct),
        participation_pct,
        constitution.max_liquidity_participation_pct,
        "ADV unavailable — liquidity headroom cannot be proven" if adv_unavailable else "",
    )

    daily_loss_pct = _pct(
        abs(min(0.0, snapshot.daily_realized_pnl_usd)), snapshot.equity_usd
    )
    add(
        "daily_loss",
        daily_loss_pct < constitution.max_daily_loss_pct,
        daily_loss_pct,
        constitution.max_daily_loss_pct,
    )
    drawdown_pct = _pct(
        snapshot.peak_equity_usd - snapshot.equity_usd,
        snapshot.peak_equity_usd,
    )
    add(
        "drawdown",
        drawdown_pct < constitution.max_drawdown_pct,
        drawdown_pct,
        constitution.max_drawdown_pct,
    )

    live_allowed = not proposal.is_live_autonomous or proposal.live_auto_owner_enabled
    add(
        "live_autonomy_permission",
        live_allowed,
        proposal.live_auto_owner_enabled,
        True if proposal.is_live_autonomous else "NOT_APPLICABLE",
    )

    reasons = tuple(check.name for check in checks if not check.passed)
    return ConstraintDecision(not reasons, tuple(checks), reasons)


__all__ = [
    "RiskConstitution",
    "RiskConstitutionError",
    "evaluate_portfolio_constraints",
    "resolve_confirmed_risk_constitution",
]
