"""Unit tests for core.ai.pipeline — §42 6-stage validation pipeline (Phase 4B)."""
import pytest

from core.ai.decision import Action, Decision
from core.ai.pipeline import (
    AUTHORITATIVE_DATA_CHECK,
    PARSE,
    POLICY,
    RISK_GATE,
    SCHEMA,
    SEMANTIC,
    STAGE_ORDER,
    StageResult,
    ValidationResult,
    _real_authoritative_services,
    run_pipeline,
)


def test_stage_order_matches_spec_42():
    assert STAGE_ORDER == (
        PARSE, SCHEMA, SEMANTIC, AUTHORITATIVE_DATA_CHECK, POLICY, RISK_GATE,
    )
    # §42 verbatim order
    assert STAGE_ORDER == (
        "PARSE", "SCHEMA", "SEMANTIC",
        "AUTHORITATIVE_DATA_CHECK", "POLICY", "RISK_GATE",
    )


def test_run_pipeline_invalid_json_returns_no_trade_at_parse():
    res = run_pipeline("not json", signal={}, positions=[], config={})
    assert res.ok is False
    assert res.stage == PARSE
    assert res.decision is not None and res.decision.action == Action.NO_TRADE
    assert "invalid_json" in (res.reason or "")


def test_run_pipeline_strips_authoritative_fields_at_schema():
    raw = {"action": "OPEN_LONG", "ticker": "NVDA", "confidence": 0.8, "price": 850.0}
    # strict_schema on → SCHEMA rejects price
    res = run_pipeline(raw, signal={}, positions=[],
                       config={"ai": {"decision": {"strict_schema": True}}})
    assert res.ok is False
    assert res.stage == SCHEMA
    assert "price" in res.reason


def test_run_pipeline_semantic_fails_open_action_without_ticker():
    # OPEN_LONG requires a ticker (semantic rule)
    raw = {"action": "OPEN_LONG", "horizon": "SWING", "confidence": 0.8}
    res = run_pipeline(raw, signal={}, positions=[], config={})
    assert res.ok is False
    assert res.stage == SEMANTIC
    assert "ticker" in res.reason.lower()


def test_run_pipeline_no_trade_action_passes_semantic_without_ticker():
    raw = {"action": "NO_TRADE", "thesis": "Conflicting signals."}

    class _RA:
        def evaluate_signal(self, s):
            return {"accepted": True, "reason": "passed", "checks": [], "metrics": {}}

    # AUTHORITATIVE/POLICY/RISK_GATE wired in Tasks 4–5; inject §43 fakes to stay hermetic
    services = {
        "evaluate_capital_entry": lambda s, p, c=None, **k: {"allowed": True, "check": "complete", "status": "validated", "reason": "ok", "checks": {}},
        "resolve_deployable_balance": lambda cm: 0.0,
        "compute_size_factor": lambda score, cfg=None, **k: {"size_factor": 0.0},
        "calculate_trade_risk_pct": lambda **k: 0.0,
        "risk_agent": _RA(),
        "should_veto_trade": lambda review, config=None: False,
    }
    res = run_pipeline(raw, signal={}, positions=[], config={}, services=services)
    assert res.ok is True
    assert res.stage != SEMANTIC  # passed beyond semantic
    assert res.decision.action == Action.NO_TRADE


def test_run_pipeline_stages_are_injectable_and_called_in_order():
    order = []

    def fake_parse(ctx):
        order.append(PARSE)
        return StageResult(passed=True, decision=Decision(action=Action.WATCH))

    def fake_schema(ctx):
        order.append(SCHEMA)
        return StageResult(passed=True, decision=ctx.decision)

    def fake_sem(ctx):
        order.append(SEMANTIC)
        return StageResult(passed=True, decision=ctx.decision)

    def fake_auth(ctx):
        order.append(AUTHORITATIVE_DATA_CHECK)
        return StageResult(passed=True, decision=ctx.decision)

    def fake_pol(ctx):
        order.append(POLICY)
        return StageResult(passed=True, decision=ctx.decision)

    def fake_risk(ctx):
        order.append(RISK_GATE)
        return StageResult(passed=True, decision=ctx.decision)

    services = {
        PARSE: fake_parse, SCHEMA: fake_schema, SEMANTIC: fake_sem,
        AUTHORITATIVE_DATA_CHECK: fake_auth, POLICY: fake_pol, RISK_GATE: fake_risk,
    }
    res = run_pipeline("ignored", signal={}, positions=[], config={}, services=services)
    assert res.ok is True
    assert order == list(STAGE_ORDER)


def test_run_pipeline_first_failing_stage_short_circuits():
    def fail_semantic(ctx):
        return StageResult(passed=False, reason="bad semantics")

    # valid decision so real PARSE+SCHEMA pass; the injected SEMANTIC fails
    raw = {"action": "OPEN_LONG", "ticker": "NVDA", "confidence": 0.8}
    services = {SEMANTIC: fail_semantic}
    res = run_pipeline(raw, signal={}, positions=[], config={}, services=services)
    assert res.ok is False
    assert res.stage == SEMANTIC
    assert res.reason == "bad semantics"


def test_validation_result_and_stage_result_frozen():
    vr = ValidationResult(ok=True, decision=Decision(action=Action.WATCH), stage="OK")
    with pytest.raises(Exception):
        vr.ok = False
    sr = StageResult(passed=True, decision=Decision(action=Action.WATCH))
    with pytest.raises(Exception):
        sr.passed = False

# ---- Task 4: AUTHORITATIVE_DATA_CHECK (§43 recalc from app services) ----

from core.ai.pipeline import default_services


def _ok_decision():
    return Decision(action=Action.OPEN_LONG, ticker="NVDA", confidence=0.8,
                    strategy_id="momentum_v3")


def test_authoritative_stage_fails_when_capital_entry_blocked():
    raw = {"action": "OPEN_LONG", "ticker": "NVDA", "confidence": 0.8}

    def block_entry(signal, positions, config=None, *, perf_cache=None, news_pulse=None):
        return {"allowed": False, "check": "data", "status": "stale_signal",
                "reason": "Signal older than max age", "checks": {"data": {"allowed": False}}}

    services = default_services({})
    services["evaluate_capital_entry"] = block_entry
    res = run_pipeline(raw, signal={"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95,
                                    "strategy_type": "SCANNER_AI_MOMENTUM"},
                      positions=[], config={}, services=services)
    assert res.ok is False
    assert res.stage == AUTHORITATIVE_DATA_CHECK
    assert "stale_signal" in res.reason or "Signal older" in res.reason


def test_authoritative_stage_recalculates_43_numbers_from_app_services():
    raw = {"action": "OPEN_LONG", "ticker": "NVDA", "confidence": 0.8,
           "position_size": 99999, "allowed_capital": 99999}  # §43 AI junk, must be ignored

    def ok_entry(signal, positions, config=None, *, perf_cache=None, news_pulse=None):
        return {"allowed": True, "check": "complete", "status": "validated",
                "reason": "ok", "checks": {}}

    def deployable(cm):
        return 50000.0

    def size_factor(score, cfg=None, **kw):
        return {"size_factor": 0.7}

    def risk_pct(*, notional_usd, stop_loss_fraction, equity):
        return notional_usd * stop_loss_fraction / equity * 100.0

    class FakeRiskAgent:
        def evaluate_signal(self, signal):
            return {"accepted": True, "reason": "passed", "checks": [], "metrics": {"risk_per_trade_pct": 0.4}}

    services = default_services({})
    services.update({
        "evaluate_capital_entry": ok_entry,
        "resolve_deployable_balance": deployable,
        "compute_size_factor": size_factor,
        "calculate_trade_risk_pct": risk_pct,
        "risk_agent": FakeRiskAgent(),
        "should_veto_trade": lambda review, config=None: False,
    })
    res = run_pipeline(raw, signal={"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95,
                                    "signal_score": 0.8, "strategy_type": "SCANNER_AI_MOMENTUM"},
                      positions=[], config={}, services=services)
    assert res.ok is True
    auth = res.checks.get("authoritative")
    assert auth is not None
    # §43: app-service outputs, NOT the AI's 99999 junk
    assert auth["allowed_capital"] == 50000.0
    assert auth["size_factor"] == 0.7
    # the AI's authoritative numbers did not leak
    assert auth.get("position_size") != 99999
    assert auth.get("allowed_capital") != 99999


def test_authoritative_stage_records_capital_entry_checks_even_on_pass():
    raw = {"action": "NO_TRADE", "thesis": "no edge"}

    def ok_entry(signal, positions, config=None, *, perf_cache=None, news_pulse=None):
        return {"allowed": True, "check": "complete", "status": "validated",
                "reason": "ok", "checks": {"data": {"allowed": True},
                                            "backtest": {"allowed": True}}}

    services = default_services({})
    services["evaluate_capital_entry"] = ok_entry
    services["resolve_deployable_balance"] = lambda cm: 0.0
    services["compute_size_factor"] = lambda score, cfg=None, **k: {"size_factor": 0.0}
    services["calculate_trade_risk_pct"] = lambda **k: 0.0

    class _RA:
        def evaluate_signal(self, s):
            return {"accepted": True, "checks": [], "metrics": {}}

    services["risk_agent"] = _RA()
    services["should_veto_trade"] = lambda review, config=None: False
    res = run_pipeline(raw, signal={}, positions=[], config={}, services=services)
    assert res.ok is True
    assert res.checks["authoritative"]["capital_entry"]["status"] == "validated"


def test_authoritative_stage_admits_scanner_experiment_from_full_paper_config():
    """Regression: full app config must not make AI reject a paper scanner signal."""
    from datetime import datetime, timezone

    from core.capital_entry_guard import evaluate_capital_entry

    class _RA:
        def evaluate_signal(self, signal):
            return {"accepted": True, "reason": "passed", "checks": [], "metrics": {}}

    services = default_services({})
    services.update(
        {
            "evaluate_capital_entry": evaluate_capital_entry,
            "resolve_deployable_balance": lambda cm: 10_000.0,
            "compute_size_factor": lambda score, cfg=None, **k: {"size_factor": 0.25},
            "calculate_trade_risk_pct": lambda **k: 0.1,
            "risk_agent": _RA(),
            "should_veto_trade": lambda review, config=None: False,
        }
    )
    config = {
        "agents": {
            "execution": {
                "mode": "paper",
                "allow_unvalidated_autonomous_fallback": True,
                "capital_entry_gate": {
                    "enabled": True,
                    "data": {
                        "max_signal_age_seconds": 300,
                        "min_risk_reward_ratio": 1.1,
                        "require_price_integrity": True,
                    },
                },
            }
        }
    }
    signal = {
        "ticker": "BTC-USD",
        "action": "BUY",
        "entry": 100.0,
        "tp": 106.0,
        "sl": 96.0,
        "signal_score": 90.0,
        "strategy_type": "SCANNER_AI_LONG",
        "strategy_source": "SCANNER_PRICE_FIRST_V1",
        "signal_mode": "AUTONOMOUS_SCANNER",
        "autonomous_source": "scanner",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "price_verified": True,
        "price_integrity": {"status": "verified", "is_verified": True},
    }

    result = run_pipeline(
        {"action": "OPEN_LONG", "ticker": "BTC-USD", "confidence": 0.85},
        signal=signal,
        positions=[],
        config=config,
        services=services,
    )

    assert result.ok is True
    capital = result.checks["authoritative"]["capital_entry"]
    assert capital["allowed"] is True
    assert capital["status"] == "paper_only"


def test_real_authoritative_services_resolve_actual_capital_balance(monkeypatch):
    class _Capital:
        def get_deployable_balance(self):
            return 12_345.0

    class _Risk:
        def __init__(self, config_manager):
            self.config_manager = config_manager

    monkeypatch.setattr("core.capital_manager.get_capital_manager", lambda: _Capital())
    monkeypatch.setattr("agents.risk_agent.RiskAgent", _Risk)

    services = _real_authoritative_services({})

    assert services["resolve_deployable_balance"](None) == 12_345.0
    assert not hasattr(services["risk_agent"].config_manager, "get_balance")


# ---- Task 5: POLICY + RISK_GATE (RiskAgent + should_veto_trade unchanged) ----

def test_policy_stage_fails_when_risk_agent_rejects():
    raw = {"action": "OPEN_LONG", "ticker": "NVDA", "confidence": 0.8}

    class _RA:
        def evaluate_signal(self, s):
            return {"accepted": False, "reason": "max_exposure_pct",
                    "checks": [{"check": "max_exposure_pct", "passed": False, "detail": "over"}],
                    "metrics": {}}

    services = default_services({})
    services["evaluate_capital_entry"] = lambda s, p, c=None, **k: {"allowed": True, "check": "complete", "status": "validated", "reason": "ok", "checks": {}}
    services["resolve_deployable_balance"] = lambda cm: 50000.0
    services["compute_size_factor"] = lambda score, cfg=None, **k: {"size_factor": 0.5}
    services["calculate_trade_risk_pct"] = lambda **k: 0.3
    services["risk_agent"] = _RA()
    services["should_veto_trade"] = lambda review, config=None: False
    res = run_pipeline(raw, signal={"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95,
                                    "signal_score": 0.8, "strategy_type": "SCANNER_AI_MOMENTUM"},
                      positions=[], config={}, services=services)
    assert res.ok is False
    assert res.stage == POLICY
    assert "max_exposure_pct" in res.reason


def test_risk_gate_forces_no_trade_when_should_veto_trade_returns_true():
    raw = {"action": "OPEN_LONG", "ticker": "NVDA", "confidence": 0.9}

    class _RA:
        def evaluate_signal(self, s):
            return {"accepted": True, "reason": "passed", "checks": [], "metrics": {}}

    def veto(review, config=None):
        # the adapter maps OPEN_LONG -> "approve"; force a veto to prove the stage honors it
        return True

    services = default_services({})
    services["evaluate_capital_entry"] = lambda s, p, c=None, **k: {"allowed": True, "check": "complete", "status": "validated", "reason": "ok", "checks": {}}
    services["resolve_deployable_balance"] = lambda cm: 50000.0
    services["compute_size_factor"] = lambda score, cfg=None, **k: {"size_factor": 0.5}
    services["calculate_trade_risk_pct"] = lambda **k: 0.3
    services["risk_agent"] = _RA()
    services["should_veto_trade"] = veto
    res = run_pipeline(raw, signal={"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95,
                                    "signal_score": 0.8, "strategy_type": "SCANNER_AI_MOMENTUM"},
                      positions=[], config={}, services=services)
    assert res.ok is True            # veto forces the decision to NO_TRADE, not a pipeline failure
    assert res.decision.action == Action.NO_TRADE
    assert res.checks.get("risk_gate", {}).get("vetoed") is True


def test_risk_gate_uses_real_should_veto_trade_signature_unchanged():
    """should_veto_trade is called UNCHANGED with a review dict (available/recommendation/confidence)
    built by the adapter — verify the real function is importable and accepts that shape."""
    from core.ollama_decision import should_veto_trade
    review = {"available": True, "recommendation": "avoid", "confidence": 0.9}
    cfg = {"ollama": {"decision_veto_enabled": True, "decision_min_confidence": 0.65}}
    assert should_veto_trade(review, cfg) is True
    assert should_veto_trade({"available": True, "recommendation": "approve", "confidence": 0.9}, cfg) is False


def test_risk_gate_maps_no_trade_action_to_avoid_recommendation():
    """Adapter: NO_TRADE -> 'avoid'; other actions -> 'approve' (semantic mapping)."""
    raw = {"action": "NO_TRADE", "thesis": "conflict"}

    class _RA:
        def evaluate_signal(self, s):
            return {"accepted": True, "reason": "passed", "checks": [], "metrics": {}}

    seen = {}

    def veto(review, config=None):
        seen["recommendation"] = review.get("recommendation")
        seen["confidence"] = review.get("confidence")
        seen["available"] = review.get("available")
        return False

    services = default_services({})
    services["evaluate_capital_entry"] = lambda s, p, c=None, **k: {"allowed": True, "check": "complete", "status": "validated", "reason": "ok", "checks": {}}
    services["resolve_deployable_balance"] = lambda cm: 0.0
    services["compute_size_factor"] = lambda score, cfg=None, **k: {"size_factor": 0.0}
    services["calculate_trade_risk_pct"] = lambda **k: 0.0
    services["risk_agent"] = _RA()
    services["should_veto_trade"] = veto
    res = run_pipeline(raw, signal={}, positions=[], config={}, services=services)
    assert res.ok is True
    assert seen["recommendation"] == "avoid"      # NO_TRADE mapped to avoid
    assert seen["available"] is True
    assert seen["confidence"] == 0.0
