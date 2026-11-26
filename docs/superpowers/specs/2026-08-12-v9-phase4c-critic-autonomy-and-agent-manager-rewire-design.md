# Phase 4C — AI Critic (§44) + Autonomy (§45/§46/§48) + `agent_manager` Rewire — Design

> **Spec basis:** `prompt_to_follow.md` §44 (AI Critic), §45 (Autonomy Modes), §46 (Paper Auto before Live Auto), §48 (AI cannot change Risk Constitution), §40 (AI must not have unrestricted access), §41 (AI Output — Decision schema, delivered 4B), §42 (AI Validation pipeline, delivered 4B), §43 (AI numbers not authoritative, delivered 4B), §74 (Kill Switch), §82/§83 (protected files / goal-hacking), §5/§7 (evolve not rewrite), §115 (tests not weakened), §118 (zero new deps), §116 (no remote push), §106 (no keys to frontend), §4 (no forbidden success language).

**Goal:** Wire the 3C/4A/4B AI platform into the live autonomous decision loop: an independent critic challenges important decisions (§44), autonomy modes + kill switch + Paper-Auto acceptance gate what the loop may do (§45/§46/§48), and `agent_manager`'s two AI call sites route through `generate_with_fallback` → `parse_decision` → `run_pipeline` → `run_critic` instead of the raw ollama singleton — without rewriting the autonomous loop or its existing risk/capital/news/profitability gates.

**Architecture (Approach A — compose, don't rewrite):**
1. A new `core/ai/decision_engine.py` is the bridge: `evaluate_trade_decision(signal, risk_result, *, config, services)` composes the existing platform (`generate_with_fallback` → `no_new_autonomous_trade_guard` → `parse_decision` → `run_pipeline` → `run_critic`) and returns an `EvaluationResult` carrying the §41 `Decision`, the §42 `ValidationResult`, the §44 `CriticVerdict`, **and** a backward-compatible `review` dict in the exact shape `agent_manager` consumes today (`{available, provider, recommendation, confidence, leverage, reason, fallback_used, ...}`).
2. `core/ollama_decision.py::evaluate_trade_decision_with_ollama` becomes a thin delegate to the new engine (the 19 pinned `test_ollama_decision.py` tests stay green); `should_veto_trade` + `parse_trade_review` + `default_trade_review` are unchanged.
3. `core/agent_manager.py`'s two call sites (research-cohort ~L1424, main autonomous ~L1853) keep their `review`-dict consumption (minimal diff) and additionally store `Decision`/`ValidationResult`/`CriticVerdict` in `decision_context`. Autonomy-mode + kill-switch + LIVE_AUTO gates are added at autonomous-order entry.
4. The §44 critic is a **post-pipeline** check, NOT a 7th §42 stage — `STAGE_ORDER` is contractually frozen by the 4B golden tests. The critic uses the router's independent `critic` tier (§38) via `generate_with_fallback`.
5. Autonomy modes (§45), kill switch (§74 minimal), and Paper-Auto acceptance (§46) live in a new `core/autonomy.py`; §48 enforcement is a guard that blocks AI/autonomous attempts to weaken protected surfaces.

**Tech Stack:** Python 3.14 stdlib (dataclasses/enum/json/typing) + the existing `core/ai/` platform (3C/4A/4B). Zero new dependencies (§118). No network/broker/live-AI in tests — all injectable via `services` fakes.

---

## Global Constraints (verbatim from spec)

- §5/§7: Evolve V7/V9 code, never rewrite. Compose existing functions unchanged.
- §4: No "mostly complete", "should work", "99%", "mock for now", "todo later", "placeholder". Honest status only.
- §40: AI must NOT have unrestricted shell/Python/SQL/filesystem access. Critic reviews provided evidence; no tool loop in 4C.
- §42: `STAGE_ORDER` == (PARSE, SCHEMA, SEMANTIC, AUTHORITATIVE_DATA_CHECK, POLICY, RISK_GATE) — exactly 6, frozen.
- §43: AI does not supply authoritative price/P&L/IV/EPS/portfolio_value/allowed_capital/position_size; app services do.
- §44: Important autonomous decisions challenged independently; valid final result can be NO_TRADE.
- §45: Modes OFF/ANALYZE/RECOMMEND/PAPER_AUTO/LIVE_ASSISTED/LIVE_AUTO; default NOT LIVE_AUTO; LIVE_AUTO owner-enabled only.
- §46: LIVE_AUTO disabled until mandatory Paper Auto acceptance tests pass.
- §48: No autonomous agent may weaken Risk Constitution / Kill Switch / promotion gates / critical tests / live permissions / holdout policy. Owner action required.
- §74: Kill switch stops new automated risk; monitoring stays active.
- §106: No keys to frontend.
- §115: Tests not weakened.
- §116: No remote push.
- §118: Zero new deps.

---

## Components

### 1. `core/autonomy.py` (NEW — additive)

**Responsibility:** §45 autonomy modes + §74 minimal kill switch + §46 Paper-Auto gate + §48 no-weakening guard.

```python
class AutonomyMode(str, Enum):
    OFF = "OFF"
    ANALYZE = "ANALYZE"
    RECOMMEND = "RECOMMEND"
    PAPER_AUTO = "PAPER_AUTO"
    LIVE_ASSISTED = "LIVE_ASSISTED"
    LIVE_AUTO = "LIVE_AUTO"

ALLOWED_MODES = frozenset(m.value for m in AutonomyMode)
```

Public functions (all pure, config-in — no I/O):
- `resolve_mode(config) -> AutonomyMode` — reads `config["autonomy"]["mode"]`; default `ANALYZE`; unknown → `ANALYZE` (fail-safe, never LIVE_AUTO).
- `mode_allows_paper_orders(mode) -> bool` — `PAPER_AUTO`, `LIVE_ASSISTED`, `LIVE_AUTO` (paper order placement is allowed in paper/live autonomous modes; `LIVE_AUTO` also allows live). `OFF`/`ANALYZE`/`RECOMMEND` → False.
- `mode_allows_live_orders(mode) -> bool` — `LIVE_AUTO` only.
- `kill_switch_active(config) -> bool` — reads `config["autonomy"]["kill_switch_active"]`; default False. When True, the autonomous loop blocks new orders (monitoring stays active — §74).
- `live_auto_owner_enabled(config) -> bool` — reads `config["autonomy"]["live_auto_owner_enabled"]`; default False (§45 — owner-explicit).
- `paper_auto_acceptance_passed(config, services=None) -> tuple[bool, str]` — runs the §46 acceptance battery (via `run_paper_auto_acceptance(config, services)`) and returns `(passed, summary)`. The battery uses fake providers/services (no network, deterministic, milliseconds) so it is safe to call from the runtime LIVE_AUTO gate; there is no separate cache/persistence layer in 4C.
- `can_enable_live_auto(config, services=None) -> tuple[bool, str]` — True iff `live_auto_owner_enabled(config)` AND `paper_auto_acceptance_passed(config, services)[0]`. §46/§48. The `services` arg lets tests inject fakes; production passes `None` (default services).
- `PROTECTED_TARGETS` frozenset: `"risk_constitution", "kill_switch", "promotion_gates", "critical_tests", "live_permissions", "holdout_policy"` (§48/§82).
- `assert_not_weakening(target: str, action: str) -> None` — raises `AutonomyViolationError(ValueError)` if `target in PROTECTED_TARGETS` and `action` is a write/weaken verb. Called by §39 tool guards and the decision engine when AI output attempts to touch protected surfaces. Owner-bypass via an explicit `ownerAuthorized=True` kwarg (documented; not set by any AI path).

```python
class AutonomyViolationError(ValueError):
    """§48 — an autonomous agent attempted to weaken a protected surface."""
```

`run_paper_auto_acceptance(config, services=None) -> dict` — executes the §46 scenario battery against the decision engine with fakes; returns `{"passed": bool, "scenarios": [...], "summary": str}`. Used by the acceptance test module AND by `paper_auto_acceptance_passed` to populate the cached boolean. The battery scenarios are defined here as data (list of `(name, signal, risk_result, expected_action, expected_ok)`), not in the test file, so the runtime gate and the test suite assert the same truth.

**Dependencies:** stdlib only; the decision engine (for the acceptance battery) imported lazily inside `run_paper_auto_acceptance` to avoid a cycle.

### 2. `core/ai/critic.py` (NEW — additive)

**Responsibility:** §44 — independently challenge an important (non-NO_TRADE) decision.

```python
@dataclass(frozen=True)
class CriticVerdict:
    challenges: tuple[str, ...]      # human-readable challenge lines
    categories: tuple[str, ...]      # subset of the 10 §44 categories found
    severity: float                  # 0.0..1.0
    veto: bool                       # True → force NO_TRADE
    rationale: str
    raw: str                         # raw critic model output (for audit)
```

`CRITIC_CATEGORIES` — the 10 §44 issues: `contradictory_evidence, stale_information, provider_conflict, weak_backtest, fragile_parameters, poor_sample_size, excessive_volatility, concentration, catalyst_risk, regime_mismatch`.

`run_critic(decision: Decision, signal: dict, *, config: dict, services: dict | None = None) -> CriticVerdict`:
- Resolves the critic model via `ModelRouter(config).select_model("critic", tier_hint="critic")` (§38 independent tier) and a provider from `services` (default `default_services(config)` extended with a critic provider factory). Injectable → tests pass a fake provider returning a canned critic JSON; no network.
- Builds a critic prompt that presents the `Decision` (action/ticker/thesis/evidence/risk_flags) + compact signal context and asks for the 10 §44 checks as strict JSON `{challenges:[...], categories:[...], severity:0..1, veto:bool, rationale:"..."}`.
- Parses with `_extract_json` (reused from `core.ai.decision`) + `_clamp`; on parse failure → `veto = critic_force_no_trade_on_fail(config)` (§44 — a failing critic forces NO_TRADE when configured, default True).
- `veto` is True when the critic says so OR (parse-fail AND `critic_force_no_trade_on_fail`). A vetoed decision forces `NO_TRADE` in the decision engine.

**Dependencies:** `core.ai.decision` (Decision, `_extract_json`), `core.ai.fallback` (generate_with_fallback), `core.ai.router` (ModelRouter), `core.ai.provider` (AIResponse, UNAVAILABLE). No §39 tool loop (§40 — critic reviews provided evidence only).

### 3. `core/ai/decision_engine.py` (NEW — the bridge)

**Responsibility:** Compose the platform into one entry point; return backward-compatible shape.

```python
@dataclass(frozen=True)
class EvaluationResult:
    decision: Decision               # §41 (action may be NO_TRADE after critic/pipeline)
    validation: ValidationResult     # §42
    critic: CriticVerdict | None     # §44 (None if not run)
    review: dict                     # backward-compat {available,provider,recommendation,confidence,leverage,reason,fallback_used,...}
    ai_available: bool
```

`evaluate_trade_decision(signal: dict, risk_result: dict | None, *, config: dict, services: dict | None = None) -> EvaluationResult`:

1. **Build prompt** — reuse `ollama_decision.build_trade_review_prompt` (evolve, unchanged) so the prompt contract with the model is preserved.
2. **Generate** — resolve primary provider + fallback chain from `config["ai"]` (via the 3C `api.ai_config_service.build_provider` factory, lazily imported) → `generate_with_fallback(primary, prompt, model=..., fallback_chain=..., system=...)` → `AIResponse`.
3. **AI unavailable** — if `all_unavailable(resp)` and `no_new_autonomous_trade_guard(resp, config)` → return `EvaluationResult` with `decision=NO_TRADE`, `review = default_trade_review(available=False, reason="all AI providers unavailable")` (GOLD-014 shape). `ai_available=False`.
4. **Parse** — `parse_decision(resp.text, strict_schema=config["ai"]["decision"]["strict_schema"])` → `Decision` (§41). Parse/schema errors → NO_TRADE review (shape: `available=False, fallback_used="invalid_json"`).
5. **Pipeline** — `run_pipeline(decision_raw=resp.text (or the parsed dict), signal, positions=[], config, services)` → `ValidationResult` (§42 6 stages). If `validation.ok is False` → review reflects the failing stage; `decision = validation.decision` (NO_TRADE).
6. **Critic** — if `validation.ok` AND `decision.action not in (NO_TRADE, WATCH)` AND `config["ai"]["decision"]["critic_enabled"]` → `run_critic(decision, signal, config, services)`. If `critic.veto` → force `decision = Decision(action=NO_TRADE, ...)` (§44 valid final result NO_TRADE); record critic in review.
7. **§48 guard** — if the parsed `Decision.raw` / model output attempts to touch a protected target (heuristic: AI output containing constitution/kill-switch/live-auto directives) → `assert_not_weakening` → force NO_TRADE + record. (Defense-in-depth; the §39 tool layer already cannot mutate these.)
8. **Map to review shape** — `_decision_to_review(decision, resp, critic, validation)` builds the backward-compat dict. **Crucial distinction** (preserves §115 — does not weaken behavior):
   - **AI-unavailable** (GOLD-014 sentinel) → `available=False, fallback_used="all_unavailable"` → triggers agent_manager's *unavailable* path (reject if AI-required, or signal-only paper fallback). This is "could not respond."
   - **Decisive rejection** (AI responded but pipeline/critic forced NO_TRADE) → `available=True, recommendation="avoid", confidence=decision.confidence` → `should_veto_trade` vetoes (agent_manager's *ollama_rejected* path ~L1981). This is "responded and said no" — it must NOT be conflated with unavailable, else a trade the AI actively rejected could fall through to a paper-only fallback.
   - **Active decision** (OPEN_LONG/OPEN_SHORT/HOLD/REDUCE/CLOSE) → `available=True`; `recommendation` = `OPEN_LONG/OPEN_SHORT/HOLD/CLOSE → "approve"`, `REDUCE → "reduce"`; `WATCH → "watch"` (no veto; WATCH is "observe," not a decisive no — consistent with legacy, which only vetoes "avoid").
   - `provider` = resp provider name; `confidence` = `decision.confidence`; `leverage` = clamp 1..5; `reason` = `decision.thesis` or stage reason; `fallback_used` = resp/error classification; plus `decision` (the §41 object), `validation_stage`, `critic_veto` (bool), `critic_categories`.
   - This shape is what `agent_manager` reads today (`ollama_review.get("recommendation")`, `.get("confidence")`, `.get("leverage")`, `.get("available")`, `.get("provider")`, `.get("reason")`, `.get("fallback_used")`). `should_veto_trade` only vetoes `recommendation=="avoid"` with `available=True` + confidence ≥ min — so the mapping is safe and defense-in-depth (a NO_TRADE decision is vetoed both by the engine and by agent_manager's separate `should_veto_trade` call).

`default_services(config)` for the engine = the 4B `pipeline.default_services(config)` extended with `"build_provider"` and `"critic_provider"` factories (injectable for fakes).

**Dependencies:** `core.ai.fallback`, `core.ai.decision`, `core.ai.pipeline`, `core.ai.critic`, `core.ai.router`, `core.ollama_decision` (build_trade_review_prompt, default_trade_review — reused), `core.api.ai_config_service` (build_provider — lazy). No cycle: `core.autonomy` imports `decision_engine` lazily only inside `run_paper_auto_acceptance`.

### 4. `core/ollama_decision.py` (MODIFY — evolve, delegate)

- `evaluate_trade_decision_with_ollama(signal, risk_result, *, config)` → delegates to `core.ai.decision_engine.evaluate_trade_decision(signal, risk_result, config=config)` and returns `result.review`. The 19 pinned tests assert the review-dict shape; delegation preserves it.
- `should_veto_trade`, `parse_trade_review`, `default_trade_review`, `build_trade_review_prompt`, `default_exit_review` — UNCHANGED.
- The raw ollama singleton path is removed from `evaluate_trade_decision_with_ollama` (the engine uses `generate_with_fallback` which includes the ollama provider via `build_provider`). If a test pins the legacy singleton-direct behavior, that test is updated to pin the new platform behavior (§115 — not weakened; the behavior is equivalent or stricter). Documented as a deviation if any such test changes.

### 5. `core/agent_manager.py` (MODIFY — minimal rewire)

Two call sites (research-cohort ~L1424, main ~L1853):
- Replace `ollama_review = evaluate_trade_decision_with_ollama(signal_data, risk_result, config=...)` with:
  ```python
  from core.ai.decision_engine import evaluate_trade_decision as _eval_decision
  _eval = _eval_decision(signal_data, risk_result, config=review_config)
  ollama_review = _eval.review
  # audit enrichment (additive):
  decision_context["ai_decision"] = _eval.decision      # §41 Decision
  decision_context["ai_validation"] = _eval.validation  # §42 ValidationResult
  if _eval.critic is not None:
      decision_context["ai_critic"] = _eval.critic       # §44 CriticVerdict
  ```
- All downstream `ollama_review.get(...)` consumption is unchanged (shape preserved). The separate `should_veto_trade(ollama_review, config)` calls (~L1433, ~L1981) stay as defense-in-depth (the pipeline's RISK_GATE already vetoes; idempotent).

Autonomous-order entry gate (added once, in the main autonomous review path before any order placement):
```python
from core.autonomy import resolve_mode, mode_allows_paper_orders, mode_allows_live_orders, kill_switch_active, can_enable_live_auto
_mode = resolve_mode(cfg)
_live = str(execution_cfg.get("mode", "paper")).lower() == "live"
if kill_switch_active(cfg):
    # §74 — block new automated risk; monitoring stays active
    record_trade_event(...); return
if _live and not mode_allows_live_orders(_mode):
    # §45 — live orders require LIVE_AUTO
    record_trade_event(...); return
if _live and not can_enable_live_auto(cfg)[0]:
    # §46/§48 — LIVE_AUTO requires owner + paper-auto acceptance
    record_trade_event(...); return
if not _live and not mode_allows_paper_orders(_mode):
    # §45 — paper autonomous orders only in PAPER_AUTO/LIVE_ASSISTED/LIVE_AUTO
    record_trade_event(...); return
```
No other gates (risk/capital/news/profitability) are touched. §5/§7 preserved.

### 6. `core/config.py` + `config.example.json` (MODIFY — additive)

`DEFAULT_CONFIG["autonomy"]`:
```python
"autonomy": {
    "mode": "ANALYZE",                 # §45 — default NOT LIVE_AUTO
    "kill_switch_active": False,       # §74 — owner-set; True halts new automated risk
    "live_auto_owner_enabled": False,  # §45 — owner-explicit
    "paper_auto_acceptance_required": True,  # §46
},
```
`deep_merge` auto-backfills. `ai.decision` already has `critic_enabled`/`critic_force_no_trade_on_fail` (4B).

### 7. `core/ai/__init__.py` (MODIFY — re-exports)

Add `AutonomyMode` (from `core.autonomy`), `CriticVerdict`, `run_critic`, `CRITIC_CATEGORIES` (from `core.ai.critic`), `EvaluationResult`, `evaluate_trade_decision` (from `core.ai.decision_engine`). Extend `__all__` alphabetically.

### 8. `tests/acceptance/test_paper_auto_acceptance.py` (NEW)

Runs `run_paper_auto_acceptance(config, services=fakes)` and asserts every scenario in the battery passes (the same battery `paper_auto_acceptance_passed` consults). This is the §46 mandatory suite. Also tests `can_enable_live_auto` is False until the suite passes + owner flag set.

### 9. Golden tests `tests/golden/test_phase4c_golden.py` (NEW)

- **GOLD-044** (§44): critic is post-pipeline (not a §42 stage); `STAGE_ORDER` stays 6; an independent critic veto forces `NO_TRADE`; a passing critic keeps the action; `critic_force_no_trade_on_fail` forces NO_TRADE on critic parse-failure.
- **GOLD-045** (§45): `resolve_mode` default is `ANALYZE` (never LIVE_AUTO); `OFF`/`ANALYZE`/`RECOMMEND` forbid autonomous orders; `LIVE_AUTO` requires owner flag; unknown mode → ANALYZE (fail-safe); kill-switch blocks.
- **GOLD-046** (§46): `can_enable_live_auto` is False until `paper_auto_acceptance_passed`; once passing + owner-enabled → True; without owner flag → False even if suite passes (§48 owner-action).

### 10. Unit tests (NEW)

- `tests/unit/test_autonomy.py` — modes, kill switch, owner flag, `can_enable_live_auto`, `assert_not_weakening` (all protected targets raise), fail-safe defaults.
- `tests/unit/test_ai_critic.py` — `CriticVerdict` frozen; `run_critic` with fake provider (veto / pass / parse-fail paths); 10 categories; `critic_force_no_trade_on_fail`.
- `tests/unit/test_ai_decision_engine.py` — `evaluate_trade_decision` end-to-end with fakes: open-long success, AI-unavailable→GOLD-014 review, parse-fail, pipeline-reject, critic-veto→NO_TRADE, WATCH skips critic, backward-compat review shape (recommendation/confidence/leverage/available/provider/reason/fallback_used).
- `tests/unit/test_ollama_decision_delegate.py` (or extend existing) — delegation preserves review-dict shape; 19 pinned tests stay green.
- `tests/unit/test_agent_manager_rewire.py` — autonomy/kill-switch entry gates block; LIVE_AUTO gate blocks without acceptance; critic verdict lands in decision_context; existing review consumption unchanged.

### 11. Control docs (MODIFY)

- `docs/V9_ARCHITECTURE.md` — "## Phase 4C" section: decision_engine bridge, critic post-pipeline, autonomy module, agent_manager rewire points, Paper-Auto gate.
- `docs/V9_AI_SYSTEM.md` — note the critic workflow + autonomy modes + §48 enforcement.
- `docs/V9_RISK_CONSTITUTION.md` — PROTECTED; **not modified by code** (only an owner edits it). The §48 guard is documented as enforced in code.
- `docs/V9_CHANGELOG.md` — Phase 4C entry.

---

## Data Flow

```
signal + risk_result
   │
   ▼
evaluate_trade_decision (core/ai/decision_engine.py)
   │
   ├─ build_trade_review_prompt  (reused from ollama_decision)
   ├─ generate_with_fallback      (3C: primary → fallback chain → AIResponse | UNAVAILABLE)
   │     └─ if UNAVAILABLE + no_new_autonomous_trade_guard → review(unavailable)  [GOLD-014]
   ├─ parse_decision              (4B: §41 Decision, strips §43 fields)
   ├─ run_pipeline                (4B: §42 6 stages → ValidationResult)
   │     └─ if !ok → review(stage-fail), decision=NO_TRADE
   ├─ run_critic                  (4C: §44, only if non-NO_TRADE/WATCH + critic_enabled)
   │     └─ if veto → decision=NO_TRADE
   ├─ assert_not_weakening        (4C: §48 defense-in-depth)
   └─ _decision_to_review         (7-action → 4-value backward-compat shape)
   │
   ▼
EvaluationResult { decision, validation, critic, review, ai_available }
   │
   ▼
agent_manager (review consumed unchanged + Decision/Validation/Critic in decision_context)
   │
   ├─ autonomy entry gate (§45 mode + §74 kill switch + §46 LIVE_AUTO acceptance)
   └─ existing risk/capital/news/profitability gates (UNTOUCHED)
   │
   ▼
paper or live order (per mode)
```

## Error Handling

- AI all-unavailable → GOLD-014 guard → `review(available=False)` → agent_manager rejects (AI-required) or signal-only paper fallback (existing logic, unchanged).
- Parse/schema fail → §42 SCHEMA stage → `review(available=False, fallback_used="invalid_json")`.
- Critic parse-fail → `critic_force_no_trade_on_fail` → veto → NO_TRADE.
- Critic model unavailable → treated as critic parse-fail → `critic_force_no_trade_on_fail` (default True) → NO_TRADE. (Conservative: a critic that cannot run blocks the trade.)
- Kill switch active → autonomous order blocked, monitoring continues (§74).
- LIVE_AUTO without acceptance/owner → blocked (§46/§48).
- Any exception in the engine → `review(available=False, fallback_used="exception")` (preserves agent_manager's existing exception handling).

## Testing

- Unit: autonomy (~12), critic (~9), decision_engine (~12), ollama_decision delegate (19 existing green), agent_manager rewire (~7).
- Acceptance: `test_paper_auto_acceptance.py` (§46 battery, ~10 scenarios).
- Golden: GOLD-044/045/046.
- Regression: curated `tests/unit tests/integration tests/contract tests/golden tests/perf` (expect 1847 + ~50 new ≈ 1897) and full `pytest tests tests_e2e` (expect 2359 + ~50 ≈ 2409). V7 untouched (f912a33). Deps diff empty (§118). §4 sweep 0 hits.

## Scope Boundaries (what 4C does NOT do)

- No §39 tool loop for the critic (§40 — critic reviews provided evidence; tool-augmented critic is a future enhancement).
- No full §74 kill-switch UI/broker-halt machinery — only the minimal `kill_switch_active` flag + autonomous-loop check. Full kill-switch operations deferred.
- No LIVE_ASSISTED UI wiring (§12) — the mode exists and gates correctly; the operator UI is Phase 12.
- No new production dependency (§118).
- No V7 modification (V7 read-only, HEAD f912a33).
- No remote push (§116).