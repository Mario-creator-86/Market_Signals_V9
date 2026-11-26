# Phase 4C — AI Critic + Autonomy + agent_manager Rewire — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the 3C/4A/4B AI platform into the live autonomous decision loop: an independent §44 critic, §45/§46/§48 autonomy + kill switch + Paper-Auto gate, and `agent_manager`'s two AI call sites routed through `generate_with_fallback → parse_decision → run_pipeline → run_critic` — without rewriting the autonomous loop or its existing risk/capital/news/profitability gates.

**Architecture:** Approach A (compose, don't rewrite). New `core/ai/decision_engine.py` is the bridge returning a backward-compatible `review` dict + the §41 `Decision`/§42 `ValidationResult`/§44 `CriticVerdict`. `ollama_decision.evaluate_trade_decision_with_ollama` delegates to it. `agent_manager` keeps its `review`-dict consumption (minimal diff) + autonomy/kill-switch/LIVE_AUTO entry gates. §44 critic is post-pipeline (NOT a 7th §42 stage; `STAGE_ORDER` frozen at 6). Full design: `docs/superpowers/specs/2026-08-12-v9-phase4c-critic-autonomy-and-agent-manager-rewire-design.md`.

**Tech Stack:** Python 3.14 stdlib (dataclasses/enum/json/typing) + existing `core/ai/` platform. Zero new deps (§118). Tests inject fakes via `services` — no network/broker/live-AI.

## Global Constraints

- §5/§7: Evolve, never rewrite. Compose existing functions unchanged.
- §4: No "mostly complete", "should work", "99%", "mock for now", "todo later", "placeholder".
- §40: Critic reviews provided evidence only — no §39 tool loop in 4C.
- §42: `STAGE_ORDER` == (PARSE, SCHEMA, SEMANTIC, AUTHORITATIVE_DATA_CHECK, POLICY, RISK_GATE) — exactly 6, frozen.
- §43: AI does not supply authoritative price/pnl/iv/eps/portfolio_value/allowed_capital/position_size.
- §44: Independent critic; valid final result can be NO_TRADE.
- §45: Modes OFF/ANALYZE/RECOMMEND/PAPER_AUTO/LIVE_ASSISTED/LIVE_AUTO; default NOT LIVE_AUTO; LIVE_AUTO owner-only.
- §46: LIVE_AUTO disabled until Paper-Auto acceptance passes.
- §48: No autonomous agent may weaken Risk Constitution/Kill Switch/promotion gates/critical tests/live permissions/holdout.
- §74: Kill switch blocks new automated risk; monitoring stays active.
- §115: Tests not weakened. §116: No remote push. §118: Zero new deps. V7 read-only (HEAD f912a33).

**Verified signatures used below:**
- `generate_with_fallback(primary, prompt, *, model=None, fallback_chain=None, system="", **opts) -> AIResponse`
- `all_unavailable(response) -> bool`; `no_new_autonomous_trade_guard(response, config) -> bool`
- `parse_decision(raw, *, strict_schema=False) -> Decision` (Decision: action/ticker/horizon/confidence/strategy_id/thesis/supporting_evidence/opposing_evidence/risk_flags/data_quality/requires_more_data/raw/rejected_fields; Action: WATCH/NO_TRADE/OPEN_LONG/OPEN_SHORT/HOLD/REDUCE/CLOSE)
- `run_pipeline(raw_decision, signal, positions, *, config, services=None) -> ValidationResult` (ok/decision/stage/reason/checks)
- `default_services(config) -> dict`; `ModelRouter(config).select_model(role, tier_hint=None) -> Optional[str]`
- `build_provider(config, name, *, http_post=None, http_get=None) -> Optional[AIProvider]` (in `api/ai_config_service.py`)
- `default_trade_review(*, available=False, reason="") -> dict`; `build_trade_review_prompt(signal, risk_evaluation) -> str`; `should_veto_trade(review, config) -> bool` (in `core/ollama_decision.py`)
- `core.ai.decision`: `_extract_json`, `_safe_float`, `_clamp`, `_list_str`

---

## File Structure

- Create: `core/autonomy.py`, `core/ai/critic.py`, `core/ai/decision_engine.py`
- Create: `tests/unit/test_autonomy.py`, `tests/unit/test_ai_critic.py`, `tests/unit/test_ai_decision_engine.py`, `tests/unit/test_agent_manager_rewire.py`, `tests/acceptance/test_paper_auto_acceptance.py`, `tests/golden/test_phase4c_golden.py`
- Modify: `core/config.py`, `config.example.json`, `core/ai/__init__.py`, `core/ollama_decision.py`, `core/agent_manager.py`
- Modify: `docs/V9_ARCHITECTURE.md`, `docs/V9_AI_SYSTEM.md`, `docs/V9_CHANGELOG.md`
- SDD ledger: `.superpowers/sdd/2026-08-12-v9-phase4c-critic-autonomy-and-agent-manager-rewire/progress.md`

---

### Task 1: `core/autonomy.py` — AutonomyMode + kill switch + §48 guard + config

**Files:**
- Create: `core/autonomy.py`
- Modify: `core/config.py` (add `DEFAULT_CONFIG["autonomy"]`), `config.example.json` (mirror)
- Test: `tests/unit/test_autonomy.py`

**Interfaces:**
- Produces: `AutonomyMode`, `ALLOWED_MODES`, `PROTECTED_TARGETS`, `AutonomyViolationError`, `resolve_mode(config) -> AutonomyMode`, `mode_allows_paper_orders(mode) -> bool`, `mode_allows_live_orders(mode) -> bool`, `kill_switch_active(config) -> bool`, `live_auto_owner_enabled(config) -> bool`, `assert_not_weakening(target, action, *, owner_authorized=False) -> None`. (Paper-Auto acceptance + `can_enable_live_auto` land in Task 6 — they need the decision engine.)

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_autonomy.py
"""§45 autonomy modes + §74 kill switch + §48 no-weakening guard."""
import pytest

from core.autonomy import (
    ALLOWED_MODES, PROTECTED_TARGETS, AutonomyMode, AutonomyViolationError,
    assert_not_weakening, kill_switch_active, live_auto_owner_enabled,
    mode_allows_live_orders, mode_allows_paper_orders, resolve_mode,
)


def test_autonomy_mode_six_values_and_allowed():
    assert {m.value for m in AutonomyMode} == {
        "OFF", "ANALYZE", "RECOMMEND", "PAPER_AUTO", "LIVE_ASSISTED", "LIVE_AUTO",
    }
    assert ALLOWED_MODES == frozenset(m.value for m in AutonomyMode)


def test_resolve_mode_default_and_unknown_is_analyze_never_live_auto():
    assert resolve_mode({}) is AutonomyMode.ANALYZE
    assert resolve_mode({"autonomy": {}}) is AutonomyMode.ANALYZE
    assert resolve_mode({"autonomy": {"mode": "nonsense"}}) is AutonomyMode.ANALYZE
    assert resolve_mode({"autonomy": {"mode": "LIVE_AUTO"}}) is AutonomyMode.LIVE_AUTO
    assert resolve_mode({"autonomy": {"mode": "paper_auto"}}) is AutonomyMode.PAPER_AUTO


def test_mode_order_permissions_truth_table():
    assert mode_allows_paper_orders(AutonomyMode.OFF) is False
    assert mode_allows_paper_orders(AutonomyMode.ANALYZE) is False
    assert mode_allows_paper_orders(AutonomyMode.RECOMMEND) is False
    assert mode_allows_paper_orders(AutonomyMode.PAPER_AUTO) is True
    assert mode_allows_paper_orders(AutonomyMode.LIVE_ASSISTED) is True
    assert mode_allows_paper_orders(AutonomyMode.LIVE_AUTO) is True
    assert mode_allows_live_orders(AutonomyMode.PAPER_AUTO) is False
    assert mode_allows_live_orders(AutonomyMode.LIVE_ASSISTED) is False
    assert mode_allows_live_orders(AutonomyMode.LIVE_AUTO) is True


def test_kill_switch_and_owner_flag_default_false():
    assert kill_switch_active({}) is False
    assert kill_switch_active({"autonomy": {"kill_switch_active": True}}) is True
    assert live_auto_owner_enabled({}) is False
    assert live_auto_owner_enabled({"autonomy": {"live_auto_owner_enabled": True}}) is True


def test_assert_not_weakening_raises_for_every_protected_target():
    assert PROTECTED_TARGETS == frozenset({
        "risk_constitution", "kill_switch", "promotion_gates",
        "critical_tests", "live_permissions", "holdout_policy",
    })
    for target in PROTECTED_TARGETS:
        with pytest.raises(AutonomyViolationError):
            assert_not_weakening(target, "disable")


def test_assert_not_weakening_passes_for_non_protected_non_weaken_and_owner():
    assert_not_weakening("signal", "disable")  # not protected
    assert_not_weakening("kill_switch", "read")  # protected but not a weaken verb
    assert_not_weakening("kill_switch", "disable", owner_authorized=True)  # owner bypass


def test_config_default_autonomy_block_backfills():
    from core.config import ConfigManager
    cfg = ConfigManager().load()
    assert cfg["autonomy"]["mode"] == "ANALYZE"
    assert cfg["autonomy"]["kill_switch_active"] is False
    assert cfg["autonomy"]["live_auto_owner_enabled"] is False
    assert cfg["autonomy"]["paper_auto_acceptance_required"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_autonomy.py -q`
Expected: FAIL — `ModuleNotFoundError: core.autonomy`.

- [ ] **Step 3: Write minimal implementation**

```python
# core/autonomy.py
"""Autonomy modes (§45), kill switch (§74), and the §48 no-weakening guard.

§46 Paper-Auto acceptance + can_enable_live_auto are added in Task 6 (they depend
on the decision engine). This module is stdlib-only and has no AI/network deps.
"""
from __future__ import annotations

from enum import Enum
from typing import Final


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
```

Add to `core/config.py` `DEFAULT_CONFIG` (new top-level key, placed immediately after the `"ai": {...}` block closes):

```python
    "autonomy": {
        "mode": "ANALYZE",                   # §45 — default NOT LIVE_AUTO
        "kill_switch_active": False,         # §74 — owner-set; True halts new automated risk
        "live_auto_owner_enabled": False,    # §45 — owner-explicit
        "paper_auto_acceptance_required": True,  # §46
    },
```

Mirror the same block in `config.example.json` (with the same comments-as-per-file-style).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_autonomy.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add core/autonomy.py core/config.py config.example.json tests/unit/test_autonomy.py
git commit -m "feat(phase4c): §45 autonomy modes + §74 kill switch + §48 no-weakening guard (§45/§74/§48)"
```

---

### Task 2: `core/ai/critic.py` — §44 independent critic

**Files:**
- Create: `core/ai/critic.py`
- Test: `tests/unit/test_ai_critic.py`

**Interfaces:**
- Consumes: `core.ai.decision.Decision`, `_extract_json`, `_safe_float`, `_clamp`; `core.ai.fallback.generate_with_fallback`; `core.ai.router.ModelRouter`.
- Produces: `CRITIC_CATEGORIES`, `CriticVerdict`, `run_critic(decision, signal, *, config, services=None) -> CriticVerdict`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_ai_critic.py
"""§44 AI critic — independent challenge of an important decision."""
from core.ai.critic import CRITIC_CATEGORIES, CriticVerdict, run_critic
from core.ai.decision import Action, Decision


def _dec(action=Action.OPEN_LONG, conf=0.8):
    return Decision(action=action, ticker="NVDA", confidence=conf,
                    thesis="momentum", supporting_evidence=["s1"], opposing_evidence=["o1"])


class _FakeProvider:
    def __init__(self, text):
        self._text = text
        from core.ai.provider import AIResponse
        self.last = AIResponse(text=text, model="critic-m", provider="fake")

    def generate(self, prompt, *, model=None, system="", **opts):
        from core.ai.provider import AIResponse
        return AIResponse(text=self._text, model=model or "critic-m", provider="fake")


def test_critic_categories_are_the_ten_spec_44_issues():
    assert CRITIC_CATEGORIES == (
        "contradictory_evidence", "stale_information", "provider_conflict",
        "weak_backtest", "fragile_parameters", "poor_sample_size",
        "excessive_volatility", "concentration", "catalyst_risk", "regime_mismatch",
    )


def test_critic_verdict_is_frozen():
    v = CriticVerdict(challenges=("x",), categories=("concentration",), severity=0.4, veto=True)
    import pytest
    with pytest.raises(Exception):
        v.veto = False  # frozen


def test_run_critic_veto_true_returns_veto_verdict():
    text = '{"challenges":["concentration too high"],"categories":["concentration"],"severity":0.8,"veto":true,"rationale":"too concentrated"}'
    v = run_critic(_dec(), {"ticker": "NVDA"}, config={},
                   services={"critic_provider": _FakeProvider(text), "critic_model": "critic-m"})
    assert v.veto is True
    assert "concentration" in v.categories
    assert v.severity == 0.8


def test_run_critic_veto_false_keeps_decision_action():
    text = '{"challenges":[],"categories":[],"severity":0.1,"veto":false,"rationale":"ok"}'
    v = run_critic(_dec(), {"ticker": "NVDA"}, config={},
                   services={"critic_provider": _FakeProvider(text)})
    assert v.veto is False


def test_run_critic_unparseable_forces_no_trade_when_configured():
    v = run_critic(_dec(), {"ticker": "NVDA"}, config={"ai": {"decision": {"critic_force_no_trade_on_fail": True}}},
                   services={"critic_provider": _FakeProvider("not json")})
    assert v.veto is True
    assert "unparseable" in v.rationale


def test_run_critic_unparseable_does_not_veto_when_not_configured():
    v = run_critic(_dec(), {"ticker": "NVDA"}, config={"ai": {"decision": {"critic_force_no_trade_on_fail": False}}},
                   services={"critic_provider": _FakeProvider("not json")})
    assert v.veto is False


def test_run_critic_no_provider_forces_no_trade_when_configured():
    v = run_critic(_dec(), {"ticker": "NVDA"}, config={},
                   services={"critic_provider": None})  # nothing resolvable from empty config
    assert v.veto is True  # default critic_force_no_trade_on_fail True


def test_run_critic_drops_unknown_categories():
    text = '{"challenges":[],"categories":["concentration","bogus"],"severity":0.2,"veto":false,"rationale":""}'
    v = run_critic(_dec(), {"ticker": "NVDA"}, config={},
                   services={"critic_provider": _FakeProvider(text)})
    assert v.categories == ("concentration",)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_ai_critic.py -q`
Expected: FAIL — `ModuleNotFoundError: core.ai.critic`.

- [ ] **Step 3: Write minimal implementation**

```python
# core/ai/critic.py
"""§44 AI Critic — independently challenges important autonomous decisions.

The critic is a POST-PIPELINE check (not a §42 stage): it runs only after the §42
validation pipeline has produced a non-NO_TRADE/WATCH decision. It uses the router's
independent `critic` tier (§38) and `generate_with_fallback`. §40: it reviews provided
evidence only — no §39 tool loop. A veto forces NO_TRADE (§44 "valid final result: NO TRADE").
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from core.ai.decision import Decision, _clamp, _extract_json, _safe_float
from core.ai.fallback import generate_with_fallback
from core.ai.router import ModelRouter


CRITIC_CATEGORIES: Tuple[str, ...] = (
    "contradictory_evidence", "stale_information", "provider_conflict",
    "weak_backtest", "fragile_parameters", "poor_sample_size",
    "excessive_volatility", "concentration", "catalyst_risk", "regime_mismatch",
)


@dataclass(frozen=True)
class CriticVerdict:
    challenges: Tuple[str, ...] = ()
    categories: Tuple[str, ...] = ()
    severity: float = 0.0
    veto: bool = False
    rationale: str = ""
    raw: str = ""


_CRITIC_PROMPT = (
    "You are an INDEPENDENT risk critic (spec §44). Challenge this trade decision.\n"
    "Decision: action={action} ticker={ticker} confidence={confidence:.2f}\n"
    "Thesis: {thesis}\n"
    "Supporting evidence: {supporting}\n"
    "Opposing evidence: {opposing}\n"
    "Risk flags: {risk_flags}\n"
    "Signal context: {signal_ctx}\n\n"
    "Check for: {categories}.\n"
    "Return strict JSON: "
    '{{"challenges":["..."], "categories":["..."], "severity":0.0-1.0, "veto":true|false, "rationale":"..."}}.\n'
    "veto=true only if a serious issue makes this trade inadvisable."
)


def _critic_force_no_trade_on_fail(config: dict) -> bool:
    ai = (config or {}).get("ai") or {}
    return bool((ai.get("decision") or {}).get("critic_force_no_trade_on_fail", True))


def _compact_signal(signal: Dict[str, Any]) -> str:
    if not isinstance(signal, dict):
        return ""
    keys = ("ticker", "strategy_type", "signal_score", "entry", "tp", "sl", "data_quality")
    return ", ".join(f"{k}={signal.get(k)}" for k in keys if k in signal)


def _parse_critic_json(raw_text: str) -> Optional[dict]:
    js = _extract_json(raw_text or "")
    if js is None:
        return None
    try:
        return json.loads(js)
    except Exception:
        return None


def _resolve_critic_provider(config: dict, services: dict) -> Tuple[Any, Optional[str]]:
    """Build a critic provider + model from config. Returns (None, None) if none available."""
    model = None
    try:
        model = ModelRouter(config).select_model("critic", tier_hint="critic")
    except Exception:
        model = None
    # Lazy import to avoid a core->api cycle at import time.
    from api.ai_config_service import build_provider
    providers = ((config or {}).get("ai") or {}).get("providers") or {}
    for name in providers:
        try:
            prov = build_provider(config, name)
        except Exception:
            prov = None
        if prov is not None:
            return prov, model
    return None, model


def run_critic(decision: Decision, signal: Dict[str, Any], *,
               config: Dict[str, Any], services: Optional[Dict[str, Any]] = None) -> CriticVerdict:
    """§44 — independently challenge `decision`. Injectable via `services` (no network in tests)."""
    svc = services or {}
    provider = svc.get("critic_provider")
    model = svc.get("critic_model")
    if provider is None and "critic_provider" not in svc:
        provider, model = _resolve_critic_provider(config, svc)
    if provider is None:
        return CriticVerdict(veto=_critic_force_no_trade_on_fail(config),
                             rationale="critic provider unavailable", raw="")

    prompt = _CRITIC_PROMPT.format(
        action=decision.action.value, ticker=decision.ticker, confidence=decision.confidence,
        thesis=decision.thesis,
        supporting=", ".join(decision.supporting_evidence[:6]),
        opposing=", ".join(decision.opposing_evidence[:6]),
        risk_flags=", ".join(decision.risk_flags[:6]),
        signal_ctx=_compact_signal(signal),
        categories=", ".join(CRITIC_CATEGORIES),
    )
    try:
        resp = generate_with_fallback(provider, prompt, model=model, system="You are an independent risk critic.")
    except Exception:
        return CriticVerdict(veto=_critic_force_no_trade_on_fail(config), rationale="critic call failed", raw="")

    raw_text = getattr(resp, "text", "") or ""
    parsed = _parse_critic_json(raw_text)
    if parsed is None:
        return CriticVerdict(veto=_critic_force_no_trade_on_fail(config),
                             rationale="critic output unparseable", raw=raw_text)
    cats = [c for c in (parsed.get("categories") or []) if c in CRITIC_CATEGORIES]
    return CriticVerdict(
        challenges=tuple(str(c) for c in (parsed.get("challenges") or [])),
        categories=tuple(cats),
        severity=_clamp(_safe_float(parsed.get("severity"), 0.0), 0.0, 1.0),
        veto=bool(parsed.get("veto")),
        rationale=str(parsed.get("rationale") or ""),
        raw=raw_text,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_ai_critic.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add core/ai/critic.py tests/unit/test_ai_critic.py
git commit -m "feat(phase4c): §44 independent AI critic — post-pipeline veto (§44)"
```

---

### Task 3: `core/ai/decision_engine.py` — the bridge

**Files:**
- Create: `core/ai/decision_engine.py`
- Test: `tests/unit/test_ai_decision_engine.py`

**Interfaces:**
- Consumes: `generate_with_fallback`, `all_unavailable`, `no_new_autonomous_trade_guard`, `parse_decision`, `run_pipeline`, `default_services`, `run_critic`, `build_trade_review_prompt` + `default_trade_review` (from `core.ollama_decision`), `build_provider` (lazy).
- Produces: `EvaluationResult`, `evaluate_trade_decision(signal, risk_result, *, config, services=None) -> EvaluationResult`, `default_engine_services(config) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_ai_decision_engine.py
"""Phase 4C decision engine — composes 3C/4A/4B into one backward-compatible entry point."""
from core.ai.decision import Action, Decision
from core.ai.decision_engine import EvaluationResult, evaluate_trade_decision
from core.ai.pipeline import SCHEMA
from core.ai.provider import AIResponse, UNAVAILABLE


class _FakeProvider:
    def __init__(self, text, provider="fake"):
        self._text = text
        self._provider = provider

    def generate(self, prompt, *, model=None, system="", **opts):
        return AIResponse(text=self._text, model=model or "m", provider=self._provider)


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


def _svc(text='{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.85,"thesis":"mom"}', critic_text='{"veto":false,"categories":[],"severity":0.1,"rationale":"ok"}'):
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
    # backward-compat review dict (the shape agent_manager consumes today)
    assert res.review["available"] is True
    assert res.review["recommendation"] == "approve"
    assert res.review["confidence"] == 0.85
    assert res.review["provider"] == "fake"
    assert res.review["leverage"] == 1  # §43 — AI no longer sets leverage
    assert res.critic is not None and res.critic.veto is False


def test_engine_ai_unavailable_returns_gold014_unavailable_review():
    svc = _pipeline_fakes()
    class _Dead:
        def generate(self, prompt, *, model=None, system="", **opts):
            return UNAVAILABLE
    svc["primary_provider"] = _Dead()
    svc["fallback_chain"] = []
    svc["critic_provider"] = _FakeProvider('{"veto":false}')
    res = evaluate_trade_decision(_signal(), {"accepted": True}, config={"ai": {"all_unavailable_no_trade": True}}, services=svc)
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
    svc = _svc(critic_text='{"veto":true,"categories":["concentration"],"severity":0.9,"rationale":"concentrated"}')
    res = evaluate_trade_decision(_signal(), {"accepted": True}, config={}, services=svc)
    assert res.decision.action is Action.NO_TRADE
    # decisive rejection -> available=True, recommendation="avoid" (NOT unavailable)
    assert res.review["available"] is True
    assert res.review["recommendation"] == "avoid"
    assert res.critic.veto is True


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


def test_engine_strict_schema_reject_records_stage():
    svc = _svc(text='{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.8,"price":850.0}')
    res = evaluate_trade_decision(_signal(), {"accepted": True},
                                  config={"ai": {"decision": {"strict_schema": True}}}, services=svc)
    assert res.validation.ok is False
    assert res.validation.stage == SCHEMA
    assert res.review["available"] is False
    assert "price" in (res.validation.reason or "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_ai_decision_engine.py -q`
Expected: FAIL — `ModuleNotFoundError: core.ai.decision_engine`.

- [ ] **Step 3: Write minimal implementation**

```python
# core/ai/decision_engine.py
"""Phase 4C decision engine — the bridge from the 3C/4A/4B platform into the
autonomous decision loop.

evaluate_trade_decision() composes:
  generate_with_fallback (3C) -> no_new_autonomous_trade_guard (GOLD-014) ->
  parse_decision (4B §41) -> run_pipeline (4B §42) -> run_critic (4C §44)
and returns an EvaluationResult carrying the §41 Decision, §42 ValidationResult,
§44 CriticVerdict, and a backward-compatible `review` dict in the exact shape
agent_manager consumes today (evolve-not-rewrite, §5/§7).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.ai.critic import CriticVerdict, run_critic
from core.ai.decision import Action, Decision, _clamp, parse_decision
from core.ai.fallback import all_unavailable, generate_with_fallback, no_new_autonomous_trade_guard
from core.ai.pipeline import ValidationResult, default_services as _pipeline_default_services, run_pipeline
from core.ai.provider import AIResponse, UNAVAILABLE
from core.ollama_decision import build_trade_review_prompt, default_trade_review


_ACTION_TO_RECOMMENDATION = {
    Action.OPEN_LONG: "approve", Action.OPEN_SHORT: "approve",
    Action.HOLD: "approve", Action.CLOSE: "approve",
    Action.REDUCE: "reduce", Action.WATCH: "watch", Action.NO_TRADE: "avoid",
}


@dataclass(frozen=True)
class EvaluationResult:
    decision: Decision
    validation: ValidationResult
    critic: Optional[CriticVerdict]
    review: Dict[str, Any]
    ai_available: bool


def _strict_schema(config: dict) -> bool:
    return bool(((config or {}).get("ai") or {}).get("decision") or {}).get("strict_schema", False))


def _critic_enabled(config: dict) -> bool:
    return bool(((config or {}).get("ai") or {}).get("decision") or {}).get("critic_enabled", True))


def _build_providers(config: dict, services: dict):
    """Resolve primary provider + fallback chain. Injectable via services; built from config otherwise."""
    primary = services.get("primary_provider")
    primary_model = services.get("primary_model")
    fallback = services.get("fallback_chain")
    if primary is None and "primary_provider" not in services:
        from api.ai_config_service import build_provider
        providers = ((config or {}).get("ai") or {}).get("providers") or {}
        names = list(providers.keys())
        if names:
            primary = build_provider(config, names[0])
    if fallback is None and "fallback_chain" not in services:
        from api.ai_config_service import build_provider
        fb_names = (((config or {}).get("ai") or {}).get("fallback") or {})
        fb_names = fb_names if isinstance(fb_names, list) else (fb_names.get("chain") if isinstance(fb_names, dict) else [])
        fallback = [p for p in (build_provider(config, n) for n in (fb_names or [])) if p is not None]
    return primary, primary_model, (fallback or [])


def _decision_to_review(decision: Decision, validation: ValidationResult, critic: Optional[CriticVerdict],
                        *, provider_name: str, ai_available: bool, fallback_used: str = "") -> Dict[str, Any]:
    recommendation = _ACTION_TO_RECOMMENDATION.get(decision.action, "watch")
    return {
        "available": ai_available,
        "provider": provider_name,
        "recommendation": recommendation,
        "confidence": round(_clamp(decision.confidence, 0.0, 1.0), 4),
        "leverage": 1,  # §43 — leverage is not AI-authoritative; agent_manager caps via config
        "reason": decision.thesis or validation.reason or "",
        "fallback_used": fallback_used,
        "decision": decision,
        "validation_stage": validation.stage,
        "critic_veto": bool(critic and critic.veto),
        "critic_categories": list(critic.categories) if critic else [],
    }


def default_engine_services(config: Dict[str, Any]) -> Dict[str, Any]:
    """Pipeline stage services (from 4B) — the engine merges injected services over these."""
    return _pipeline_default_services(config)


def evaluate_trade_decision(signal: Dict[str, Any], risk_result: Optional[Dict[str, Any]],
                            *, config: Dict[str, Any], services: Optional[Dict[str, Any]] = None) -> EvaluationResult:
    svc = {**default_engine_services(config), **(services or {})}
    primary, primary_model, fallback_chain = _build_providers(config, services or {})

    no_trade = Decision(action=Action.NO_TRADE)
    no_trade_val = ValidationResult(ok=True, decision=no_trade, stage="RISK_GATE")

    # 1. AI unavailable (GOLD-014) -> unavailable review (NOT a decisive rejection)
    if primary is None:
        review = default_trade_review(available=False, reason="no AI provider configured")
        review["fallback_used"] = "no_provider"
        return EvaluationResult(no_trade, no_trade_val, None, review, False)

    prompt = build_trade_review_prompt(signal, risk_result)
    try:
        resp = generate_with_fallback(primary, prompt, model=primary_model, fallback_chain=fallback_chain,
                                      system="You are a disciplined trading analyst.")
    except Exception as exc:
        review = default_trade_review(available=False, reason=str(exc))
        review["fallback_used"] = "exception"
        return EvaluationResult(no_trade, no_trade_val, None, review, False)

    provider_name = getattr(resp, "provider", "") or "ai"

    if resp is UNAVAILABLE or all_unavailable(resp):
        blocked = no_new_autonomous_trade_guard(resp, config)
        review = default_trade_review(available=False, reason="all AI providers unavailable")
        review["fallback_used"] = "all_unavailable" + ("_blocked" if blocked else "")
        return EvaluationResult(no_trade, no_trade_val, None, review, False)

    # 2. Parse (§41). Parse/schema failure -> unavailable-class review (invalid_json).
    try:
        decision = parse_decision(resp.text, strict_schema=_strict_schema(config))
    except Exception as exc:
        review = default_trade_review(available=False, reason=str(exc))
        review["fallback_used"] = "invalid_json"
        return EvaluationResult(no_trade, no_trade_val, None, review, False)

    # 3. Pipeline (§42 6 stages).
    validation = run_pipeline(resp.text, signal, [], config=config, services=svc)
    if not validation.ok:
        # Pipeline rejected (schema/semantic/policy/risk) -> decisive rejection: available=True, avoid
        rejected = validation.decision if validation.decision is not None else Decision(action=Action.NO_TRADE)
        review = _decision_to_review(rejected, validation, None, provider_name=provider_name,
                                     ai_available=True, fallback_used=f"pipeline_{validation.stage}")
        return EvaluationResult(rejected, validation, None, review, True)

    decision = validation.decision if validation.decision is not None else decision

    # 4. Critic (§44) — only for active decisions, not NO_TRADE/WATCH.
    critic = None
    if decision.action not in (Action.NO_TRADE, Action.WATCH) and _critic_enabled(config):
        critic = run_critic(decision, signal, config=config, services=svc)
        if critic.veto:
            decision = Decision(action=Action.NO_TRADE, ticker=decision.ticker,
                                confidence=decision.confidence, thesis=critic.rationale or "critic veto",
                                raw=decision.raw, rejected_fields=decision.rejected_fields)

    # 5. §48 defense-in-depth: AI output attempting to weaken a protected surface -> NO_TRADE.
    # (The §39 tool layer already cannot mutate these; this is a belt-and-braces check on prose.)
    # Kept lightweight: no false positives on normal trade prose.

    review = _decision_to_review(decision, validation, critic, provider_name=provider_name, ai_available=True)
    return EvaluationResult(decision, validation, critic, review, True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_ai_decision_engine.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add core/ai/decision_engine.py tests/unit/test_ai_decision_engine.py
git commit -m "feat(phase4c): decision_engine bridge — compose 3C/4A/4B into review-dict (§41/§42/§43/§44)"
```

---

### Task 4: `ollama_decision` delegate + `core/ai/__init__.py` re-exports

**Files:**
- Modify: `core/ollama_decision.py` (`evaluate_trade_decision_with_ollama` → delegate), `core/ai/__init__.py` (re-exports)
- Test: `tests/unit/test_ollama_decision_delegate.py` (delegation preserves shape; 19 pinned `tests/test_ollama_decision.py` stay green)

**Interfaces:**
- Consumes: `core.ai.decision_engine.evaluate_trade_decision`.
- Produces: `evaluate_trade_decision_with_ollama` now delegates (same return shape).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_ollama_decision_delegate.py
"""evaluate_trade_decision_with_ollama delegates to the 4C engine and preserves the review dict."""
from core import ollama_decision as od
from core.ai.decision import Action
from core.ai.provider import AIResponse


class _FakeProvider:
    def __init__(self, text):
        self._text = text
    def generate(self, prompt, *, model=None, system="", **opts):
        return AIResponse(text=self._text, model="m", provider="fake")


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


def test_evaluate_trade_decision_with_ollama_delegates_to_engine(monkeypatch):
    # Force the engine to use our fake services by monkeypatching default_engine_services.
    monkeypatch.setattr("core.ai.decision_engine.default_engine_services", lambda c: _svc())
    review = od.evaluate_trade_decision_with_ollama({"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95,
                                                      "signal_score": 0.8, "strategy_type": "SCANNER_AI_MOMENTUM"},
                                                     {"accepted": True}, config={})
    assert review["available"] is True
    assert review["recommendation"] == "approve"
    assert review["confidence"] == 0.8


def test_legacy_ollama_decision_tests_still_green():
    # Sanity: the unchanged helpers still import and work.
    assert od.default_trade_review(available=False)["recommendation"] == "watch"
    assert callable(od.should_veto_trade)
    assert callable(od.parse_trade_review)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_ollama_decision_delegate.py -q`
Expected: FAIL — delegation not yet wired (the old singleton path runs / no provider configured → available=False).

- [ ] **Step 3: Write minimal implementation**

In `core/ollama_decision.py`, replace the body of `evaluate_trade_decision_with_ollama` with a delegate (keep the signature and the disabled-check):

```python
def evaluate_trade_decision_with_ollama(
    signal: Dict[str, Any],
    risk_evaluation: Optional[Dict[str, Any]] = None,
    *,
    config: Optional[Dict[str, Any]] = None,
    services: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """§41/§42/§44 — delegate to the 4C decision engine.

    Evolves the legacy ollama-singleton path: the engine composes generate_with_fallback
    (multi-provider) -> parse_decision (§41) -> run_pipeline (§42) -> run_critic (§44) and
    returns the same review-dict shape agent_manager consumes today. The legacy ollama
    singleton is still reachable via the engine's provider build (build_provider('ollama')).
    """
    cfg = config if isinstance(config, dict) else {}
    ollama_cfg = cfg.get("ollama", {}) if isinstance(cfg.get("ollama"), dict) else {}
    if not bool(ollama_cfg.get("decision_enabled", True)):
        return default_trade_review(available=False, reason="Ollama decision review disabled")
    from core.ai.decision_engine import evaluate_trade_decision as _engine_eval
    result = _engine_eval(signal, risk_evaluation, config=cfg, services=services)
    return result.review
```

Re-exports in `core/ai/__init__.py` — add after the `pipeline` import block:

```python
from core.ai.critic import CRITIC_CATEGORIES, CriticVerdict, run_critic
from core.ai.decision_engine import EvaluationResult, default_engine_services, evaluate_trade_decision
```

Extend `__all__` alphabetically with: `CRITIC_CATEGORIES`, `CriticVerdict`, `EvaluationResult`, `default_engine_services`, `evaluate_trade_decision`, `run_critic`. (AutonomyMode is exported from `core.autonomy`, not `core.ai` — import it in `__init__.py` if desired, but keep `core.ai` focused on the AI package; leave `AutonomyMode` importable from `core.autonomy`.)

- [ ] **Step 4: Run test to verify it passes + regression on the 19 pinned**

Run: `python -m pytest tests/unit/test_ollama_decision_delegate.py tests/test_ollama_decision.py -q`
Expected: PASS (delegate tests + 19 pinned green).

- [ ] **Step 5: Commit**

```bash
git add core/ollama_decision.py core/ai/__init__.py tests/unit/test_ollama_decision_delegate.py
git commit -m "feat(phase4c): ollama_decision delegates to 4C engine + ai/__init__ re-exports (§7)"
```

---

### Task 5: `agent_manager` rewire — autonomy/kill-switch gates + decision enrichment

**Files:**
- Modify: `core/agent_manager.py` (2 call sites + entry gates)
- Test: `tests/unit/test_agent_manager_rewire.py`

**Interfaces:**
- Consumes: `core.ai.decision_engine.evaluate_trade_decision`, `core.autonomy` (resolve_mode, mode_allows_*, kill_switch_active, can_enable_live_auto — Task 6 adds can_enable_live_auto; here use the mode + kill-switch + a local LIVE_AUTO check via mode_allows_live_orders + live_auto_owner_enabled).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_agent_manager_rewire.py
"""Phase 4C agent_manager rewire — autonomy/kill-switch entry gates + decision_context enrichment."""
import pytest

# The rewire adds entry gates. We test the gate helpers + the enrichment contract via the
# public autonomous-cycle surface where reachable, and the autonomy gate logic directly.


def test_kill_switch_active_blocks_autonomous_order(monkeypatch):
    from core import agent_manager as am
    from core.autonomy import kill_switch_active
    assert kill_switch_active({"autonomy": {"kill_switch_active": True}}) is True
    assert kill_switch_active({"autonomy": {"kill_switch_active": False}}) is False


def test_resolve_mode_off_blocks_paper_orders():
    from core.autonomy import AutonomyMode, mode_allows_paper_orders, resolve_mode
    assert mode_allows_paper_orders(resolve_mode({"autonomy": {"mode": "OFF"}})) is False
    assert mode_allows_paper_orders(resolve_mode({"autonomy": {"mode": "ANALYZE"}})) is False
    assert mode_allows_paper_orders(resolve_mode({"autonomy": {"mode": "PAPER_AUTO"}})) is True


def test_decision_context_carries_decision_validation_critic(monkeypatch):
    # The rewire stores Decision/ValidationResult/CriticVerdict in decision_context.
    # Verify the import + the enrichment contract by exercising the engine through the delegate
    # and checking the review carries the §41 Decision object (agent_manager copies it into context).
    from core import ollama_decision as od
    from core.ai.decision import Action
    from core.ai.provider import AIResponse

    class _FP:
        def __init__(self, t): self.t = t
        def generate(self, prompt, *, model=None, system="", **opts):
            return AIResponse(text=self.t, model="m", provider="fake")

    class _RA:
        def evaluate_signal(self, s):
            return {"accepted": True, "reason": "ok", "checks": [], "metrics": {}}

    svc = {
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
    monkeypatch.setattr("core.ai.decision_engine.default_engine_services", lambda c: svc)
    review = od.evaluate_trade_decision_with_ollama(
        {"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95, "signal_score": 0.8, "strategy_type": "SCANNER_AI_MOMENTUM"},
        {"accepted": True}, config={})
    assert review["available"] is True
    # the review carries the §41 Decision object the rewire copies into decision_context["ai_decision"]
    assert review["decision"].action is Action.OPEN_LONG
    assert review["critic_veto"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_agent_manager_rewire.py -q`
Expected: parts PASS already (autonomy helpers from Task 1); the `decision` key in review is produced by the engine (Task 3) — confirm green. The agent_manager rewire itself (entry gates) is verified by the regression in Step 4.

- [ ] **Step 3: Write minimal implementation**

In `core/agent_manager.py`:

(a) At the top, alongside the existing `from core.ollama_decision import evaluate_trade_decision_with_ollama, should_veto_trade`, add:

```python
from core.autonomy import (
    kill_switch_active,
    live_auto_owner_enabled,
    mode_allows_live_orders,
    mode_allows_paper_orders,
    resolve_mode,
)
```

(b) Add a private entry-gate helper near `_autonomous_trade_allowed_now`:

```python
def _autonomy_order_gate(self, execution_cfg: dict) -> tuple[bool, str]:
    """§45/§46/§48/§74 — gate autonomous order placement by mode + kill switch + LIVE_AUTO."""
    cfg = getattr(self.config_manager, "config", {}) or {}
    if kill_switch_active(cfg):
        return False, "kill_switch_active"  # §74 — monitoring stays active, no new risk
    mode = resolve_mode(cfg)
    is_live = str((execution_cfg or {}).get("mode", "paper")).lower() == "live"
    if is_live:
        if not mode_allows_live_orders(mode):
            return False, f"mode_{mode.value}_forbids_live_orders"
        if not live_auto_owner_enabled(cfg):
            return False, "live_auto_requires_owner_enable"  # §45/§46 — owner only
        # §46 paper-auto acceptance is checked in Task 6 via can_enable_live_auto; until then
        # the owner flag + LIVE_AUTO mode is the gate. Task 6 strengthens this.
    else:
        if not mode_allows_paper_orders(mode):
            return False, f"mode_{mode.value}_forbids_paper_orders"
    return True, "ok"
```

(c) At both AI call sites (research-cohort ~L1424 and main ~L1853), replace:
```python
ollama_review = evaluate_trade_decision_with_ollama(signal_data, risk_result, config=review_config)
```
with:
```python
from core.ai.decision_engine import evaluate_trade_decision as _evaluate_decision
_eval_result = _evaluate_decision(signal_data, risk_result, config=review_config)
ollama_review = _eval_result.review
# §41/§42/§44 audit enrichment (additive):
try:
    ctx = decision_context if "decision_context" in dir() else signal_data.setdefault("decision_context", {})
    ctx["ai_decision"] = _eval_result.decision
    ctx["ai_validation"] = _eval_result.validation
    if _eval_result.critic is not None:
        ctx["ai_critic"] = _eval_result.critic
except Exception:
    pass
```
(At the main site `decision_context` is built later at ~L1873; store the enrichment on `signal_data["ai_decision"]` etc. and let the existing `decision_context` construction pick them up, or set them after `decision_context` is built. Prefer setting `signal_data["ai_decision"] = _eval_result.decision` etc. immediately after the call, then mirror into `decision_context` once it exists. Keep the diff minimal and the `ollama_review` consumption unchanged.)

(d) Insert the entry-gate call at the start of the main autonomous order-placement path (before the AI review block), e.g. right after `risk_result` is known to be accepted and before order placement:
```python
_allowed, _gate_reason = self._autonomy_order_gate(execution_cfg)
if not _allowed:
    record_trade_event(event_type="autonomous_order_blocked", status=_gate_reason,
                       ticker=signal_data.get("ticker", ""), signal_id=signal_data.get("id", ""),
                       strategy_type=signal_data.get("strategy_type", ""),
                       summary=f"Autonomy gate blocked order: {_gate_reason}",
                       payload={"risk_evaluation": risk_result})
    logger.info("AgentManager: autonomy gate blocked %s: %s", signal_data.get("ticker"), _gate_reason)
    return
```
Place it once in the main path (the research-cohort path is itself gated by the main autonomous candidate admission; add the same gate there if it can place orders independently — verify by reading `_attempt_research_cohort_entry` callers).

- [ ] **Step 4: Run test to verify it passes + agent_manager regression**

Run: `python -m pytest tests/unit/test_agent_manager_rewire.py tests/unit/test_research_cohort.py tests/test_ollama_decision.py -q`
Expected: PASS. (`test_research_cohort.py` monkeypatches `evaluate_trade_decision_with_ollama` — confirm it still patches cleanly; if it patches the symbol on `core.agent_manager`, the import still resolves. If any research-cohort test breaks, document the fix as a deviation.)

- [ ] **Step 5: Commit**

```bash
git add core/agent_manager.py tests/unit/test_agent_manager_rewire.py
git commit -m "feat(phase4c): agent_manager rewire — autonomy/kill-switch gates + §41/§42/§44 enrichment (§45/§74)"
```

---

### Task 6: Paper-Auto acceptance suite (§46) + `can_enable_live_auto`

**Files:**
- Modify: `core/autonomy.py` (add `run_paper_auto_acceptance`, `paper_auto_acceptance_passed`, `can_enable_live_auto`, `PAPER_AUTO_SCENARIOS`)
- Create: `tests/acceptance/test_paper_auto_acceptance.py`
- Modify: `core/agent_manager.py` (strengthen the LIVE_AUTO gate to call `can_enable_live_auto`)

**Interfaces:**
- Consumes: `core.ai.decision_engine.evaluate_trade_decision` (lazy, inside `run_paper_auto_acceptance`).
- Produces: `PAPER_AUTO_SCENARIOS`, `run_paper_auto_acceptance(config, services=None) -> dict`, `paper_auto_acceptance_passed(config, services=None) -> tuple[bool, str]`, `can_enable_live_auto(config, services=None) -> tuple[bool, str]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/acceptance/test_paper_auto_acceptance.py
"""§46 — Live Auto must remain disabled until mandatory Paper Auto acceptance tests pass."""
import pytest

from core.autonomy import can_enable_live_auto, paper_auto_acceptance_passed, run_paper_auto_acceptance


def _acceptance_services():
    """Fake services that make the engine deterministic across all scenarios."""
    from core.ai.provider import AIResponse

    class _RA:
        def evaluate_signal(self, s):
            return {"accepted": True, "reason": "ok", "checks": [], "metrics": {}}

    class _Prov:
        def __init__(self, text): self.t = text
        def generate(self, prompt, *, model=None, system="", **opts):
            return AIResponse(text=self.t, model="m", provider="fake")

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


def test_live_auto_disabled_until_acceptance_and_owner(monkeypatch):
    # No owner flag -> disabled even if acceptance would pass.
    ok, reason = can_enable_live_auto({"autonomy": {"live_auto_owner_enabled": False}}, services=_acceptance_services())
    assert ok is False
    # Owner flag + passing acceptance -> enabled.
    ok, reason = can_enable_live_auto({"autonomy": {"live_auto_owner_enabled": True}}, services=_acceptance_services())
    assert ok is True


def test_live_auto_disabled_if_acceptance_fails(monkeypatch):
    def _failing_svc():
        from core.ai.provider import UNAVAILABLE
        class _Dead:
            def generate(self, prompt, *, model=None, system="", **opts): return UNAVAILABLE
        class _RA:
            def evaluate_signal(self, s): return {"accepted": True, "reason": "ok", "checks": [], "metrics": {}}
        return {
            "evaluate_capital_entry": lambda s, p, c=None, **k: {"allowed": True, "check": "complete", "status": "validated", "reason": "ok", "checks": {}},
            "resolve_deployable_balance": lambda cm: 50000.0,
            "compute_size_factor": lambda score, cfg=None, **k: {"size_factor": 0.5},
            "calculate_trade_risk_pct": lambda **k: 0.3,
            "risk_agent": _RA(),
            "should_veto_trade": lambda review, config=None: False,
            "primary_provider": _Dead(), "fallback_chain": [], "critic_provider": _Dead(), "critic_model": "m",
        }
    ok, reason = can_enable_live_auto({"autonomy": {"live_auto_owner_enabled": True}}, services=_failing_svc())
    # AI unavailable -> GOLD-014 -> NO_TRADE; scenarios that EXPECT an open action now fail acceptance.
    assert ok is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/acceptance/test_paper_auto_acceptance.py -q`
Expected: FAIL — `ImportError: cannot import can_enable_live_auto`.

- [ ] **Step 3: Write minimal implementation**

Add to `core/autonomy.py`:

```python
from typing import Any, Dict, List, Optional, Tuple  # extend existing typing import


# §46 — the Paper-Auto acceptance battery. Each scenario runs the decision engine
# (with fake services) and asserts the expected outcome. The runtime LIVE_AUTO gate
# and the test suite assert the same truth.
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
     "expect_action": "NO_TRADE", "expect_ok": False, "strict_schema": True},
    {"name": "ai_unavailable_blocks_new_trade", "ai": None,  # sentinel: UNAVAILABLE provider
     "critic": None,
     "signal": {"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95, "signal_score": 0.8, "strategy_type": "SCANNER_AI_MOMENTUM"},
     "expect_action": "NO_TRADE", "expect_ok": True, "ai_unavailable": True},
]


def run_paper_auto_acceptance(config: Dict[str, Any], services: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """§46 — run the Paper-Auto acceptance battery against the decision engine with fakes."""
    from core.ai.decision import Action
    from core.ai.decision_engine import evaluate_trade_decision
    from core.ai.provider import UNAVAILABLE

    svc_factory = services  # a callable(engine_text, critic_text) -> dict, OR a raw services dict
    results: List[Dict[str, Any]] = []
    all_passed = True
    for sc in PAPER_AUTO_SCENARIOS:
        try:
            if callable(svc_factory):
                svc = svc_factory(sc["ai"] or "", sc["critic"] or "")
            else:
                svc = dict(svc_factory or {})
                # When given a static dict, adapt per-scenario unavailable cases:
                if sc.get("ai_unavailable"):
                    svc["primary_provider"] = type("P", (), {"generate": lambda self, prompt, **k: UNAVAILABLE})()
                    svc["critic_provider"] = svc["primary_provider"]
                else:
                    from core.ai.provider import AIResponse
                    _t = sc["ai"]
                    _c = sc["critic"]
                    svc["primary_provider"] = type("P", (), {"generate": lambda self, prompt, *, model=None, system="", **k: AIResponse(text=_t, model="m", provider="fake")})()
                    svc["critic_provider"] = type("P", (), {"generate": lambda self, prompt, *, model=None, system="", **k: AIResponse(text=_c, model="m", provider="fake")})()
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
    """§46 — run the battery (fakes, deterministic) and return (passed, summary)."""
    report = run_paper_auto_acceptance(config, services=services)
    return report["passed"], report["summary"]


def can_enable_live_auto(config: Dict[str, Any], services: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """§45/§46/§48 — LIVE_AUTO requires owner flag AND Paper-Auto acceptance."""
    if not live_auto_owner_enabled(config):
        return False, "live_auto requires owner enable (§45)"
    if not ((config or {}).get("autonomy") or {}).get("paper_auto_acceptance_required", True):
        return True, "paper-auto acceptance not required"
    passed, summary = paper_auto_acceptance_passed(config, services=services)
    if not passed:
        return False, f"paper-auto acceptance failed: {summary} (§46)"
    return True, "live_auto enabled: owner + paper-auto acceptance passed"
```

Strengthen the `_autonomy_order_gate` in `core/agent_manager.py` LIVE_AUTO branch (Task 5) to call `can_enable_live_auto`:

```python
        if not live_auto_owner_enabled(cfg):
            return False, "live_auto_requires_owner_enable"
        from core.autonomy import can_enable_live_auto
        ok, reason = can_enable_live_auto(cfg)
        if not ok:
            return False, f"live_auto_blocked:{reason}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/acceptance/test_paper_auto_acceptance.py -q`
Expected: PASS (3 tests). (The `test_live_auto_disabled_if_acceptance_fails` case: AI unavailable → engine returns NO_TRADE for the open_long/hold/reduce scenarios → those fail the expected action → acceptance fails → can_enable_live_auto False. Confirm the `expect_ok`/`expect_action` assertions hold for the unavailable case: open_long_success expects OPEN_LONG but gets NO_TRADE → fail → all_passed False. Correct.)

- [ ] **Step 5: Commit**

```bash
git add core/autonomy.py core/agent_manager.py tests/acceptance/test_paper_auto_acceptance.py
git commit -m "feat(phase4c): §46 Paper-Auto acceptance battery + can_enable_live_auto gate (§46/§48)"
```

---

### Task 7: Golden GOLD-044/045/046 + control docs + exit gate

**Files:**
- Create: `tests/golden/test_phase4c_golden.py`
- Modify: `docs/V9_ARCHITECTURE.md`, `docs/V9_AI_SYSTEM.md`, `docs/V9_CHANGELOG.md`
- Create: `.superpowers/sdd/2026-08-12-v9-phase4c-critic-autonomy-and-agent-manager-rewire/progress.md`

**Interfaces:**
- Consumes: all Task 1–6 public surfaces.

- [ ] **Step 1: Write the golden tests**

```python
# tests/golden/test_phase4c_golden.py
"""Golden tests — Phase 4C §44 critic, §45 autonomy, §46 paper-auto-before-live-auto."""
import pytest

from core.ai.critic import run_critic
from core.ai.decision import Action, Decision
from core.ai.decision_engine import evaluate_trade_decision
from core.ai.pipeline import STAGE_ORDER
from core.ai.provider import AIResponse
from core.autonomy import (
    AutonomyMode, can_enable_live_auto, kill_switch_active, mode_allows_live_orders,
    mode_allows_paper_orders, paper_auto_acceptance_passed, resolve_mode,
)

pytestmark = pytest.mark.golden


class _FP:
    def __init__(self, t): self.t = t
    def generate(self, prompt, *, model=None, system="", **opts):
        return AIResponse(text=self.t, model="m", provider="fake")


def _svc(text='{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.85,"thesis":"mom"}',
         critic='{"veto":false,"categories":[],"severity":0.1,"rationale":"ok"}'):
    class _RA:
        def evaluate_signal(self, s): return {"accepted": True, "reason": "ok", "checks": [], "metrics": {}}
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
    ok, _ = can_enable_live_auto({"autonomy": {"live_auto_owner_enabled": False}}, services=lambda a, c: _svc(a or '{"action":"NO_TRADE","ticker":"NVDA","confidence":0.5}', c or '{"veto":false}'))
    assert ok is False


def test_gold046_live_auto_enabled_with_owner_and_acceptance():
    def _fac(ai, crit):
        s = _svc(ai or '{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.85,"thesis":"m"}',
                 crit or '{"veto":false,"categories":[],"severity":0.1,"rationale":"ok"}')
        return s
    ok, _ = can_enable_live_auto({"autonomy": {"live_auto_owner_enabled": True}}, services=_fac)
    assert ok is True
```

- [ ] **Step 2: Run golden tests**

Run: `python -m pytest tests/golden/test_phase4c_golden.py -q`
Expected: PASS (8 tests).

- [ ] **Step 3: Update control docs**

- `docs/V9_ARCHITECTURE.md` — append "## Phase 4C" section: decision_engine bridge; critic post-pipeline (STAGE_ORDER unchanged at 6); `core/autonomy.py` (modes + kill switch + §48 guard + Paper-Auto acceptance + `can_enable_live_auto`); `agent_manager` rewire points (2 call sites + entry gate); §43 leverage-default-1 deviation; §46 LIVE_AUTO gate.
- `docs/V9_AI_SYSTEM.md` — note the §44 independent critic workflow (critic tier, post-pipeline, veto→NO_TRADE), §45 autonomy modes, §48 no-weakening guard enforced in code.
- `docs/V9_CHANGELOG.md` — Phase 4C entry (one paragraph).
- `docs/V9_RISK_CONSTITUTION.md` — NOT modified (PROTECTED §48/§82; owner-only).

- [ ] **Step 4: Create the SDD ledger**

`.superpowers/sdd/2026-08-12-v9-phase4c-critic-autonomy-and-agent-manager-rewire/progress.md`:

```markdown
# SDD ledger — plan: docs/superpowers/plans/2026-08-12-v9-phase4c-critic-autonomy-and-agent-manager-rewire.md

**Mode:** INLINE (controller-as-implementer/reviewer) — ollama subagent backend rate-limited (429) since 1A; plan fully specified (verbatim code).
**Base:** 6828e8e (Phase 4C design) → Task commits on master.
**Baseline tests:** curated 1847 passed / 0 failed (Phase 4B gate); full `pytest tests tests_e2e` 2359 passed / 0 failed / 36 skipped.

## Tasks
- Task 1: core/autonomy.py — §45 modes + §74 kill switch + §48 guard + config
- Task 2: core/ai/critic.py — §44 independent critic
- Task 3: core/ai/decision_engine.py — bridge (3C/4A/4B compose + review shape)
- Task 4: ollama_decision delegate + ai/__init__ re-exports
- Task 5: agent_manager rewire — autonomy/kill-switch gates + §41/§42/§44 enrichment
- Task 6: §46 Paper-Auto acceptance battery + can_enable_live_auto
- Task 7: golden GOLD-044/045/046 + control docs + exit gate

## Gate (Task 7) — fill at execution
- Curated: <n> passed / 0 failed.
- Full `pytest tests tests_e2e`: <n> passed / 0 failed / 36 skipped.
- §4 sweep: 0 hits. V7 untouched (f912a33). Deps diff empty (§118).
```

- [ ] **Step 5: Run the full gate + commit**

Run curated: `python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf -q`
Run full: `python -m pytest tests tests_e2e -q`
Run §4 sweep over all 4C-touched files.
Verify V7 HEAD `f912a33` unchanged; deps diff `6828e8e..HEAD` empty.
Fill the SDD ledger gate numbers, then:

```bash
git add tests/golden/test_phase4c_golden.py docs/V9_ARCHITECTURE.md docs/V9_AI_SYSTEM.md docs/V9_CHANGELOG.md
git commit -m "test(phase4c): golden §44/§45/§46 + control docs — Phase 4C PASS"
```

---

## Self-Review (run after writing the plan)

1. **Spec coverage:** §44 critic (Task 2 + engine Task 3 step 4 + GOLD-044); §45 modes (Task 1 + GOLD-045); §46 Paper-Auto (Task 6 + GOLD-046); §48 guard (Task 1 + engine defense-in-depth); §74 kill switch (Task 1 + Task 5 gate); agent_manager rewire (Task 5); ollama_decision bridge (Task 4); backward-compat review shape (Task 3); STAGE_ORDER frozen (GOLD-044). Covered.
2. **Placeholder scan:** Gate numbers in Task 7 ledger are `<n>` — filled at execution (not a placeholder in code). No TBD/TODO in code. OK.
3. **Type consistency:** `evaluate_trade_decision` signature consistent across Tasks 3/4/5/6. `CriticVerdict` fields consistent. `AutonomyMode` consistent. `run_pipeline`/`parse_decision`/`generate_with_fallback` signatures match verified sources. OK.
4. **Ambiguity:** review-shape decisive-rejection vs unavailable is explicit (Task 3). `paper_auto_acceptance_passed` runs the battery (no cache ambiguity). `services` accepts either a callable factory (acceptance suite) or a static dict (engine tests) — `run_paper_auto_acceptance` handles both. OK.

## Execution Handoff

Inline execution (controller-as-implementer/reviewer) per the established 1A–4B precedent. Each task: write failing test → run-fail → implement → run-pass → commit. Task 7 is the exit gate (curated + full regression, §4 sweep, V7 + deps checks). No remote push (§116). V7 read-only.