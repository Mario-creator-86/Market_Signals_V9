"""evaluate_trade_decision_with_ollama delegates to the 4C engine and preserves the review dict."""
from core import ollama_decision as od
from core.ai.decision import Action
from core.ai.provider import AIResponse


class _FakeProvider:
    name = "fake"

    def __init__(self, text):
        self._text = text

    def generate(self, prompt, *, model=None, system="", **opts):
        return AIResponse(text=self._text, model="m")


def _svc(text='{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.8,"thesis":"mom"}'):
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
        "primary_provider": _FakeProvider(text),
        "fallback_chain": [],
        "critic_provider": _FakeProvider('{"veto":false,"categories":[],"severity":0.1,"rationale":"ok"}'),
        "critic_model": "critic-m",
    }


def _signal():
    return {"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95, "signal_score": 0.8, "strategy_type": "SCANNER_AI_MOMENTUM"}


def test_evaluate_trade_decision_with_ollama_delegates_to_engine():
    review = od.evaluate_trade_decision_with_ollama(_signal(), {"accepted": True}, config={}, services=_svc())
    assert review["available"] is True
    assert review["recommendation"] == "approve"
    assert review["confidence"] == 0.8
    assert review["decision"].action is Action.OPEN_LONG


def test_evaluate_trade_decision_with_ollama_disabled_returns_unavailable():
    review = od.evaluate_trade_decision_with_ollama(_signal(), {"accepted": True},
                                                     config={"ollama": {"decision_enabled": False}})
    assert review["available"] is False


def test_evaluate_trade_decision_with_ollama_critic_veto_maps_to_avoid():
    svc = _svc()
    svc["critic_provider"] = _FakeProvider('{"veto":true,"categories":["concentration"],"severity":0.9,"rationale":"conc"}')
    review = od.evaluate_trade_decision_with_ollama(_signal(), {"accepted": True}, config={}, services=svc)
    assert review["available"] is True
    assert review["recommendation"] == "avoid"
    assert review["critic_veto"] is True


def test_legacy_ollama_decision_helpers_still_present():
    # §5/§7 — the unchanged helpers still import and work.
    assert od.default_trade_review(available=False)["recommendation"] == "watch"
    assert callable(od.should_veto_trade)
    assert callable(od.parse_trade_review)
    assert callable(od.build_trade_review_prompt)