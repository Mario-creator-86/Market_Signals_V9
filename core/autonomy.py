"""Autonomy modes (§45), kill switch (§74), and the §48 no-weakening guard.

§46 Paper-Auto acceptance + can_enable_live_auto are added in Task 6 (they depend
on the decision engine). This module is stdlib-only and has no AI/network deps.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Final, List, Optional, Tuple


class AutonomyMode(str, Enum):
    OFF = "OFF"
    ANALYZE = "ANALYZE"
    RECOMMEND = "RECOMMEND"
    PAPER_AUTO = "PAPER_AUTO"
    LIVE_ASSISTED = "LIVE_ASSISTED"
    LIVE_AUTO = "LIVE_AUTO"


ALLOWED_MODES: Final[frozenset] = frozenset(m.value for m in AutonomyMode)

# §48 / §82 — surfaces no autonomous agent may weaken without owner authorization.
PROTECTED_TARGETS: Final[frozenset] = frozenset({
    "risk_constitution", "kill_switch", "promotion_gates",
    "critical_tests", "live_permissions", "holdout_policy",
})

_WEAKEN_VERBS: Final[frozenset] = frozenset({
    "disable", "delete", "weaken", "lower", "remove", "edit", "override", "escalate",
})


class AutonomyViolationError(ValueError):
    """§48 — an autonomous agent attempted to weaken a protected surface."""


def resolve_mode(config: dict) -> AutonomyMode:
    """§45 — resolve the autonomy mode. Unknown/missing -> ANALYZE (fail-safe, never LIVE_AUTO)."""
    cfg = ((config or {}).get("autonomy") or {})
    raw = str(cfg.get("mode") or "").upper().strip()
    try:
        return AutonomyMode(raw)
    except ValueError:
        return AutonomyMode.ANALYZE


def mode_allows_paper_orders(mode: AutonomyMode) -> bool:
    return mode in (AutonomyMode.PAPER_AUTO, AutonomyMode.LIVE_ASSISTED, AutonomyMode.LIVE_AUTO)


def mode_allows_live_orders(mode: AutonomyMode) -> bool:
    return mode is AutonomyMode.LIVE_AUTO


def kill_switch_active(config: dict) -> bool:
    """§74 — when True the autonomous loop blocks new automated risk (monitoring stays active)."""
    return bool(((config or {}).get("autonomy") or {}).get("kill_switch_active", False))


def live_auto_owner_enabled(config: dict) -> bool:
    """§45 — LIVE_AUTO may only be enabled explicitly by the owner."""
    return bool(((config or {}).get("autonomy") or {}).get("live_auto_owner_enabled", False))


def assert_not_weakening(target: str, action: str, *, owner_authorized: bool = False) -> None:
    """§48 — raise if an autonomous agent tries to weaken a protected surface. Owner bypass is explicit."""
    if owner_authorized:
        return
    t = str(target or "").lower().strip()
    a = str(action or "").lower().strip()
    if t in PROTECTED_TARGETS and a in _WEAKEN_VERBS:
        raise AutonomyViolationError(
            f"§48 — autonomous agent may not weaken protected surface "
            f"'{t}' (action='{a}'); owner action required."
        )


# ---------------------------------------------------------------------------
# §46 — Paper-Auto acceptance battery + can_enable_live_auto
# ---------------------------------------------------------------------------

# The Paper-Auto acceptance battery. Each scenario runs the decision engine
# (with fake/deterministic services) and asserts the expected outcome. The CI
# acceptance suite and an owner "run acceptance" action call run_paper_auto_acceptance
# to set the recorded `paper_auto_acceptance_passed` flag; the runtime LIVE_AUTO gate
# (can_enable_live_auto) reads that flag — it does NOT re-run live AI at order time.
PAPER_AUTO_SCENARIOS: List[Dict[str, Any]] = [
    {"name": "open_long_success", "ai": '{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.85,"thesis":"mom"}',
     "critic": '{"veto":false,"categories":[],"severity":0.1,"rationale":"ok"}',
     "signal": {"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95, "signal_score": 0.85, "strategy_type": "SCANNER_AI_MOMENTUM"},
     "expect_action": "OPEN_LONG", "expect_ok": True},
    {"name": "no_trade_decision", "ai": '{"action":"NO_TRADE","ticker":"NVDA","confidence":0.7}',
     "critic": '{"veto":false,"categories":[],"severity":0.0,"rationale":""}',
     "signal": {"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95, "signal_score": 0.5, "strategy_type": "SCANNER_AI_MOMENTUM"},
     "expect_action": "NO_TRADE", "expect_ok": True},
    {"name": "watch_observes", "ai": '{"action":"WATCH","ticker":"NVDA","confidence":0.4}',
     "critic": '{"veto":false,"categories":[],"severity":0.0,"rationale":""}',
     "signal": {"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95, "signal_score": 0.4, "strategy_type": "SCANNER_AI_MOMENTUM"},
     "expect_action": "WATCH", "expect_ok": True},
    {"name": "critic_veto_forces_no_trade", "ai": '{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.9,"thesis":"mom"}',
     "critic": '{"veto":true,"categories":["concentration"],"severity":0.9,"rationale":"concentrated"}',
     "signal": {"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95, "signal_score": 0.9, "strategy_type": "SCANNER_AI_MOMENTUM"},
     "expect_action": "NO_TRADE", "expect_ok": True},
    {"name": "hold_action", "ai": '{"action":"HOLD","ticker":"NVDA","confidence":0.6,"thesis":"hold"}',
     "critic": '{"veto":false,"categories":[],"severity":0.1,"rationale":"ok"}',
     "signal": {"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95, "signal_score": 0.6, "strategy_type": "SCANNER_AI_MOMENTUM"},
     "expect_action": "HOLD", "expect_ok": True},
    {"name": "reduce_action", "ai": '{"action":"REDUCE","ticker":"NVDA","confidence":0.7,"thesis":"reduce"}',
     "critic": '{"veto":false,"categories":[],"severity":0.1,"rationale":"ok"}',
     "signal": {"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95, "signal_score": 0.7, "strategy_type": "SCANNER_AI_MOMENTUM"},
     "expect_action": "REDUCE", "expect_ok": True},
    {"name": "strict_schema_rejects_authoritative", "ai": '{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.8,"price":850.0}',
     "critic": '{"veto":false,"categories":[],"severity":0.0,"rationale":""}',
     "signal": {"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95, "signal_score": 0.8, "strategy_type": "SCANNER_AI_MOMENTUM"},
     "expect_action": "OPEN_LONG", "expect_ok": False, "strict_schema": True,
     "note": "strict_schema rejects the authoritative `price` field (validation.ok=False); the parsed action is preserved but the review is unavailable-class"},
    {"name": "ai_unavailable_blocks_new_trade", "ai": None,  # sentinel: UNAVAILABLE provider
     "critic": None,
     "signal": {"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95, "signal_score": 0.8, "strategy_type": "SCANNER_AI_MOMENTUM"},
     "expect_action": "NO_TRADE", "expect_ok": True, "ai_unavailable": True},
]


def _scenario_provider(text: str, *, name: str = "fake"):
    """Build a deterministic fake provider for a scenario (no network)."""
    from core.ai.provider import AIResponse

    class _P:
        pass
    _P.name = name
    _P.generate = lambda self, prompt, *, model=None, system="", **opts: AIResponse(text=text, model="m")
    return _P()


def _unavailable_provider(*, name: str = "dead"):
    from core.ai.provider import UNAVAILABLE

    class _P:
        pass
    _P.name = name
    _P.generate = lambda self, prompt, *, model=None, system="", **opts: UNAVAILABLE
    return _P()


def run_paper_auto_acceptance(config: Dict[str, Any], services: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """§46 — run the Paper-Auto acceptance battery against the decision engine.

    `services` is either a callable factory `svc(ai_text, critic_text) -> dict` (the
    canonical path used by CI/owners — fully deterministic fakes) or a raw services dict
    (adapted per-scenario for the unavailable case). Returns {passed, scenarios, summary}.
    """
    # Package first, then the submodule: a direct first import of
    # `core.ai.decision_engine` races other threads inside `core.ai/__init__`
    # (KeyError('core.ai') / _DeadlockError).
    import importlib

    importlib.import_module("core.ai")
    from core.ai.decision_engine import evaluate_trade_decision

    svc_factory = services
    results: List[Dict[str, Any]] = []
    all_passed = True
    for sc in PAPER_AUTO_SCENARIOS:
        try:
            if callable(svc_factory):
                svc = svc_factory(sc["ai"] or "", sc["critic"] or "")
            else:
                svc = dict(svc_factory or {})
                if sc.get("ai_unavailable"):
                    dead = _unavailable_provider()
                    svc["primary_provider"] = dead
                    svc["critic_provider"] = dead
                else:
                    svc["primary_provider"] = _scenario_provider(sc["ai"])
                    svc["critic_provider"] = _scenario_provider(sc["critic"])
            # The ai_unavailable sentinel must yield a true UNAVAILABLE provider regardless
            # of whether services came from a factory or a static dict.
            if sc.get("ai_unavailable"):
                dead = _unavailable_provider()
                svc["primary_provider"] = dead
                svc["critic_provider"] = dead
            # Paper auto acceptance must stay deterministic and network-free
            # (documented hermetic contract) — never fetch live flow evidence.
            svc["skip_flow_evidence"] = True
            cfg = dict(config or {})
            if sc.get("strict_schema"):
                cfg.setdefault("ai", {}).setdefault("decision", {})["strict_schema"] = True
            res = evaluate_trade_decision(sc["signal"], {"accepted": True}, config=cfg, services=svc)
            got_action = res.decision.action.value
            ok = (got_action == sc["expect_action"]) and (res.validation.ok == sc["expect_ok"])
            results.append({"name": sc["name"], "ok": ok, "action": got_action,
                            "expected": sc["expect_action"], "stage": res.validation.stage})
            if not ok:
                all_passed = False
        except Exception as exc:
            results.append({"name": sc["name"], "ok": False, "error": str(exc)})
            all_passed = False
    return {"passed": all_passed, "scenarios": results,
            "summary": f"{sum(1 for r in results if r['ok'])}/{len(results)} scenarios passed"}


def paper_auto_acceptance_passed(config: Dict[str, Any], services: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """§46 — run the battery (deterministic fakes) and return (passed, summary)."""
    report = run_paper_auto_acceptance(config, services=services)
    return report["passed"], report["summary"]


def can_enable_live_auto(config: Dict[str, Any], services: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """§45/§46/§48 — LIVE_AUTO requires the owner flag AND a passed Paper-Auto acceptance.

    With `services` provided (CI / owner "run acceptance" action) the deterministic battery
    is re-run live. At runtime (no services) the RECORDED `paper_auto_acceptance_passed` flag
    is consulted — the autonomous loop never re-runs live AI to authorize its own promotion.
    """
    if not live_auto_owner_enabled(config):
        return False, "live_auto requires owner enable (§45)"
    auto = (config or {}).get("autonomy") or {}
    if not auto.get("paper_auto_acceptance_required", True):
        return True, "paper-auto acceptance not required"
    if auto.get("paper_auto_acceptance_passed", False):
        return True, "live_auto enabled: owner + paper-auto acceptance passed"
    if services is not None:
        passed, summary = paper_auto_acceptance_passed(config, services=services)
        if passed:
            return True, "live_auto enabled: owner + paper-auto acceptance passed"
        return False, f"paper-auto acceptance failed: {summary} (§46)"
    return False, "paper-auto acceptance not yet passed (§46)"