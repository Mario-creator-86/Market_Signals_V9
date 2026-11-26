"""Shared validation and arithmetic for operator-configurable risk limits."""

from __future__ import annotations

import math
from numbers import Real
from typing import Any, Mapping, Tuple


MIN_DAILY_LOSS_PCT = 0.5
MAX_DAILY_LOSS_PCT = 10.0
DEFAULT_DAILY_LOSS_PCT = 1.0

MIN_RISK_PER_TRADE_PCT = 0.05
MAX_RISK_PER_TRADE_PCT = 5.0
DEFAULT_RISK_PER_TRADE_PCT = 0.5


class RiskLimitValidationError(ValueError):
    """Raised when an operator-provided risk limit is unsafe or malformed."""


def _strict_finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise RiskLimitValidationError(f"{label} must be a JSON number")
    number = float(value)
    if not math.isfinite(number):
        raise RiskLimitValidationError(f"{label} must be finite")
    return number


def validate_risk_limits(
    max_daily_loss_pct: Any,
    max_risk_per_trade_pct: Any,
) -> Tuple[float, float]:
    """Validate and normalize the two limits exposed by the Risk Manager card."""
    daily = _strict_finite_number(max_daily_loss_pct, "Daily loss")
    per_trade = _strict_finite_number(max_risk_per_trade_pct, "Risk per trade")

    if not MIN_DAILY_LOSS_PCT <= daily <= MAX_DAILY_LOSS_PCT:
        raise RiskLimitValidationError(
            f"Daily loss must be between {MIN_DAILY_LOSS_PCT:g}% and {MAX_DAILY_LOSS_PCT:g}%"
        )
    if not MIN_RISK_PER_TRADE_PCT <= per_trade <= MAX_RISK_PER_TRADE_PCT:
        raise RiskLimitValidationError(
            "Risk per trade must be between "
            f"{MIN_RISK_PER_TRADE_PCT:g}% and {MAX_RISK_PER_TRADE_PCT:g}%"
        )
    if per_trade > daily:
        raise RiskLimitValidationError("Risk per trade cannot exceed the daily loss limit")

    return round(daily, 4), round(per_trade, 4)


def configured_daily_loss_pct(config: Mapping[str, Any] | None) -> float:
    return _bounded_config_number(
        (config or {}).get("max_daily_loss_pct"),
        minimum=MIN_DAILY_LOSS_PCT,
        maximum=MAX_DAILY_LOSS_PCT,
        fallback=DEFAULT_DAILY_LOSS_PCT,
    )


def configured_risk_per_trade_pct(config: Mapping[str, Any] | None) -> float:
    return _bounded_config_number(
        (config or {}).get("max_risk_per_trade_pct"),
        minimum=MIN_RISK_PER_TRADE_PCT,
        maximum=MAX_RISK_PER_TRADE_PCT,
        fallback=DEFAULT_RISK_PER_TRADE_PCT,
    )


def _bounded_config_number(value: Any, *, minimum: float, maximum: float, fallback: float) -> float:
    """Fail safely for legacy/manual config edits while the API remains strict."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return max(minimum, min(maximum, number))


def calculate_trade_risk_pct(
    *,
    notional_usd: Any,
    stop_loss_fraction: Any,
    equity: Any,
) -> float:
    """Return loss-at-stop as a percentage of total Capital equity."""
    try:
        notional = float(notional_usd)
        stop_fraction = float(stop_loss_fraction)
        account_equity = float(equity)
    except (TypeError, ValueError):
        return 0.0
    if not all(math.isfinite(value) for value in (notional, stop_fraction, account_equity)):
        return 0.0
    if notional <= 0 or stop_fraction <= 0 or account_equity <= 0:
        return 0.0
    return notional * stop_fraction / account_equity * 100.0
