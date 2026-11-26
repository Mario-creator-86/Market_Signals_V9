from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest

from agents.risk_agent import RiskAgent


class DummyCapitalManager:
    def __init__(self, balance=10_000.0, positions=None):
        self._balance = balance
        self._positions = positions or []

    def get_balance(self):
        return self._balance

    def get_positions(self):
        return list(self._positions)

    def get_equity_curve(self, limit=500):
        return []


def _confirmed_constitution(**changes):
    values = {
        "owner_confirmed": True,
        "max_risk_per_trade_pct": 2.0,
        "max_position_size_pct": 25.0,
        "max_total_exposure_pct": 80.0,
        "max_leverage": 2.0,
        "minimum_cash_pct": 10.0,
        "max_sector_exposure_pct": 50.0,
        "correlation_threshold": 0.7,
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
    return values


def _constitution_signal(**changes):
    values = {
        "ticker": "NVDA",
        "sector": "Technology",
        "action": "BUY",
        "entry": 100.0,
        "tp": 104.0,
        "sl": 98.0,
        "invested_amount": 500.0,
        "usd_size": 1_000.0,
        "leverage": 2,
        "estimated_fees_usd": 1.0,
        "average_daily_volume_usd": 1_000_000.0,
        "data_age_seconds": 5.0,
        "has_data_conflict": False,
        "strategy_status": "APPROVED",
        "portfolio_correlations": {},
        "signal_score": 95.0,
    }
    values.update(changes)
    return values


def _risk_config(*, constitution, kill_switch=False, min_rr=1.2):
    risk = {
        "enabled": True,
        "max_daily_loss_pct": 10.0,
        "max_daily_loss_trades": 10,
        "max_open_trades": 5,
        "max_exposure_pct": 80.0,
        "max_position_size_pct": 20.0,
        "max_risk_per_trade_pct": 2.0,
        "min_risk_reward_ratio": min_rr,
    }
    application = {
        "risk_constitution": constitution,
        "autonomy": {
            "mode": "PAPER_AUTO",
            "kill_switch_active": kill_switch,
            "live_auto_owner_enabled": False,
        },
    }
    return SimpleNamespace(agents={"risk": risk}, config=application)


def test_invalid_explicitly_confirmed_constitution_rejects_before_evaluation(monkeypatch):
    cfg = _risk_config(constitution={"owner_confirmed": True})
    monkeypatch.setattr("core.capital_manager.get_capital_manager", lambda: DummyCapitalManager())

    result = RiskAgent(cfg).evaluate_signal(_constitution_signal())

    assert result["accepted"] is False
    assert result["reason"] == "risk_constitution_invalid"


def test_confirmed_constitution_kill_switch_vetoes_new_risk(monkeypatch):
    cfg = _risk_config(constitution=_confirmed_constitution(), kill_switch=True)
    monkeypatch.setattr("core.capital_manager.get_capital_manager", lambda: DummyCapitalManager())

    result = RiskAgent(cfg).evaluate_signal(_constitution_signal())

    assert result["accepted"] is False
    assert result["reason"] == "risk_constitution_veto"
    assert "kill_switch" in result["reasons"]
    assert len(result["metrics"]["risk_constitution_fingerprint"]) == 64


def test_confirmed_constitution_requires_complete_proposal_evidence(monkeypatch):
    cfg = _risk_config(constitution=_confirmed_constitution())
    monkeypatch.setattr("core.capital_manager.get_capital_manager", lambda: DummyCapitalManager())

    result = RiskAgent(cfg).evaluate_signal(
        _constitution_signal(average_daily_volume_usd=None)
    )

    # Unknown ADV is no longer an evidence crash: the liquidity constraint
    # rejects informatively (conservative — headroom cannot be proven).
    assert result["accepted"] is False
    assert result["reason"] == "risk_constitution_veto"
    assert any("liquidity" in str(reason) for reason in result["reasons"])


def test_confirmed_constitution_cannot_weaken_legacy_risk_checks(monkeypatch):
    cfg = _risk_config(constitution=_confirmed_constitution(), min_rr=3.0)
    monkeypatch.setattr("core.capital_manager.get_capital_manager", lambda: DummyCapitalManager())

    result = RiskAgent(cfg).evaluate_signal(_constitution_signal())

    assert result["accepted"] is False
    assert result["reason"] == "min_risk_reward_ratio"
    assert any(item["check"] == "constitution_confirmed" for item in result["checks"])


def test_unconfirmed_constitution_keeps_v7_compatibility_path(monkeypatch):
    cfg = _risk_config(
        constitution={"owner_confirmed": False},
        min_rr=3.0,
    )
    monkeypatch.setattr("core.capital_manager.get_capital_manager", lambda: DummyCapitalManager())

    result = RiskAgent(cfg).evaluate_signal(
        {
            "ticker": "NVDA",
            "action": "BUY",
            "entry": 100.0,
            "tp": 104.0,
            "sl": 98.0,
            "invested_amount": 500.0,
            "signal_score": 95.0,
        }
    )

    assert result["reason"] == "min_risk_reward_ratio"
    assert not any(item["check"] == "constitution_confirmed" for item in result["checks"])


def test_risk_agent_rejects_low_risk_reward(monkeypatch):
    cfg = SimpleNamespace(
        agents={
            "risk": {
                "enabled": True,
                "max_daily_loss_pct": 5.0,
                "max_open_trades": 5,
                "max_exposure_pct": 70.0,
                "max_position_size_pct": 10.0,
                "min_risk_reward_ratio": 1.5,
            }
        }
    )
    monkeypatch.setattr("core.capital_manager.get_capital_manager", lambda: DummyCapitalManager())

    agent = RiskAgent(cfg)
    result = agent.evaluate_signal(
        {
            "ticker": "AAPL",
            "action": "BUY",
            "entry": 100.0,
            "tp": 101.0,
            "sl": 99.0,
            "invested_amount": 500.0,
            "signal_score": 80.0,
        }
    )

    assert result["accepted"] is False
    assert result["reason"] == "min_risk_reward_ratio"


def test_risk_agent_fail_closed_on_internal_error(monkeypatch):
    cfg = SimpleNamespace(agents={"risk": {"enabled": True}})
    monkeypatch.setattr("core.capital_manager.get_capital_manager", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    agent = RiskAgent(cfg)
    result = agent.evaluate_signal({"ticker": "AAPL"})

    assert result["accepted"] is False
    assert result["reason"] == "risk_evaluation_error"


def test_risk_agent_ignores_historical_losses_for_daily_limit(monkeypatch):
    cfg = SimpleNamespace(
        agents={
            "risk": {
                "enabled": True,
                "max_daily_loss_pct": 2.0,
                "max_open_trades": 5,
                "max_exposure_pct": 70.0,
                "max_position_size_pct": 10.0,
                "min_risk_reward_ratio": 1.2,
            }
        }
    )
    old_loss = {
        "status": "CLOSED",
        "closed_at": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
        "pnl": -2500.0,
    }
    monkeypatch.setattr("core.capital_manager.get_capital_manager", lambda: DummyCapitalManager(positions=[old_loss]))

    agent = RiskAgent(cfg)
    result = agent.evaluate_signal(
        {
            "ticker": "MSFT",
            "action": "BUY",
            "entry": 100.0,
            "tp": 104.0,
            "sl": 98.0,
            "invested_amount": 500.0,
            "signal_score": 97.0,
        }
    )

    assert result["accepted"] is True
    assert result["metrics"]["daily_loss_pct"] == 0.0


def test_risk_agent_uses_net_realized_pnl_for_daily_limit(monkeypatch):
    cfg = SimpleNamespace(
        agents={
            "risk": {
                "enabled": True,
                "max_daily_loss_pct": 2.0,
                "max_open_trades": 5,
                "max_exposure_pct": 70.0,
                "max_position_size_pct": 10.0,
                "min_risk_reward_ratio": 1.2,
            }
        }
    )
    now = datetime.now(timezone.utc).isoformat()
    positions = [
        {"status": "CLOSED", "closed_at": now, "pnl": -2500.0},
        {"status": "CLOSED", "closed_at": now, "pnl": 3000.0},
    ]
    monkeypatch.setattr("core.capital_manager.get_capital_manager", lambda: DummyCapitalManager(positions=positions))

    agent = RiskAgent(cfg)
    result = agent.evaluate_signal(
        {
            "ticker": "CLSK",
            "action": "BUY",
            "entry": 100.0,
            "tp": 104.0,
            "sl": 98.0,
            "invested_amount": 500.0,
            "signal_score": 90.0,
        }
    )

    assert result["accepted"] is True
    assert result["metrics"]["daily_loss_pct"] == 0.0


def test_risk_agent_rejects_position_above_conservative_cap(monkeypatch):
    cfg = SimpleNamespace(
        agents={
            "risk": {
                "enabled": True,
                "max_daily_loss_pct": 1.0,
                "max_daily_loss_trades": 2,
                "max_open_trades": 5,
                "max_exposure_pct": 70.0,
                "max_position_size_pct": 0.5,
                "min_risk_reward_ratio": 1.2,
            }
        }
    )
    monkeypatch.setattr("core.capital_manager.get_capital_manager", lambda: DummyCapitalManager())

    agent = RiskAgent(cfg)
    result = agent.evaluate_signal(
        {
            "ticker": "NVDA",
            "action": "BUY",
            "entry": 100.0,
            "tp": 104.0,
            "sl": 98.0,
            "invested_amount": 100.0,
            "signal_score": 95.0,
        }
    )

    assert result["accepted"] is False
    assert result["reason"] == "max_position_size_pct"


def test_risk_agent_stops_after_two_losses_today(monkeypatch):
    cfg = SimpleNamespace(
        agents={
            "risk": {
                "enabled": True,
                "max_daily_loss_pct": 99.0,
                "max_daily_loss_trades": 2,
                "max_open_trades": 5,
                "max_exposure_pct": 70.0,
                "max_position_size_pct": 10.0,
                "min_risk_reward_ratio": 1.2,
            }
        }
    )
    now = datetime.now(timezone.utc).isoformat()
    positions = [
        {"status": "CLOSED", "closed_at": now, "net_pnl": -1.0},
        {"status": "CLOSED", "closed_at": now, "net_pnl": -2.0},
    ]
    monkeypatch.setattr("core.capital_manager.get_capital_manager", lambda: DummyCapitalManager(positions=positions))

    agent = RiskAgent(cfg)
    result = agent.evaluate_signal(
        {
            "ticker": "MSFT",
            "action": "BUY",
            "entry": 100.0,
            "tp": 104.0,
            "sl": 98.0,
            "invested_amount": 50.0,
            "signal_score": 95.0,
        }
    )

    assert result["accepted"] is False
    assert result["reason"] == "daily_loss_count"


def test_risk_agent_excludes_research_cohort_from_capital_limits(monkeypatch):
    cfg = SimpleNamespace(
        agents={
            "risk": {
                "enabled": True,
                "max_daily_loss_pct": 1.0,
                "max_daily_loss_trades": 1,
                "max_open_trades": 1,
                "max_exposure_pct": 1.0,
                "max_position_size_pct": 1.0,
                "min_risk_reward_ratio": 1.2,
            }
        }
    )
    now = datetime.now(timezone.utc).isoformat()
    positions = [
        {
            "status": "OPEN",
            "position_type": "research_cohort",
            "research_only": True,
            "usd_size": 5000.0,
        },
        {
            "status": "CLOSED",
            "closed_at": now,
            "position_type": "research_cohort",
            "research_only": True,
            "net_pnl": -500.0,
        },
    ]
    monkeypatch.setattr(
        "core.capital_manager.get_capital_manager",
        lambda: DummyCapitalManager(positions=positions),
    )

    result = RiskAgent(cfg).evaluate_signal(
        {
            "ticker": "AAPL",
            "action": "BUY",
            "entry": 100.0,
            "tp": 104.0,
            "sl": 98.0,
            "invested_amount": 50.0,
            "signal_score": 95.0,
        }
    )

    assert result["accepted"] is True
    assert result["metrics"]["open_positions"] == 0
    assert result["metrics"]["daily_loss_trades"] == 0
    assert result["metrics"]["daily_loss_pct"] == 0.0
    assert result["metrics"]["exposure_pct"] == 0.0


@pytest.mark.parametrize(
    ("configured_limit", "expected_accepted"),
    [(1.5, False), (2.0, True), (2.5, True)],
)
def test_risk_per_trade_limit_above_half_percent_is_honored_with_leverage(
    monkeypatch,
    configured_limit,
    expected_accepted,
):
    cfg = SimpleNamespace(
        agents={
            "risk": {
                "enabled": True,
                "max_daily_loss_pct": 10.0,
                "max_daily_loss_trades": 10,
                "max_open_trades": 5,
                "max_exposure_pct": 80.0,
                "max_position_size_pct": 20.0,
                "max_risk_per_trade_pct": configured_limit,
                "min_risk_reward_ratio": 1.2,
            }
        }
    )
    monkeypatch.setattr(
        "core.capital_manager.get_capital_manager",
        lambda: DummyCapitalManager(balance=10_000.0),
    )

    result = RiskAgent(cfg).evaluate_signal(
        {
            "ticker": "NVDA",
            "action": "BUY",
            "entry": 100.0,
            "tp": 120.0,
            "sl": 90.0,
            "invested_amount": 1_000.0,
            "leverage": 2,
            "signal_score": 95.0,
        }
    )

    assert result["accepted"] is expected_accepted
    assert result["metrics"]["risk_per_trade_pct"] == 2.0
    assert result["metrics"]["max_risk_per_trade_pct"] == configured_limit


def test_daily_loss_blocks_at_exact_configured_limit(monkeypatch):
    cfg = SimpleNamespace(
        agents={
            "risk": {
                "enabled": True,
                "max_daily_loss_pct": 10.0,
                "max_daily_loss_trades": 10,
                "max_open_trades": 5,
                "max_exposure_pct": 80.0,
                "max_position_size_pct": 20.0,
                "max_risk_per_trade_pct": 2.0,
                "min_risk_reward_ratio": 1.2,
            }
        }
    )
    loss = {
        "status": "CLOSED",
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "net_pnl": -1_000.0,
    }
    monkeypatch.setattr(
        "core.capital_manager.get_capital_manager",
        lambda: DummyCapitalManager(balance=10_000.0, positions=[loss]),
    )

    result = RiskAgent(cfg).evaluate_signal(
        {
            "ticker": "AAPL",
            "action": "BUY",
            "entry": 100.0,
            "tp": 104.0,
            "sl": 98.0,
            "invested_amount": 100.0,
            "signal_score": 95.0,
        }
    )

    assert result["accepted"] is False
    assert result["reason"] == "daily_loss_limit"
    assert result["metrics"]["daily_loss_pct"] == 10.0


def test_risk_blocks_opposite_direction_for_same_open_ticker(monkeypatch):
    cfg = _risk_config(constitution=_confirmed_constitution())
    open_short = {
        "position_id": "pos_bb_short",
        "status": "OPEN",
        "ticker": "BB",
        "direction": "SHORT",
        "position_type": "paper_signal",
        "usd_size": 100.0,
        "margin_used": 100.0,
        "sector": "Technology",
    }
    monkeypatch.setattr(
        "core.capital_manager.get_capital_manager",
        lambda: DummyCapitalManager(positions=[open_short]),
    )

    result = RiskAgent(cfg).evaluate_signal(
        _constitution_signal(ticker="BB", action="BUY")
    )

    assert result["accepted"] is False
    assert result["reason"] == "opposite_open_position"
    assert "existing SHORT, proposed LONG" in result["reasons"][0]


def test_daily_loss_percentage_uses_equity_not_cash_after_reserved_margin(monkeypatch):
    cfg = SimpleNamespace(
        agents={
            "risk": {
                "enabled": True,
                "max_daily_loss_pct": 6.0,
                "max_daily_loss_trades": 10,
                "max_open_trades": 5,
                "max_exposure_pct": 80.0,
                "max_position_size_pct": 20.0,
                "max_risk_per_trade_pct": 2.0,
                "min_risk_reward_ratio": 1.2,
            }
        }
    )
    now = datetime.now(timezone.utc).isoformat()
    positions = [
        {
            "status": "OPEN",
            "ticker": "MSFT",
            "direction": "LONG",
            "margin_used": 5_000.0,
            "usd_size": 5_000.0,
            "pnl": 0.0,
        },
        {"status": "CLOSED", "closed_at": now, "net_pnl": -500.0},
    ]
    monkeypatch.setattr(
        "core.capital_manager.get_capital_manager",
        lambda: DummyCapitalManager(balance=5_000.0, positions=positions),
    )

    result = RiskAgent(cfg).evaluate_signal(
        {
            "ticker": "AAPL",
            "action": "BUY",
            "entry": 100.0,
            "tp": 104.0,
            "sl": 98.0,
            "invested_amount": 100.0,
            "signal_score": 95.0,
        }
    )

    assert result["accepted"] is True
    assert result["metrics"]["balance"] == 10_000.0
    assert result["metrics"]["daily_loss_pct"] == 5.0
