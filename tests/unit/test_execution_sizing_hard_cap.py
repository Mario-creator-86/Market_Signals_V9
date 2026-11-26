"""Tests for the configurable not-profitable size hard cap (execution_agent.py:73)."""
import pytest

from agents.execution_agent import ExecutionAgent


def _cfg(**over):
    base = {
        "balance_usage_pct": 2.0,
        "unprofitable_balance_usage_cap_pct": 2.0,
        "unprofitable_balance_usage_hard_cap_pct": 2.0,
        "profitability_min_sample": 30,
        "profitability_min_profit_factor": 1.2,
        "profitability_min_expectancy": 0.0,
    }
    base.update(over)
    return base


def _force_not_profitable(monkeypatch):
    """Make _resolve_balance_usage_pct see zero trade history -> not profitable."""
    import core.feedback_loop as fl

    monkeypatch.setattr(fl, "get_recent_trades", lambda n: [])
    monkeypatch.setattr(
        fl,
        "compute_portfolio_metrics",
        lambda trades: {"total_trades": 0, "profit_factor": None, "expectancy": 0.0},
    )


def test_hard_cap_key_raises_not_profitable_to_2pct(monkeypatch):
    _force_not_profitable(monkeypatch)
    eff, profile = ExecutionAgent._resolve_balance_usage_pct(_cfg())
    assert profile["profitable"] is False
    assert eff == pytest.approx(0.02, rel=1e-9)


def test_default_hard_cap_preserves_0_5pct_when_key_absent(monkeypatch):
    _force_not_profitable(monkeypatch)
    cfg = _cfg(balance_usage_pct=0.5, unprofitable_balance_usage_cap_pct=0.5)
    cfg.pop("unprofitable_balance_usage_hard_cap_pct", None)
    eff, profile = ExecutionAgent._resolve_balance_usage_pct(cfg)
    assert profile["profitable"] is False
    assert eff == pytest.approx(0.005, rel=1e-9)  # 0.5% — current behavior


def test_hard_cap_lower_than_configured_caps_effective(monkeypatch):
    _force_not_profitable(monkeypatch)
    # configured 2.0 but hard cap 1.0 -> effective 1.0%
    eff, _ = ExecutionAgent._resolve_balance_usage_pct(
        _cfg(balance_usage_pct=2.0, unprofitable_balance_usage_cap_pct=2.0,
              unprofitable_balance_usage_hard_cap_pct=1.0)
    )
    assert eff == pytest.approx(0.01, rel=1e-9)


def test_profitability_is_scoped_to_exact_strategy_and_direction(monkeypatch):
    import core.feedback_loop as fl

    history = [
        {"strategy_type": "OTHER", "direction": "LONG", "net_pnl": 20.0}
        for _ in range(40)
    ] + [
        {"strategy_type": "PRE_EARNINGS_AI_LONG", "direction": "LONG", "net_pnl": -5.0}
        for _ in range(30)
    ]
    monkeypatch.setattr(fl, "get_recent_trades", lambda _n: history)

    effective, profile = ExecutionAgent._resolve_balance_usage_pct(
        _cfg(balance_usage_pct=8.0, unprofitable_balance_usage_cap_pct=0.5,
             unprofitable_balance_usage_hard_cap_pct=0.5),
        strategy_id="PRE_EARNINGS_AI_LONG",
        direction="LONG",
    )

    assert profile["total_trades"] == 30
    assert profile["profitable"] is False
    assert effective == pytest.approx(0.005, rel=1e-9)
