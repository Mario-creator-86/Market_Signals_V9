# V9 Phase 3C — Model Assignment + Router + Fallback + §108 AI-Config API + Golden Tests

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the Phase 3 multi-provider AI platform — wire the 10-role Model Assignment (§37), the 3-tier Model Router (§38), the `generate_with_fallback` chain (GOLD-013) with the `all_unavailable_no_trade` guard (GOLD-014), the §108 AI-configuration API journey (configure → test → discover → assign → save → validated request), the MLflow AI Gateway evaluation record, and the GOLD-013/GOLD-014 golden tests — closing Phase 3.

**Architecture:** New modules under `core/ai/` consume the 3A `AIProvider` ABC + `UNAVAILABLE` sentinel and the 3B `ModelRegistry`. `assignment.py` resolves a model per role (with the `all` alias); `router.py` resolves a model per tier/role; `fallback.py` walks a provider chain returning the first non-error `AIResponse` or `UNAVAILABLE`, and exposes the autonomous-trade guard. A new `api/ai_config_service.py` + `api/main.py` routes expose the §108 journey; API keys never leave the backend (only `has_key: bool`, §106). The existing `core/ollama_decision.py` and `core/agent_manager.py` autonomous decision path are untouched (§5 evolve, §7 preserve — rewiring the autonomous loop to call `generate_with_fallback` is Phase 4 with the §41 decision schema + critic workflow).

**Tech Stack:** Python 3.14.3, FastAPI + pydantic, stdlib `dataclasses`/`threading`, the already-pinned `requests` via `core/http_utils` (zero new deps, §118), pytest (`golden` marker registered in `pytest.ini`).

## Global Constraints

Copied verbatim from the spec / earlier phase baselines — every task implicitly includes these:

- **§117:** V7 (`C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium`, HEAD `f912a33`) is READ-ONLY — never modify it; everything happens in V9.
- **§116:** No remote push (repo `remotes = 0`); commits stay local on `master`.
- **§106:** Never commit API keys; never store secrets in prompts; never expose keys to the frontend; never expose broker credentials to Coding AI; never log tokens. AI keys are read from env vars named by `api_key_env`; API responses carry only `has_key: bool`, never the key value.
- **§115:** No skipped/xfail critical test; no `assert True`; no deleted failing test; no weakened assertion.
- **§4:** No forbidden success language ("mostly complete", "approximately complete", "99%", "should work", "probably works", "backend finished", "frontend pending", "TODO later", "mock for now", "temporary workaround", "untested but implemented").
- **§5 / §7:** Evolve, no rewrite — preserve what works. `core/ollama_client.py`, `core/ollama_decision.py`, `core/agent_manager.py`, and the 13 Ollama call sites are NOT modified in 3C.
- **§118 / §25:** No new merged dependencies; pin dependencies. 3C adds no deps (stdlib + already-pinned `requests` via `core/http_utils` + already-pinned FastAPI/pydantic).
- **§35:** Do NOT hardcode model names; the provider factory builds providers from config (`type` + `base_url` + `api_key_env` + `default_model`), and models come from `list_models()` discovery.
- **§37:** 10 roles — `market_analyst`, `pre_earnings_analyst`, `news_analyst`, `quant_analyst`, `portfolio_manager`, `risk_critic`, `research_scientist`, `coding_agent`, `code_reviewer`, `security_reviewer`; allow one model for all roles (`all`).
- **§38:** 3 tiers — `triage` (fast/cheap), `analysis` (strong reasoning), `critic` (independent). Do not spend expensive AI calls on thousands of low-quality candidates.
- **§108:** E2E journey — Open AI Settings → configure provider → Test Connection → discover models → select role models → save → make validated request.
- **§107:** GOLD-013 (AI provider fallback), GOLD-014 (all AI unavailable → no new autonomous trade). All critical golden tests must PASS.

**Baseline:** `cddd301` (Phase 3B gate tip). Tests: 1704 passed / 0 failed / exit 0.

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `core/ai/assignment.py` | 10-role + `all` Model Assignment; resolve a model per role from `config["ai"]["assignment"]`. | NEW |
| `core/ai/router.py` | 3-tier Model Router; `select_model(role, tier_hint)` from `config["ai"]["router"]` + assignment. | NEW |
| `core/ai/fallback.py` | `generate_with_fallback` (GOLD-013) + `all_unavailable` + `no_new_autonomous_trade_guard` (GOLD-014). | NEW |
| `api/ai_config_service.py` | Provider factory + §108 service functions (list/test/discover/assignment/router/validated-request); keys never to frontend. | NEW |
| `api/main.py` | FastAPI routes for the §108 journey + pydantic request models. | MODIFY (additive — routes + models + import) |
| `core/ai/__init__.py` | Re-export `ModelAssignment`, `ModelRouter`, `generate_with_fallback`, `all_unavailable`, `no_new_autonomous_trade_guard`. | MODIFY (additive exports) |
| `tests/unit/test_ai_assignment.py` | ModelAssignment unit tests. | NEW |
| `tests/unit/test_ai_router.py` | ModelRouter unit tests. | NEW |
| `tests/unit/test_ai_fallback.py` | generate_with_fallback + guard unit tests. | NEW |
| `tests/unit/test_ai_config_service.py` | ai_config_service unit tests (fake providers, no keys/network). | NEW |
| `tests/integration/test_ai_config_routes.py` | §108 FastAPI route tests (TestClient). | NEW |
| `tests/golden/test_ai_fallback_golden.py` | GOLD-013 + GOLD-014 golden tests. | NEW |
| `docs/V9_TEST_MATRIX.md` | GOLD-013 + GOLD-014 rows → PASS. | MODIFY |
| `docs/V9_ARCHITECTURE.md` | "Assignment + Router + Fallback (Phase 3C)" subsection + MLflow AI Gateway eval record. | MODIFY (additive) |
| `docs/V9_IMPLEMENTATION.md` | Phase 3C record; Phase 3 row → PASS. | MODIFY |

**NOT modified:** `core/ollama_client.py`, `core/ollama_decision.py`, `core/agent_manager.py`, `core/ai/provider.py`, `core/ai/ollama_provider.py`, `core/ai/openai_provider.py`, `core/ai/anthropic_provider.py`, `core/ai/gemini_provider.py`, `core/ai/registry.py`, `core/config.py`, `requirements*.txt`/`pyproject.toml`/`setup.*`.

---

### Task 1: ModelAssignment (§37)

**Files:**
- Create: `core/ai/assignment.py`
- Modify: `core/ai/__init__.py`
- Test: `tests/unit/test_ai_assignment.py`

**Interfaces:**
- Consumes: `config["ai"]["assignment"]` (dict: `all` + 10 role keys → `model_id | None`, set in 3A `core/config.py`); `core.ai.registry.ModelRegistry` (for `validate`).
- Produces: `ROLES` (tuple of 10 role names), `ModelAssignment` class with `resolve(role) -> Optional[str]`, `set(role, model_id)`, `set_all(model_id)`, `to_dict() -> dict`, `from_config(config) -> ModelAssignment` (classmethod), `validate(model_id, registry) -> bool`.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_ai_assignment.py`:
```python
from core.ai.assignment import ROLES, ModelAssignment
from core.ai.registry import ModelEntry, ModelRegistry


def test_roles_are_the_ten_spec_roles():
    assert ROLES == (
        "market_analyst",
        "pre_earnings_analyst",
        "news_analyst",
        "quant_analyst",
        "portfolio_manager",
        "risk_critic",
        "research_scientist",
        "coding_agent",
        "code_reviewer",
        "security_reviewer",
    )
    assert len(ROLES) == 10


def test_resolve_returns_role_model_when_set():
    a = ModelAssignment({"market_analyst": "gpt-4o", "all": None})
    assert a.resolve("market_analyst") == "gpt-4o"


def test_resolve_falls_back_to_all_alias():
    a = ModelAssignment({"all": "claude-sonnet", "market_analyst": None})
    assert a.resolve("market_analyst") == "claude-sonnet"
    assert a.resolve("news_analyst") == "claude-sonnet"


def test_resolve_explicit_role_overrides_all():
    a = ModelAssignment({"all": "claude-sonnet", "risk_critic": "gpt-4o"})
    assert a.resolve("risk_critic") == "gpt-4o"
    assert a.resolve("news_analyst") == "claude-sonnet"


def test_resolve_unknown_role_returns_none():
    a = ModelAssignment({"all": "m"})
    assert a.resolve("nonexistent_role") is None


def test_set_and_set_all_mutate_in_memory():
    a = ModelAssignment({})
    a.set("quant_analyst", "q")
    assert a.resolve("quant_analyst") == "q"
    a.set_all("m")
    assert a.resolve("quant_analyst") == "q"  # explicit role wins
    assert a.resolve("news_analyst") == "m"   # all fills the rest


def test_to_dict_round_trips_all_roles():
    a = ModelAssignment({"all": "m", "risk_critic": "r"})
    d = a.to_dict()
    assert d["all"] == "m"
    assert d["risk_critic"] == "r"
    assert set(ROLES).issubset(set(d.keys()))


def test_from_config_reads_ai_assignment_block():
    config = {"ai": {"assignment": {"all": "m", "market_analyst": "ma"}}}
    a = ModelAssignment.from_config(config)
    assert a.resolve("market_analyst") == "ma"
    assert a.resolve("news_analyst") == "m"


def test_validate_checks_registry_membership():
    reg = ModelRegistry()
    reg.register(ModelEntry(provider="openai", model_id="gpt-4o", display_name="gpt-4o"))
    a = ModelAssignment({})
    assert a.validate("gpt-4o", reg) is True
    assert a.validate("not-a-model", reg) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_ai_assignment.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.ai.assignment'`

- [ ] **Step 3: Write minimal implementation**

`core/ai/assignment.py`:
```python
"""ModelAssignment (spec §37) — map each of the 10 AI roles to a model id.

The `all` alias, when set, applies to every role that has no explicit model.
Selection is explicit (§108 "select role models → save"); no model names are
hardcoded (§35) — model ids come from config / ModelRegistry discovery.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# The 10 §37 roles (snake_case config keys).
ROLES = (
    "market_analyst",
    "pre_earnings_analyst",
    "news_analyst",
    "quant_analyst",
    "portfolio_manager",
    "risk_critic",
    "research_scientist",
    "coding_agent",
    "code_reviewer",
    "security_reviewer",
)


class ModelAssignment:
    def __init__(self, assignment: Optional[Dict[str, Any]] = None) -> None:
        self._assignment: Dict[str, Any] = {role: None for role in ROLES}
        self._assignment["all"] = None
        if assignment:
            for k, v in assignment.items():
                if k in self._assignment:
                    self._assignment[k] = v

    def resolve(self, role: str) -> Optional[str]:
        """Return the model for `role`: explicit role model, else the `all` alias, else None."""
        explicit = self._assignment.get(role)
        if explicit:
            return explicit
        return self._assignment.get("all")

    def set(self, role: str, model_id: Optional[str]) -> None:
        if role in self._assignment:
            self._assignment[role] = model_id

    def set_all(self, model_id: Optional[str]) -> None:
        self._assignment["all"] = model_id

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._assignment)

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "ModelAssignment":
        ai = (config or {}).get("ai") or {}
        return cls(ai.get("assignment") or {})

    def validate(self, model_id: str, registry: Any) -> bool:
        """True iff `model_id` is registered in `registry` (§36 ModelRegistry)."""
        if model_id is None:
            return False
        entry = registry.get(model_id) if registry is not None else None
        return entry is not None
```

Add to `core/ai/__init__.py` (after the existing `ModelRegistry` import line):
```python
from core.ai.assignment import ROLES, ModelAssignment
```
and add `"ROLES"`, `"ModelAssignment"` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_ai_assignment.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add core/ai/assignment.py core/ai/__init__.py tests/unit/test_ai_assignment.py
git commit -m "feat(phase3c): ModelAssignment (§37, 10 roles + all alias)"
```

---

### Task 2: ModelRouter (§38)

**Files:**
- Create: `core/ai/router.py`
- Modify: `core/ai/__init__.py`
- Test: `tests/unit/test_ai_router.py`

**Interfaces:**
- Consumes: `config["ai"]["router"]` (dict: `triage`/`analysis`/`critic` → `{model: model_id | None}`, set in 3A); `core.ai.assignment.ModelAssignment`.
- Produces: `TIERS` (tuple `("triage", "analysis", "critic")`), `ModelRouter` class with `select_model(role, tier_hint=None) -> Optional[str]`, `to_dict()`, `from_config(config, assignment) -> ModelRouter` (classmethod).

- [ ] **Step 1: Write the failing test**

`tests/unit/test_ai_router.py`:
```python
from core.ai.router import TIERS, ModelRouter
from core.ai.assignment import ModelAssignment


def test_tiers_are_triage_analysis_critic():
    assert TIERS == ("triage", "analysis", "critic")


def _setup(assignment_dict, router_dict):
    return ModelRouter(router_dict, ModelAssignment(assignment_dict))


def test_tier_model_wins_when_set():
    r = _setup({"all": "cheap"}, {"triage": {"model": "fast-mini"}, "analysis": {"model": None}, "critic": {"model": None}})
    assert r.select_model("market_analyst", tier_hint="triage") == "fast-mini"


def test_falls_back_to_assignment_when_tier_unset():
    r = _setup({"all": "cheap"}, {"triage": {"model": None}, "analysis": {"model": None}, "critic": {"model": None}})
    assert r.select_model("market_analyst", tier_hint="triage") == "cheap"


def test_falls_back_to_assignment_when_no_tier_hint():
    r = _setup({"market_analyst": "ma"}, {"triage": {"model": None}, "analysis": {"model": None}, "critic": {"model": None}})
    assert r.select_model("market_analyst") == "ma"


def test_tier_hint_unknown_treated_as_no_hint():
    r = _setup({"all": "m"}, {"triage": {"model": None}, "analysis": {"model": None}, "critic": {"model": None}})
    assert r.select_model("news_analyst", tier_hint="bogus") == "m"


def test_critic_tier_uses_critic_model():
    r = _setup({"risk_critic": None}, {"triage": {"model": None}, "analysis": {"model": None}, "critic": {"model": "independent-1"}})
    assert r.select_model("risk_critic", tier_hint="critic") == "independent-1"


def test_to_dict_round_trips():
    r = _setup({}, {"triage": {"model": "t"}, "analysis": {"model": None}, "critic": {"model": "c"}})
    d = r.to_dict()
    assert d["triage"]["model"] == "t"
    assert d["critic"]["model"] == "c"


def test_from_config_reads_ai_router_block():
    config = {"ai": {"router": {"triage": {"model": "t"}, "analysis": {"model": None}, "critic": {"model": "c"}}, "assignment": {"all": "m"}}}
    r = ModelRouter.from_config(config, ModelAssignment.from_config(config))
    assert r.select_model("market_analyst", tier_hint="triage") == "t"
    assert r.select_model("market_analyst", tier_hint="critic") == "c"
    assert r.select_model("market_analyst", tier_hint="analysis") == "m"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_ai_router.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.ai.router'`

- [ ] **Step 3: Write minimal implementation**

`core/ai/router.py`:
```python
"""ModelRouter (spec §38) — triage / analysis / critic tiers.

`select_model(role, tier_hint)` returns the model for a role given a tier hint:
the tier's configured model wins (fast/cheap → triage, strong → analysis,
independent → critic); otherwise it falls back to the ModelAssignment for the
role. This is how V9 avoids spending expensive AI calls on thousands of
low-quality candidates (§38): triage runs on the cheap tier, only survivors
escalate to analysis/critic.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from core.ai.assignment import ModelAssignment

TIERS = ("triage", "analysis", "critic")


class ModelRouter:
    def __init__(self, router: Optional[Dict[str, Any]], assignment: ModelAssignment) -> None:
        self._router: Dict[str, Dict[str, Any]] = {t: {"model": None} for t in TIERS}
        if isinstance(router, dict):
            for t in TIERS:
                if isinstance(router.get(t), dict):
                    self._router[t] = {"model": router[t].get("model")}
        self._assignment = assignment

    def select_model(self, role: str, tier_hint: Optional[str] = None) -> Optional[str]:
        if tier_hint in self._router:
            tier_model = self._router[tier_hint].get("model")
            if tier_model:
                return tier_model
        return self._assignment.resolve(role)

    def to_dict(self) -> Dict[str, Any]:
        return {t: dict(self._router[t]) for t in TIERS}

    @classmethod
    def from_config(cls, config: Dict[str, Any], assignment: ModelAssignment) -> "ModelRouter":
        ai = (config or {}).get("ai") or {}
        return cls(ai.get("router") or {}, assignment)
```

Add to `core/ai/__init__.py`:
```python
from core.ai.router import TIERS, ModelRouter
```
and add `"TIERS"`, `"ModelRouter"` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_ai_router.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add core/ai/router.py core/ai/__init__.py tests/unit/test_ai_router.py
git commit -m "feat(phase3c): ModelRouter (§38, triage/analysis/critic tiers)"
```

---

### Task 3: generate_with_fallback (GOLD-013) + no_new_autonomous_trade_guard (GOLD-014)

**Files:**
- Create: `core/ai/fallback.py`
- Modify: `core/ai/__init__.py`
- Test: `tests/unit/test_ai_fallback.py`

**Interfaces:**
- Consumes: `core.ai.provider.AIProvider` (the `generate` capability), `core.ai.provider.AIResponse`, `core.ai.provider.UNAVAILABLE` (the 3A GOLD-014 sentinel).
- Produces: `generate_with_fallback(primary, prompt, *, model=None, fallback_chain=None, system="", **opts) -> AIResponse`; `all_unavailable(response) -> bool`; `no_new_autonomous_trade_guard(response, config) -> bool`.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_ai_fallback.py`:
```python
from core.ai.fallback import all_unavailable, generate_with_fallback, no_new_autonomous_trade_guard
from core.ai.provider import AIResponse, UNAVAILABLE


class _FakeProvider:
    def __init__(self, name, *, fail=False, text="ok", model="m"):
        self.name = name
        self._fail = fail
        self._text = text
        self.default_model = model
        self.calls = 0

    def generate(self, prompt, *, model=None, system="", **opts):
        self.calls += 1
        if self._fail:
            return AIResponse(text="", error=f"{self.name} unavailable")
        return AIResponse(text=self._text, model=model or self.default_model)


def test_primary_serves_when_healthy():
    primary = _FakeProvider("openai", text="primary-answer")
    resp = generate_with_fallback(primary, "hello", model="gpt-4o")
    assert resp.text == "primary-answer"
    assert resp.is_error is False
    assert resp.model == "gpt-4o"


def test_fallback_chain_serves_when_primary_fails_gold013():
    primary = _FakeProvider("openai", fail=True)
    fb1 = _FakeProvider("anthropic", fail=True)
    fb2 = _FakeProvider("gemini", text="gemini-answer")
    resp = generate_with_fallback(primary, "hello", model="gpt-4o", fallback_chain=[fb1, fb2])
    assert resp.text == "gemini-answer"
    assert resp.is_error is False
    assert primary.calls == 1 and fb1.calls == 1 and fb2.calls == 1


def test_all_fail_returns_unavailable_sentinel_gold013():
    primary = _FakeProvider("openai", fail=True)
    fb = _FakeProvider("anthropic", fail=True)
    resp = generate_with_fallback(primary, "hello", model="gpt-4o", fallback_chain=[fb])
    assert all_unavailable(resp) is True
    assert resp is UNAVAILABLE


def test_no_fallback_chain_primary_fail_returns_unavailable():
    primary = _FakeProvider("openai", fail=True)
    resp = generate_with_fallback(primary, "hello", model="gpt-4o", fallback_chain=[])
    assert all_unavailable(resp) is True


def test_all_unavailable_recognizes_sentinel_and_equivalent_error():
    assert all_unavailable(UNAVAILABLE) is True
    assert all_unavailable(AIResponse(text="", error="all providers unavailable")) is True
    assert all_unavailable(AIResponse(text="ok")) is False
    assert all_unavailable(AIResponse(text="", error="some other error")) is False


def test_guard_blocks_when_all_unavailable_and_flag_set_gold014():
    config = {"ai": {"all_unavailable_no_trade": True}}
    assert no_new_autonomous_trade_guard(UNAVAILABLE, config) is True


def test_guard_does_not_block_when_ai_available_gold014():
    config = {"ai": {"all_unavailable_no_trade": True}}
    assert no_new_autonomous_trade_guard(AIResponse(text="ok"), config) is False


def test_guard_does_not_block_when_flag_disabled():
    config = {"ai": {"all_unavailable_no_trade": False}}
    assert no_new_autonomous_trade_guard(UNAVAILABLE, config) is False


def test_guard_defaults_to_block_when_flag_missing_is_treated_true():
    # Spec default (3A config) is True; missing key must not silently allow trades.
    config = {"ai": {}}
    assert no_new_autonomous_trade_guard(UNAVAILABLE, config) is True


def test_fallback_chain_empty_and_primary_ok_no_extra_calls():
    primary = _FakeProvider("openai", text="ok")
    generate_with_fallback(primary, "hello", model="m", fallback_chain=[])
    assert primary.calls == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_ai_fallback.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.ai.fallback'`

- [ ] **Step 3: Write minimal implementation**

`core/ai/fallback.py`:
```python
"""Fallback + autonomous-trade guard (spec §38 chain, §107 GOLD-013/ GOLD-014).

`generate_with_fallback` walks a provider chain (GOLD-013): the primary provider
is tried first with the requested model; on any error the next provider in
`fallback_chain` is tried with ITS OWN default model (§35 — no hardcoded model
cross-provider). If every provider fails, the 3A `UNAVAILABLE` sentinel is
returned (identity-stable) so callers can gate on it.

`no_new_autonomous_trade_guard` enforces GOLD-014: when
`config["ai"]["all_unavailable_no_trade"]` is true (the 3A default) and the
response is `UNAVAILABLE`, autonomous trading must NOT open a new position.
The spec default is True; a missing key is treated as True (never silently
allow a trade when the policy is unspecified).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.ai.provider import AIProvider, AIResponse, UNAVAILABLE


def generate_with_fallback(
    primary: AIProvider,
    prompt: str,
    *,
    model: Optional[str] = None,
    fallback_chain: Optional[List[AIProvider]] = None,
    system: str = "",
    **opts: Any,
) -> AIResponse:
    """Try `primary` (with `model`), then each fallback provider (its default model).

    Returns the first non-error AIResponse, or the `UNAVAILABLE` sentinel if all
    providers error. An error response (`response.is_error`) triggers the next hop.
    """
    candidates: List[tuple] = [(primary, model)]
    for fb in fallback_chain or []:
        candidates.append((fb, None))  # fallback uses its own default_model

    last_error: Optional[AIResponse] = None
    for provider, mdl in candidates:
        try:
            resp = provider.generate(prompt, model=mdl, system=system, **opts)
        except Exception as exc:  # a raised exception = this provider unavailable
            last_error = AIResponse(text="", error=f"{getattr(provider, 'name', '?')}: {exc}")
            continue
        if resp is None:
            last_error = AIResponse(text="", error=f"{getattr(provider, 'name', '?')}: None response")
            continue
        if resp.is_error:
            last_error = resp
            continue
        return resp
    # Every provider failed: return the identity-stable sentinel (GOLD-014).
    return UNAVAILABLE


def all_unavailable(response: AIResponse) -> bool:
    """True iff `response` is the all-providers-unavailable sentinel (GOLD-014)."""
    if response is UNAVAILABLE:
        return True
    return bool(response) and not response.text and response.error == UNAVAILABLE.error


def no_new_autonomous_trade_guard(response: AIResponse, config: Dict[str, Any]) -> bool:
    """GOLD-014 — True when autonomous trading must NOT open a new position.

    Blocks iff `config["ai"]["all_unavailable_no_trade"]` is true (default True
    when the key is missing) AND `response` is the all-unavailable sentinel.
    """
    ai = (config or {}).get("ai") or {}
    flag = ai.get("all_unavailable_no_trade")
    flag_enabled = True if flag is None else bool(flag)
    return flag_enabled and all_unavailable(response)
```

Add to `core/ai/__init__.py`:
```python
from core.ai.fallback import all_unavailable, generate_with_fallback, no_new_autonomous_trade_guard
```
and add `"all_unavailable"`, `"generate_with_fallback"`, `"no_new_autonomous_trade_guard"` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_ai_fallback.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add core/ai/fallback.py core/ai/__init__.py tests/unit/test_ai_fallback.py
git commit -m "feat(phase3c): generate_with_fallback (GOLD-013) + no_new_autonomous_trade_guard (GOLD-014)"
```

---

### Task 4: ai_config_service + §108 API routes

**Files:**
- Create: `api/ai_config_service.py`
- Modify: `api/main.py` (additive — import + pydantic request models + routes)
- Test: `tests/unit/test_ai_config_service.py`
- Test: `tests/integration/test_ai_config_routes.py`

**Interfaces:**
- Consumes: `core.config.ConfigManager` / `CONFIG_PATH` (the existing config load/save path); `api.config_service.get_config`/`save_config` (reuse redaction + deep-merge, §106); `core.ai` providers + `ModelRegistry` + `ModelAssignment` + `ModelRouter`; `core.ai.ollama_provider.get_ollama_provider`.
- Produces: `build_provider(config, name) -> AIProvider | None` factory; `list_ai_providers(config) -> list[dict]` (with `has_key: bool`, never the key); `test_ai_provider(config, name) -> dict`; `discover_ai_models(config, name) -> list[dict]`; `get_ai_assignment(config) -> dict` / `set_ai_assignment(config, assignment) -> bool`; `get_ai_router(config) -> dict` / `set_ai_router(config, router) -> bool`; `ai_validated_request(config, name, prompt, *, model=None) -> dict`.

**§106 rule (load-bearing):** `list_ai_providers` returns `has_key: bool` derived from `os.getenv(api_key_env)`, NEVER the key value. The redaction in `api.config_service.redact_secrets` already masks `api_key`, but `api_key_env` (the env-var NAME) is not secret — only the resolved key value is. The service never reads the key into a response payload.

- [ ] **Step 1: Write the failing unit test**

`tests/unit/test_ai_config_service.py`:
```python
import os
from unittest.mock import MagicMock

from api.ai_config_service import (
    build_provider,
    list_ai_providers,
    test_ai_provider,
    discover_ai_models,
    ai_validated_request,
)


def _config(providers=None):
    base = {
        "ai": {
            "providers": {
                "ollama": {"enabled": True, "type": "ollama"},
                "openai": {
                    "enabled": True,
                    "type": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "api_key_env": "OPENAI_API_KEY",
                    "default_model": "",
                    "timeout": 60,
                },
                "xai": {
                    "enabled": False,
                    "type": "openai_compatible",
                    "base_url": "https://api.x.ai/v1",
                    "api_key_env": "XAI_API_KEY",
                    "default_model": "",
                    "timeout": 60,
                },
            },
            "registry": [],
        }
    }
    if providers:
        base["ai"]["providers"].update(providers)
    return base


def test_build_provider_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    p = build_provider(_config(), "openai")
    assert p is not None
    assert p.name == "openai"
    assert p.base_url == "https://api.openai.com/v1"
    assert p.api_key_env == "OPENAI_API_KEY"


def test_build_provider_openai_compatible_for_xai(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    p = build_provider(_config(), "xai")
    assert p is not None
    # OpenAICompatibleProvider subclasses OpenAIProvider (same wire shape)
    assert p.base_url == "https://api.x.ai/v1"


def test_build_provider_ollama_returns_cached_singleton():
    p = build_provider(_config(), "ollama")
    assert p is not None
    assert p.name == "ollama"


def test_build_provider_unknown_returns_none():
    assert build_provider(_config(), "nope") is None


def test_build_provider_disabled_returns_none():
    cfg = _config()
    p = build_provider(cfg, "xai")  # xai enabled=False in _config
    assert p is None


def test_list_ai_providers_returns_has_key_not_the_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    rows = list_ai_providers(_config())
    names = {r["name"]: r for r in rows}
    assert "openai" in names
    assert names["openai"]["has_key"] is True
    # §106 — the key value must NEVER appear in the payload
    assert "sk-secret-value" not in str(rows)
    assert "api_key" not in names["openai"]  # only has_key, never the value


def test_list_ai_providers_has_key_false_when_env_missing(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rows = list_ai_providers(_config())
    names = {r["name"]: r for r in rows}
    assert names["openai"]["has_key"] is False


def test_test_ai_provider_reports_ok(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = _config()
    fake_provider = MagicMock()
    fake_provider.health_check.return_value = True
    fake_provider.list_models.return_value = ["gpt-4o", "gpt-4o-mini"]
    import api.ai_config_service as svc
    monkeypatch.setattr(svc, "build_provider", lambda c, n: fake_provider)
    result = test_ai_provider(cfg, "openai")
    assert result["ok"] is True
    assert result["models"] == ["gpt-4o", "gpt-4o-mini"]


def test_test_ai_provider_reports_not_ok_when_unhealthy(monkeypatch):
    cfg = _config()
    fake_provider = MagicMock()
    fake_provider.health_check.return_value = False
    import api.ai_config_service as svc
    monkeypatch.setattr(svc, "build_provider", lambda c, n: fake_provider)
    result = test_ai_provider(cfg, "openai")
    assert result["ok"] is False


def test_discover_ai_models_returns_entries(monkeypatch):
    cfg = _config()
    fake_provider = MagicMock()
    fake_provider.name = "openai"
    fake_provider.list_models.return_value = ["gpt-4o", "o3-mini"]
    fake_provider.capabilities.return_value = MagicMock(structured_output=True, tool_calling=True)
    import api.ai_config_service as svc
    monkeypatch.setattr(svc, "build_provider", lambda c, n: fake_provider)
    rows = discover_ai_models(cfg, "openai")
    ids = {r["model_id"] for r in rows}
    assert {"gpt-4o", "o3-mini"} == ids
    for r in rows:
        assert r["provider"] == "openai"


def test_ai_validated_request_returns_text_and_usage(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = _config()
    fake_provider = MagicMock()
    fake_provider.generate.return_value = MagicMock(text="answer", is_error=False, error=None, model="gpt-4o", usage=MagicMock(prompt_tokens=5, completion_tokens=3, total_tokens=8))
    import api.ai_config_service as svc
    monkeypatch.setattr(svc, "build_provider", lambda c, n: fake_provider)
    out = ai_validated_request(cfg, "openai", "hello", model="gpt-4o")
    assert out["ok"] is True
    assert out["text"] == "answer"
    assert out["model"] == "gpt-4o"
    assert "key" not in out  # §106 — no key in the response
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_ai_config_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'api.ai_config_service'`

- [ ] **Step 3: Write minimal implementation**

`api/ai_config_service.py`:
```python
# api/ai_config_service.py — §108 AI configuration service (spec §34–§38, §106, §108)
"""Multi-provider AI configuration service for the §108 E2E journey:
configure provider → Test Connection → discover models → select role models →
save → make a validated request.

§106: API keys NEVER leave the backend. `list_ai_providers` returns `has_key: bool`
(derived from the named env var), never the key value. No key is logged.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from core.ai import (
    AIProvider,
    AnthropicProvider,
    GeminiProvider,
    ModelAssignment,
    ModelRegistry,
    ModelRouter,
    OpenAICompatibleProvider,
    OpenAIProvider,
    get_ollama_provider,
)
from core.config import CONFIG_PATH, ConfigManager

logger = logging.getLogger("AIConfigService")


def _load_config() -> Dict[str, Any]:
    cm = ConfigManager(CONFIG_PATH)
    cm.reload_if_changed()
    return cm._config


def _provider_cfg(config: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    providers = (config or {}).get("ai", {}).get("providers", {}) or {}
    cfg = providers.get(name)
    if not isinstance(cfg, dict) or not cfg.get("enabled", False):
        return None
    return cfg


def build_provider(config: Dict[str, Any], name: str, *, http_post=None, http_get=None) -> Optional[AIProvider]:
    """Construct the AIProvider for `name` from config. Returns None if disabled/unknown.

    §35: no model names are hardcoded — `default_model` comes from config (`""` = discover).
    """
    cfg = _provider_cfg(config, name)
    if cfg is None:
        return None
    ptype = cfg.get("type")
    if ptype == "ollama":
        return get_ollama_provider()
    base_url = cfg.get("base_url", "")
    api_key_env = cfg.get("api_key_env", "")
    default_model = cfg.get("default_model", "")
    timeout = int(cfg.get("timeout", 60) or 60)
    if ptype == "openai":
        return OpenAIProvider(name=name, base_url=base_url, api_key_env=api_key_env, default_model=default_model, timeout=timeout, http_post=http_post, http_get=http_get)
    if ptype == "anthropic":
        return AnthropicProvider(name=name, base_url=base_url, api_key_env=api_key_env, default_model=default_model, timeout=timeout, http_post=http_post, http_get=http_get)
    if ptype == "gemini":
        return GeminiProvider(name=name, base_url=base_url, api_key_env=api_key_env, default_model=default_model, timeout=timeout, http_post=http_post, http_get=http_get)
    if ptype == "openai_compatible":
        return OpenAICompatibleProvider(name=name, base_url=base_url, api_key_env=api_key_env, default_model=default_model, timeout=timeout, http_post=http_post, http_get=http_get)
    return None


def list_ai_providers(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """List configured AI providers. §106 — returns `has_key: bool`, NEVER the key value."""
    providers = (config or {}).get("ai", {}).get("providers", {}) or {}
    rows: List[Dict[str, Any]] = []
    for name, cfg in providers.items():
        if not isinstance(cfg, dict):
            continue
        api_key_env = cfg.get("api_key_env")
        has_key = bool(api_key_env) and bool(os.getenv(api_key_env))
        rows.append({
            "name": name,
            "enabled": bool(cfg.get("enabled", False)),
            "type": cfg.get("type", ""),
            "default_model": cfg.get("default_model", ""),
            "has_key": has_key,
            # base_url is not secret; include it so the UI can show the endpoint.
            "base_url": cfg.get("base_url", "") if cfg.get("type") != "ollama" else "",
        })
    return rows


def test_ai_provider(config: Dict[str, Any], name: str) -> Dict[str, Any]:
    """§108 Test Connection — build the provider and run health_check + list_models."""
    provider = build_provider(config, name)
    if provider is None:
        return {"ok": False, "error": "provider disabled or not configured"}
    try:
        ok = bool(provider.health_check())
        models = list(provider.list_models()) if ok else []
        return {"ok": ok, "models": models}
    except Exception as exc:
        logger.warning("AI provider %s test failed: %s", name, exc)
        return {"ok": False, "error": str(exc)}


def discover_ai_models(config: Dict[str, Any], name: str) -> List[Dict[str, Any]]:
    """§108 discover models — populate the ModelRegistry from the provider's list_models()."""
    provider = build_provider(config, name)
    if provider is None:
        return []
    registry = ModelRegistry()
    ai = (config or {}).get("ai", {})
    registry.from_list(ai.get("registry") or [])
    added = registry.discover(provider)
    return [entry_to_dict(e) for e in added]


def entry_to_dict(entry) -> Dict[str, Any]:
    from dataclasses import asdict
    return asdict(entry)


def get_ai_assignment(config: Dict[str, Any]) -> Dict[str, Any]:
    return ModelAssignment.from_config(config).to_dict()


def set_ai_assignment(config: Dict[str, Any], assignment: Dict[str, Any]) -> bool:
    from api.config_service import save_config
    merged = {"ai": {"assignment": assignment}}
    return save_config(merged)


def get_ai_router(config: Dict[str, Any]) -> Dict[str, Any]:
    assignment = ModelAssignment.from_config(config)
    return ModelRouter.from_config(config, assignment).to_dict()


def set_ai_router(config: Dict[str, Any], router: Dict[str, Any]) -> bool:
    from api.config_service import save_config
    merged = {"ai": {"router": router}}
    return save_config(merged)


def ai_validated_request(
    config: Dict[str, Any],
    name: str,
    prompt: str,
    *,
    model: Optional[str] = None,
    system: str = "",
) -> Dict[str, Any]:
    """§108 validated request — a single generate() through the provider. No key in response (§106)."""
    provider = build_provider(config, name)
    if provider is None:
        return {"ok": False, "error": "provider disabled or not configured"}
    try:
        resp = provider.generate(prompt, model=model, system=system)
        if resp.is_error:
            return {"ok": False, "error": resp.error}
        usage = resp.usage
        return {
            "ok": True,
            "text": resp.text,
            "model": resp.model,
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            },
        }
    except Exception as exc:
        logger.warning("AI validated request to %s failed: %s", name, exc)
        return {"ok": False, "error": str(exc)}
```

- [ ] **Step 4: Run unit test to verify it passes**

Run: `pytest tests/unit/test_ai_config_service.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Write the integration test for the routes**

`tests/integration/test_ai_config_routes.py`:
```python
from unittest.mock import MagicMock


def test_get_ai_providers_route(client, monkeypatch):
    # ollama is enabled by default; ensure no key value leaks (§106)
    resp = client.get("/api/ai/providers")
    assert resp.status_code == 200
    rows = resp.json()
    names = {r["name"]: r for r in rows}
    assert "ollama" in names
    assert "has_key" in names["ollama"]
    assert "api_key" not in names["ollama"]


def test_test_ai_provider_route_disabled(client):
    resp = client.post("/api/ai/providers/nope/test")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_get_assignment_route(client):
    resp = client.get("/api/ai/assignment")
    assert resp.status_code == 200
    body = resp.json()
    assert "all" in body
    assert "market_analyst" in body


def test_get_router_route(client):
    resp = client.get("/api/ai/router")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"triage", "analysis", "critic"}


def test_validated_request_route_provider_disabled(client):
    resp = client.post("/api/ai/request", json={"provider": "nope", "prompt": "hi"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_test_ai_provider_route_with_fake(client, monkeypatch):
    fake = MagicMock()
    fake.health_check.return_value = True
    fake.list_models.return_value = ["m1"]
    import api.ai_config_service as svc
    monkeypatch.setattr(svc, "build_provider", lambda c, n: fake)
    resp = client.post("/api/ai/providers/openai/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["models"] == ["m1"]


def test_discover_route_with_fake(client, monkeypatch):
    fake = MagicMock()
    fake.name = "openai"
    fake.list_models.return_value = ["m1", "m2"]
    fake.capabilities.return_value = MagicMock(structured_output=True, tool_calling=True)
    import api.ai_config_service as svc
    monkeypatch.setattr(svc, "build_provider", lambda c, n: fake)
    resp = client.post("/api/ai/providers/openai/discover")
    assert resp.status_code == 200
    ids = {r["model_id"] for r in resp.json()}
    assert {"m1", "m2"} == ids
```

- [ ] **Step 6: Add routes + request models to `api/main.py`**

Add to the imports section (near the existing `from api.config_service import get_config, save_config` line ~59):
```python
from api.ai_config_service import (
    ai_validated_request,
    discover_ai_models,
    get_ai_assignment,
    get_ai_router,
    list_ai_providers,
    set_ai_assignment,
    set_ai_router,
    test_ai_provider,
)
```

Add pydantic request models near the other `BaseModel` classes (after `ConfigUpdateRequest`, ~line 940):
```python
class AIAssignmentRequest(BaseModel):
    assignment: Dict[str, Any] = Field(default_factory=dict)

class AIRouterRequest(BaseModel):
    router: Dict[str, Any] = Field(default_factory=dict)

class AIValidatedRequest(BaseModel):
    provider: str
    prompt: str
    model: Optional[str] = None
    system: str = ""
```
(`Any` and `Optional` are already imported in main.py.)

Add the routes (place after the `/api/config` POST block, ~line 2036):
```python
@app.get("/api/ai/providers")
def api_ai_providers():
    """§108 — list configured AI providers (has_key only, never the key — §106)."""
    try:
        from api.config_service import get_config
        return list_ai_providers(get_config())
    except Exception as e:
        logger.error(f"Error listing AI providers: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/ai/providers/{name}/test")
def api_ai_test_provider(name: str):
    """§108 — Test Connection."""
    try:
        from api.config_service import get_config
        return test_ai_provider(get_config(), name)
    except Exception as e:
        logger.error(f"Error testing AI provider {name}: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/ai/providers/{name}/discover")
def api_ai_discover_models(name: str):
    """§108 — discover models into the registry."""
    try:
        from api.config_service import get_config
        return discover_ai_models(get_config(), name)
    except Exception as e:
        logger.error(f"Error discovering models for {name}: {e}", exc_info=True)
        raise _safe_error()


@app.get("/api/ai/assignment")
def api_ai_get_assignment():
    try:
        from api.config_service import get_config
        return get_ai_assignment(get_config())
    except Exception as e:
        logger.error(f"Error getting AI assignment: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/ai/assignment")
def api_ai_set_assignment(request: AIAssignmentRequest):
    """§108 — select role models → save."""
    try:
        from api.config_service import get_config
        set_ai_assignment(get_config(), request.assignment)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error saving AI assignment: {e}", exc_info=True)
        raise _safe_error()


@app.get("/api/ai/router")
def api_ai_get_router():
    try:
        from api.config_service import get_config
        return get_ai_router(get_config())
    except Exception as e:
        logger.error(f"Error getting AI router: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/ai/router")
def api_ai_set_router(request: AIRouterRequest):
    try:
        from api.config_service import get_config
        set_ai_router(get_config(), request.router)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error saving AI router: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/ai/request")
def api_ai_validated_request(request: AIValidatedRequest):
    """§108 — make a validated request (no key in response — §106)."""
    try:
        from api.config_service import get_config
        return ai_validated_request(get_config(), request.provider, request.prompt, model=request.model, system=request.system)
    except Exception as e:
        logger.error(f"Error in AI validated request: {e}", exc_info=True)
        raise _safe_error()
```

- [ ] **Step 7: Run integration test to verify it passes**

Run: `pytest tests/integration/test_ai_config_routes.py -v`
Expected: PASS (7 tests)

- [ ] **Step 8: Commit**

```bash
git add api/ai_config_service.py api/main.py tests/unit/test_ai_config_service.py tests/integration/test_ai_config_routes.py
git commit -m "feat(phase3c): ai_config_service + §108 API routes (has_key only, §106)"
```

---

### Task 5: Golden tests GOLD-013/014 + control docs + exit gate → Phase 3 PASS

**Files:**
- Create: `tests/golden/test_ai_fallback_golden.py`
- Modify: `docs/V9_TEST_MATRIX.md` (GOLD-013, GOLD-014 → PASS)
- Modify: `docs/V9_ARCHITECTURE.md` (additive: Assignment + Router + Fallback subsection + MLflow AI Gateway eval record)
- Modify: `docs/V9_IMPLEMENTATION.md` (Phase 3C record; Phase 3 row → PASS)

**Interfaces:**
- Consumes: `core.ai.fallback.generate_with_fallback`, `all_unavailable`, `no_new_autonomous_trade_guard`; `core.ai.provider.UNAVAILABLE`; `core.ai.assignment.ModelAssignment`; `core.ai.router.ModelRouter`.

- [ ] **Step 1: Write the golden tests**

`tests/golden/test_ai_fallback_golden.py`:
```python
"""Golden tests for the Phase 3 multi-provider fallback + autonomous-trade guard.

GOLD-013 — AI provider fallback: the primary provider failing does NOT abort the
request; the fallback chain serves it, and only when every provider is down is
the UNAVAILABLE sentinel returned.

GOLD-014 — All AI unavailable → no new autonomous trade: when the sentinel is
returned and config["ai"]["all_unavailable_no_trade"] is true (the default), the
guard blocks autonomous trading from opening a new position.
"""
import pytest

from core.ai.assignment import ModelAssignment
from core.ai.fallback import (
    all_unavailable,
    generate_with_fallback,
    no_new_autonomous_trade_guard,
)
from core.ai.provider import AIResponse, UNAVAILABLE
from core.ai.router import ModelRouter

pytestmark = pytest.mark.golden


class _FakeProvider:
    def __init__(self, name, *, fail=False, text="ok", model="m"):
        self.name = name
        self.default_model = model
        self._fail = fail
        self._text = text

    def generate(self, prompt, *, model=None, system="", **opts):
        if self._fail:
            return AIResponse(text="", error=f"{self.name} down")
        return AIResponse(text=self._text, model=model or self.default_model)


# --- GOLD-013: AI provider fallback ---

def test_gold013_primary_healthy_no_fallback_needed():
    primary = _FakeProvider("openai", text="primary")
    resp = generate_with_fallback(primary, "p", model="gpt-4o", fallback_chain=[_FakeProvider("anthropic", text="fb")])
    assert resp.text == "primary"
    assert not resp.is_error


def test_gold013_fallback_chain_serves_when_primary_down():
    primary = _FakeProvider("openai", fail=True)
    fb1 = _FakeProvider("anthropic", fail=True)
    fb2 = _FakeProvider("gemini", text="gemini-served")
    resp = generate_with_fallback(primary, "p", model="gpt-4o", fallback_chain=[fb1, fb2])
    assert resp.text == "gemini-served"
    assert not resp.is_error


def test_gold013_all_down_returns_unavailable_sentinel():
    primary = _FakeProvider("openai", fail=True)
    fb = _FakeProvider("anthropic", fail=True)
    resp = generate_with_fallback(primary, "p", model="gpt-4o", fallback_chain=[fb])
    assert resp is UNAVAILABLE
    assert all_unavailable(resp) is True


# --- GOLD-014: all AI unavailable → no new autonomous trade ---

def test_gold014_guard_blocks_new_autonomous_trade_when_all_down():
    config = {"ai": {"all_unavailable_no_trade": True}}
    assert no_new_autonomous_trade_guard(UNAVAILABLE, config) is True


def test_gold014_guard_allows_trade_when_ai_available():
    config = {"ai": {"all_unavailable_no_trade": True}}
    assert no_new_autonomous_trade_guard(AIResponse(text="ok"), config) is False


def test_gold014_guard_blocks_via_full_fallback_path():
    # End-to-end: all providers down → generate_with_fallback → guard blocks.
    config = {"ai": {"all_unavailable_no_trade": True, "fallback": {"enabled": True, "chain": []}}}
    primary = _FakeProvider("openai", fail=True)
    resp = generate_with_fallback(primary, "p", model="gpt-4o", fallback_chain=[_FakeProvider("anthropic", fail=True)])
    assert no_new_autonomous_trade_guard(resp, config) is True


# --- §37/§38 wiring: router + assignment resolve consistently with fallback ---

def test_router_assignment_resolve_consistently_for_critic_tier():
    assignment = ModelAssignment({"all": "cheap-mini", "risk_critic": "independent-1"})
    router = ModelRouter({"triage": {"model": "fast-mini"}, "analysis": {"model": None}, "critic": {"model": None}}, assignment)
    # triage uses the cheap tier model
    assert router.select_model("market_analyst", tier_hint="triage") == "fast-mini"
    # critic role with no critic-tier model falls back to the role assignment (independent)
    assert router.select_model("risk_critic", tier_hint="critic") == "independent-1"
    # a role with no explicit assignment uses `all`
    assert router.select_model("news_analyst") == "cheap-mini"
```

- [ ] **Step 2: Run golden tests to verify they pass**

Run: `pytest tests/golden/test_ai_fallback_golden.py -v`
Expected: PASS (7 tests)

- [ ] **Step 3: Update `docs/V9_TEST_MATRIX.md`**

Set the GOLD-013 and GOLD-014 rows to PASS:
```
| GOLD-013 | AI provider fallback | PASS | Phase 3C | generate_with_fallback chain → UNAVAILABLE; tests/golden/test_ai_fallback_golden.py |
| GOLD-014 | All AI unavailable → no new autonomous trade | PASS | Phase 3C | no_new_autonomous_trade_guard(UNAVAILABLE) blocks; tests/golden/test_ai_fallback_golden.py |
```
Also set the `AI PROVIDERS` acceptance-area row to `IN PROGRESS` (platform delivered; AI DECISION ENGINE is Phase 4).

- [ ] **Step 4: Update `docs/V9_ARCHITECTURE.md`**

Add an additive "Assignment + Router + Fallback (Phase 3C)" subsection after the Phase 3B subsection (before the UI→client→API map), documenting:
- `ModelAssignment` (§37, 10 roles + `all` alias, `resolve(role)`).
- `ModelRouter` (§38, triage/analysis/critic, `select_model(role, tier_hint)` — tier model wins, else assignment).
- `generate_with_fallback` (GOLD-013) — primary → fallback chain → `UNAVAILABLE` sentinel.
- `no_new_autonomous_trade_guard` (GOLD-014) — blocks new autonomous trades when `all_unavailable_no_trade` (default true) and the response is `UNAVAILABLE`. The existing `agent_manager` autonomous loop is untouched in 3C; rewiring it to call `generate_with_fallback` is Phase 4 (§41 decision schema + critic workflow).
- The §108 API routes (`/api/ai/providers`, `/api/ai/providers/{name}/test`, `/api/ai/providers/{name}/discover`, `/api/ai/assignment`, `/api/ai/router`, `/api/ai/request`); keys never to frontend (`has_key: bool` only, §106).
- **MLflow AI Gateway (§17) evaluation record:** the MLflow AI Gateway exposes an OpenAI-compatible endpoint for many models behind one gateway. V9's `OpenAICompatibleProvider` (3B) can point at an MLflow AI Gateway `base_url` with no MLflow dependency — so the multi-provider layer already *accommodates* it. Phase 3 records this evaluation (ADAPTER-noted, deferred); the full MLflow experiment-tracking integration is Phase 14 per the manifest. This honors §17 ("V9 keeps own AIProvider") and "may be evaluated as part of the multi-provider layer." No MLflow code or dependency is added in Phase 3.

Add the new AI routes to the UI→client→API map table (a new "AI Settings" row — service `api/ai_config_service.py`, provider `core/ai/*`). Mark the UI tab as Phase 12 (backend + API only in 3C).

- [ ] **Step 5: Update `docs/V9_IMPLEMENTATION.md`**

Add the Phase 3C record (test counts filled from the actual run): new modules, golden tests GOLD-013/014 PASS, §108 routes, zero new deps, V7 untouched. Set the **Phase 3 row → PASS** (3A + 3B + 3C all PASS — Phase 3 Multi-Provider AI Platform COMPLETE).

- [ ] **Step 6: Run the full regression**

Run: `pytest -q`
Expected: all green, 0 failed. Record the count (baseline 1704 + new 3C tests).

- [ ] **Step 7: Exit gate audit**

- V7 = `f912a33` (unchanged): `git -C "C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium" rev-parse HEAD` → `f912a335...`.
- `core/ollama_client.py`, `core/ollama_decision.py`, `core/agent_manager.py` untouched: `git diff --stat cddd301..HEAD -- core/ollama_client.py core/ollama_decision.py core/agent_manager.py` → empty.
- `core/config.py` untouched: `git diff --stat cddd301..HEAD -- core/config.py` → empty.
- Deps diff empty: `git diff --stat cddd301..HEAD -- requirements.txt requirements-dev.txt pyproject.toml setup.py setup.cfg` → empty.
- §4 forbidden-language sweep: 0 hits in new/modified files.
- §106 audit: grep the new service + routes for any key value emission — confirm only `has_key: bool` reaches responses (`grep -n "api_key" api/ai_config_service.py` shows only `api_key_env` references, never the resolved value in a response).

- [ ] **Step 8: Commit the gate**

```bash
git add tests/golden/test_ai_fallback_golden.py docs/V9_TEST_MATRIX.md docs/V9_ARCHITECTURE.md docs/V9_IMPLEMENTATION.md
git commit -m "test(phase3c): GOLD-013/014 golden tests + control docs — Phase 3 PASS"
```

- [ ] **Step 9: Write the SDD final-review report + update the ledger**

Write `.superpowers/sdd/2026-08-12-v9-phase3c-assignment-router-fallback-api-golden/progress.md` (ledger) and `final-review-report.md`. Verdict: record any deviations (expected: none, or strict-improvement deviations noted inline). State **Phase 3C = PASS — Phase 3 row → PASS (Multi-Provider AI Platform COMPLETE).**

---

## Self-Review (run before execution)

1. **Spec coverage:**
   - §37 (10 roles + all) → Task 1. ✓
   - §38 (triage/analysis/critic) → Task 2. ✓
   - §107 GOLD-013 (fallback) → Task 3 + Task 5 golden. ✓
   - §107 GOLD-014 (all unavailable → no new autonomous trade) → Task 3 guard + Task 5 golden. ✓
   - §108 (configure → test → discover → assign → save → validated request) → Task 4 routes. ✓
   - §17 (MLflow AI Gateway evaluated) → Task 5 architecture record. ✓
   - §106 (keys never to frontend) → Task 4 `has_key` only + Task 5 §106 audit. ✓
   - §35 (no hardcoded model names) → Task 4 factory uses config `default_model` + discovery. ✓
   - §5/§7 (evolve, preserve) → ollama_client/ollama_decision/agent_manager untouched (Task 5 gate). ✓
   - §117/§116/§118/§115/§4 → Task 5 gate audit. ✓

2. **Placeholder scan:** no TBD/TODO; provider defaults are `""` (discover-then-select, not a placeholder). ✓

3. **Type consistency:** `ModelAssignment.resolve(role) -> Optional[str]` (Task 1) consumed by `ModelRouter.select_model` (Task 2) and by `from_config(config, assignment)` (Task 2 classmethod) — signatures match. `generate_with_fallback(primary, prompt, *, model, fallback_chain, system, **opts) -> AIResponse` (Task 3) consumed by the golden tests (Task 5). `build_provider(config, name) -> AIProvider | None` (Task 4) consumed by `test_ai_provider`/`discover_ai_models`/`ai_validated_request`. `no_new_autonomous_trade_guard(response, config) -> bool` (Task 3) consumed by golden tests (Task 5). ✓

4. **§106 double-check:** `list_ai_providers` returns `has_key: bool` only; `ai_validated_request` returns text/model/usage (no key); routes never echo a key. The unit test `test_list_ai_providers_returns_has_key_not_the_key` asserts the key value is absent. ✓

5. **Phase boundary (§39/§41 → Phase 4):** `tool_call()` is not wired to a tool registry here (3B delivered the round-trip); the critic workflow and agent_manager rewiring are NOT in 3C. ✓

No issues found. Plan is complete.