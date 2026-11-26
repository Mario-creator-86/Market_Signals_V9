"""Phase 4B — §42 validation pipeline.

PARSE -> SCHEMA -> SEMANTIC -> AUTHORITATIVE DATA CHECK -> POLICY -> RISK GATE
(prompt_to_follow.md §42). AI prose alone never produces an order: the pipeline
returns a validated Decision (or NO_TRADE); the side-effecting broker surface
(open_position/close_position/process_signal) is never reached from here.

The stages COMPOSE existing functions (§5/§7 evolve-not-rewrite):
  PARSE/SCHEMA  -> core.ai.decision.parse_decision (evolves parse_trade_review)
  AUTHORITATIVE -> core.capital_entry_guard.evaluate_capital_entry +
                  evaluate_autonomous_performance + evaluate_autonomous_news (called
                  inside evaluate_capital_entry) + §43 recalc (Task 4)
  POLICY/RISK   -> agents.risk_agent.RiskAgent.evaluate_signal +
                  core.risk_limits.calculate_trade_risk_pct +
                  ollama_decision.should_veto_trade (unchanged) (Task 5)

Stage functions are injectable via `services` (design decision 8) so §42/§43/§44
become executable unit tests with fakes (no live market/broker/AI).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Optional

from core.ai.decision import Action, Decision, parse_decision

# §42 stage names, in spec order
PARSE = "PARSE"
SCHEMA = "SCHEMA"
SEMANTIC = "SEMANTIC"
AUTHORITATIVE_DATA_CHECK = "AUTHORITATIVE_DATA_CHECK"
POLICY = "POLICY"
RISK_GATE = "RISK_GATE"
STAGE_ORDER = (PARSE, SCHEMA, SEMANTIC, AUTHORITATIVE_DATA_CHECK, POLICY, RISK_GATE)


@dataclass(frozen=True)
class StageResult:
    passed: bool
    decision: Optional[Decision] = None
    reason: str = ""
    checks: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    decision: Optional[Decision]
    stage: str
    reason: str = ""
    checks: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _StageContext:
    """Mutable carrier threaded through the stages in one pipeline run."""
    raw_decision: Any
    signal: Dict[str, Any]
    positions: Iterable[Dict[str, Any]]
    config: Dict[str, Any]
    decision: Optional[Decision] = None
    checks: Dict[str, Any] = field(default_factory=dict)
    services: Dict[str, Any] = field(default_factory=dict)  # merged stage + §43 service callables


# ---- Stage implementations (PARSE/SCHEMA/SEMANTIC real; later stages wired in Tasks 4–5) ----

def stage_parse(ctx: _StageContext) -> StageResult:
    # Parse without strict_schema: §43 authoritative fields are stripped and recorded
    # in decision.rejected_fields. The strict rejection is a SCHEMA-stage concern so
    # the pipeline attributes it to SCHEMA (the loop names the stage).
    decision = parse_decision(ctx.raw_decision, strict_schema=False)
    if decision.raw.get("parse_error") == "invalid_json":
        return StageResult(passed=False, decision=decision,
                           reason="PARSE: invalid_json (no JSON object found)",
                           checks={"stage": PARSE})
    ctx.decision = decision
    return StageResult(passed=True, decision=decision)


def stage_schema(ctx: _StageContext) -> StageResult:
    # §43 authoritative fields are stripped inside parse_decision; rejected_fields
    # records them. In strict_schema mode their presence is a schema violation
    # attributed to this stage.
    decision = ctx.decision
    if decision is None:
        return StageResult(passed=False, reason="SCHEMA: no decision from PARSE")
    if decision.rejected_fields:
        ctx.checks["rejected_authoritative_fields"] = list(decision.rejected_fields)
    strict = bool(ctx.config.get("ai", {}).get("decision", {}).get("strict_schema", False))
    if strict and decision.rejected_fields:
        return StageResult(
            passed=False, decision=decision,
            reason="SCHEMA: §43 authoritative field(s) rejected: "
                   + ", ".join(decision.rejected_fields),
            checks={"rejected_authoritative_fields": list(decision.rejected_fields)},
        )
    return StageResult(passed=True, decision=decision)


def stage_semantic(ctx: _StageContext) -> StageResult:
    decision = ctx.decision
    if decision is None:
        return StageResult(passed=False, reason="SEMANTIC: no decision")
    needs_ticker = decision.action in {
        Action.OPEN_LONG, Action.OPEN_SHORT, Action.HOLD, Action.REDUCE, Action.CLOSE,
    }
    if needs_ticker and not decision.ticker:
        return StageResult(
            passed=False, decision=decision,
            reason=f"SEMANTIC: action {decision.action.value} requires a ticker",
        )
    if not (0.0 <= decision.confidence <= 1.0):
        return StageResult(passed=False, decision=decision,
                           reason="SEMANTIC: confidence out of [0,1]")
    return StageResult(passed=True, decision=decision)


def _real_authoritative_services(config: Dict[str, Any]) -> Dict[str, Any]:
    """Lazy real §43 service callables. Imported here (not at module top) so that
    importing core.ai.pipeline never triggers heavy/coupled imports."""
    from core.capital_entry_guard import evaluate_capital_entry
    from core.capital_manager import get_capital_manager, resolve_deployable_balance
    from core.position_sizing import compute_size_factor
    from core.risk_limits import calculate_trade_risk_pct
    from agents.risk_agent import RiskAgent
    from core.config import ConfigManager
    from core.ollama_decision import should_veto_trade

    config_manager = ConfigManager()
    capital_manager = get_capital_manager()
    return {
        "evaluate_capital_entry": evaluate_capital_entry,
        "resolve_deployable_balance": lambda _cm: resolve_deployable_balance(capital_manager),
        "compute_size_factor": compute_size_factor,
        "calculate_trade_risk_pct": calculate_trade_risk_pct,
        "risk_agent": RiskAgent(config_manager),
        "should_veto_trade": should_veto_trade,
    }


_AUTHORITATIVE_SERVICE_KEYS = (
    "evaluate_capital_entry", "resolve_deployable_balance",
    "compute_size_factor", "calculate_trade_risk_pct",
    "risk_agent", "should_veto_trade",
)


def _ensure_authoritative_services(ctx: _StageContext) -> None:
    """Lazily fill any missing §43 service callables with the real implementations,
    cached on ctx.services. Injected fakes are NEVER overwritten — only missing keys
    are filled. Tests that inject fakes for all six keys never build the real block
    (no ConfigManager/CapitalManager constructed)."""
    if all(k in ctx.services and ctx.services[k] is not None for k in _AUTHORITATIVE_SERVICE_KEYS):
        return
    if ctx.checks.get("_real_svc_built"):
        return
    ctx.checks["_real_svc_built"] = True
    try:
        real = _real_authoritative_services(ctx.config)
    except Exception:
        real = {}
    for k, v in real.items():
        if k not in ctx.services or ctx.services[k] is None:
            ctx.services[k] = v


def stage_authoritative_data_check(ctx: _StageContext) -> StageResult:
    """§42 AUTHORITATIVE DATA CHECK + §43 recalc.

    Composes core.capital_entry_guard.evaluate_capital_entry (which internally runs
    evaluate_entry_data -> evaluate_backtest_validation -> evaluate_autonomous_performance
    -> evaluate_autonomous_news) and then recalculates the §43 authoritative numbers
    from app services — allowed capital (resolve_deployable_balance), position size
    (compute_size_factor), trade risk (calculate_trade_risk_pct), portfolio risk
    (RiskAgent.evaluate_signal). AI-supplied authoritative numbers were already
    stripped at SCHEMA; the values stored here are app-calculated only."""
    if ctx.decision is None:
        return StageResult(passed=False, reason="AUTHORITATIVE: no decision")

    _ensure_authoritative_services(ctx)
    svc = ctx.services
    signal = ctx.signal or {}
    positions = list(ctx.positions or [])

    # §43: the numbers come from app services or not at all. When the services
    # could not be built there is nothing authoritative to check the decision
    # against, so the stage fails closed with a reason instead of a KeyError.
    missing = [
        k for k in _AUTHORITATIVE_SERVICE_KEYS
        if k != "should_veto_trade" and svc.get(k) is None  # the veto fn has its own lazy fallback
    ]
    if missing:
        ctx.checks.pop("_real_svc_built", None)
        return StageResult(
            passed=False, decision=ctx.decision,
            reason="AUTHORITATIVE: app services unavailable (" + ", ".join(missing) + ")",
            checks={"authoritative": {"available": False, "missing_services": missing}},
        )

    entry = svc["evaluate_capital_entry"](signal, positions, ctx.config)
    if not entry.get("allowed", False):
        return StageResult(
            passed=False, decision=ctx.decision,
            reason=f"AUTHORITATIVE: capital_entry blocked — {entry.get('check','?')}: {entry.get('reason','')}",
            checks={"authoritative": {"capital_entry": entry}},
        )

    # §43 recalc from app services (never from the AI Decision)
    deployable = svc["resolve_deployable_balance"](None)
    size = svc["compute_size_factor"](signal.get("signal_score", 0.0), ctx.config)
    equity = deployable if deployable > 0 else 1.0
    stop_frac = 0.0
    try:
        entry_px = float(signal.get("entry") or 0.0)
        sl_px = float(signal.get("sl") or 0.0)
        if entry_px > 0 and sl_px > 0:
            stop_frac = abs(entry_px - sl_px) / entry_px
    except (TypeError, ValueError):
        stop_frac = 0.0
    notional = deployable * float(size.get("size_factor", 0.0))
    trade_risk_pct = svc["calculate_trade_risk_pct"](
        notional_usd=notional, stop_loss_fraction=stop_frac, equity=equity,
    )
    risk_eval = svc["risk_agent"].evaluate_signal(signal)

    authoritative = {
        "capital_entry": entry,
        "allowed_capital": deployable,
        "size_factor": size.get("size_factor", 0.0),
        "trade_risk_pct": trade_risk_pct,
        "portfolio_risk": risk_eval,
    }
    ctx.checks.pop("_real_svc_built", None)  # internal bookkeeping, not a check output
    ctx.checks["authoritative"] = authoritative
    # §42: AUTHORITATIVE records the §43 numbers; the risk_agent accept/reject decision
    # is the POLICY stage's gate, not this stage's. Capital-entry block is the only
    # AUTHORITATIVE short-circuit (data provenance / OOS validation / realized edge).
    return StageResult(passed=True, decision=ctx.decision, checks={"authoritative": authoritative})


def stage_policy(ctx: _StageContext) -> StageResult:
    """§42 POLICY — agents.risk_agent.RiskAgent.evaluate_signal + risk_limits.

    Verifies the signal passes the portfolio risk policy. The risk_agent is the
    same one the AUTHORITATIVE stage used; we call it again here as the named POLICY
    stage (§42 separates AUTHORITATIVE data from POLICY validation). On reject,
    short-circuit with the risk_agent reason."""
    if ctx.decision is None:
        return StageResult(passed=False, reason="POLICY: no decision")
    _ensure_authoritative_services(ctx)
    svc = ctx.services
    risk_eval = svc["risk_agent"].evaluate_signal(ctx.signal or {})
    ctx.checks["policy"] = risk_eval
    if not risk_eval.get("accepted", False):
        return StageResult(
            passed=False, decision=ctx.decision,
            reason=f"POLICY: {risk_eval.get('reason','risk_agent_rejected')}",
            checks={"policy": risk_eval},
        )
    return StageResult(passed=True, decision=ctx.decision, checks={"policy": risk_eval})


# Decision -> legacy review-shape adapter for the UNCHANGED should_veto_trade (§42 RISK GATE)
_ACTION_TO_RECOMMENDATION = {
    Action.NO_TRADE: "avoid",
    Action.REDUCE: "reduce",
    Action.WATCH: "watch",
    Action.OPEN_LONG: "approve",
    Action.OPEN_SHORT: "approve",
    Action.HOLD: "approve",
    Action.CLOSE: "approve",
}


def _decision_to_review(decision: Decision) -> Dict[str, Any]:
    """Map a §41 Decision to the legacy review dict shape should_veto_trade expects:
    {available, recommendation, confidence}. should_veto_trade itself is UNCHANGED."""
    return {
        "available": True,
        # An action outside the map is never read as an approval.
        "recommendation": _ACTION_TO_RECOMMENDATION.get(decision.action, "watch"),
        "confidence": decision.confidence,
    }


def stage_risk_gate(ctx: _StageContext) -> StageResult:
    """§42 RISK GATE — ollama_decision.should_veto_trade (UNCHANGED).

    A veto forces the decision to NO_TRADE. The pipeline result is still ok=True
    (a NO_TRADE is a valid validated outcome, §44 'valid final result can be NO TRADE'),
    but the decision action is overridden and risk_gate['vetoed']=True is recorded."""
    if ctx.decision is None:
        return StageResult(passed=False, reason="RISK_GATE: no decision")
    _ensure_authoritative_services(ctx)
    svc = ctx.services
    veto_fn = svc.get("should_veto_trade")
    if veto_fn is None:
        from core.ollama_decision import should_veto_trade as veto_fn  # lazy real fallback
    review = _decision_to_review(ctx.decision)
    vetoed = bool(veto_fn(review, ctx.config))
    gate = {"vetoed": vetoed, "review": review}
    ctx.checks["risk_gate"] = gate
    if vetoed:
        forced = Decision(
            action=Action.NO_TRADE,
            ticker=ctx.decision.ticker,
            horizon=ctx.decision.horizon,
            confidence=ctx.decision.confidence,
            strategy_id=ctx.decision.strategy_id,
            thesis=ctx.decision.thesis,
            supporting_evidence=ctx.decision.supporting_evidence,
            opposing_evidence=ctx.decision.opposing_evidence,
            risk_flags=list(ctx.decision.risk_flags) + ["risk_gate_veto"],
            data_quality=ctx.decision.data_quality,
            requires_more_data=ctx.decision.requires_more_data,
            raw=ctx.decision.raw,
            rejected_fields=ctx.decision.rejected_fields,
        )
        return StageResult(passed=True, decision=forced,
                           checks={"risk_gate": gate})
    return StageResult(passed=True, decision=ctx.decision, checks={"risk_gate": gate})


def default_services(config: Dict[str, Any]) -> Dict[str, Callable[[_StageContext], StageResult]]:
    """Return the real stage callables. Heavy service imports are lazy so importing
    pipeline.py is side-effect-free. Tasks 4–5 replace the pass-throughs."""
    return {
        PARSE: stage_parse,
        SCHEMA: stage_schema,
        SEMANTIC: stage_semantic,
        AUTHORITATIVE_DATA_CHECK: stage_authoritative_data_check,
        POLICY: stage_policy,
        RISK_GATE: stage_risk_gate,
    }


def run_pipeline(
    raw_decision: Any,
    signal: Dict[str, Any],
    positions: Iterable[Dict[str, Any]],
    *,
    config: Dict[str, Any],
    services: Optional[Dict[str, Callable[[_StageContext], StageResult]]] = None,
) -> ValidationResult:
    """Run the §42 6-stage validation pipeline. Returns a ValidationResult.
    On any stage failure, short-circuits with ok=False and the failing stage name.
    On all-pass, returns ok=True with the validated Decision (or NO_TRADE)."""
    svc = {**default_services(config), **(services or {})}
    ctx = _StageContext(raw_decision=raw_decision, signal=signal,
                        positions=positions, config=config, services=svc)
    for stage_name in STAGE_ORDER:
        stage_fn = svc[stage_name]
        result = stage_fn(ctx)
        if not result.passed:
            return ValidationResult(
                ok=False,
                decision=result.decision if result.decision is not None else ctx.decision,
                stage=stage_name,
                reason=result.reason,
                checks={**ctx.checks, **result.checks},
            )
        if result.decision is not None:
            ctx.decision = result.decision
        ctx.checks.update(result.checks)
    return ValidationResult(
        ok=True,
        decision=ctx.decision,
        stage="OK",
        reason="validated",
        checks=ctx.checks,
    )
