import pytest

from core.portfolio import CapitalMethod, SizingRequest
from core.portfolio.accounting import (
    CapitalAccountingError,
    open_trade_accounting,
    settle_trade_accounting,
)
from core.portfolio.sizing import CapitalSizingError, calculate_capital_size


@pytest.mark.parametrize(
    ("sizing_request", "expected", "formula"),
    [
        (
            SizingRequest(
                CapitalMethod.FIXED_AMOUNT,
                equity_usd=10_000.0,
                fixed_amount_usd=500.0,
            ),
            500.0,
            "fixed_amount_usd",
        ),
        (
            SizingRequest(
                CapitalMethod.FIXED_PERCENTAGE,
                equity_usd=10_000.0,
                percentage_of_equity=10.0,
            ),
            1_000.0,
            "equity_usd * percentage_of_equity / 100",
        ),
        (
            SizingRequest(
                CapitalMethod.FIXED_RISK,
                equity_usd=10_000.0,
                risk_per_trade_pct=1.0,
                stop_loss_fraction=0.02,
            ),
            5_000.0,
            "equity_usd * risk_per_trade_pct / 100 / stop_loss_fraction",
        ),
        (
            SizingRequest(
                CapitalMethod.INVERSE_VOLATILITY,
                equity_usd=10_000.0,
                risk_budget_pct=1.0,
                asset_volatility=0.20,
            ),
            500.0,
            "equity_usd * risk_budget_pct / 100 / asset_volatility",
        ),
        (
            SizingRequest(
                CapitalMethod.VOLATILITY_TARGETING,
                equity_usd=10_000.0,
                allocation_pct=50.0,
                target_volatility=0.10,
                asset_volatility=0.20,
            ),
            2_500.0,
            "equity_usd * allocation_pct / 100 * target_volatility / asset_volatility",
        ),
    ],
)
def test_section_65_sizing_formulas_are_pinned(sizing_request, expected, formula):
    result = calculate_capital_size(sizing_request)

    assert result.notional_usd == pytest.approx(expected)
    assert result.formula == formula
    assert result.is_authoritative is True


@pytest.mark.parametrize(
    "sizing_request",
    [
        SizingRequest(CapitalMethod.FIXED_AMOUNT, 10_000.0),
        SizingRequest(CapitalMethod.FIXED_PERCENTAGE, 10_000.0, percentage_of_equity=101.0),
        SizingRequest(
            CapitalMethod.FIXED_RISK,
            10_000.0,
            risk_per_trade_pct=1.0,
            stop_loss_fraction=0.0,
        ),
        SizingRequest(
            CapitalMethod.INVERSE_VOLATILITY,
            10_000.0,
            risk_budget_pct=1.0,
            asset_volatility=0.0,
        ),
        SizingRequest(
            CapitalMethod.VOLATILITY_TARGETING,
            10_000.0,
            allocation_pct=50.0,
            target_volatility=0.1,
            asset_volatility=0.0,
        ),
    ],
)
def test_sizing_missing_or_unsafe_inputs_fail_closed(sizing_request):
    with pytest.raises(CapitalSizingError):
        calculate_capital_size(sizing_request)


def test_long_trade_accounting_conserves_cash_and_charges_both_fees():
    opened = open_trade_accounting(
        direction="LONG",
        starting_cash_usd=10_000.0,
        entry_price=100.0,
        notional_usd=1_000.0,
        leverage=2.0,
        entry_fee_rate=0.001,
    )
    settled = settle_trade_accounting(opened, exit_price=110.0, exit_fee_rate=0.001)

    assert opened.margin_usd == pytest.approx(500.0)
    assert opened.entry_fee_usd == pytest.approx(1.0)
    assert opened.cash_after_open_usd == pytest.approx(9_499.0)
    assert settled.gross_pnl_usd == pytest.approx(100.0)
    assert settled.exit_fee_usd == pytest.approx(1.1)
    assert settled.net_pnl_usd == pytest.approx(97.9)
    assert settled.ending_cash_usd == pytest.approx(10_097.9)


def test_short_trade_accounting_uses_inverse_price_move():
    opened = open_trade_accounting(
        direction="SHORT",
        starting_cash_usd=5_000.0,
        entry_price=50.0,
        notional_usd=500.0,
        leverage=1.0,
        entry_fee_rate=0.0,
    )
    settled = settle_trade_accounting(opened, exit_price=45.0, exit_fee_rate=0.0)

    assert settled.gross_pnl_usd == pytest.approx(50.0)
    assert settled.ending_cash_usd == pytest.approx(5_050.0)


def test_open_rejects_negative_cash_or_impossible_reserve():
    with pytest.raises(CapitalAccountingError, match="insufficient cash"):
        open_trade_accounting(
            direction="LONG",
            starting_cash_usd=100.0,
            entry_price=10.0,
            notional_usd=200.0,
            leverage=1.0,
            entry_fee_rate=0.01,
        )


def test_accounting_rejects_non_finite_or_negative_fee_rate():
    with pytest.raises(CapitalAccountingError):
        open_trade_accounting("LONG", 1_000.0, 10.0, 100.0, 1.0, float("nan"))
    opened = open_trade_accounting("LONG", 1_000.0, 10.0, 100.0, 1.0, 0.0)
    with pytest.raises(CapitalAccountingError):
        settle_trade_accounting(opened, 11.0, -0.1)
