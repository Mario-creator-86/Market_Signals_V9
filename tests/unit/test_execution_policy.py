from core.execution.policy import evaluate_paper_entry_readiness


def _config(**autonomy):
    return {
        "autonomy": {
            "mode": "PAPER_AUTO",
            "kill_switch_active": False,
            **autonomy,
        },
        "paper_execution": {"reduce_fraction_pct": 25.0},
        "agents": {"execution": {"enabled": True, "mode": "paper"}},
        "risk_constitution": {
            "owner_confirmed": True,
            "max_risk_per_trade_pct": 0.4,
            "max_position_size_pct": 2.0,
            "max_total_exposure_pct": 20.0,
            "max_leverage": 2,
            "minimum_cash_pct": 30.0,
            "max_positions": 6,
            "max_sector_exposure_pct": 10.0,
            "correlation_threshold": 0.75,
            "max_correlated_exposure_pct": 15.0,
            "max_liquidity_participation_pct": 0.5,
            "max_daily_loss_pct": 2.0,
            "max_daily_loss_trades": 3,
            "max_drawdown_pct": 8.0,
            "max_data_age_seconds": 120,
            "block_data_conflicts": True,
            "require_approved_strategy": True,
        },
    }


def test_paper_auto_ready_only_with_owner_values_and_healthy_reconciliation():
    result = evaluate_paper_entry_readiness(_config(), reconciliation_status="HEALTHY")
    assert result.ready is True
    assert result.reasons == ()


def test_kill_switch_blocks_new_risk_without_disabling_monitoring_contract():
    result = evaluate_paper_entry_readiness(
        _config(kill_switch_active=True), reconciliation_status="HEALTHY"
    )
    assert result.ready is False
    assert "STOP AUTONOMOUS TRADING is active" in result.reasons


def test_unselected_reduce_value_and_inactive_constitution_fail_closed():
    config = _config()
    config["paper_execution"]["reduce_fraction_pct"] = None
    config["risk_constitution"]["owner_confirmed"] = False
    result = evaluate_paper_entry_readiness(config, reconciliation_status="UNVERIFIED")
    assert result.ready is False
    assert any("Risk Constitution" in reason for reason in result.reasons)
    assert any("reduction percentage" in reason for reason in result.reasons)
    assert any("reconciliation" in reason for reason in result.reasons)
