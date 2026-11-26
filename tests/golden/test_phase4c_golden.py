"""Golden tests — Phase 4C §44 critic, §45 autonomy, §46 paper-auto-before-live-auto."""
import pytest

from core.ai.decision import Action
from core.ai.decision_engine import evaluate_trade_decision
from core.ai.pipeline import STAGE_ORDER
from core.ai.provider import AIResponse
from core.autonomy import (
    AutonomyMode, can_enable_live_auto, kill_switch_active, mode_allows_live_orders,
    mode_allows_paper_orders, resolve_mode,
)

pytestmark = pytest.mark.golden


class _FP:
    name = "fake"

    def __init__(self, t):
        self.t = t

    def generate(self, prompt, *, model=None, system="", **opts):
        return AIResponse(text=self.t, model="m")


def _svc(text='{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.85,"thesis":"mom"}',
         critic='{"veto":false,"categories":[],"severity":0.1,"rationale":"ok"}'):
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
        "primary_provider": _FP(text), "fallback_chain": [],
        "critic_provider": _FP(critic), "critic_model": "critic-m",
    }


def _signal():
    return {"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95, "signal_score": 0.85, "strategy_type": "SCANNER_AI_MOMENTUM"}


# GOLD-044 — §44 critic is post-pipeline (NOT a §42 stage); veto forces NO_TRADE.
def test_gold044_critic_is_post_pipeline_stage_order_stays_six():
    assert STAGE_ORDER == ("PARSE", "SCHEMA", "SEMANTIC", "AUTHORITATIVE_DATA_CHECK", "POLICY", "RISK_GATE")


def test_gold044_critic_veto_forces_no_trade():
    svc = _svc(critic='{"veto":true,"categories":["concentration"],"severity":0.9,"rationale":"concentrated"}')
    res = evaluate_trade_decision(_signal(), {"accepted": True}, config={}, services=svc)
    assert res.decision.action is Action.NO_TRADE
    assert res.critic.veto is True
    assert res.review["recommendation"] == "avoid"  # decisive rejection, not unavailable
    assert res.review["available"] is True


def test_gold044_critic_pass_keeps_action():
    svc = _svc(critic='{"veto":false,"categories":[],"severity":0.1,"rationale":"ok"}')
    res = evaluate_trade_decision(_signal(), {"accepted": True}, config={}, services=svc)
    assert res.decision.action is Action.OPEN_LONG
    assert res.critic.veto is False


# GOLD-045 — §45 autonomy modes: default NOT LIVE_AUTO; owner-only escalation; fail-safe.
def test_gold045_default_mode_is_analyze_never_live_auto():
    assert resolve_mode({}) is AutonomyMode.ANALYZE
    assert resolve_mode({"autonomy": {"mode": "garbage"}}) is AutonomyMode.ANALYZE


def test_gold045_off_analyze_recommend_forbid_autonomous_orders():
    for m in (AutonomyMode.OFF, AutonomyMode.ANALYZE, AutonomyMode.RECOMMEND):
        assert mode_allows_paper_orders(m) is False
        assert mode_allows_live_orders(m) is False


def test_gold045_live_auto_requires_owner_flag():
    assert mode_allows_live_orders(AutonomyMode.LIVE_AUTO) is True
    assert kill_switch_active({"autonomy": {"kill_switch_active": True}}) is True


# GOLD-046 — §46 LIVE_AUTO disabled until Paper-Auto acceptance passes + owner flag.
def test_gold046_live_auto_disabled_without_owner_even_if_acceptance_passes():
    ok, _ = can_enable_live_auto(
        {"autonomy": {"live_auto_owner_enabled": False}},
        services=lambda a, c: _svc(a or '{"action":"NO_TRADE","ticker":"NVDA","confidence":0.5}',
                                   c or '{"veto":false}'))
    assert ok is False


def test_gold046_live_auto_enabled_with_owner_and_acceptance():
    def _fac(ai, crit):
        return _svc(ai or '{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.85,"thesis":"m"}',
                    crit or '{"veto":false,"categories":[],"severity":0.1,"rationale":"ok"}')
    ok, _ = can_enable_live_auto({"autonomy": {"live_auto_owner_enabled": True}}, services=_fac)
    assert ok is True


def test_gold046_live_auto_blocked_at_runtime_without_recorded_acceptance():
    # At runtime (no services) the recorded flag gates LIVE_AUTO — no live AI re-run.
    ok, reason = can_enable_live_auto({"autonomy": {"live_auto_owner_enabled": True}})
    assert ok is False
    assert "acceptance" in reason
    ok2, _ = can_enable_live_auto({"autonomy": {"live_auto_owner_enabled": True,
                                                "paper_auto_acceptance_passed": True}})
    assert ok2 is True