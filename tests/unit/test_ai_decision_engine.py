"""Phase 4C decision engine — composes 3C/4A/4B into one backward-compatible entry point."""
from core.ai.decision import Action, Decision
from core.ai.decision_engine import EvaluationResult, evaluate_trade_decision
from core.ai.pipeline import SCHEMA
from core.ai.provider import AIResponse, UNAVAILABLE


class _FakeProvider:
    name = "fake"

    def __init__(self, text):
        self._text = text

    def generate(self, prompt, *, model=None, system="", **opts):
        self.last_model = model
        return AIResponse(text=self._text, model="m")


def _pipeline_fakes():
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
    }


def _svc(text='{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.85,"thesis":"mom"}',
         critic_text='{"veto":false,"categories":[],"severity":0.1,"rationale":"ok"}'):
    svc = _pipeline_fakes()
    svc["primary_provider"] = _FakeProvider(text)
    svc["fallback_chain"] = []
    svc["critic_provider"] = _FakeProvider(critic_text)
    svc["critic_model"] = "critic-m"
    return svc


def _signal():
    return {"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95, "signal_score": 0.8, "strategy_type": "SCANNER_AI_MOMENTUM"}


def test_engine_open_long_success_backward_compat_review_shape():
    res = evaluate_trade_decision(_signal(), {"accepted": True}, config={}, services=_svc())
    assert isinstance(res, EvaluationResult)
    assert res.ai_available is True
    assert res.decision.action is Action.OPEN_LONG
    assert res.validation.ok is True
    assert res.review["available"] is True
    assert res.review["recommendation"] == "approve"
    assert res.review["confidence"] == 0.85
    assert res.review["provider"] == "fake"
    assert res.review["leverage"] == 1  # §43 — AI no longer sets leverage
    assert res.critic is not None and res.critic.veto is False


def test_engine_ai_unavailable_returns_gold014_unavailable_review():
    svc = _pipeline_fakes()

    class _Dead:
        name = "dead"
        def generate(self, prompt, *, model=None, system="", **opts):
            return UNAVAILABLE
    svc["primary_provider"] = _Dead()
    svc["fallback_chain"] = []
    svc["critic_provider"] = _FakeProvider('{"veto":false}')
    res = evaluate_trade_decision(_signal(), {"accepted": True},
                                  config={"ai": {"all_unavailable_no_trade": True}}, services=svc)
    assert res.ai_available is False
    assert res.review["available"] is False
    assert res.decision.action is Action.NO_TRADE
    assert res.critic is None  # critic not run when AI unavailable


def test_engine_parse_fail_returns_unavailable_review():
    svc = _svc(text="the chart looks great")
    res = evaluate_trade_decision(_signal(), {"accepted": True}, config={}, services=svc)
    assert res.ai_available is False
    assert "invalid_json" in (res.review.get("fallback_used") or "")
    assert res.decision.action is Action.NO_TRADE


def test_engine_critic_veto_forces_no_trade_review_avoid():
    svc = _svc(critic_text='{"veto":true,"categories":["concentration"],"severity":0.96,"rationale":"concentrated"}')
    res = evaluate_trade_decision(_signal(), {"accepted": True}, config={}, services=svc)
    assert res.decision.action is Action.NO_TRADE
    # decisive rejection -> available=True, recommendation="avoid" (NOT unavailable)
    assert res.review["available"] is True
    assert res.review["recommendation"] == "avoid"
    assert res.critic.veto is True


def test_engine_moderate_critic_concern_reduces_instead_of_blocking_good_setup():
    svc = _svc(
        critic_text=(
            '{"veto":true,"categories":["catalyst_risk"],"severity":0.78,'
            '"challenges":["ordinary event uncertainty"],"rationale":"reduce size"}'
        )
    )
    signal = _signal()
    signal["signal_score"] = 84.0
    # Wave 3 (2026-11-15): only a measured validation score may override a
    # model veto; a rule score alone no longer does.
    signal["validation_score"] = 84.0

    res = evaluate_trade_decision(signal, {"accepted": True}, config={}, services=svc)

    assert res.decision.action is Action.OPEN_LONG
    assert res.critic.model_veto is True
    assert res.critic.veto is False
    assert res.review["recommendation"] == "reduce"
    assert 0.5 <= res.review["size_factor"] < 1.0


def test_engine_watch_skips_critic():
    svc = _svc(text='{"action":"WATCH","ticker":"NVDA","confidence":0.4}')
    res = evaluate_trade_decision(_signal(), {"accepted": True}, config={}, services=svc)
    assert res.decision.action is Action.WATCH
    assert res.review["recommendation"] == "watch"
    assert res.critic is None  # WATCH skips critic


def test_engine_no_trade_decision_maps_to_avoid():
    svc = _svc(text='{"action":"NO_TRADE","ticker":"NVDA","confidence":0.7}')
    res = evaluate_trade_decision(_signal(), {"accepted": True}, config={}, services=svc)
    assert res.decision.action is Action.NO_TRADE
    assert res.review["available"] is True
    assert res.review["recommendation"] == "avoid"


def test_engine_accepts_documented_legacy_trade_review_payload():
    svc = _svc(
        text=(
            '{"recommendation":"reduce","confidence":0.82,'
            '"size_factor":0.6,"leverage":4,"sentiment":"bullish",'
            '"supports_direction":true,"reason":"valid but size down","risks":["event"]}'
        )
    )
    signal = _signal()
    signal["action"] = "BUY"

    res = evaluate_trade_decision(signal, {"accepted": True}, config={}, services=svc)

    assert res.validation.ok is True
    assert res.decision.action is Action.OPEN_LONG
    assert res.review["recommendation"] == "reduce"
    assert res.review["size_factor"] == 0.6
    assert res.review["leverage"] == 1


def test_engine_strict_schema_reject_records_stage():
    svc = _svc(text='{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.8,"price":850.0}')
    res = evaluate_trade_decision(_signal(), {"accepted": True},
                                  config={"ai": {"decision": {"strict_schema": True}}}, services=svc)
    assert res.validation.ok is False
    assert res.validation.stage == SCHEMA
    assert res.review["available"] is False
    assert "price" in (res.validation.reason or "")


def test_engine_no_provider_configured_returns_unavailable():
    svc = _pipeline_fakes()  # no primary_provider key, empty config -> none resolvable
    res = evaluate_trade_decision(_signal(), {"accepted": True}, config={}, services=svc)
    assert res.ai_available is False
    assert res.decision.action is Action.NO_TRADE
    assert "no_provider" in (res.review.get("fallback_used") or "")


def test_engine_routes_configured_role_model(monkeypatch):
    provider = _FakeProvider(
        '{"action":"NO_TRADE","ticker":"NVDA","confidence":0.5,"thesis":"wait"}'
    )
    monkeypatch.setattr("api.ai_config_service.build_provider", lambda _cfg, _name: provider)
    cfg = {
        "ai": {
            "providers": {"ollama": {"enabled": True}},
            "assignment": {
                "market_analyst": "market-model",
                "pre_earnings_analyst": "earnings-model",
            },
        }
    }

    evaluate_trade_decision(
        _signal(),
        {"accepted": True},
        config=cfg,
        services=_pipeline_fakes(),
        role="pre_earnings_analyst",
    )

    assert provider.last_model == "earnings-model"
