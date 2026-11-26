"""§46 — Live Auto must remain disabled until mandatory Paper Auto acceptance tests pass."""
from core.autonomy import can_enable_live_auto, paper_auto_acceptance_passed, run_paper_auto_acceptance


def _acceptance_services():
    """Fake services factory that makes the engine deterministic across all scenarios."""
    from core.ai.provider import AIResponse

    class _RA:
        def evaluate_signal(self, s):
            return {"accepted": True, "reason": "ok", "checks": [], "metrics": {}}

    class _Prov:
        name = "fake"

        def __init__(self, text):
            self.t = text

        def generate(self, prompt, *, model=None, system="", **opts):
            return AIResponse(text=self.t, model="m")

    def engine_svc(text, critic='{"veto":false,"categories":[],"severity":0.1,"rationale":"ok"}'):
        return {
            "evaluate_capital_entry": lambda s, p, c=None, **k: {"allowed": True, "check": "complete", "status": "validated", "reason": "ok", "checks": {}},
            "resolve_deployable_balance": lambda cm: 50000.0,
            "compute_size_factor": lambda score, cfg=None, **k: {"size_factor": 0.5},
            "calculate_trade_risk_pct": lambda **k: 0.3,
            "risk_agent": _RA(),
            "should_veto_trade": lambda review, config=None: False,
            "primary_provider": _Prov(text),
            "fallback_chain": [],
            "critic_provider": _Prov(critic),
            "critic_model": "critic-m",
        }
    return engine_svc


def test_paper_auto_acceptance_runs_all_scenarios_and_passes_with_fakes():
    report = run_paper_auto_acceptance({}, services=_acceptance_services())
    assert report["passed"] is True
    assert len(report["scenarios"]) >= 8


def test_live_auto_disabled_until_acceptance_and_owner():
    # No owner flag -> disabled even if acceptance would pass.
    ok, reason = can_enable_live_auto({"autonomy": {"live_auto_owner_enabled": False}}, services=_acceptance_services())
    assert ok is False
    # Owner flag + passing acceptance (services provided -> re-run battery) -> enabled.
    ok, reason = can_enable_live_auto({"autonomy": {"live_auto_owner_enabled": True}}, services=_acceptance_services())
    assert ok is True


def test_live_auto_runtime_requires_recorded_acceptance_flag():
    # At runtime (no services) LIVE_AUTO requires the recorded paper_auto_acceptance_passed flag.
    ok, reason = can_enable_live_auto({"autonomy": {"live_auto_owner_enabled": True}})
    assert ok is False
    assert "acceptance" in reason  # §46 — not yet passed
    ok2, reason2 = can_enable_live_auto({"autonomy": {"live_auto_owner_enabled": True, "paper_auto_acceptance_passed": True}})
    assert ok2 is True


def test_live_auto_disabled_if_acceptance_fails():
    from core.ai.provider import UNAVAILABLE

    class _Dead:
        name = "dead"
        def generate(self, prompt, *, model=None, system="", **opts):
            return UNAVAILABLE

    class _RA:
        def evaluate_signal(self, s):
            return {"accepted": True, "reason": "ok", "checks": [], "metrics": {}}

    def _failing_svc():
        return {
            "evaluate_capital_entry": lambda s, p, c=None, **k: {"allowed": True, "check": "complete", "status": "validated", "reason": "ok", "checks": {}},
            "resolve_deployable_balance": lambda cm: 50000.0,
            "compute_size_factor": lambda score, cfg=None, **k: {"size_factor": 0.5},
            "calculate_trade_risk_pct": lambda **k: 0.3,
            "risk_agent": _RA(),
            "should_veto_trade": lambda review, config=None: False,
            "primary_provider": _Dead(), "fallback_chain": [], "critic_provider": _Dead(), "critic_model": "m",
        }
    ok, reason = can_enable_live_auto({"autonomy": {"live_auto_owner_enabled": True}}, services=_failing_svc)
    # AI unavailable -> GOLD-014 -> NO_TRADE; open-action scenarios fail acceptance.
    assert ok is False


def test_paper_auto_acceptance_passed_returns_summary_tuple():
    passed, summary = paper_auto_acceptance_passed({}, services=_acceptance_services())
    assert passed is True
    assert "scenarios passed" in summary