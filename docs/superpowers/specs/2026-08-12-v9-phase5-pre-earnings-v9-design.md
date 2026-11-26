# Phase 5 — Pre-Earnings V9 (Design Spec)

> Spec for `prompt_to_follow.md` §49 (Pre-Earnings decision system), §50 (PRE-EARNINGS SCORE 0–100 + DATA COVERAGE + DATA CONFIDENCE), §51 (Pre-Earnings autonomy → WATCH/NO_TRADE/OPEN_LONG/OPEN_SHORT/HOLD/REDUCE/CLOSE), §93 (Pre-Earnings UI), §109 (E2E journey).
>
 Evolves the 11,857-line `agents/pre_earnings_agent.py` (evolve-not-rewrite, §5/§7). Composes the Phase 4C `core/ai/decision_engine` + `core/autonomy` into Pre-Earnings. Zero new deps (§118). No remote push (§116). No secrets (§106). No forbidden success language (§4).

## 1. Goal

Turn Pre-Earnings from a *scoring surface* into a *decision system*. Today the agent emits per-symbol items carrying `gap_risk`, `direction_bias`, `coverage_score`, `data_confidence`, and a proto `opportunity_score`, plus an AI-qualified eligibility flag. Phase 5:

1. **Formalizes the §50 triple** — a canonical **PRE-EARNINGS SCORE 0–100** with *visible components*, alongside the already-present **DATA COVERAGE** and **DATA CONFIDENCE**, and pins the invariant *"Score is NOT probability."*
2. **Wires §51 autonomy** — each Pre-Earnings candidate flows through the Phase 4C decision engine (`evaluate_trade_decision`) with the full §51 input stack (Pre-Earnings + strategy evidence + signal + backtest + AI + critic + data quality + portfolio + risk + capital) and resolves to one of the 7 actions, gated by `core/autonomy` + the kill switch.
3. **Surfaces §93 UI columns** and proves the **§109 E2E journey** end-to-end.

## 2. Existing V7/V9 surface reused (evolve, do not rewrite)

Inspected in `agents/pre_earnings_agent.py`:

| Existing | Lines | Phase 5 role |
|---|---|---|
| `compute_gap_risk_score` | 509 | §50 score component (gap/move risk axis) — reused verbatim |
| `compute_direction_bias_score` | 563 | §50 score component (direction axis) — reused verbatim |
| `_compute_analysis_coverage` | 5780 | **§50 DATA COVERAGE** — already returns `(score 0-100, label, contributors[])` with 6 weighted components. Promoted to canonical, unchanged math |
| `data_confidence` bands | 5083-5097 | **§50 DATA CONFIDENCE** (HIGH/MEDIUM/LOW/NONE on `historical_samples`). Promoted to canonical, made multi-input |
| `_compute_opportunity_score` | 5828 | proto §50 score (`0.65*gap + 0.35*|bias|) * coverage/100`). Evolved into canonical `pre_earnings_score` |
| `_estimate_confidence_intervals` | 8065 | intervals — reused, feeds DATA CONFIDENCE |
| `_ai_decision_qualifies_pre_earnings` | 249 | §51 AI-eligibility — reused as one §51 input |
| `_build_item_for_symbol` | 4535 | item builder — additively extended with §50/§51 fields |
| `run_cycle` | 2355 | integration point for §51 decision pass |

**Phase 4C platform reused (no modification):** `core/ai/decision_engine.evaluate_trade_decision`, `core/ai/critic.run_critic`, `core/ai/run_pipeline` (§42), `core/autonomy` (§45/§46/§48/§74), `agent_manager._autonomy_order_gate`.

## 3. Architecture

```
agents/pre_earnings_agent.py  (evolved; _build_item_for_symbol + run_cycle additively extended)
        │
        │  enriched_data + scores
        ▼
core/pre_earnings/score.py        (NEW — §50 pure scoring, no I/O)
   compute_pre_earnings_score(...) -> PreEarningsScoreResult
        │
        ▼
core/pre_earnings/decision.py     (NEW — §51 context assembly + engine bridge)
   build_pre_earnings_decision_context(item, *, services) -> dict
   evaluate_pre_earnings_decision(item, *, config, services) -> EvaluationResult
        │  calls core.ai.decision_engine.evaluate_trade_decision
        ▼
core/ai/decision_engine.py        (UNCHANGED — Phase 4C)
        │
        ▼
   §41 Decision + §42 ValidationResult + §44 CriticVerdict + review dict
        │
        ▼
item["pre_earnings_decision"]  →  §93 UI  →  §109 E2E
```

**Three sub-phases, each independently testable:**

- **5A — Pre-Earnings Score & Data Quality (§49/§50):** new `core/pre_earnings/score.py`; formalize the §50 triple; evolve `_compute_opportunity_score` → canonical score; promote coverage + confidence. Pure, deterministic, no I/O.
- **5B — Pre-Earnings Decision & Autonomy (§51):** new `core/pre_earnings/decision.py`; assemble the §51 input stack (with honest stubs for later-phase inputs); bridge to `decision_engine`; wire into `run_cycle`; autonomy gate via `core/autonomy`.
- **5C — UI, E2E & Control Docs (§93/§109):** §93 scanner columns + detail evidence; §109 E2E golden/acceptance test; update V9 control docs + Phase 5 row.

## 4. Components

### 4.1 `core/pre_earnings/score.py` (NEW — 5A)

Pure functions, stdlib only, no I/O, fully deterministic.

```python
@dataclass(frozen=True)
class PreEarningsScoreResult:
    score: int                       # 0-100, the PRE-EARNINGS SCORE
    label: str                       # VERY_LOW/LOW/MODERATE/HIGH/VERY_HIGH
    components: List[Dict[str, Any]] # visible: {name, value 0-1, weight, note}
    coverage_score: int              # 0-100 (DATA COVERAGE)
    coverage_label: str              # HIGH/MEDIUM/LOW
    coverage_contributors: List[Dict[str, Any]]
    data_confidence: str             # HIGH/MEDIUM/LOW/NONE (DATA CONFIDENCE)
    data_confidence_reason: str
    is_not_probability: bool         # always True — pinned invariant

def compute_pre_earnings_score(
    *,
    gap_risk: Optional[int],            # from compute_gap_risk_score
    direction_bias: Optional[int],      # from compute_direction_bias_score (-100..100)
    coverage_score: int,                # from _compute_analysis_coverage
    coverage_contributors: List[Dict[str, Any]],
    historical_samples: int,
    data_confidence: str,               # from the promoted bands
    data_confidence_reason: str,
    news_count: int,
    implied_available: bool,
    weight_overrides: Optional[Dict[str, float]] = None,
) -> PreEarningsScoreResult: ...
```

**Score math (evolution of `_compute_opportunity_score`, NOT a rewrite):**

```
signal_strength = 0.65 * gap_risk + 0.35 * abs(direction_bias)     # 0-100
score = round(clamp(signal_strength * (coverage_score / 100), 0, 100))
```

This is exactly the existing `opportunity_score` formula — promoted to canonical, with **visible components** emitted (`gap_risk`, `direction_bias_magnitude`, `coverage_modulator`) so the §50 "visible components" requirement is satisfied and the item keeps `opportunity_score` for back-compat. When `gap_risk` is None (insufficient history), `signal_strength` falls back to `abs(direction_bias)` scaled by coverage; when both are absent, score = 0 with `data_confidence=NONE`.

**DATA COVERAGE:** delegated to the existing `_compute_analysis_coverage` math (6 components). 5A exposes it as a first-class field, unchanged.

**DATA CONFIDENCE (evolved, multi-input):** today it is `historical_samples`-only. 5A makes it a small documented function of (historical_samples, coverage_score, implied_available, news_count):

```
historical_samples >= 3 AND coverage_score >= 45        -> HIGH
historical_samples >= 2 OR  coverage_score >= 45        -> MEDIUM
historical_samples == 1                                  -> LOW
historical_samples == 0                                  -> NONE
```
This is a superset of the current bands (a row that was HIGH stays HIGH; some MEDIUM rows may promote to HIGH only when coverage is also strong — a strictly more-honest, never-less-honest, change). The current `data_confidence`/`data_message` item fields are preserved; the new multi-input version replaces the inline block at 5083-5097.

**Invariant:** `is_not_probability` is a constant `True` field. The score is a weighted signal-strength×coverage composite; it is never interpreted as P(up) and never feeds a sizing/risk calculation as a probability. Golden test pins this.

### 4.2 `core/pre_earnings/decision.py` (NEW — 5B)

```python
def build_pre_earnings_decision_context(
    item: Dict[str, Any],
    *,
    services: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the §51 input stack for a Pre-Earnings candidate."""

def evaluate_pre_earnings_decision(
    item: Dict[str, Any],
    *,
    config: Dict[str, Any],
    services: Optional[Dict[str, Any]] = None,
) -> "EvaluationResult":
    """Bridge to core.ai.decision_engine.evaluate_trade_decision."""
```

**§51 input stack** assembled into the signal dict handed to the engine:

| §51 input | Source now | Later phase |
|---|---|---|
| Pre-Earnings | `PreEarningsScoreResult` + `gap_risk`/`direction_bias`/components | — |
| strategy evidence | honest stub: `{"available": False, "reason": "strategy registry not built (Phase 8)"}` | Phase 8 |
| signal | honest stub: `{"available": False, "reason": "signal engine not built (Phase 6)"}` | Phase 6 |
| backtest | honest stub: `{"available": False, "reason": "backtest engine not built (Phase 7)"}` | Phase 7 |
| AI | engine call itself (§41 Decision) | — |
| critic | engine `run_critic` (§44) | — |
| data quality | `data_confidence` + `coverage_score` + `news_only_call` | — |
| portfolio | honest stub: `{"available": False, "reason": "portfolio service not built (Phase 9)"}` | Phase 9 |
| risk | `risk_result` from agent's existing risk pass (if present) else stub | Phase 9 |
| capital | honest stub: `{"available": False, "reason": "capital service not built (Phase 9)"}` | Phase 9 |

Honest stubs are explicit `available: False` dicts with a reason string — **never** faked numbers (§4, §43). The engine's §42 AUTHORITATIVE DATA CHECK + §43 (AI numbers not authoritative) already reject AI that tries to invent these.

**Bridge:** `evaluate_pre_earnings_decision` builds a `signal` dict in the shape `agent_manager` already hands the engine (`ticker`, `horizon`, `score`, `direction_bias`, `thesis`, `supporting_evidence`, `risk_flags`, `data_quality`, the §51 stack) and calls `evaluate_trade_decision(signal, risk_result, config=config, services=services)`. It returns the `EvaluationResult` (§41/§42/§44 + `review`).

**Autonomy gate (§51/§45/§74):** before any order-bearing action is accepted, `evaluate_pre_earnings_decision` consults `core.autonomy.resolve_mode` + `kill_switch_active` + `mode_allows_paper_orders`/`mode_allows_live_orders`. Below PAPER_AUTO, or with the kill switch active, the decision is forced to `WATCH` (with the engine's `review` still attached for transparency) — exactly the 4C gate semantics, reused not duplicated. A Pre-Earnings item never emits an autonomous order unless autonomy + kill-switch + (LIVE_AUTO → `can_enable_live_auto`) all allow it.

### 4.3 `agents/pre_earnings_agent.py` (evolved — 5A/5B)

- **5A:** in `_build_item_for_symbol`, replace the inline `data_confidence` band block (5083-5097) and the `opportunity_score` call (5112-5116) with a single `compute_pre_earnings_score(...)` call. The item additively gains `pre_earnings_score`, `pre_earnings_score_label`, `pre_earnings_score_components`, `pre_earnings_is_not_probability`. Existing fields (`opportunity_score`, `coverage_score`, `data_confidence`, `data_message`) are preserved for back-compat — `opportunity_score` is set equal to `pre_earnings_score`.
- **5B:** in `run_cycle`, after items are built, a `evaluate_pre_earnings_decision` pass attaches `item["pre_earnings_decision"]` (the §41 action + `review` summary). Default action `WATCH` when autonomy < PAPER_AUTO or data insufficient. The existing `_ai_decision_qualifies_pre_earnings` AI-eligibility path is preserved as one input, not removed.

### 4.4 `core/pre_earnings/__init__.py` (NEW)

Re-exports `PreEarningsScoreResult`, `compute_pre_earnings_score`, `build_pre_earnings_decision_context`, `evaluate_pre_earnings_decision`.

## 5. Data flow (§109 E2E)

```
candidate (calendar)
→ data validation            (_event_gate_blockers + DATA COVERAGE/CONFIDENCE)
→ Pre-Earnings analysis      (compute_pre_earnings_score → §50 triple)
→ strategy evidence          (stub now — Phase 8)
→ backtest evidence          (stub now — Phase 7)
→ AI Analyst                 (decision_engine.generate_with_fallback → §41)
→ AI Critic                  (decision_engine.run_critic → §44)
→ Decision                   (§42 pipeline → 7-action enum)
→ Risk                       (risk_result; §42 RISK GATE stage)
→ Capital                    (stub now — Phase 9)
→ Paper order                (only when autonomy ≥ PAPER_AUTO + kill switch off)
→ position                   (agent_manager position track — Phase 11 surface)
→ Decision Ledger            (Phase 13 surface; item carries decisionContext)
```

## 6. Error handling

- **Insufficient data:** `data_confidence=NONE` → score 0, decision forced `WATCH` (never NO_TRADE-by-default; WATCH keeps the candidate visible with an honest "needs more data" reason). The §4 NEWS_ONLY_CALL guard is preserved.
- **AI unavailable / malformed:** engine returns `available=False` (4C semantics) → decision `WATCH` with `review.reason`. Never falls through to an order.
- **Critic veto:** engine forces `NO_TRADE` with `review.recommendation="avoid"` (4C semantics).
- **Autonomy below PAPER_AUTO / kill switch active:** decision forced `WATCH`, `review.reason="autonomy_mode_{mode}"` / `"kill_switch_active"`. No order path.
- **Later-phase input absent:** honest stub `available: False` — engine's §42 AUTHORITATIVE DATA CHECK down-weights; never faked.

## 7. Testing

- **5A unit (`tests/unit/test_pre_earnings_score.py`):** score determinism; visible components present; `opportunity_score` back-compat equality; coverage delegation; DATA CONFIDENCE bands (incl. the never-less-honest promotion); `is_not_probability` invariant; None-gap_risk fallback; weight_overrides.
- **5A golden (`tests/golden/test_phase5a_golden.py`, `pytest.mark.golden`):** GOLD-050 — score formula frozen, score-is-not-probability invariant, coverage/confidence band boundaries.
- **5B unit (`tests/unit/test_pre_earnings_decision.py`):** §51 context assembly (all 10 inputs present, stubs honest); bridge returns `EvaluationResult`; autonomy gate forces WATCH below PAPER_AUTO; kill switch forces WATCH; critic veto → NO_TRADE/avoid; AI unavailable → WATCH/unavailable; honest stubs never produce faked numbers.
- **5B acceptance (`tests/acceptance/test_pre_earnings_autonomy.py`):** the 7-action enum is reachable per autonomy mode; PAPER_AUTO allows paper OPEN_LONG; ANALYZE/RECOMMEND force WATCH; LIVE_AUTO blocked without `can_enable_live_auto`.
- **5C E2E (`tests_e2e/` or `tests/acceptance/test_pre_earnings_e2e_journey.py`):** §109 journey candidate→…→Decision Ledger with fake services (no network), asserting each stage's output and that no order fires under ANALYZE.
- **5C golden (`tests/golden/test_phase5c_golden.py`):** §93 UI column contract (scanner columns present on the item; detail carries all evidence).
- **Regression:** curated gate `pytest tests/unit tests/integration tests/contract tests/golden tests/perf` green; full `pytest tests tests_e2e` green. §4 sweep over all Phase-5 files = 0 hits. Deps diff empty (§118). V7 untouched (f912a33).

## 8. Scope / out-of-scope

- **In scope:** §50 triple, §51 decision + autonomy, §93 UI columns, §109 E2E proof, control docs.
- **Out of scope (honest stubs now, real later):** strategy registry (Phase 8), signal/ML engine (Phase 6), backtest (Phase 7), news FinBERT/SEC (Phase 10), portfolio/risk/capital services (Phase 9), Decision Ledger persistence (Phase 13), live positions (Phase 11). Stubs are `available: False` with a reason — never faked (§4/§43).
- **No rewrite of `pre_earnings_agent.py`:** additive extension of `_build_item_for_symbol` + `run_cycle` only. The 11,857-line body is otherwise untouched.

## 9. Sub-phase gates

- **5A PASS:** §50 triple formalized, golden GOLD-050 green, curated gate green.
- **5B PASS:** §51 decision + autonomy wired, acceptance green, curated gate green.
- **5C PASS:** §93 UI + §109 E2E + control docs, full gate green.
- **Phase 5 PASS:** 5A+5B+5C PASS, §4 sweep 0, deps diff empty, V7 untouched.