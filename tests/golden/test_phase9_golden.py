"""GOLD-006/007/008/009/016 capital and protected-risk contracts."""

import pytest

import core.capital_manager as capital_mod
from core.autonomy import AutonomyViolationError, assert_not_weakening
from core.capital_manager import CapitalManager
from core.portfolio import (
    OrderProposal,
    PortfolioPosition,
    PortfolioSnapshot,
    PositionAction,
    RiskConstitution,
    evaluate_portfolio_constraints,
    open_trade_accounting,
    reassess_position,
    settle_trade_accounting,
)


pytestmark = pytest.mark.golden


def _constitution(**changes):
    values = {
        "owner_confirmed": True,
        "max_risk_per_trade_pct": 2.0,
        "max_position_size_pct": 25.0,
        "max_total_exposure_pct": 50.0,
        "max_leverage": 2.0,
        "minimum_cash_pct": 10.0,
        "max_sector_exposure_pct": 50.0,
        "correlation_threshold": 0.7,
        "max_correlated_exposure_pct": 50.0,
        "max_positions": 1,
        "max_liquidity_participation_pct": 5.0,
        "max_daily_loss_pct": 5.0,
        "max_daily_loss_trades": 2,
        "max_drawdown_pct": 10.0,
        "max_data_age_seconds": 60,
        "block_data_conflicts": True,
        "require_approved_strategy": True,
    }
    values.update(changes)
    return RiskConstitution(**values)


@pytest.mark.parametrize(
    ("direction", "exit_price"),
    [("LONG", 110.0), ("LONG", 95.0), ("SHORT", 90.0), ("SHORT", 105.0)],
)
def test_gold006_capital_conservation(direction, exit_price):
    opened = open_trade_accounting(
        direction=direction,
        starting_cash_usd=10_000.0,
        entry_price=100.0,
        notional_usd=1_000.0,
        leverage=2.0,
        entry_fee_rate=0.0025,
    )
    settled = settle_trade_accounting(opened, exit_price, exit_fee_rate=0.0025)

    assert opened.cash_after_open_usd == pytest.approx(
        opened.starting_cash_usd - opened.margin_usd - opened.entry_fee_usd
    )
    assert settled.ending_cash_usd == pytest.approx(
        opened.cash_after_open_usd
        + opened.margin_usd
        + settled.gross_pnl_usd
        - settled.exit_fee_usd
    )
    assert settled.ending_cash_usd == pytest.approx(
        opened.starting_cash_usd + settled.net_pnl_usd
    )


@pytest.fixture
def isolated_capital_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("MS_DISABLE_CAPITAL_THREADS", "1")
    monkeypatch.setenv("MS_DISABLE_ENGINE_AUTOSTART", "1")
    monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", tmp_path / "capital.json")
    monkeypatch.setattr(capital_mod, "get_sqlite_store", lambda: None)
    try:
        import core.trade_audit as trade_audit

        monkeypatch.setattr(trade_audit, "record_trade_event", lambda *args, **kwargs: None)
    except Exception:
        pass
    try:
        import core.feedback_loop as feedback_loop

        monkeypatch.setattr(feedback_loop, "process_closed_trade", lambda *args, **kwargs: None)
    except Exception:
        pass
    CapitalManager._instance = None
    CapitalManager._price_cache = {}
    CapitalManager._atr_cache = {}
    manager = CapitalManager()
    manager.set_balance(10_000.0, reason="GOLD-007", clear_positions=True)
    yield manager
    CapitalManager._instance = None
    CapitalManager._price_cache = {}
    CapitalManager._atr_cache = {}


@pytest.mark.parametrize(
    ("direction", "exit_price"),
    [("long", 110.0), ("short", 90.0)],
)
def test_gold007_exact_trade_accounting_matches_v7_capital_manager(
    isolated_capital_manager,
    direction,
    exit_price,
):
    manager = isolated_capital_manager
    opened = manager.open_manual_position(
        ticker="AAPL",
        direction=direction,
        leverage=2,
        usd_size=1_000.0,
        entry_price=100.0,
        max_size_pct=1.0,
        fees_bps=25.0,
        cost_model={"commission_bps_per_side": 25.0, "provenance": "golden"},
        reason="GOLD-007 exact accounting",
    )
    contract_open = open_trade_accounting(
        direction=direction.upper(),
        starting_cash_usd=10_000.0,
        entry_price=100.0,
        notional_usd=1_000.0,
        leverage=2.0,
        entry_fee_rate=0.0025,
    )

    assert manager.get_balance() == pytest.approx(contract_open.cash_after_open_usd)
    assert opened["margin_used"] == pytest.approx(contract_open.margin_usd)
    assert opened["commission_usd"] == pytest.approx(contract_open.entry_fee_usd)

    closed = manager.close_position(
        position_id=opened["position_id"],
        exit_price=exit_price,
        reason="GOLD-007 close",
        sync_signal=False,
    )
    contract_close = settle_trade_accounting(
        contract_open,
        exit_price=exit_price,
        exit_fee_rate=0.0025,
    )

    assert closed["gross_pnl"] == pytest.approx(contract_close.gross_pnl_usd)
    assert closed["net_pnl"] == pytest.approx(contract_close.net_pnl_usd)
    assert closed["exit_commission"] == pytest.approx(contract_close.exit_fee_usd)
    assert manager.get_balance() == pytest.approx(contract_close.ending_cash_usd)


def test_gold008_every_mandatory_risk_constraint_vetoes():
    snapshot = PortfolioSnapshot(
        equity_usd=9_000.0,
        cash_usd=500.0,
        positions=(PortfolioPosition("AAPL", "TECHNOLOGY", 4_000.0, 1.0, 80.0),),
        daily_realized_pnl_usd=-600.0,
        peak_equity_usd=11_000.0,
        correlations={"AAPL": 0.9},
    )
    proposal = OrderProposal(
        symbol="NVDA",
        sector="TECHNOLOGY",
        notional_usd=4_000.0,
        margin_required_usd=2_000.0,
        fees_usd=10.0,
        leverage=3.0,
        loss_at_stop_usd=300.0,
        average_daily_volume_usd=50_000.0,
        data_age_seconds=61.0,
        has_data_conflict=True,
        strategy_status="RESEARCH",
        is_live_autonomous=True,
        live_auto_owner_enabled=False,
        kill_switch_active=True,
    )
    decision = evaluate_portfolio_constraints(snapshot, proposal, _constitution())

    assert decision.allowed is False
    assert set(decision.reasons) == {
        "kill_switch",
        "strategy_evidence",
        "data_freshness",
        "data_conflict",
        "max_leverage",
        "max_positions",
        "max_position",
        "max_total_exposure",
        "minimum_cash",
        "max_sector_exposure",
        "max_correlated_exposure",
        "risk_per_trade",
        "liquidity",
        "daily_loss",
        "drawdown",
        "live_autonomy_permission",
    }
    assert len(decision.checks) == 18
    assert next(item for item in decision.checks if item.name == "constitution_confirmed").passed
    assert next(item for item in decision.checks if item.name == "correlation_data").passed


def test_gold009_kill_switch_blocks_new_risk_but_keeps_position_monitoring():
    snapshot = PortfolioSnapshot(equity_usd=10_000.0, cash_usd=10_000.0)
    proposal = OrderProposal(
        symbol="AAPL",
        sector="TECHNOLOGY",
        notional_usd=500.0,
        margin_required_usd=500.0,
        fees_usd=1.0,
        leverage=1.0,
        loss_at_stop_usd=10.0,
        average_daily_volume_usd=1_000_000.0,
        data_age_seconds=1.0,
        has_data_conflict=False,
        strategy_status="APPROVED",
        kill_switch_active=True,
    )
    decision = evaluate_portfolio_constraints(
        snapshot,
        proposal,
        _constitution(max_positions=5, max_total_exposure_pct=80.0),
    )
    assessment = reassess_position(
        {
            "position_id": "pos-1",
            "direction": "LONG",
            "stop_loss_price": 95.0,
            "status": "OPEN",
        },
        {"kill_switch_active": True, "current_price": 100.0},
        assessed_at="2026-08-13T12:00:00+00:00",
    )

    assert decision.allowed is False
    assert decision.reasons == ("kill_switch",)
    assert assessment.action is PositionAction.HOLD
    assert "kill_switch_monitoring_active" in assessment.reasons


def test_gold016_protected_policy_modification_is_rejected():
    with pytest.raises(AutonomyViolationError):
        assert_not_weakening("risk_constitution", "lower")
    assert assert_not_weakening(
        "risk_constitution",
        "lower",
        owner_authorized=True,
    ) is None
