from dataclasses import FrozenInstanceError, fields

import pytest

from core.portfolio import (
    CapitalMethod,
    ConstraintCheck,
    ConstraintDecision,
    OpenTradeAccounting,
    OrderProposal,
    PortfolioPosition,
    PortfolioSnapshot,
    PositionAction,
    PositionAssessment,
    PositionEvidence,
    SizingRequest,
    SizingResult,
    TradeSettlement,
)


def test_capital_method_set_matches_section_65():
    assert {method.value for method in CapitalMethod} == {
        "FIXED_AMOUNT",
        "FIXED_PERCENTAGE",
        "FIXED_RISK",
        "INVERSE_VOLATILITY",
        "VOLATILITY_TARGETING",
    }


def test_sizing_contracts_are_finite_frozen_and_authoritative():
    request = SizingRequest(CapitalMethod.FIXED_AMOUNT, equity_usd=10_000.0)
    result = SizingResult(
        method=CapitalMethod.FIXED_AMOUNT,
        notional_usd=500.0,
        formula="fixed_amount_usd",
        inputs={"fixed_amount_usd": 500.0},
    )

    assert result.is_authoritative is True
    with pytest.raises(FrozenInstanceError):
        request.equity_usd = 20_000.0
    with pytest.raises(TypeError):
        result.inputs["fixed_amount_usd"] = 1.0
    with pytest.raises(ValueError, match="finite"):
        SizingRequest(CapitalMethod.FIXED_AMOUNT, equity_usd=float("nan"))


def test_portfolio_position_and_snapshot_validate_identity_and_capital():
    position = PortfolioPosition(
        symbol="aapl",
        sector="technology",
        notional_usd=1_000.0,
        leverage=1.0,
        loss_at_stop_usd=20.0,
    )
    snapshot = PortfolioSnapshot(
        equity_usd=10_000.0,
        cash_usd=8_990.0,
        positions=(position,),
        daily_realized_pnl_usd=-25.0,
        peak_equity_usd=10_100.0,
        correlations={"AAPL": 0.8},
    )

    assert position.symbol == "AAPL"
    assert position.sector == "TECHNOLOGY"
    assert snapshot.positions == (position,)
    with pytest.raises(ValueError):
        PortfolioSnapshot(equity_usd=0.0, cash_usd=0.0)
    with pytest.raises(ValueError):
        PortfolioPosition("AAPL", "TECH", -1.0, 1.0, 1.0)


def test_order_proposal_carries_every_constraint_input():
    proposal = OrderProposal(
        symbol="nvda",
        sector="technology",
        notional_usd=2_000.0,
        margin_required_usd=1_000.0,
        fees_usd=2.0,
        leverage=2.0,
        loss_at_stop_usd=50.0,
        average_daily_volume_usd=1_000_000.0,
        data_age_seconds=5.0,
        has_data_conflict=False,
        strategy_status="APPROVED",
        is_live_autonomous=False,
        live_auto_owner_enabled=False,
        kill_switch_active=False,
    )

    assert proposal.symbol == "NVDA"
    assert proposal.strategy_status == "APPROVED"
    with pytest.raises(ValueError, match="margin"):
        OrderProposal(
            symbol="NVDA", sector="TECH", notional_usd=100.0,
            margin_required_usd=101.0, fees_usd=0.0, leverage=1.0,
            loss_at_stop_usd=1.0, average_daily_volume_usd=1_000.0,
            data_age_seconds=0.0, has_data_conflict=False,
            strategy_status="APPROVED",
        )


def test_constraint_decision_must_match_all_checks():
    passed = ConstraintCheck("max_leverage", True, actual=1.0, limit=2.0)
    failed = ConstraintCheck("minimum_cash", False, actual=50.0, limit=100.0)

    assert ConstraintDecision(True, (passed,), ()).allowed is True
    rejected = ConstraintDecision(False, (passed, failed), ("minimum_cash",))
    assert rejected.allowed is False
    with pytest.raises(ValueError):
        ConstraintDecision(True, (failed,), ())


def test_accounting_types_pin_the_conservation_identity():
    opened = OpenTradeAccounting(
        direction="LONG",
        starting_cash_usd=10_000.0,
        entry_price=100.0,
        quantity=10.0,
        notional_usd=1_000.0,
        leverage=2.0,
        margin_usd=500.0,
        entry_fee_usd=1.0,
        cash_after_open_usd=9_499.0,
    )
    settled = TradeSettlement(
        opened=opened,
        exit_price=110.0,
        exit_fee_usd=1.1,
        gross_pnl_usd=100.0,
        net_pnl_usd=97.9,
        ending_cash_usd=10_097.9,
    )

    assert settled.ending_cash_usd == pytest.approx(
        opened.starting_cash_usd + settled.net_pnl_usd
    )


def test_position_evidence_contains_every_section_67_field():
    expected = {
        "entry_thesis", "supporting_evidence", "opposing_evidence", "strategy",
        "signal", "ai_model", "invalidation_rules", "risk_state",
    }
    evidence = PositionEvidence(
        entry_thesis="confirmed breakout",
        supporting_evidence=("volume",),
        opposing_evidence=("event risk",),
        strategy="S2_BREAKOUT_RETEST",
        signal={"signal_id": "sig-1"},
        ai_model="model-1",
        invalidation_rules=("close below 95",),
        risk_state={"status": "NORMAL"},
    )

    assert {item.name for item in fields(PositionEvidence)} == expected
    assert evidence.signal["signal_id"] == "sig-1"
    with pytest.raises(TypeError):
        evidence.risk_state["status"] = "CHANGED"


def test_position_assessment_is_deterministic_action_contract():
    assessment = PositionAssessment(
        action=PositionAction.REDUCE,
        reasons=("sector_exposure",),
        triggered_rules=("max_sector_exposure",),
        assessed_at="2026-08-13T10:00:00+00:00",
    )

    assert assessment.action is PositionAction.REDUCE
    assert assessment.assessed_at == "2026-08-13T10:00:00+00:00"
