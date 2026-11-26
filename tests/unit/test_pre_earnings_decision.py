from core.ai.decision import Action
from core.ai.decision_engine import EvaluationResult
from core.ai.provider import AIResponse
from core.pre_earnings.decision import (
    build_pre_earnings_decision_context, evaluate_pre_earnings_decision,
)


class _FP:
    name = "fake"

    def __init__(self, text: str):
        self._text = text

    def generate(self, prompt, *, model=None, system=""):
        return AIResponse(text=self._text, model="m")

    def capabilities(self):
        return {}

    def health_check(self):
        return True


def _approving_services():
    return {
        "primary_provider": _FP('{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.8,"thesis":"strong pre-earnings setup"}'),
    }


def _critic_ok_services():
    s = _approving_services()
    s["critic_provider"] = _FP('{"veto":false,"categories":[],"severity":0.1,"rationale":"ok"}')
    return s


def _item():
    return {
        "symbol": "NVDA", "price": 850.0, "direction_bias": 40, "gap_risk": 70,
        "pre_earnings_score": 55, "pre_earnings_score_label": "MODERATE",
        "data_confidence": "HIGH", "coverage_score": 70, "coverage_label": "HIGH",
        "news_only_call": False, "earnings_time_utc": "2026-08-20T12:00:00+00:00",
        "session": "AMC", "expected_move_pct": 8.5, "historical_median_move_pct": 6.2,
    }


def test_context_assembles_all_ten_inputs_with_honest_stubs():
    ctx = build_pre_earnings_decision_context(_item(), services=_critic_ok_services())
    for key in ("pre_earnings", "strategy_evidence", "signal", "backtest",
                "ai", "critic", "data_quality", "portfolio", "risk", "capital"):
        assert key in ctx, f"missing §51 input: {key}"
    # Later-phase stubs are honest (available: False + reason), never faked numbers.
    for stub_key in ("strategy_evidence", "signal", "backtest", "portfolio", "capital"):
        stub = ctx[stub_key]
        assert stub["available"] is False
        assert "reason" in stub and stub["reason"]


def test_bridge_returns_evaluation_result_and_open_long_under_paper_auto():
    cfg = {"autonomy": {"mode": "PAPER_AUTO"}, "ai": {"decision": {"critic_enabled": True}}}
    result = evaluate_pre_earnings_decision(_item(), config=cfg, services=_critic_ok_services())
    assert isinstance(result, EvaluationResult)
    assert result.decision.action is Action.OPEN_LONG
    assert result.review["recommendation"] == "approve"


def test_analyze_mode_clamps_order_to_watch():
    cfg = {"autonomy": {"mode": "ANALYZE"}, "ai": {"decision": {"critic_enabled": True}}}
    result = evaluate_pre_earnings_decision(_item(), config=cfg, services=_critic_ok_services())
    assert result.decision.action is Action.WATCH
    assert "autonomy_mode_ANALYZE" in result.review["reason"]


def test_kill_switch_clamps_to_watch():
    cfg = {"autonomy": {"mode": "PAPER_AUTO", "kill_switch_active": True},
           "ai": {"decision": {"critic_enabled": True}}}
    result = evaluate_pre_earnings_decision(_item(), config=cfg, services=_critic_ok_services())
    assert result.decision.action is Action.WATCH
    assert result.review["reason"] == "kill_switch_active"


def test_critic_veto_stays_no_trade_not_upgraded_to_watch():
    s = _critic_ok_services()
    s["critic_provider"] = _FP('{"veto":true,"categories":["concentration"],"severity":0.9,"rationale":"concentrated"}')
    cfg = {"autonomy": {"mode": "PAPER_AUTO"}, "ai": {"decision": {"critic_enabled": True}}}
    result = evaluate_pre_earnings_decision(_item(), config=cfg, services=s)
    assert result.decision.action is Action.NO_TRADE
    assert result.review["recommendation"] == "avoid"


def test_ai_unavailable_maps_to_watch_not_no_trade():
    # No provider configured -> engine returns unavailable; Pre-Earnings maps to WATCH.
    cfg = {"autonomy": {"mode": "PAPER_AUTO"}}
    result = evaluate_pre_earnings_decision(_item(), config=cfg, services={})
    assert result.decision.action is Action.WATCH
    assert result.review["available"] is False


def test_live_auto_without_acceptance_clamps_order_to_watch():
    cfg = {"autonomy": {"mode": "LIVE_AUTO", "live_auto_owner_enabled": True,
                        "paper_auto_acceptance_passed": False},
           "ai": {"decision": {"critic_enabled": True}}}
    result = evaluate_pre_earnings_decision(_item(), config=cfg, services=_critic_ok_services())
    assert result.decision.action is Action.WATCH
    assert "live_auto_blocked" in result.review["reason"]


def test_pre_earnings_signal_input_is_real_when_item_has_signal():
    # §51 'signal' input: when the item carries real signal fields (§52/§53 overlay),
    # build_pre_earnings_decision_context surfaces a real Signal (available: True) with
    # the horizon resolved from the mode — not the Phase 6 honest stub.
    item = {"symbol": "NVDA", "direction": "LONG", "signal_score": 72.0,
            "mode": "MOMENTUM", "signal_type": "v1_logistic"}
    ctx = build_pre_earnings_decision_context(item, services={})
    sig = ctx["signal"]
    assert sig["available"] is True
    assert sig["horizon"] == "SWING"          # MOMENTUM → SWING (§53)
    assert sig["is_not_probability"] is True  # §55 — never display as probability
    assert sig["direction"] == "LONG"
    assert sig["score"] == 72.0


def test_pre_earnings_signal_input_honest_when_absent():
    # No signal fields on the item → honest stub (available: False + reason).
    ctx = build_pre_earnings_decision_context({"symbol": "NVDA"}, services={})
    assert ctx["signal"]["available"] is False
    assert ctx["signal"]["reason"] == "signal_not_available_on_item"