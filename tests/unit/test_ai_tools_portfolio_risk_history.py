"""§43 — portfolio/risk tools delegate to authoritative app services."""
from core.ai.tools.portfolio_risk import (
    make_get_portfolio_tool, make_get_position_tool, make_get_portfolio_risk_tool,
    make_get_available_capital_tool, make_calculate_allowed_position_size_tool, portfolio_risk_tools,
)
from core.ai.tools.history import make_get_decision_history_tool, make_get_trade_history_tool, history_tools


class _FakeCM:
    def get_positions(self):
        return [{"id": "P1", "ticker": "NVDA"}]
    def get_position(self, pid):
        return {"id": pid, "ticker": "NVDA"} if pid == "P1" else None
    def get_deployable_balance(self):
        return 50000.0


def test_get_portfolio_delegates():
    t = make_get_portfolio_tool(capital_manager=_FakeCM())
    r = t.execute()
    assert r.ok and r.data["positions"][0]["id"] == "P1"


def test_get_position_delegates():
    t = make_get_position_tool(capital_manager=_FakeCM())
    r = t.execute(position_id="P1")
    assert r.ok and r.data["ticker"] == "NVDA"
    r2 = t.execute(position_id="MISSING")
    assert r2.ok is False


def test_get_available_capital_uses_authoritative_deployable_balance():
    # §43 — AI must not compute capital; delegate to get_deployable_balance.
    t = make_get_available_capital_tool(capital_manager=_FakeCM())
    r = t.execute()
    assert r.ok and r.data["available_capital"] == 50000.0


def test_calculate_allowed_position_size_uses_compute_size_factor_and_risk_limits():
    # §43 — size = app services (compute_size_factor + calculate_trade_risk_pct), not AI.
    def fake_size_factor(score, cfg=None, **k):
        return {"size_factor": 0.8, "base_factor": 0.5}
    def fake_trade_risk_pct(*, notional_usd, stop_loss_fraction, equity):
        return notional_usd * stop_loss_fraction / equity * 100.0
    t = make_calculate_allowed_position_size_tool(
        compute_size_factor=fake_size_factor, calculate_trade_risk_pct=fake_trade_risk_pct,
        capital_manager=_FakeCM())
    r = t.execute(score=80, notional_usd=10000, stop_loss_fraction=0.05)
    assert r.ok
    assert r.data["size_factor"] == 0.8
    assert r.data["loss_at_stop_pct"] == 10000 * 0.05 / 50000.0 * 100.0


def test_get_portfolio_risk_delegates_to_risk_agent():
    def fake_eval(signal):
        return {"accepted": True, "checks": [], "metrics": {"exposure_pct": 12.0}}
    t = make_get_portfolio_risk_tool(evaluate_signal=fake_eval)
    r = t.execute(signal={"ticker": "NVDA"})
    assert r.ok and r.data["accepted"] is True


def test_get_decision_history_delegates():
    def fake(limit, **k):
        return [{"event": "open"}]
    t = make_get_decision_history_tool(get_decisions=fake)
    r = t.execute(limit=10)
    assert r.ok and r.data["value"] == [{"event": "open"}]


def test_get_trade_history_delegates():
    def fake(limit=100, **k):
        return [{"event": "fill"}]
    t = make_get_trade_history_tool(read_recent=fake)
    r = t.execute(limit=20)
    assert r.ok and r.data["value"] == [{"event": "fill"}]


def test_portfolio_risk_and_history_tool_lists():
    assert [t.name for t in portfolio_risk_tools()] == [
        "get_portfolio", "get_position", "get_portfolio_risk",
        "get_available_capital", "calculate_allowed_position_size"]
    assert [t.name for t in history_tools()] == ["get_decision_history", "get_trade_history"]