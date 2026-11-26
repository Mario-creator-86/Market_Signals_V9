import pytest

from core.portfolio import OrderProposal, PortfolioPosition, PortfolioSnapshot
from core.portfolio.constraints import (
    RiskConstitution,
    RiskConstitutionError,
    evaluate_portfolio_constraints,
    resolve_confirmed_risk_constitution,
)


def _constitution(**changes):
    values = {
        "owner_confirmed": True,
        "max_risk_per_trade_pct": 2.0,
        "max_position_size_pct": 25.0,
        "max_total_exposure_pct": 80.0,
        "max_leverage": 2.0,
        "minimum_cash_pct": 10.0,
        "max_sector_exposure_pct": 50.0,
        "correlation_threshold": 0.70,
        "max_correlated_exposure_pct": 50.0,
        "max_positions": 5,
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


def _snapshot(**changes):
    values = {
        "equity_usd": 10_000.0,
        "cash_usd": 8_000.0,
        "positions": (
            PortfolioPosition("AAPL", "TECHNOLOGY", 1_000.0, 1.0, 20.0),
        ),
        "daily_realized_pnl_usd": 0.0,
        "peak_equity_usd": 10_000.0,
        "correlations": {"AAPL": 0.2},
    }
    values.update(changes)
    return PortfolioSnapshot(**values)


def _proposal(**changes):
    values = {
        "symbol": "NVDA",
        "sector": "TECHNOLOGY",
        "notional_usd": 1_000.0,
        "margin_required_usd": 500.0,
        "fees_usd": 1.0,
        "leverage": 2.0,
        "loss_at_stop_usd": 20.0,
        "average_daily_volume_usd": 100_000.0,
        "data_age_seconds": 5.0,
        "has_data_conflict": False,
        "strategy_status": "APPROVED",
        "is_live_autonomous": False,
        "live_auto_owner_enabled": False,
        "kill_switch_active": False,
    }
    values.update(changes)
    return OrderProposal(**values)


def test_constitution_requires_every_explicit_owner_confirmed_value():
    mapping = _constitution().to_dict()
    assert RiskConstitution.from_mapping(mapping) == _constitution()
    assert len(_constitution().fingerprint) == 64

    with pytest.raises(RiskConstitutionError, match="owner-confirmed"):
        RiskConstitution.from_mapping({**mapping, "owner_confirmed": False})
    mapping.pop("max_leverage")
    with pytest.raises(RiskConstitutionError, match="max_leverage"):
        RiskConstitution.from_mapping(mapping)


def test_config_resolver_preserves_legacy_path_until_owner_confirmation():
    template = {name: None for name in RiskConstitution.__dataclass_fields__}
    template["owner_confirmed"] = False

    assert resolve_confirmed_risk_constitution({}) is None
    assert resolve_confirmed_risk_constitution({"risk_constitution": template}) is None
    with pytest.raises(RiskConstitutionError, match="boolean"):
        resolve_confirmed_risk_constitution(
            {"risk_constitution": {**template, "owner_confirmed": "true"}}
        )


def test_config_resolver_validates_every_value_after_explicit_confirmation():
    mapping = _constitution().to_dict()

    assert resolve_confirmed_risk_constitution(
        {"risk_constitution": mapping}
    ) == _constitution()
    with pytest.raises(RiskConstitutionError, match="finite number"):
        resolve_confirmed_risk_constitution(
            {"risk_constitution": {**mapping, "max_leverage": None}}
        )


def test_valid_proposal_passes_every_mandatory_constraint():
    decision = evaluate_portfolio_constraints(_snapshot(), _proposal(), _constitution())

    assert decision.allowed is True
    assert len(decision.checks) == 18
    assert all(check.passed for check in decision.checks)
    assert decision.reasons == ()


@pytest.mark.parametrize(
    ("snapshot", "proposal", "constitution", "reason"),
    [
        (_snapshot(), _proposal(kill_switch_active=True), _constitution(), "kill_switch"),
        (_snapshot(), _proposal(strategy_status="RESEARCH"), _constitution(), "strategy_evidence"),
        (_snapshot(), _proposal(data_age_seconds=61.0), _constitution(), "data_freshness"),
        (_snapshot(), _proposal(has_data_conflict=True), _constitution(), "data_conflict"),
        (_snapshot(), _proposal(leverage=3.0), _constitution(), "max_leverage"),
        (_snapshot(), _proposal(), _constitution(max_positions=1), "max_positions"),
        (_snapshot(), _proposal(), _constitution(max_position_size_pct=5.0), "max_position"),
        (_snapshot(), _proposal(), _constitution(max_total_exposure_pct=15.0), "max_total_exposure"),
        (_snapshot(), _proposal(), _constitution(minimum_cash_pct=80.0), "minimum_cash"),
        (_snapshot(), _proposal(), _constitution(max_sector_exposure_pct=15.0), "max_sector_exposure"),
        (_snapshot(correlations={}), _proposal(), _constitution(), "correlation_data"),
        (
            _snapshot(correlations={"AAPL": 0.9}),
            _proposal(),
            _constitution(max_correlated_exposure_pct=15.0),
            "max_correlated_exposure",
        ),
        (_snapshot(), _proposal(loss_at_stop_usd=300.0), _constitution(), "risk_per_trade"),
        (
            _snapshot(),
            _proposal(average_daily_volume_usd=10_000.0),
            _constitution(),
            "liquidity",
        ),
        (
            _snapshot(daily_realized_pnl_usd=-600.0),
            _proposal(),
            _constitution(),
            "daily_loss",
        ),
        (
            _snapshot(equity_usd=9_000.0, peak_equity_usd=11_000.0),
            _proposal(),
            _constitution(),
            "drawdown",
        ),
        (
            _snapshot(),
            _proposal(is_live_autonomous=True, live_auto_owner_enabled=False),
            _constitution(),
            "live_autonomy_permission",
        ),
    ],
)
def test_each_mandatory_constraint_vetoes_independently(
    snapshot,
    proposal,
    constitution,
    reason,
):
    decision = evaluate_portfolio_constraints(snapshot, proposal, constitution)

    assert decision.allowed is False
    assert reason in decision.reasons
    assert any(check.name == reason and not check.passed for check in decision.checks)


def test_same_symbol_add_does_not_increase_position_count_but_updates_symbol_cap():
    proposal = _proposal(symbol="AAPL", notional_usd=1_000.0)
    decision = evaluate_portfolio_constraints(
        _snapshot(), proposal, _constitution(max_positions=1, max_position_size_pct=25.0)
    )

    assert next(check for check in decision.checks if check.name == "max_positions").passed
    assert next(check for check in decision.checks if check.name == "max_position").actual == 20.0


def test_optional_policies_can_allow_non_approved_strategy_and_data_conflict():
    decision = evaluate_portfolio_constraints(
        _snapshot(),
        _proposal(strategy_status="RESEARCH", has_data_conflict=True),
        _constitution(require_approved_strategy=False, block_data_conflicts=False),
    )

    assert decision.allowed is True
