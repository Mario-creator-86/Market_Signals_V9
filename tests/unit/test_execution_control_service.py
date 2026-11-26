import copy

from api.execution_service import (
    get_execution_control,
    set_kill_switch,
)


def _constitution():
    return {
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
        "max_daily_loss_trades": 2,
        "max_drawdown_pct": 8.0,
        "max_data_age_seconds": 120,
        "block_data_conflicts": True,
        "require_approved_strategy": True,
    }


class _Store:
    def get_reconciliation(self):
        return {"status": "HEALTHY", "conflicts": [], "checked_at": "now"}


class _Broker:
    store = _Store()


class _Config:
    def __init__(self, *, with_constitution=True):
        self.config = {
            "autonomy": {"mode": "ANALYZE", "kill_switch_active": False},
            "paper_execution": {"reduce_fraction_pct": None},
            "agents": {"execution": {"enabled": True, "mode": "paper"}},
        }
        if with_constitution:
            self.config["risk_constitution"] = _constitution()
        self.calls = []

    def update_values(self, updates):
        values = list(updates)
        self.calls.append(copy.deepcopy(values))
        for path, value in values:
            target = self.config
            for part in path[:-1]:
                target = target.setdefault(part, {})
            target[path[-1]] = value

    def update_value(self, path, value):
        self.update_values([(path, value)])


def test_execution_control_is_read_only_status_owned_by_risk_manager(monkeypatch):
    config = _Config()
    config.config["autonomy"]["mode"] = "PAPER_AUTO"
    config.config["paper_execution"]["reduce_fraction_pct"] = 25.0
    monkeypatch.setattr("api.execution_service.get_paper_broker", lambda **kwargs: _Broker())
    control = get_execution_control(config)
    assert control["policy_owner"] == "RISK_MANAGER"
    assert control["write_endpoint"] == "/api/agents/risk/constitution"
    assert control["readiness"]["ready"] is True
    assert config.calls == []


def test_execution_status_reflects_risk_manager_pre_earnings_thresholds(monkeypatch):
    config = _Config()
    config.config["agents"]["execution"].update({
        "pre_earnings_min_gap_risk": 40,
        "pre_earnings_min_abs_bias": 18,
        "pre_earnings_min_coverage_score": 45,
        "pre_earnings_min_score": 76.5,
        "pre_earnings_require_canonical_decision": False,
    })
    monkeypatch.setattr("api.execution_service.get_paper_broker", lambda **kwargs: _Broker())
    control = get_execution_control(config)
    assert control["pre_earnings_thresholds"] == {
        "gap_risk": 40.0,
        "abs_bias": 18.0,
        "coverage_score": 45.0,
        "candidate_score": 76.5,
    }
    assert control["pre_earnings_policy"] == {
        "require_canonical_ai_open": False,
    }
    assert config.calls == []


def test_execution_control_exposes_autonomous_allocation_and_watchlist_source(monkeypatch):
    config = _Config()
    config.config["agents"]["execution"].update({
        "autonomous_sources": {"scanner": True, "watchlist": True, "pre_earnings": True},
        "max_autonomous_candidates_per_cycle": 0,
        "max_autonomous_trades_per_cycle": 1,
        "autonomous_scanner_max_symbols_per_bucket": 50,
        "autonomous_watchlist_max_symbols": 100,
        "autonomous_allocation": {
            "enabled": True,
            "buckets_pct": {"stocks": 33.0, "crypto": 33.0, "pre_earnings": 33.0},
            "fallback_when_empty": True,
        },
    })
    monkeypatch.setattr("api.execution_service.get_paper_broker", lambda **kwargs: _Broker())

    control = get_execution_control(config)

    assert control["autonomous_sources"]["watchlist"] is True
    assert control["autonomous_allocation"]["enabled"] is True
    assert control["autonomous_allocation"]["buckets_pct"] == {
        "stocks": 33.0,
        "crypto": 33.0,
        "pre_earnings": 33.0,
    }
    assert control["autonomous_cycle_limits"] == {
        "candidate_reviews_per_cycle": 0,
        "trade_submissions_per_cycle": 1,
        "scanner_symbols_per_bucket": 50,
        "watchlist_symbols": 100,
    }


def test_paper_auto_status_is_blocked_without_active_owner_constitution(monkeypatch):
    config = _Config(with_constitution=False)
    config.config["autonomy"]["mode"] = "PAPER_AUTO"
    config.config["paper_execution"]["reduce_fraction_pct"] = 25.0
    monkeypatch.setattr("api.execution_service.get_paper_broker", lambda **kwargs: _Broker())
    control = get_execution_control(config)
    assert control["readiness"]["ready"] is False
    assert any("Risk Constitution" in reason for reason in control["readiness"]["reasons"])
    assert config.calls == []


def test_kill_switch_is_hot_applied_without_disabling_monitoring(monkeypatch):
    config = _Config()
    monkeypatch.setattr("api.execution_service.get_paper_broker", lambda **kwargs: _Broker())
    result = set_kill_switch(config=config, active=True)
    assert result["new_automated_risk_blocked"] is True
    assert result["monitoring_active"] is True
    assert config.config["autonomy"]["kill_switch_active"] is True
