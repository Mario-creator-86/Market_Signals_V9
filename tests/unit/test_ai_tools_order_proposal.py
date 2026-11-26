"""§40/§42 — propose_order/propose_close return DRAFTS and never execute."""
import pytest

from core.ai.tools.order_proposal import (
    make_propose_order_tool, make_propose_close_tool, order_proposal_tools,
)


class _GuardCM:
    """Capital manager whose mutating methods raise if reached."""
    def __init__(self, deployable=40000.0, position=None, price=100.0):
        self._deployable = deployable
        self._position = position
        self._price = price
        self.open_position_called = False
        self.close_position_called = False

    def get_deployable_balance(self):
        return self._deployable

    def get_positions(self):
        return [] if self._position is None else [self._position]

    def get_position(self, pid):
        return self._position if (self._position or {}).get("id") == pid else None

    def open_position(self, *a, **k):
        self.open_position_called = True
        raise AssertionError("propose_order MUST NOT call open_position (§42)")

    def close_position(self, *a, **k):
        self.close_position_called = True
        raise AssertionError("propose_close MUST NOT call close_position (§42)")


def test_propose_order_returns_draft_and_never_calls_open_position():
    cm = _GuardCM()
    t = make_propose_order_tool(
        capital_manager=cm,
        compute_size_factor=lambda score, **k: {"size_factor": 0.5},
        calculate_trade_risk_pct=lambda *, notional_usd, stop_loss_fraction, equity: 1.0,
        evaluate_capital_entry=lambda *a, **k: {"allowed": True, "checks": {}},
    )
    r = t.execute(ticker="NVDA", direction="LONG", score=80, notional_usd=8000, stop_loss_fraction=0.05,
                  take_profit_pct=0.10, stop_loss_pct=0.05)
    assert r.ok
    assert r.data["draft"] is True
    assert r.data["ticker"] == "NVDA"
    assert r.data["direction"] == "LONG"
    assert cm.open_position_called is False


def test_propose_order_includes_capital_entry_evidence():
    cm = _GuardCM()
    t = make_propose_order_tool(
        capital_manager=cm,
        compute_size_factor=lambda score, **k: {"size_factor": 0.5},
        calculate_trade_risk_pct=lambda *, notional_usd, stop_loss_fraction, equity: 1.0,
        evaluate_capital_entry=lambda signal, positions, config=None, **k: {"allowed": False, "reason": "weak backtest"},
    )
    r = t.execute(ticker="X", direction="LONG", score=50, notional_usd=1000, stop_loss_fraction=0.05,
                  take_profit_pct=0.1, stop_loss_pct=0.05)
    assert r.ok
    assert r.data["capital_entry"]["allowed"] is False
    assert r.data["capital_entry"]["reason"] == "weak backtest"


def test_propose_close_returns_draft_and_never_calls_close_position():
    pos = {"id": "P1", "ticker": "NVDA", "direction": "LONG"}
    cm = _GuardCM(position=pos, price=120.0)
    t = make_propose_close_tool(capital_manager=cm)
    r = t.execute(position_id="P1", reason="thesis invalidated")
    assert r.ok
    assert r.data["draft"] is True
    assert r.data["position_id"] == "P1"
    assert r.data["exit_reason"] == "thesis invalidated"
    assert cm.close_position_called is False


def test_propose_close_missing_position_returns_error():
    cm = _GuardCM(position=None)
    t = make_propose_close_tool(capital_manager=cm)
    r = t.execute(position_id="MISSING", reason="x")
    assert r.ok is False


def test_order_proposal_tools_returns_two():
    assert [t.name for t in order_proposal_tools()] == ["propose_order", "propose_close"]