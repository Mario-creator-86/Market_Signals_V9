"""Golden tests for the Phase 4B §42 validation pipeline + §43 non-authoritative numbers.

GOLD-042 — §42 ordering: every decision passes PARSE -> SCHEMA -> SEMANTIC ->
AUTHORITATIVE DATA CHECK -> POLICY -> RISK GATE in that exact order; the first
failing stage short-circuits; AI prose alone never produces an order.

GOLD-043 — §43 non-authoritative numbers: AI-supplied price/P&L/IV/EPS/portfolio
value/allowed_capital/position_size are stripped (or rejected in strict mode) and
the validated numbers come from app services (resolve_deployable_balance,
compute_size_factor, calculate_trade_risk_pct, RiskAgent.evaluate_signal).
"""
import pytest

from core.ai.decision import Action
from core.ai.pipeline import (
    AUTHORITATIVE_DATA_CHECK,
    PARSE,
    POLICY,
    RISK_GATE,
    SCHEMA,
    SEMANTIC,
    STAGE_ORDER,
    run_pipeline,
)

pytestmark = pytest.mark.golden


def _full_fake_services():
    class _RA:
        def evaluate_signal(self, s):
            return {"accepted": True, "reason": "passed", "checks": [], "metrics": {}}

    return {
        "evaluate_capital_entry": lambda s, p, c=None, **k: {"allowed": True, "check": "complete", "status": "validated", "reason": "ok", "checks": {}},
        "resolve_deployable_balance": lambda cm: 50000.0,
        "compute_size_factor": lambda score, cfg=None, **k: {"size_factor": 0.5},
        "calculate_trade_risk_pct": lambda **k: 0.3,
        "risk_agent": _RA(),
        "should_veto_trade": lambda review, config=None: False,
    }


def test_gold042_pipeline_stage_order_is_exactly_spec_42():
    assert STAGE_ORDER == (
        "PARSE", "SCHEMA", "SEMANTIC",
        "AUTHORITATIVE_DATA_CHECK", "POLICY", "RISK_GATE",
    )


def test_gold042_ai_prose_alone_never_orders():
    # pure prose, no JSON — must resolve to NO_TRADE at PARSE, never an open action
    res = run_pipeline("The chart looks great, I think we should buy NVDA aggressively!",
                       signal={}, positions=[], config={}, services=_full_fake_services())
    assert res.ok is False
    assert res.stage == PARSE
    assert res.decision.action == Action.NO_TRADE


def test_gold042_first_failing_stage_short_circuits_in_order():
    # invalid action -> PARSE produces NO_TRADE (parse success) -> passes; force a
    # SEMANTIC fail (OPEN_LONG, no ticker) and confirm the stage is SEMANTIC
    res = run_pipeline({"action": "OPEN_LONG", "confidence": 0.8},
                       signal={}, positions=[], config={}, services=_full_fake_services())
    assert res.ok is False
    assert res.stage == SEMANTIC


def test_gold043_ai_authoritative_numbers_are_stripped_not_used():
    raw = {"action": "OPEN_LONG", "ticker": "NVDA", "confidence": 0.8,
           "price": 1.0, "pnl": 2.0, "iv": 3.0, "eps": 4.0,
           "portfolio_value": 5.0, "allowed_capital": 999999, "position_size": 888888}
    res = run_pipeline(raw, signal={"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95,
                                     "signal_score": 0.8, "strategy_type": "SCANNER_AI_MOMENTUM"},
                       positions=[], config={}, services=_full_fake_services())
    assert res.ok is True
    auth = res.checks["authoritative"]
    # §43: app-service numbers, NOT the AI's 999999 / 888888
    assert auth["allowed_capital"] == 50000.0
    assert auth["size_factor"] == 0.5
    assert auth["trade_risk_pct"] == 0.3
    # the Decision schema has no authoritative fields at all
    for forbidden in ("price", "pnl", "iv", "eps", "portfolio_value", "allowed_capital", "position_size"):
        assert not hasattr(res.decision, forbidden)


def test_gold043_strict_schema_rejects_authoritative_fields():
    raw = {"action": "OPEN_LONG", "ticker": "NVDA", "confidence": 0.8, "price": 850.0}
    res = run_pipeline(raw, signal={}, positions=[],
                       config={"ai": {"decision": {"strict_schema": True}}},
                       services=_full_fake_services())
    assert res.ok is False
    assert res.stage == SCHEMA
    assert "price" in res.reason


def test_gold042_vetoed_risk_gate_forces_no_trade_valid_outcome():
    svc = _full_fake_services()
    svc["should_veto_trade"] = lambda review, config=None: True
    raw = {"action": "OPEN_LONG", "ticker": "NVDA", "confidence": 0.9}
    res = run_pipeline(raw, signal={"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95,
                                     "signal_score": 0.9, "strategy_type": "SCANNER_AI_MOMENTUM"},
                       positions=[], config={}, services=svc)
    # §44: a valid final result can be NO_TRADE
    assert res.ok is True
    assert res.decision.action == Action.NO_TRADE
    assert res.checks["risk_gate"]["vetoed"] is True