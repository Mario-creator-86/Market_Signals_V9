"""Phase 4C agent_manager rewire — autonomy/kill-switch entry gates + decision_context enrichment.

Tests the gate helpers directly and the enrichment contract via the engine-through-delegate
path. The entry-gate behavior in the live autonomous loop is additionally covered by the
research_cohort regression (which now carries an autonomy PAPER_AUTO fixture).
"""
import json

from core import ollama_decision as od
from core.ai.decision import Action, Decision
from core.ai.provider import AIResponse


class _FP:
    name = "fake"

    def __init__(self, t):
        self.t = t

    def generate(self, prompt, *, model=None, system="", **opts):
        return AIResponse(text=self.t, model="m")


def _svc():
    class _RA:
        def evaluate_signal(self, s):
            return {"accepted": True, "reason": "ok", "checks": [], "metrics": {}}
    return {
        "evaluate_capital_entry": lambda s, p, c=None, **k: {"allowed": True, "check": "complete", "status": "validated", "reason": "ok", "checks": {}},
        "resolve_deployable_balance": lambda cm: 50000.0,
        "compute_size_factor": lambda score, cfg=None, **k: {"size_factor": 0.5},
        "calculate_trade_risk_pct": lambda **k: 0.3,
        "risk_agent": _RA(),
        "should_veto_trade": lambda review, config=None: False,
        "primary_provider": _FP('{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.8,"thesis":"mom"}'),
        "fallback_chain": [],
        "critic_provider": _FP('{"veto":false,"categories":[],"severity":0.1,"rationale":"ok"}'),
        "critic_model": "critic-m",
    }


def test_ai_critic_sizing_uses_one_independent_cap_not_compounding():
    from core.agent_manager import _combine_independent_size_caps

    assert _combine_independent_size_caps(0.25, 0.25) == 0.25
    assert _combine_independent_size_caps(0.25, 0.60) == 0.25
    assert _combine_independent_size_caps(1.00, 0.60) == 0.60
    assert _combine_independent_size_caps(0.50, 0.30) == 0.30
    assert _combine_independent_size_caps(0.00, 0.80) == 0.00


def test_kill_switch_active_blocks_autonomous_order():
    from core.autonomy import kill_switch_active
    assert kill_switch_active({"autonomy": {"kill_switch_active": True}}) is True
    assert kill_switch_active({"autonomy": {"kill_switch_active": False}}) is False


def test_resolve_mode_off_blocks_paper_orders():
    from core.autonomy import mode_allows_paper_orders, resolve_mode
    assert mode_allows_paper_orders(resolve_mode({"autonomy": {"mode": "OFF"}})) is False
    assert mode_allows_paper_orders(resolve_mode({"autonomy": {"mode": "ANALYZE"}})) is False
    assert mode_allows_paper_orders(resolve_mode({"autonomy": {"mode": "PAPER_AUTO"}})) is True


def test_autonomy_gate_kill_switch_blocks(monkeypatch):
    from core.agent_manager import AgentManager
    from types import SimpleNamespace
    mgr = AgentManager.__new__(AgentManager)
    mgr.config_manager = SimpleNamespace(config={"autonomy": {"kill_switch_active": True}})
    allowed, reason = mgr._autonomy_order_gate({"mode": "paper"})
    assert allowed is False
    assert reason == "kill_switch_active"


def test_autonomy_gate_analyze_blocks_paper_orders(monkeypatch):
    from core.agent_manager import AgentManager
    from types import SimpleNamespace
    mgr = AgentManager.__new__(AgentManager)
    mgr.config_manager = SimpleNamespace(config={"autonomy": {"mode": "ANALYZE"}})
    allowed, reason = mgr._autonomy_order_gate({"mode": "paper"})
    assert allowed is False
    assert "forbids_paper_orders" in reason


def test_autonomy_gate_paper_auto_allows_paper_orders(monkeypatch):
    from core.agent_manager import AgentManager
    from types import SimpleNamespace
    mgr = AgentManager.__new__(AgentManager)
    mgr.config_manager = SimpleNamespace(config={"autonomy": {"mode": "PAPER_AUTO"}})
    allowed, reason = mgr._autonomy_order_gate({"mode": "paper"})
    assert allowed is True
    assert reason == "ok"


def test_autonomy_gate_live_requires_owner_and_live_auto(monkeypatch):
    from core.agent_manager import AgentManager
    from types import SimpleNamespace
    # PAPER_AUTO mode + live execution_cfg -> forbids live (not LIVE_AUTO)
    mgr = AgentManager.__new__(AgentManager)
    mgr.config_manager = SimpleNamespace(config={"autonomy": {"mode": "PAPER_AUTO"}})
    allowed, reason = mgr._autonomy_order_gate({"mode": "live"})
    assert allowed is False
    assert "forbids_live_orders" in reason
    # LIVE_AUTO mode but owner flag off -> blocked (§45)
    mgr2 = AgentManager.__new__(AgentManager)
    mgr2.config_manager = SimpleNamespace(config={"autonomy": {"mode": "LIVE_AUTO", "live_auto_owner_enabled": False}})
    allowed2, reason2 = mgr2._autonomy_order_gate({"mode": "live"})
    assert allowed2 is False
    assert "live_auto_blocked" in reason2
    # LIVE_AUTO + owner enabled but no recorded acceptance -> blocked (§46)
    mgr3 = AgentManager.__new__(AgentManager)
    mgr3.config_manager = SimpleNamespace(config={"autonomy": {"mode": "LIVE_AUTO", "live_auto_owner_enabled": True}})
    allowed3, reason3 = mgr3._autonomy_order_gate({"mode": "live"})
    assert allowed3 is False
    assert "live_auto_blocked" in reason3
    # LIVE_AUTO + owner + recorded acceptance flag -> allowed
    mgr4 = AgentManager.__new__(AgentManager)
    mgr4.config_manager = SimpleNamespace(config={"autonomy": {"mode": "LIVE_AUTO", "live_auto_owner_enabled": True,
                                                              "paper_auto_acceptance_passed": True}})
    allowed4, reason4 = mgr4._autonomy_order_gate({"mode": "live"})
    assert allowed4 is True
    assert reason4 == "ok"


def test_autonomy_gate_fail_safe_when_config_unreadable(monkeypatch):
    from core.agent_manager import AgentManager
    mgr = AgentManager.__new__(AgentManager)
    # no config_manager attribute at all -> must fail-safe to block (not enable)
    allowed, reason = mgr._autonomy_order_gate({"mode": "paper"})
    assert allowed is False
    assert "forbids_paper_orders" in reason  # resolved to ANALYZE


def test_decision_context_carries_decision_validation_critic(monkeypatch):
    # The rewire stores Decision/ValidationResult/CriticVerdict via signal_data -> decision_context.
    # Verify the engine review carries the §41 Decision object the rewire copies into context.
    svc = _svc()
    monkeypatch.setattr("core.ai.decision_engine.default_engine_services", lambda c: svc)
    review = od.evaluate_trade_decision_with_ollama(
        {"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95, "signal_score": 0.8, "strategy_type": "SCANNER_AI_MOMENTUM"},
        {"accepted": True}, config={},
    )
    assert review["available"] is True
    # the review carries the §41 Decision object the rewire copies into decision_context["ai_decision"]
    assert review["decision"].action is Action.OPEN_LONG
    assert review["critic_veto"] is False


def test_agent_manager_projects_ai_artifacts_to_durable_json(monkeypatch):
    from core.agent_manager import _json_safe_ai_artifacts
    from core.ai.decision_engine import evaluate_trade_decision

    result = evaluate_trade_decision(
        {
            "ticker": "NVDA",
            "action": "BUY",
            "entry": 100,
            "tp": 110,
            "sl": 95,
            "signal_score": 0.8,
            "strategy_type": "SCANNER_AI_MOMENTUM",
        },
        {"accepted": True},
        config={},
        services=_svc(),
    )

    decision, validation, critic, review = _json_safe_ai_artifacts(result)
    json.dumps(
        {
            "ai_decision": decision,
            "ai_validation": validation,
            "ai_critic": critic,
            "ollama": review,
        }
    )
    assert decision["action"] == "OPEN_LONG"
    assert validation["ok"] is True
    assert review["decision"]["action"] == "OPEN_LONG"


def test_ai_entry_block_honors_effective_critic_veto_at_low_ai_confidence():
    from core.agent_manager import _ai_entry_block
    from core.ai.critic import CriticVerdict
    from core.ai.decision_engine import EvaluationResult
    from core.ai.pipeline import ValidationResult, RISK_GATE

    decision = Decision(action=Action.NO_TRADE, ticker="NVDA", confidence=0.45)
    result = EvaluationResult(
        decision=decision,
        validation=ValidationResult(ok=True, decision=decision, stage=RISK_GATE),
        critic=CriticVerdict(veto=True, model_veto=True, severity=0.96),
        review={"available": True, "recommendation": "avoid", "confidence": 0.45, "critic_veto": True},
        ai_available=True,
    )

    blocked, reason = _ai_entry_block(result, result.review)

    assert blocked is True
    assert reason == "critic_veto"


def test_ai_entry_block_allows_calibrated_critic_pass():
    from core.agent_manager import _ai_entry_block
    from core.ai.critic import CriticVerdict
    from core.ai.decision_engine import EvaluationResult
    from core.ai.pipeline import ValidationResult, RISK_GATE

    decision = Decision(action=Action.OPEN_LONG, ticker="NVDA", confidence=0.8)
    result = EvaluationResult(
        decision=decision,
        validation=ValidationResult(ok=True, decision=decision, stage=RISK_GATE),
        critic=CriticVerdict(veto=False, model_veto=True, severity=0.78, size_factor=0.61),
        review={"available": True, "recommendation": "reduce", "critic_veto": False},
        ai_available=True,
    )

    assert _ai_entry_block(result, result.review) == (False, "")


def test_ai_entry_block_fails_closed_when_validation_rejects_active_proposal():
    from core.agent_manager import _ai_entry_block
    from core.ai.decision_engine import EvaluationResult
    from core.ai.pipeline import ValidationResult, POLICY

    decision = Decision(action=Action.OPEN_LONG, ticker="BNTX", confidence=0.5)
    result = EvaluationResult(
        decision=decision,
        validation=ValidationResult(
            ok=False,
            decision=decision,
            stage=POLICY,
            reason="POLICY: risk_constitution_veto",
        ),
        critic=None,
        review={"available": True, "recommendation": "avoid"},
        ai_available=True,
    )

    assert _ai_entry_block(result, result.review) == (True, "ai_validation_policy")


def test_required_capital_ai_invalid_response_is_rejected_before_ledger_status():
    from core.agent_manager import _ai_review_availability_policy

    policy = _ai_review_availability_policy(
        {"_autonomous_candidate": True},
        {
            "mode": "paper",
            "require_ai_review": True,
            "allow_signal_only_ai_fallback": True,
            "capital_entry_gate": {"enabled": True, "require_ai_review": True},
        },
        {"decision_enabled": True},
        {"available": False, "fallback_used": "invalid_json"},
    )

    assert policy["capital_required"] is True
    assert policy["fallback"] is False
    assert policy["blocked"] is True


def test_non_capital_paper_ai_failure_can_use_reduced_fallback():
    from core.agent_manager import _ai_review_availability_policy

    policy = _ai_review_availability_policy(
        {"_autonomous_candidate": True},
        {
            "mode": "paper",
            "require_ai_review": True,
            "allow_signal_only_ai_fallback": True,
            "capital_entry_gate": {"enabled": False, "require_ai_review": True},
        },
        {"decision_enabled": True},
        {"available": False, "fallback_used": "timeout"},
    )

    assert policy["capital_required"] is False
    assert policy["fallback"] is True
    assert policy["blocked"] is False
