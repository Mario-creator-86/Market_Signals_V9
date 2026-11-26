"""Exact paper-trade cash, margin, fee, and P/L accounting."""

from __future__ import annotations

import math
from numbers import Real

from .types import OpenTradeAccounting, TradeSettlement


class CapitalAccountingError(ValueError):
    """Raised when a trade cannot satisfy exact conservation accounting."""


def _number(value, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise CapitalAccountingError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise CapitalAccountingError(f"{name} must be a finite number")
    if minimum is not None and result < minimum:
        raise CapitalAccountingError(f"{name} must be >= {minimum}")
    return result


def open_trade_accounting(
    direction: str,
    starting_cash_usd: float,
    entry_price: float,
    notional_usd: float,
    leverage: float,
    entry_fee_rate: float,
) -> OpenTradeAccounting:
    cash = _number(starting_cash_usd, "starting_cash_usd", minimum=0.0)
    price = _number(entry_price, "entry_price", minimum=0.0)
    notional = _number(notional_usd, "notional_usd", minimum=0.0)
    leverage_value = _number(leverage, "leverage", minimum=1.0)
    fee_rate = _number(entry_fee_rate, "entry_fee_rate", minimum=0.0)
    if price <= 0 or notional <= 0:
        raise CapitalAccountingError("entry_price and notional_usd must be > 0")
    margin = notional / leverage_value
    entry_fee = notional * fee_rate
    cash_after_open = cash - margin - entry_fee
    if cash_after_open < 0:
        raise CapitalAccountingError("insufficient cash for margin and entry fee")
    return OpenTradeAccounting(
        direction=direction,
        starting_cash_usd=cash,
        entry_price=price,
        quantity=notional / price,
        notional_usd=notional,
        leverage=leverage_value,
        margin_usd=margin,
        entry_fee_usd=entry_fee,
        cash_after_open_usd=cash_after_open,
    )


def settle_trade_accounting(
    opened: OpenTradeAccounting,
    exit_price: float,
    exit_fee_rate: float,
) -> TradeSettlement:
    if not isinstance(opened, OpenTradeAccounting):
        raise TypeError("opened must be OpenTradeAccounting")
    price = _number(exit_price, "exit_price", minimum=0.0)
    fee_rate = _number(exit_fee_rate, "exit_fee_rate", minimum=0.0)
    if price <= 0:
        raise CapitalAccountingError("exit_price must be > 0")
    exit_notional = price * opened.quantity
    exit_fee = exit_notional * fee_rate
    price_move = (
        price - opened.entry_price
        if opened.direction == "LONG"
        else opened.entry_price - price
    )
    gross_pnl = price_move * opened.quantity
    net_pnl = gross_pnl - opened.entry_fee_usd - exit_fee
    ending_cash = opened.starting_cash_usd + net_pnl
    if ending_cash < 0:
        raise CapitalAccountingError("settlement would produce negative cash")
    return TradeSettlement(
        opened=opened,
        exit_price=price,
        exit_fee_usd=exit_fee,
        gross_pnl_usd=gross_pnl,
        net_pnl_usd=net_pnl,
        ending_cash_usd=ending_cash,
    )


__all__ = [
    "CapitalAccountingError",
    "open_trade_accounting",
    "settle_trade_accounting",
]
