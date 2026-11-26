"""Deterministic section 65 capital sizing methods."""

from __future__ import annotations

from .types import CapitalMethod, SizingRequest, SizingResult


class CapitalSizingError(ValueError):
    """Raised when a capital sizing request is incomplete or unsafe."""


def _positive(value: float | None, name: str) -> float:
    if value is None or value <= 0:
        raise CapitalSizingError(f"{name} must be > 0")
    return float(value)


def _percentage(value: float | None, name: str) -> float:
    result = _positive(value, name)
    if result > 100.0:
        raise CapitalSizingError(f"{name} must be <= 100")
    return result


def calculate_capital_size(request: SizingRequest) -> SizingResult:
    """Calculate requested notional; portfolio constraints approve it separately."""

    if not isinstance(request, SizingRequest):
        raise TypeError("request must be a SizingRequest")
    if request.equity_usd <= 0:
        raise CapitalSizingError("equity_usd must be > 0")

    inputs: dict[str, float] = {"equity_usd": request.equity_usd}
    if request.method is CapitalMethod.FIXED_AMOUNT:
        fixed = _positive(request.fixed_amount_usd, "fixed_amount_usd")
        notional = fixed
        formula = "fixed_amount_usd"
        inputs["fixed_amount_usd"] = fixed
    elif request.method is CapitalMethod.FIXED_PERCENTAGE:
        percentage = _percentage(request.percentage_of_equity, "percentage_of_equity")
        notional = request.equity_usd * percentage / 100.0
        formula = "equity_usd * percentage_of_equity / 100"
        inputs["percentage_of_equity"] = percentage
    elif request.method is CapitalMethod.FIXED_RISK:
        risk_pct = _percentage(request.risk_per_trade_pct, "risk_per_trade_pct")
        stop_fraction = _positive(request.stop_loss_fraction, "stop_loss_fraction")
        if stop_fraction > 1.0:
            raise CapitalSizingError("stop_loss_fraction must be <= 1")
        notional = request.equity_usd * risk_pct / 100.0 / stop_fraction
        formula = "equity_usd * risk_per_trade_pct / 100 / stop_loss_fraction"
        inputs.update({"risk_per_trade_pct": risk_pct, "stop_loss_fraction": stop_fraction})
    elif request.method is CapitalMethod.INVERSE_VOLATILITY:
        risk_budget = _percentage(request.risk_budget_pct, "risk_budget_pct")
        volatility = _positive(request.asset_volatility, "asset_volatility")
        notional = request.equity_usd * risk_budget / 100.0 / volatility
        formula = "equity_usd * risk_budget_pct / 100 / asset_volatility"
        inputs.update({"risk_budget_pct": risk_budget, "asset_volatility": volatility})
    elif request.method is CapitalMethod.VOLATILITY_TARGETING:
        allocation = _percentage(request.allocation_pct, "allocation_pct")
        target = _positive(request.target_volatility, "target_volatility")
        volatility = _positive(request.asset_volatility, "asset_volatility")
        notional = request.equity_usd * allocation / 100.0 * target / volatility
        formula = "equity_usd * allocation_pct / 100 * target_volatility / asset_volatility"
        inputs.update(
            {
                "allocation_pct": allocation,
                "target_volatility": target,
                "asset_volatility": volatility,
            }
        )
    else:  # Enum exhaustiveness guard.
        raise CapitalSizingError(f"unsupported capital method: {request.method}")

    return SizingResult(
        method=request.method,
        notional_usd=notional,
        formula=formula,
        inputs=inputs,
    )


__all__ = ["CapitalSizingError", "calculate_capital_size"]
