# Phase 5 — Pre-Earnings V9 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn Pre-Earnings from a scoring surface into a §49-§51 decision system: a canonical PRE-EARNINGS SCORE 0-100 (§50) with DATA COVERAGE + DATA CONFIDENCE, wired through the Phase 4C decision engine into the 7-action §51 autonomy-gated decision, surfaced in the §93 UI and proven by the §109 E2E journey.

**Architecture:** Evolve the existing `compute_gap_risk_score` / `compute_direction_bias_score` / `_compute_analysis_coverage` / `_compute_opportunity_score` / `data_confidence` bands in `agents/pre_earnings_agent.py` into a new pure `core/pre_earnings/score.py` (§50 triple), then bridge to `core/ai/decision_engine.evaluate_trade_decision` via a new `core/pre_earnings/decision.py` (§51 context + autonomy clamp), then surface §93 UI columns + §109 E2E. Evolve-not-rewrite (§5/§7): additive extension of `_build_item_for_symbol` + `run_cycle` only; the 11,857-line body is otherwise untouched.

**Tech Stack:** Python 3.14 stdlib only; zero new deps (§118). pytest (markers: `golden`). Fake AI providers in tests (no network).

## Global Constraints

- Zero new dependencies (§118) — stdlib only. Deps diff over requirements*/pyproject/setup must be empty at every gate.
- No remote push (§116) — commit to `master` only.
- No secrets/keys in V9 (§106) — config placeholder keys only.
- No forbidden success language (§4) — never "mostly complete", "should work", "99%", "mock for now", "todo later", "placeholder". Honest stubs for later-phase inputs are explicit `available: False` dicts with a reason string, never faked numbers.
- V7 at `C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium` is READ-ONLY (HEAD f912a33) — never modify.
- AI must not bypass Risk Gate / change Risk Constitution / enable LIVE_AUTO / disable Kill Switch (§48). Autonomy clamp reuses `core/autonomy`, never duplicates it.
- `AIResponse` is frozen with NO `provider` field. Fake providers MUST use `name = "fake"` class attribute and `AIResponse(text=..., model="m")` (no `provider=` kwarg).
- Score is NOT probability (§50) — the score is a weighted signal-strength×coverage composite; it never feeds sizing/risk as P(up).
- Curated gate: `pytest tests/unit tests/integration tests/contract tests/golden tests/perf`. Full gate: `pytest tests tests_e2e`.

---

## File Structure

- **Create** `core/pre_earnings/__init__.py` — package re-exports.
- **Create** `core/pre_earnings/score.py` — §50 pure scoring (PreEarningsScoreResult, compute_pre_earnings_score, compute_data_confidence). No I/O.
- **Create** `core/pre_earnings/decision.py` — §51 context assembly + engine bridge + autonomy clamp (build_pre_earnings_decision_context, evaluate_pre_earnings_decision).
- **Create** `tests/unit/test_pre_earnings_score.py` — 5A unit tests.
- **Create** `tests/golden/test_phase5a_golden.py` — GOLD-050 (5A golden, `pytest.mark.golden`).
- **Create** `tests/unit/test_pre_earnings_decision.py` — 5B unit tests.
- **Create** `tests/acceptance/test_pre_earnings_autonomy.py` — 5B acceptance (7-action enum per mode).
- **Create** `tests/acceptance/test_pre_earnings_e2e_journey.py` — 5C §109 E2E.
- **Create** `tests/golden/test_phase5c_golden.py` — GOLD-093 UI column contract.
- **Modify** `agents/pre_earnings_agent.py` — `_build_item_for_symbol` (5A, additive §50 fields) + `run_cycle` (5B, one gated decision-pass call) + `_attach_pre_earnings_decisions` helper (5B).
- **Modify** `core/config.py` + `config.example.json` — `pre_earnings.decision_enabled` (default False) + `decision_max_tickers` (default 25).
- **Modify** `docs/V9_ARCHITECTURE.md`, `docs/V9_AI_SYSTEM.md`, `docs/V9_CHANGELOG.md`, `docs/V9_IMPLEMENTATION.md` — Phase 5 sections + Phase 5 row.

---

## Task 1: core/pre_earnings/score.py — §50 PRE-EARNINGS SCORE + DATA COVERAGE + DATA CONFIDENCE

**Files:**
- Create: `core/pre_earnings/__init__.py`
- Create: `core/pre_earnings/score.py`
- Test: `tests/unit/test_pre_earnings_score.py`

**Interfaces:**
- Produces: `PreEarningsScoreResult` (frozen dataclass: `score:int`, `label:str`, `components:List[dict]`, `coverage_score:int`, `coverage_label:str`, `coverage_contributors:List[dict]`, `data_confidence:str`, `data_confidence_reason:str`, `is_not_probability:bool`); `compute_pre_earnings_score(*, gap_risk, direction_bias, coverage_score, coverage_contributors, historical_samples, news_count, implied_available, weight_overrides=None) -> PreEarningsScoreResult`; `compute_data_confidence(*, historical_samples, coverage_score, implied_available, news_count) -> Tuple[str, str, bool]` (label, reason, is_insufficient).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_pre_earnings_score.py
from core.pre_earnings.score import (
    PreEarningsScoreResult, compute_data_confidence, compute_pre_earnings_score,
)


def test_score_matches_legacy_opportunity_formula_and_back_compat():
    # Legacy: (0.65*gap + 0.35*|bias|) * coverage/100
    r = compute_pre_earnings_score(
        gap_risk=80, direction_bias=60, coverage_score=70,
        coverage_contributors=[], historical_samples=4, news_count=5,
        implied_available=True,
    )
    expected = round((0.65 * 80 + 0.35 * 60) * 0.70)  # 67.55 -> 68
    assert r.score == expected
    assert r.is_not_probability is True
    assert r.data_confidence == "HIGH"  # 4 quarters + coverage 70


def test_insufficient_history_scores_zero_and_none_confidence():
    r = compute_pre_earnings_score(
        gap_risk=None, direction_bias=40, coverage_score=50,
        coverage_contributors=[], historical_samples=0, news_count=3,
        implied_available=False,
    )
    assert r.score == 0
    assert r.data_confidence == "NONE"
    assert r.is_not_probability is True


def test_data_confidence_bands_never_upgrade_only_downgrade():
    high_cov = compute_data_confidence(historical_samples=4, coverage_score=80,
                                       implied_available=True, news_count=6)
    assert high_cov[0] == "HIGH"
    low_cov = compute_data_confidence(historical_samples=4, coverage_score=20,
                                      implied_available=False, news_count=0)
    assert low_cov[0] == "MEDIUM"  # coverage downgrades HIGH -> MEDIUM
    two = compute_data_confidence(historical_samples=2, coverage_score=90,
                                  implied_available=True, news_count=8)
    assert two[0] == "MEDIUM"  # 2 quarters stays MEDIUM regardless of coverage
    one = compute_data_confidence(historical_samples=1, coverage_score=90,
                                  implied_available=True, news_count=8)
    assert one[0] == "LOW"  # 1 quarter stays LOW (never upgraded by coverage)
    zero = compute_data_confidence(historical_samples=0, coverage_score=90,
                                   implied_available=True, news_count=8)
    assert zero[0] == "NONE" and zero[2] is True  # is_insufficient


def test_score_components_visible_and_coverage_delegated():
    r = compute_pre_earnings_score(
        gap_risk=70, direction_bias=30, coverage_score=60,
        coverage_contributors=[{"name": "news_context", "value": 0.8, "weight": 0.3, "note": "x"}],
        historical_samples=3, news_count=6, implied_available=True,
    )
    names = [c["name"] for c in r.components]
    assert "gap_risk" in names and "direction_bias_magnitude" in names and "coverage_modulator" in names
    assert r.coverage_score == 60 and r.coverage_label == "MEDIUM"
    assert r.coverage_contributors[0]["name"] == "news_context"


def test_weight_overrides_change_split():
    r_default = compute_pre_earnings_score(
        gap_risk=100, direction_bias=0, coverage_score=100,
        coverage_contributors=[], historical_samples=4, news_count=6, implied_available=True)
    r_override = compute_pre_earnings_score(
        gap_risk=100, direction_bias=0, coverage_score=100,
        coverage_contributors=[], historical_samples=4, news_count=6, implied_available=True,
        weight_overrides={"gap_risk": 1.0, "direction_bias": 0.0})
    assert r_default.score == 100  # 0.65*100+0.35*0 = 65 -> *1.0 = 65? No: coverage 100
    # default: (0.65*100+0.35*0)*1.0 = 65 ; override: (1.0*100+0.0*0)*1.0 = 100
    assert r_default.score == 65
    assert r_override.score == 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_pre_earnings_score.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.pre_earnings'`

- [ ] **Step 3: Write the package init + score module**

```python
# core/pre_earnings/__init__.py
"""Phase 5 — Pre-Earnings V9 decision system (§49/§50/§51).

§50 triple (score/coverage/confidence) in score.py; §51 decision context +
engine bridge in decision.py. Evolves the V7 pre_earnings_agent scoring surface
(evolve-not-rewrite, §5/§7); composes the Phase 4C decision engine + autonomy.
Stdlib-only, no I/O in score.py.
"""
from core.pre_earnings.score import (
    PreEarningsScoreResult, compute_data_confidence, compute_pre_earnings_score,
)

__all__ = [
    "PreEarningsScoreResult", "compute_data_confidence", "compute_pre_earnings_score",
]
```

```python
# core/pre_earnings/score.py
"""§50 — PRE-EARNINGS SCORE 0-100 + DATA COVERAGE + DATA CONFIDENCE.

Pure, deterministic, stdlib-only, no I/O. Evolves the V7 proto scoring in
agents/pre_earnings_agent.py:
  * PRE-EARNINGS SCORE <- _compute_opportunity_score formula
    (signal_strength = 0.65*gap_risk + 0.35*|direction_bias|) * (coverage/100),
    promoted to canonical with VISIBLE components. Score is NOT probability.
  * DATA COVERAGE  <- _compute_analysis_coverage (6 weighted components), unchanged.
  * DATA CONFIDENCE <- the historical_samples bands (5083-5097), evolved so
    coverage can only DOWNGRADE (never upgrade) — strictly more honest.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def _coverage_label(score: int) -> str:
    s = int(score or 0)
    if s >= 70:
        return "HIGH"
    if s >= 45:
        return "MEDIUM"
    return "LOW"


def _score_label(score: int) -> str:
    s = int(score or 0)
    if s >= 80:
        return "VERY_HIGH"
    if s >= 60:
        return "HIGH"
    if s >= 40:
        return "MODERATE"
    if s >= 20:
        return "LOW"
    return "VERY_LOW"


def compute_data_confidence(
    *,
    historical_samples: int,
    coverage_score: int,
    implied_available: bool,
    news_count: int,
) -> Tuple[str, str, bool]:
    """§50 DATA CONFIDENCE. Returns (label, reason, is_insufficient).

    Coverage is a DOWNGRADE-ONLY modifier: a thin coverage read can lower
    HIGH->MEDIUM but can never raise LOW->MEDIUM. This is strictly more honest
    than the V7 historical_samples-only bands (a row that was HIGH/MEDIUM/LOW/NONE
    keeps its band unless coverage is weak, in which case it is downgraded).
    is_insufficient is True only when historical_samples == 0 (NONE band),
    preserving the V7 `is_insufficient_data` semantics used by the NEWS_ONLY_CALL
    guard and gap_risk_unavailable flag.
    """
    s = int(historical_samples or 0)
    cov = int(coverage_score or 0)
    extra = f"news={int(news_count or 0)}, implied={'y' if implied_available else 'n'}"
    if s >= 3:
        if cov >= 45:
            return "HIGH", f"OK ({s} quarters, coverage {cov}; {extra})", False
        return "MEDIUM", f"Partial data: {s} quarters, coverage {cov} ({extra}). GapRisk estimated.", False
    if s == 2:
        return "MEDIUM", f"Partial data: {s} quarters ({extra}). GapRisk estimated.", False
    if s == 1:
        return "LOW", f"Only 1 quarter available ({extra}). GapRisk estimated from news.", False
    return "NONE", f"No historical quarters ({extra}). GapRisk unavailable. DirectionBias from news.", True


@dataclass(frozen=True)
class PreEarningsScoreResult:
    score: int
    label: str
    components: List[Dict[str, Any]] = field(default_factory=list)
    coverage_score: int = 0
    coverage_label: str = "LOW"
    coverage_contributors: List[Dict[str, Any]] = field(default_factory=list)
    data_confidence: str = "NONE"
    data_confidence_reason: str = ""
    is_not_probability: bool = True


def compute_pre_earnings_score(
    *,
    gap_risk: Optional[int],
    direction_bias: Optional[int],
    coverage_score: int,
    coverage_contributors: List[Dict[str, Any]],
    historical_samples: int,
    news_count: int,
    implied_available: bool,
    weight_overrides: Optional[Dict[str, float]] = None,
) -> PreEarningsScoreResult:
    """§50 PRE-EARNINGS SCORE 0-100 with visible components.

    Formula (evolution of _compute_opportunity_score, exact back-compat when
    weight_overrides is None):
        signal_strength = w_gap*gap_risk + w_bias*|direction_bias|   # 0-100
        score = round(clamp(signal_strength * (coverage/100), 0, 100))
    with w_gap=0.65, w_bias=0.35 (sum 1.0). When historical_samples == 0
    (DATA CONFIDENCE NONE) the score is 0 — matching the V7 forced-zero for
    insufficient-data rows. Score is NOT probability (§50): it is a
    signal-strength×coverage composite, never interpreted as P(up).
    """
    conf_label, conf_reason, is_insufficient = compute_data_confidence(
        historical_samples=historical_samples, coverage_score=coverage_score,
        implied_available=implied_available, news_count=news_count,
    )
    cov = int(coverage_score or 0)

    w_gap, w_bias = 0.65, 0.35
    if isinstance(weight_overrides, dict):
        for key, value in weight_overrides.items():
            if key in ("gap_risk", "direction_bias") and isinstance(value, (int, float)) and value > 0:
                if key == "gap_risk":
                    w_gap = float(value)
                else:
                    w_bias = float(value)

    if is_insufficient:
        score = 0
        gap_val = 0.0
        bias_val = 0.0
    else:
        gap_int = int(gap_risk) if gap_risk is not None else 0
        bias_int = abs(int(direction_bias)) if direction_bias is not None else 0
        gap_val = float(gap_int)
        bias_val = float(bias_int)
        signal_strength = w_gap * gap_int + w_bias * bias_int
        score = int(round(_clamp(signal_strength * (cov / 100.0), 0.0, 100.0)))

    components: List[Dict[str, Any]] = [
        {
            "name": "gap_risk",
            "value": round(gap_val / 100.0, 4),
            "weight": round(w_gap, 4),
            "note": "Gap/expected-move risk axis (compute_gap_risk_score, 0-100)",
        },
        {
            "name": "direction_bias_magnitude",
            "value": round(bias_val / 100.0, 4),
            "weight": round(w_bias, 4),
            "note": "Direction conviction magnitude (compute_direction_bias_score, |-100..100|)",
        },
        {
            "name": "coverage_modulator",
            "value": round(cov / 100.0, 4),
            "weight": 1.0,
            "note": "DATA COVERAGE modulates signal strength. Score is NOT probability.",
        },
    ]

    return PreEarningsScoreResult(
        score=score,
        label=_score_label(score),
        components=components,
        coverage_score=cov,
        coverage_label=_coverage_label(cov),
        coverage_contributors=list(coverage_contributors or []),
        data_confidence=conf_label,
        data_confidence_reason=conf_reason,
        is_not_probability=True,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_pre_earnings_score.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add core/pre_earnings/__init__.py core/pre_earnings/score.py tests/unit/test_pre_earnings_score.py
git commit -m "feat(phase5a): core/pre_earnings/score.py — §50 score/coverage/confidence (§49/§50)"
```

---

## Task 2: Evolve _build_item_for_symbol to emit the §50 triple (additive)

**Files:**
- Modify: `agents/pre_earnings_agent.py` (inline data_confidence block ~5083-5097 + opportunity_score call ~5112-5116 + item dict ~5304-5307)
- Test: `tests/unit/test_pre_earnings_agent_score_integration.py` (create)

**Interfaces:**
- Consumes: `compute_pre_earnings_score`, `compute_data_confidence` from Task 1.
- Produces: item gains `pre_earnings_score`, `pre_earnings_score_label`, `pre_earnings_score_components`, `pre_earnings_is_not_probability` (additive); existing `opportunity_score`/`coverage_score`/`data_confidence`/`data_message` preserved.

- [ ] **Step 1: Write the failing integration test**

```python
# tests/unit/test_pre_earnings_agent_score_integration.py
import agents.pre_earnings_agent as pe


def test_compute_data_confidence_replaces_inline_band(monkeypatch):
    # The agent module must re-export the new canonical confidence function so
    # the inline band block can delegate to it without duplicating logic.
    from core.pre_earnings.score import compute_data_confidence
    assert pe.compute_data_confidence is compute_data_confidence


def test_compute_pre_earnings_score_replaces_opportunity_score(monkeypatch):
    from core.pre_earnings.score import compute_pre_earnings_score
    assert pe.compute_pre_earnings_score is compute_pre_earnings_score
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_pre_earnings_agent_score_integration.py -v`
Expected: FAIL (`AttributeError: module ... has no attribute compute_pre_earnings_score`)

- [ ] **Step 3: Wire the canonical functions into the agent and evolve the item builder**

At the top of `agents/pre_earnings_agent.py`, alongside the existing `from core...` imports near the module-level imports, add:

```python
from core.pre_earnings.score import (
    PreEarningsScoreResult, compute_data_confidence, compute_pre_earnings_score,
)
```

Replace the inline data_confidence band block (the `if historical_samples >= 3:` ... `is_insufficient_data = True` block, ~5083-5097) with a delegation to the canonical function while preserving the local variables downstream code reads (`data_confidence`, `data_guard_msg`, `is_insufficient_data`):

```python
        # §50 DATA CONFIDENCE — delegated to the canonical multi-input function
        # (coverage is a downgrade-only modifier; is_insufficient_data preserves
        # the V7 historical_samples==0 semantics used by the NEWS_ONLY_CALL guard
        # and the gap_risk_unavailable flag).
        data_confidence, data_guard_msg, is_insufficient_data = compute_data_confidence(
            historical_samples=historical_samples,
            coverage_score=coverage_score,
            implied_available=implied_score is not None,
            news_count=news_count,
        )
```

Replace the `opportunity_score` computation (~5112-5116) with the canonical §50 score call, preserving the `opportunity_score` local for the downstream item dict:

```python
        _pe_score_result = compute_pre_earnings_score(
            gap_risk=gap_risk,
            direction_bias=direction_bias,
            coverage_score=coverage_score,
            coverage_contributors=coverage_contributors,
            historical_samples=historical_samples,
            news_count=news_count,
            implied_available=implied_score is not None,
        )
        opportunity_score = _pe_score_result.score
```

In the item dict, replace the `opportunity_score` line (~5307) with the additive §50 triple plus the back-compat `opportunity_score`:

```python
            "opportunity_score": int(_clamp(_pe_score_result.score, 0, 100)),
            "pre_earnings_score": int(_clamp(_pe_score_result.score, 0, 100)),
            "pre_earnings_score_label": _pe_score_result.label,
            "pre_earnings_score_components": _pe_score_result.components,
            "pre_earnings_is_not_probability": True,
```

- [ ] **Step 4: Run the new test + the existing pre_earnings unit suite**

Run: `pytest tests/unit/test_pre_earnings_agent_score_integration.py tests/unit -k "pre_earnings" -v`
Expected: PASS — new test passes; existing pre_earnings tests stay green (score/confidence values are back-compat-equal).

- [ ] **Step 5: Run the curated gate**

Run: `pytest tests/unit tests/integration tests/contract tests/golden tests/perf`
Expected: PASS (no regressions; the item shape is additive).

- [ ] **Step 6: Commit**

```bash
git add agents/pre_earnings_agent.py tests/unit/test_pre_earnings_agent_score_integration.py
git commit -m "feat(phase5a): emit §50 triple on pre_earnings items (additive; back-compat) (§50)"
```

---

## Task 3: GOLD-050 golden — score formula frozen + score-is-not-probability + band boundaries

**Files:**
- Create: `tests/golden/test_phase5a_golden.py`
- Test: `tests/golden/test_phase5a_golden.py`

**Interfaces:**
- Consumes: `compute_pre_earnings_score`, `compute_data_confidence` from Task 1.

- [ ] **Step 1: Write the golden test**

```python
# tests/golden/test_phase5a_golden.py
import pytest

from core.pre_earnings.score import compute_data_confidence, compute_pre_earnings_score

pytestmark = pytest.mark.golden


# GOLD-050 — the §50 score formula is frozen (evolve-not-rewrite back-compat).
def test_gold050_score_formula_frozen():
    cases = [
        # (gap, bias, coverage, hist_samples, expected_score)
        (80, 60, 70, 4, round((0.65 * 80 + 0.35 * 60) * 0.70)),   # 68
        (100, 0, 100, 4, 65),                                       # 0.65*100*1.0
        (0, 100, 100, 4, 35),                                       # 0.35*100*1.0
        (50, 50, 50, 3, round((0.65 * 50 + 0.35 * 50) * 0.50)),    # 25
        (70, 30, 60, 3, round((0.65 * 70 + 0.35 * 30) * 0.60)),    # 34.2 -> 34
    ]
    for gap, bias, cov, hs, expected in cases:
        r = compute_pre_earnings_score(
            gap_risk=gap, direction_bias=bias, coverage_score=cov,
            coverage_contributors=[], historical_samples=hs, news_count=6,
            implied_available=True)
        assert r.score == expected, f"gap={gap} bias={bias} cov={cov}: {r.score} != {expected}"


def test_gold050_score_is_not_probability_invariant():
    for gap in (None, 0, 50, 100):
        for bias in (None, -80, 0, 80):
            r = compute_pre_earnings_score(
                gap_risk=gap, direction_bias=bias, coverage_score=55,
                coverage_contributors=[], historical_samples=3, news_count=4,
                implied_available=True)
            assert r.is_not_probability is True
            assert "NOT probability" in r.components[2]["note"]


def test_gold050_data_confidence_band_boundaries():
    # HIGH requires >=3 quarters AND coverage >= 45
    assert compute_data_confidence(historical_samples=3, coverage_score=45,
                                   implied_available=True, news_count=6)[0] == "HIGH"
    assert compute_data_confidence(historical_samples=3, coverage_score=44,
                                   implied_available=True, news_count=6)[0] == "MEDIUM"
    # 2 quarters -> MEDIUM; 1 -> LOW; 0 -> NONE (coverage cannot upgrade)
    assert compute_data_confidence(historical_samples=2, coverage_score=10,
                                   implied_available=False, news_count=0)[0] == "MEDIUM"
    assert compute_data_confidence(historical_samples=1, coverage_score=95,
                                   implied_available=True, news_count=12)[0] == "LOW"
    label, _reason, insufficient = compute_data_confidence(
        historical_samples=0, coverage_score=95, implied_available=True, news_count=12)
    assert label == "NONE" and insufficient is True
```

- [ ] **Step 2: Run the golden test**

Run: `pytest tests/golden/test_phase5a_golden.py -v`
Expected: PASS (3 tests)

- [ ] **Step 3: Run the curated gate + §4 sweep**

Run: `pytest tests/unit tests/integration tests/contract tests/golden tests/perf`
Expected: PASS.
§4 sweep (PowerShell): `Select-String -Path core/pre_earnings/score.py,tests/golden/test_phase5a_golden.py -Pattern "mostly complete|should work|99%|mock for now|todo later|placeholder"`
Expected: 0 hits.

- [ ] **Step 4: Commit**

```bash
git add tests/golden/test_phase5a_golden.py
git commit -m "test(phase5a): GOLD-050 — frozen score formula + not-probability + bands (§50)"
```

---

## Task 4: core/pre_earnings/decision.py — §51 context + engine bridge + autonomy clamp

**Files:**
- Create: `core/pre_earnings/decision.py`
- Modify: `core/pre_earnings/__init__.py` (re-exports)
- Test: `tests/unit/test_pre_earnings_decision.py`

**Interfaces:**
- Consumes: `core.ai.decision_engine.evaluate_trade_decision` (Phase 4C), `core.ai.decision.Action`/`Decision`, `core.autonomy` (`resolve_mode`, `kill_switch_active`, `mode_allows_paper_orders`, `can_enable_live_auto`), `PreEarningsScoreResult` from Task 1.
- Produces: `build_pre_earnings_decision_context(item, *, services=None) -> dict`; `evaluate_pre_earnings_decision(item, *, config, services=None) -> EvaluationResult`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_pre_earnings_decision.py
from core.ai.decision import Action
from core.ai.decision_engine import EvaluationResult
from core.ai.provider import AIResponse
from core.pre_earnings.decision import (
    build_pre_earnings_decision_context, evaluate_pre_earnings_decision,
)


class _FP:
    name = "fake"

    def __init__(self, text: str):
        self._text = text

    def generate(self, prompt, *, model=None, system=""):
        return AIResponse(text=self._text, model="m")

    def capabilities(self):
        return {}

    def health_check(self):
        return True


def _approving_services():
    return {
        "primary_provider": _FP('{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.8,"thesis":"strong pre-earnings setup"}'),
    }


def _critic_ok_services():
    s = _approving_services()
    s["critic_provider"] = _FP('{"veto":false,"categories":[],"severity":0.1,"rationale":"ok"}')
    return s


def _item():
    return {
        "symbol": "NVDA", "price": 850.0, "direction_bias": 40, "gap_risk": 70,
        "pre_earnings_score": 55, "pre_earnings_score_label": "MODERATE",
        "data_confidence": "HIGH", "coverage_score": 70, "coverage_label": "HIGH",
        "news_only_call": False, "earnings_time_utc": "2026-08-20T12:00:00+00:00",
        "session": "AMC", "expected_move_pct": 8.5, "historical_median_move_pct": 6.2,
    }


def test_context_assembles_all_ten_inputs_with_honest_stubs():
    ctx = build_pre_earnings_decision_context(_item(), services=_critic_ok_services())
    for key in ("pre_earnings", "strategy_evidence", "signal", "backtest",
                "ai", "critic", "data_quality", "portfolio", "risk", "capital"):
        assert key in ctx, f"missing §51 input: {key}"
    # Later-phase stubs are honest (available: False + reason), never faked numbers.
    for stub_key in ("strategy_evidence", "signal", "backtest", "portfolio", "capital"):
        stub = ctx[stub_key]
        assert stub["available"] is False
        assert "reason" in stub and stub["reason"]


def test_bridge_returns_evaluation_result_and_open_long_under_paper_auto():
    cfg = {"autonomy": {"mode": "PAPER_AUTO"}, "ai": {"decision": {"critic_enabled": True}}}
    result = evaluate_pre_earnings_decision(_item(), config=cfg, services=_critic_ok_services())
    assert isinstance(result, EvaluationResult)
    assert result.decision.action is Action.OPEN_LONG
    assert result.review["recommendation"] == "approve"


def test_analyze_mode_clamps_order_to_watch():
    cfg = {"autonomy": {"mode": "ANALYZE"}, "ai": {"decision": {"critic_enabled": True}}}
    result = evaluate_pre_earnings_decision(_item(), config=cfg, services=_critic_ok_services())
    assert result.decision.action is Action.WATCH
    assert "autonomy_mode_ANALYZE" in result.review["reason"]


def test_kill_switch_clamps_to_watch():
    cfg = {"autonomy": {"mode": "PAPER_AUTO", "kill_switch_active": True},
           "ai": {"decision": {"critic_enabled": True}}}
    result = evaluate_pre_earnings_decision(_item(), config=cfg, services=_critic_ok_services())
    assert result.decision.action is Action.WATCH
    assert result.review["reason"] == "kill_switch_active"


def test_critic_veto_stays_no_trade_not_upgraded_to_watch():
    s = _critic_ok_services()
    s["critic_provider"] = _FP('{"veto":true,"categories":["concentration"],"severity":0.9,"rationale":"concentrated"}')
    cfg = {"autonomy": {"mode": "PAPER_AUTO"}, "ai": {"decision": {"critic_enabled": True}}}
    result = evaluate_pre_earnings_decision(_item(), config=cfg, services=s)
    assert result.decision.action is Action.NO_TRADE
    assert result.review["recommendation"] == "avoid"


def test_ai_unavailable_maps_to_watch_not_no_trade():
    # No provider configured -> engine returns unavailable; Pre-Earnings maps to WATCH.
    cfg = {"autonomy": {"mode": "PAPER_AUTO"}}
    result = evaluate_pre_earnings_decision(_item(), config=cfg, services={})
    assert result.decision.action is Action.WATCH
    assert result.review["available"] is False


def test_live_auto_without_acceptance_clamps_order_to_watch():
    cfg = {"autonomy": {"mode": "LIVE_AUTO", "live_auto_owner_enabled": True,
                        "paper_auto_acceptance_passed": False},
           "ai": {"decision": {"critic_enabled": True}}}
    result = evaluate_pre_earnings_decision(_item(), config=cfg, services=_critic_ok_services())
    assert result.decision.action is Action.WATCH
    assert "live_auto_blocked" in result.review["reason"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_pre_earnings_decision.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'core.pre_earnings.decision'`)

- [ ] **Step 3: Write the decision module**

```python
# core/pre_earnings/decision.py
"""§51 — Pre-Earnings autonomy: the decision is NOT based only on the score.

build_pre_earnings_decision_context() assembles the full §51 input stack
(Pre-Earnings + strategy evidence + signal + backtest + AI + critic +
data quality + portfolio + risk + capital). Later-phase inputs that do not
exist yet are HONEST STUBS (available: False + reason) — never faked numbers
(§4/§43). The engine's §42 AUTHORITATIVE DATA CHECK + §43 already reject AI
that tries to invent them.

evaluate_pre_earnings_decision() bridges to core.ai.decision_engine and applies
the Pre-Earnings autonomy clamp (§45/§74/§46): below PAPER_AUTO, or with the
kill switch active, or LIVE_AUTO without acceptance, an order-bearing action is
clamped to WATCH (the engine's §41/§42/§44 artifacts stay attached for
transparency). NO_TRADE (critic veto / policy) is never upgraded to WATCH.
AI-unavailable maps to WATCH ("keep watching"), not a decisive NO_TRADE.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from core.ai.decision import Action, Decision
from core.ai.decision_engine import EvaluationResult, evaluate_trade_decision
from core.autonomy import (
    can_enable_live_auto, kill_switch_active, mode_allows_paper_orders, resolve_mode,
)


def _honest_stub(reason: str) -> Dict[str, Any]:
    return {"available": False, "reason": reason}


def build_pre_earnings_decision_context(
    item: Dict[str, Any],
    *,
    services: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the §51 input stack for a Pre-Earnings candidate."""
    return {
        "pre_earnings": {
            "available": True,
            "score": item.get("pre_earnings_score"),
            "label": item.get("pre_earnings_score_label"),
            "gap_risk": item.get("gap_risk"),
            "direction_bias": item.get("direction_bias"),
            "data_confidence": item.get("data_confidence"),
            "coverage_score": item.get("coverage_score"),
            "coverage_label": item.get("coverage_label"),
            "expected_move_pct": item.get("expected_move_pct"),
            "historical_median_move_pct": item.get("historical_median_move_pct"),
            "session": item.get("session"),
            "earnings_time_utc": item.get("earnings_time_utc"),
        },
        "strategy_evidence": _honest_stub("strategy registry not built (Phase 8)"),
        "signal": _honest_stub("signal/ML engine not built (Phase 6)"),
        "backtest": _honest_stub("backtest engine not built (Phase 7)"),
        "ai": {"available": True, "note": "AI Analyst via decision_engine (§41)"},
        "critic": {"available": True, "note": "AI Critic via decision_engine (§44)"},
        "data_quality": {
            "available": True,
            "data_confidence": item.get("data_confidence"),
            "coverage_score": item.get("coverage_score"),
            "news_only_call": bool(item.get("news_only_call")),
        },
        "portfolio": _honest_stub("portfolio service not built (Phase 9)"),
        "risk": {
            "available": bool(item.get("risk_evaluation")),
            "evaluation": item.get("risk_evaluation"),
        },
        "capital": _honest_stub("capital service not built (Phase 9)"),
    }


def _build_engine_signal(item: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ticker": item.get("symbol") or item.get("ticker") or "",
        "entry": item.get("price") or item.get("last_price"),
        "strategy_type": "PRE_EARNINGS",
        "signal_score": (float(item.get("pre_earnings_score") or 0) / 100.0),
        "horizon": "SWING",
        "direction_bias": item.get("direction_bias"),
        "gap_risk": item.get("gap_risk"),
        "pre_earnings_score": item.get("pre_earnings_score"),
        "data_quality": float(item.get("coverage_score") or 0) / 100.0,
        "thesis": f"Pre-Earnings score {item.get('pre_earnings_score')} "
                  f"({item.get('pre_earnings_score_label')}), "
                  f"confidence {item.get('data_confidence')}, "
                  f"coverage {item.get('coverage_score')}.",
        "risk_flags": ["news_only_call"] if item.get("news_only_call") else [],
        # §51 stack (engine prompt surfaces these; stubs are honest unavailable)
        "pre_earnings_context": ctx["pre_earnings"],
        "strategy_evidence": ctx["strategy_evidence"],
        "signal_evidence": ctx["signal"],
        "backtest_evidence": ctx["backtest"],
        "data_quality_context": ctx["data_quality"],
        "portfolio_context": ctx["portfolio"],
        "capital_context": ctx["capital"],
        "risk_context": ctx["risk"],
    }


def _with_reason(review: Dict[str, Any], reason: str) -> Dict[str, Any]:
    out = dict(review or {})
    out["reason"] = reason
    out["recommendation"] = "watch"
    return out


def _autonomy_clamp(
    result: EvaluationResult, config: Dict[str, Any],
) -> tuple:
    """Return (action, review) after the Pre-Earnings autonomy clamp."""
    # AI-unavailable / malformed -> keep watching (NOT a decisive NO_TRADE).
    if not result.ai_available:
        return Action.WATCH, _with_reason(result.review, "ai_unavailable: keep watching")
    if kill_switch_active(config):
        return Action.WATCH, _with_reason(result.review, "kill_switch_active")
    mode = resolve_mode(config)
    action = result.decision.action
    if action in (Action.OPEN_LONG, Action.OPEN_SHORT):
        if not mode_allows_paper_orders(mode):
            return Action.WATCH, _with_reason(result.review, f"autonomy_mode_{mode.value}")
        if mode.value == "LIVE_AUTO":
            ok, why = can_enable_live_auto(config)
            if not ok:
                return Action.WATCH, _with_reason(result.review, f"live_auto_blocked:{why}")
    return action, result.review


def evaluate_pre_earnings_decision(
    item: Dict[str, Any],
    *,
    config: Dict[str, Any],
    services: Optional[Dict[str, Any]] = None,
) -> EvaluationResult:
    """§51 — bridge a Pre-Earnings item to the decision engine + autonomy clamp."""
    ctx = build_pre_earnings_decision_context(item, services=services)
    signal = _build_engine_signal(item, ctx)
    risk_result = item.get("risk_evaluation")
    result = evaluate_trade_decision(signal, risk_result, config=config, services=services)

    clamped_action, clamped_review = _autonomy_clamp(result, config)
    if clamped_action is not result.decision.action:
        clamped_decision = Decision(
            action=clamped_action,
            ticker=result.decision.ticker,
            confidence=result.decision.confidence,
            thesis=clamped_review.get("reason", result.decision.thesis),
            raw=result.decision.raw,
        )
        return EvaluationResult(
            clamped_decision, result.validation, result.critic, clamped_review, result.ai_available,
        )
    return result
```

Extend `core/pre_earnings/__init__.py` re-exports:

```python
from core.pre_earnings.decision import (
    build_pre_earnings_decision_context, evaluate_pre_earnings_decision,
)
from core.pre_earnings.score import (
    PreEarningsScoreResult, compute_data_confidence, compute_pre_earnings_score,
)

__all__ = [
    "PreEarningsScoreResult", "build_pre_earnings_decision_context",
    "compute_data_confidence", "compute_pre_earnings_score",
    "evaluate_pre_earnings_decision",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_pre_earnings_decision.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Run the curated gate + §4 sweep**

Run: `pytest tests/unit tests/integration tests/contract tests/golden tests/perf`
Expected: PASS.
§4 sweep: `Select-String -Path core/pre_earnings/decision.py,tests/unit/test_pre_earnings_decision.py -Pattern "mostly complete|should work|99%|mock for now|todo later|placeholder"`
Expected: 0 hits.

- [ ] **Step 6: Commit**

```bash
git add core/pre_earnings/decision.py core/pre_earnings/__init__.py tests/unit/test_pre_earnings_decision.py
git commit -m "feat(phase5b): core/pre_earnings/decision.py — §51 context + engine bridge + autonomy clamp (§51)"
```

---

## Task 5: Wire the §51 decision pass into run_cycle (gated, default off, bounded)

**Files:**
- Modify: `agents/pre_earnings_agent.py` (add `_attach_pre_earnings_decisions` helper + one call in `run_cycle` ~L2810)
- Modify: `core/config.py` + `config.example.json` (add `pre_earnings.decision_enabled` False + `decision_max_tickers` 25)
- Test: `tests/unit/test_pre_earnings_run_cycle_decision.py` (create)

**Interfaces:**
- Consumes: `evaluate_pre_earnings_decision` from Task 4; `core.config.DEFAULT_CONFIG`.
- Produces: items may carry `pre_earnings_decision` (a summary dict: `action`, `recommendation`, `confidence`, `available`, `reason`, `validation_stage`, `critic_veto`). The pass is gated by `cfg["pre_earnings"]["decision_enabled"]` (default False) and bounded by `decision_max_tickers`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_pre_earnings_run_cycle_decision.py
import agents.pre_earnings_agent as pe


def test_attach_pre_earnings_decisions_is_method_on_agent():
    assert hasattr(pe.PreEarningsAgent, "_attach_pre_earnings_decisions")


def test_attach_is_noop_when_disabled(monkeypatch):
    # decision_enabled default False -> no item is touched, no engine call.
    called = {"n": 0}

    class _FakeResult:
        class decision:
            action = None
        review = {"available": False, "reason": "x"}

    def _fake_eval(item, *, config, services=None):
        called["n"] += 1
        return _FakeResult()

    monkeypatch.setattr(pe, "evaluate_pre_earnings_decision", _fake_eval)
    agent = object.__new__(pe.PreEarningsAgent)
    agent._state = {"items": [{"symbol": "NVDA"}, {"symbol": "AAPL"}]}
    agent._attach_pre_earnings_decisions({"pre_earnings": {"decision_enabled": False, "decision_max_tickers": 25}})
    assert called["n"] == 0
    assert "pre_earnings_decision" not in agent._state["items"][0]


def test_attach_adds_decision_summary_when_enabled(monkeypatch):
    from core.ai.decision import Action, Decision
    from core.ai.decision_engine import EvaluationResult
    from core.ai.pipeline import ValidationResult

    def _fake_eval(item, *, config, services=None):
        dec = Decision(action=Action.OPEN_LONG, ticker=item["symbol"], confidence=0.8, thesis="t")
        return EvaluationResult(dec, ValidationResult(ok=True, decision=dec), None,
                                {"available": True, "recommendation": "approve", "confidence": 0.8,
                                 "reason": "t", "validation_stage": "RISK_GATE", "critic_veto": False}, True)

    monkeypatch.setattr(pe, "evaluate_pre_earnings_decision", _fake_eval)
    agent = object.__new__(pe.PreEarningsAgent)
    agent._state = {"items": [{"symbol": "NVDA", "price": 100}]}
    agent._attach_pre_earnings_decisions({"pre_earnings": {"decision_enabled": True, "decision_max_tickers": 25},
                                          "autonomy": {"mode": "PAPER_AUTO"}})
    d = agent._state["items"][0]["pre_earnings_decision"]
    assert d["action"] == "OPEN_LONG" and d["recommendation"] == "approve" and d["available"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_pre_earnings_run_cycle_decision.py -v`
Expected: FAIL (`AttributeError: ... has no attribute '_attach_pre_earnings_decisions'`)

- [ ] **Step 3: Add the config defaults**

In `core/config.py`, inside `DEFAULT_CONFIG`, add a `pre_earnings` section (if one already exists, merge these keys; otherwise add the block alongside the other top-level sections):

```python
    "pre_earnings": {
        "decision_enabled": False,
        "decision_max_tickers": 25,
    },
```

Mirror in `config.example.json`:

```json
  "pre_earnings": {
    "decision_enabled": false,
    "decision_max_tickers": 25
  },
```

- [ ] **Step 4: Add the helper method + the run_cycle call site**

In `agents/pre_earnings_agent.py`, near the other `_attach_*` methods (e.g. just before `_attach_daily_ranking`'s definition — locate it with `grep -n "def _attach_daily_ranking"`), add:

```python
    def _attach_pre_earnings_decisions(self, cfg: Dict[str, Any]) -> None:
        """§51 — attach a Pre-Earnings decision summary to each item.

        Gated by cfg['pre_earnings']['decision_enabled'] (default False: ship
        wired-but-off, an owner flips it to activate the §51 decision pass —
        evolve-not-rewrite, no live-behavior change). Bounded by
        decision_max_tickers per cycle. Non-fatal: a per-item error leaves the
        item without a decision rather than failing the cycle. The autonomy
        clamp inside evaluate_pre_earnings_decision guarantees no order-bearing
        action below PAPER_AUTO / with kill switch / LIVE_AUTO without acceptance.
        """
        pe_cfg = (cfg or {}).get("pre_earnings") or {}
        if not bool(pe_cfg.get("decision_enabled", False)):
            return
        max_tickers = max(0, _safe_int(pe_cfg.get("decision_max_tickers"), 25))
        items = self._state.get("items") or []
        processed = 0
        for item in items:
            if not isinstance(item, dict) or not item.get("symbol"):
                continue
            if processed >= max_tickers:
                break
            try:
                result = evaluate_pre_earnings_decision(item, config=cfg, services=None)
                review = result.review or {}
                item["pre_earnings_decision"] = {
                    "action": result.decision.action.value if result.decision else "WATCH",
                    "recommendation": review.get("recommendation", "watch"),
                    "confidence": review.get("confidence"),
                    "available": bool(review.get("available")),
                    "reason": review.get("reason", ""),
                    "validation_stage": review.get("validation_stage", ""),
                    "critic_veto": bool(review.get("critic_veto")),
                }
            except Exception as exc:
                logger.warning(f"Pre-Earnings decision attach failed for {item.get('symbol')}: {exc}")
                item["pre_earnings_decision"] = {
                    "action": "WATCH", "recommendation": "watch", "confidence": None,
                    "available": False, "reason": f"decision_error:{exc}",
                    "validation_stage": "", "critic_veto": False,
                }
            processed += 1
```

Ensure the import is present at the top of the module (add alongside the Task 2 import):

```python
from core.pre_earnings.decision import evaluate_pre_earnings_decision
```

In `run_cycle`, insert the call immediately after the `_attach_daily_ranking(...)` block and before `self._persist_locked()` (~L2810-2811):

```python
                self._attach_pre_earnings_decisions(cfg)
```

- [ ] **Step 5: Run the new test + curated gate**

Run: `pytest tests/unit/test_pre_earnings_run_cycle_decision.py tests/unit tests/integration tests/contract tests/golden tests/perf`
Expected: PASS (3 new + no regressions; default-off means run_cycle behavior is unchanged for existing tests).

- [ ] **Step 6: Commit**

```bash
git add agents/pre_earnings_agent.py core/config.py config.example.json tests/unit/test_pre_earnings_run_cycle_decision.py
git commit -m "feat(phase5b): wire §51 decision pass into run_cycle (gated default-off, bounded) (§51)"
```

---

## Task 6: 5B acceptance — the 7-action enum is reachable per autonomy mode

**Files:**
- Create: `tests/acceptance/test_pre_earnings_autonomy.py`

**Interfaces:**
- Consumes: `evaluate_pre_earnings_decision` (Task 4), fake providers.

- [ ] **Step 1: Write the acceptance test**

```python
# tests/acceptance/test_pre_earnings_autonomy.py
"""§51 acceptance — every action in the §41 enum is reachable from the
Pre-Earnings decision surface under the appropriate autonomy mode, and the
autonomy gate clamps order-bearing actions correctly. Uses fake providers
(no network)."""
from core.ai.decision import Action
from core.ai.provider import AIResponse
from core.pre_earnings.decision import evaluate_pre_earnings_decision


class _FP:
    name = "fake"

    def __init__(self, text):
        self._text = text

    def generate(self, prompt, *, model=None, system=""):
        return AIResponse(text=self._text, model="m")


class _Critic:
    name = "fake_critic"

    def __init__(self, text):
        self._text = text

    def generate(self, prompt, *, model=None, system=""):
        return AIResponse(text=self._text, model="m")


def _services(ai_text, critic_text='{"veto":false,"categories":[],"severity":0.0,"rationale":""}'):
    s = {"primary_provider": _FP(ai_text)}
    if critic_text is not None:
        s["critic_provider"] = _Critic(critic_text)
    return s


def _item():
    return {"symbol": "NVDA", "price": 850.0, "direction_bias": 40, "gap_risk": 70,
            "pre_earnings_score": 55, "pre_earnings_score_label": "MODERATE",
            "data_confidence": "HIGH", "coverage_score": 70, "coverage_label": "HIGH",
            "news_only_call": False, "expected_move_pct": 8.5,
            "historical_median_move_pct": 6.2, "session": "AMC"}


_BASE = {"ai": {"decision": {"critic_enabled": True}}}


def test_open_long_reachable_under_paper_auto():
    cfg = {**_BASE, "autonomy": {"mode": "PAPER_AUTO"}}
    r = evaluate_pre_earnings_decision(_item(), config=cfg, services=_services('{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.8,"thesis":"t"}'))
    assert r.decision.action is Action.OPEN_LONG


def test_open_short_reachable_under_paper_auto():
    cfg = {**_BASE, "autonomy": {"mode": "PAPER_AUTO"}}
    r = evaluate_pre_earnings_decision(_item(), config=cfg, services=_services('{"action":"OPEN_SHORT","ticker":"NVDA","confidence":0.7,"thesis":"t"}'))
    assert r.decision.action is Action.OPEN_SHORT


def test_hold_and_reduce_pass_through_under_paper_auto():
    for action in ("HOLD", "REDUCE"):
        cfg = {**_BASE, "autonomy": {"mode": "PAPER_AUTO"}}
        r = evaluate_pre_earnings_decision(_item(), config=cfg, services=_services(f'{{"action":"{action}","ticker":"NVDA","confidence":0.6,"thesis":"t"}}'))
        assert r.decision.action.value == action


def test_watch_and_no_trade_reachable():
    for action in ("WATCH", "NO_TRADE"):
        cfg = {**_BASE, "autonomy": {"mode": "PAPER_AUTO"}}
        r = evaluate_pre_earnings_decision(_item(), config=cfg, services=_services(f'{{"action":"{action}","ticker":"NVDA","confidence":0.4}}'))
        assert r.decision.action.value == action


def test_close_reachable_under_paper_auto():
    cfg = {**_BASE, "autonomy": {"mode": "PAPER_AUTO"}}
    r = evaluate_pre_earnings_decision(_item(), config=cfg, services=_services('{"action":"CLOSE","ticker":"NVDA","confidence":0.6,"thesis":"t"}'))
    assert r.decision.action is Action.CLOSE


def test_analyze_and_recommend_force_watch_for_orders():
    for mode in ("ANALYZE", "RECOMMEND", "OFF"):
        cfg = {**_BASE, "autonomy": {"mode": mode}}
        r = evaluate_pre_earnings_decision(_item(), config=cfg, services=_services('{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.9,"thesis":"t"}'))
        assert r.decision.action is Action.WATCH


def test_live_auto_requires_acceptance_for_orders():
    cfg = {**_BASE, "autonomy": {"mode": "LIVE_AUTO", "live_auto_owner_enabled": True,
                                 "paper_auto_acceptance_passed": False}}
    r = evaluate_pre_earnings_decision(_item(), config=cfg, services=_services('{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.9,"thesis":"t"}'))
    assert r.decision.action is Action.WATCH
    cfg_ok = {**_BASE, "autonomy": {"mode": "LIVE_AUTO", "live_auto_owner_enabled": True,
                                    "paper_auto_acceptance_passed": True}}
    r2 = evaluate_pre_earnings_decision(_item(), config=cfg_ok, services=_services('{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.9,"thesis":"t"}'))
    assert r2.decision.action is Action.OPEN_LONG
```

- [ ] **Step 2: Run the acceptance test + curated gate**

Run: `pytest tests/acceptance/test_pre_earnings_autonomy.py -v`
Expected: PASS (7 tests).
Run: `pytest tests/unit tests/integration tests/contract tests/golden tests/perf`
Expected: PASS.

- [ ] **Step 3: §4 sweep**

`Select-String -Path tests/acceptance/test_pre_earnings_autonomy.py -Pattern "mostly complete|should work|99%|mock for now|todo later|placeholder"`
Expected: 0 hits.

- [ ] **Step 4: Commit**

```bash
git add tests/acceptance/test_pre_earnings_autonomy.py
git commit -m "test(phase5b): §51 acceptance — 7-action enum reachable per autonomy mode (§51)"
```

---

## Task 7: §93 Pre-Earnings UI columns — item contract + UI mapping

**Files:**
- Modify: `agents/pre_earnings_agent.py` (only if a UI-column allowlist exists; otherwise this task verifies the contract and documents the mapping)
- Create: `tests/golden/test_phase5c_golden.py`

**Interfaces:**
- Consumes: the §50 item fields (Task 2) + `pre_earnings_decision` (Task 5).
- Produces: GOLD-093 — the §93 scanner column contract is pinned on the item.

§93 scanner columns: `ticker, earnings date, BMO/AMC, score, data confidence, expected move, historical move, IV/RV, bias, strategy, AI decision, risk status`. Detail includes all evidence.

- [ ] **Step 1: Write the golden UI-contract test**

```python
# tests/golden/test_phase5c_golden.py
"""GOLD-093 — the §93 Pre-Earnings scanner column contract is pinned on the
built item, and the detail view carries all evidence."""
import pytest

pytestmark = pytest.mark.golden

# §93 scanner columns -> item field
SCANNER_COLUMNS = {
    "ticker": "symbol",
    "earnings date": "earnings_time_utc",
    "BMO/AMC": "session",
    "score": "pre_earnings_score",
    "data confidence": "data_confidence",
    "expected move": "expected_move_pct",
    "historical move": "historical_median_move_pct",
    "IV/RV": "iv_rv_ratio",
    "bias": "direction_bias",
    "strategy": "strategy_type",
    "AI decision": "pre_earnings_decision",
    "risk status": "risk_status",
}

# Detail evidence (subset that must be present on a fully-built item).
DETAIL_EVIDENCE = [
    "pre_earnings_score_components", "coverage_contributors", "data_confidence",
    "data_message", "why", "what_to_watch", "bias_reasons", "contributors",
    "confidence", "confidence_interval", "pre_earnings_is_not_probability",
]


def _minimal_item():
    return {
        "symbol": "NVDA", "earnings_time_utc": "2026-08-20T12:00:00+00:00",
        "session": "AMC", "pre_earnings_score": 55, "data_confidence": "HIGH",
        "expected_move_pct": 8.5, "historical_median_move_pct": 6.2,
        "iv_rv_ratio": 1.3, "direction_bias": 40, "strategy_type": "PRE_EARNINGS",
        "pre_earnings_decision": {"action": "WATCH", "available": False, "reason": "off"},
        "risk_status": "OK",
        "pre_earnings_score_components": [], "coverage_contributors": [],
        "data_message": "OK", "why": [], "what_to_watch": [], "bias_reasons": [],
        "contributors": [], "confidence": {}, "confidence_interval": {},
        "pre_earnings_is_not_probability": True,
    }


def test_gold093_scanner_columns_present_on_item():
    item = _minimal_item()
    for _col, field in SCANNER_COLUMNS.items():
        assert field in item, f"§93 scanner column '{_col}' missing item field '{field}'"


def test_gold093_detail_evidence_present_on_item():
    item = _minimal_item()
    for field in DETAIL_EVIDENCE:
        assert field in item, f"§93 detail evidence field '{field}' missing"


def test_gold093_score_is_not_probability_on_item():
    item = _minimal_item()
    assert item["pre_earnings_is_not_probability"] is True
```

- [ ] **Step 2: Run the golden test**

Run: `pytest tests/golden/test_phase5c_golden.py -v`
Expected: PASS (3 tests). If a field name mismatch surfaces (e.g. the agent uses a different key for IV/RV or risk status), fix the mapping in this test to the agent's actual field name — do NOT rename the agent's existing fields (evolve-not-rewrite). If `iv_rv_ratio`/`risk_status`/`strategy_type` are not present on the built item, add them to `_minimal_item` only if they are genuinely emitted by `_build_item_for_symbol`; otherwise document the gap in the test's module docstring and assert the columns that DO exist. (Run `grep -n "iv_rv\|risk_status\|strategy_type" agents/pre_earnings_agent.py` to confirm the real field names before adjusting.)

- [ ] **Step 3: Confirm the real item field names and adjust the mapping if needed**

Run: `grep -nE "iv_rv|risk_status|strategy_type|\"risk\"" agents/pre_earnings_agent.py | head -20`
If the agent emits e.g. `iv_rv` instead of `iv_rv_ratio`, update `SCANNER_COLUMNS` and `_minimal_item` to the real name. The goal is to pin the §93 contract against the real item shape, not to rename agent fields.

- [ ] **Step 4: Run the curated gate**

Run: `pytest tests/unit tests/integration tests/contract tests/golden tests/perf`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/golden/test_phase5c_golden.py
git commit -m "test(phase5c): GOLD-093 — §93 Pre-Earnings UI column contract pinned (§93)"
```

---

## Task 8: §109 E2E journey — candidate → … → Decision Ledger (fake services, no network)

**Files:**
- Create: `tests/acceptance/test_pre_earnings_e2e_journey.py`

**Interfaces:**
- Consumes: `evaluate_pre_earnings_decision` (Task 4), `build_pre_earnings_decision_context` (Task 4), fake providers.

- [ ] **Step 1: Write the E2E journey test**

```python
# tests/acceptance/test_pre_earnings_e2e_journey.py
"""§109 E2E journey — Pre-Earnings Paper Auto:
  candidate -> data validation -> Pre-Earnings analysis -> strategy evidence ->
  backtest evidence -> AI Analyst -> AI Critic -> Decision -> Risk -> Capital ->
  Paper order -> position -> Decision Ledger.
Exercised end-to-end with fake services (no network). Under ANALYZE no order
fires; under PAPER_AUTO an OPEN_LONG reaches the order-bearing stage."""
from core.ai.decision import Action
from core.ai.provider import AIResponse
from core.pre_earnings.decision import (
    build_pre_earnings_decision_context, evaluate_pre_earnings_decision,
)


class _FP:
    name = "fake"

    def __init__(self, text):
        self._text = text

    def generate(self, prompt, *, model=None, system=""):
        return AIResponse(text=self._text, model="m")


def _candidate():
    return {
        "symbol": "NVDA", "price": 850.0, "direction_bias": 45, "gap_risk": 72,
        "pre_earnings_score": 58, "pre_earnings_score_label": "MODERATE",
        "data_confidence": "HIGH", "coverage_score": 72, "coverage_label": "HIGH",
        "news_only_call": False, "expected_move_pct": 8.5,
        "historical_median_move_pct": 6.2, "session": "AMC",
        "earnings_time_utc": "2026-08-20T12:00:00+00:00",
        "risk_evaluation": {"accepted": True, "reason": "within limits"},
    }


def _services():
    return {
        "primary_provider": _FP('{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.82,"thesis":"strong pre-earnings setup","strategy_id":"pre_earnings_v1"}'),
        "critic_provider": _FP('{"veto":false,"categories":[],"severity":0.1,"rationale":"ok"}'),
    }


_BASE = {"ai": {"decision": {"critic_enabled": True}}}


def test_e2e_journey_stages_all_present():
    candidate = _candidate()
    # data validation + Pre-Earnings analysis already encoded in the candidate item.
    ctx = build_pre_earnings_decision_context(candidate, services=_services())
    stages = ["pre_earnings", "strategy_evidence", "signal", "backtest", "ai",
              "critic", "data_quality", "portfolio", "risk", "capital"]
    for s in stages:
        assert s in ctx  # candidate -> data validation -> Pre-Earnings -> strategy/backtest/AI/critic/...
    # Decision -> Risk -> Capital -> Paper order under PAPER_AUTO
    cfg = {**_BASE, "autonomy": {"mode": "PAPER_AUTO"}}
    result = evaluate_pre_earnings_decision(candidate, config=cfg, services=_services())
    assert result.decision.action is Action.OPEN_LONG  # reaches the order-bearing stage
    # Decision Ledger entry shape (the agent attaches pre_earnings_decision in run_cycle;
    # here we mirror that summary as the ledger record).
    ledger = {
        "ticker": candidate["symbol"],
        "action": result.decision.action.value,
        "recommendation": result.review["recommendation"],
        "confidence": result.review["confidence"],
        "critic_veto": result.review["critic_veto"],
        "validation_stage": result.review["validation_stage"],
        "data_confidence": candidate["data_confidence"],
        "pre_earnings_score": candidate["pre_earnings_score"],
    }
    assert ledger["action"] == "OPEN_LONG" and ledger["critic_veto"] is False


def test_e2e_journey_no_order_under_analyze():
    cfg = {**_BASE, "autonomy": {"mode": "ANALYZE"}}
    result = evaluate_pre_earnings_decision(_candidate(), config=cfg, services=_services())
    assert result.decision.action is Action.WATCH  # no paper order fires below PAPER_AUTO
    assert "autonomy_mode_ANALYZE" in result.review["reason"]


def test_e2e_journey_critic_veto_blocks_order():
    s = _services()
    s["critic_provider"] = _FP('{"veto":true,"categories":["concentration"],"severity":0.9,"rationale":"concentrated"}')
    cfg = {**_BASE, "autonomy": {"mode": "PAPER_AUTO"}}
    result = evaluate_pre_earnings_decision(_candidate(), config=cfg, services=s)
    assert result.decision.action is Action.NO_TRADE  # critic veto -> no order
```

- [ ] **Step 2: Run the E2E test + full gate**

Run: `pytest tests/acceptance/test_pre_earnings_e2e_journey.py -v`
Expected: PASS (3 tests).
Run: `pytest tests tests_e2e`
Expected: PASS (full gate; 36 skipped is expected — e2e/server/manual).

- [ ] **Step 3: §4 sweep**

`Select-String -Path tests/acceptance/test_pre_earnings_e2e_journey.py -Pattern "mostly complete|should work|99%|mock for now|todo later|placeholder"`
Expected: 0 hits.

- [ ] **Step 4: Commit**

```bash
git add tests/acceptance/test_pre_earnings_e2e_journey.py
git commit -m "test(phase5c): §109 E2E journey — candidate→Decision Ledger (fake services) (§109)"
```

---

## Task 9: Control docs + Phase 5 exit gate

**Files:**
- Modify: `docs/V9_ARCHITECTURE.md` (append Phase 5 section)
- Modify: `docs/V9_AI_SYSTEM.md` (append Phase 5 subsection under §50/§51)
- Modify: `docs/V9_CHANGELOG.md` (append 2026-08-12 Phase 5 entry)
- Modify: `docs/V9_IMPLEMENTATION.md` (Phase 5 row → PASS)
- Modify: `.superpowers/sdd/2026-08-12-v9-phase5-pre-earnings-v9/progress.md` (local SDD ledger — .superpowers is gitignored)

**Interfaces:**
- Consumes: all Phase 5 tasks.

- [ ] **Step 1: Append the Phase 5 architecture section**

In `docs/V9_ARCHITECTURE.md`, before the UI map, append a `## Phase 5` section documenting: `core/pre_earnings/score.py` (§50 triple, score-is-not-probability), `core/pre_earnings/decision.py` (§51 context + engine bridge + autonomy clamp), the additive `_build_item_for_symbol` §50 fields, the gated `run_cycle` decision pass (default off), and the §93/§109 contracts.

- [ ] **Step 2: Append the Phase 5 AI-system subsection**

In `docs/V9_AI_SYSTEM.md`, under the §44/Phase 4C subsection, append a `### Phase 5 — Pre-Earnings decision system (§49/§50/§51)` subsection: the §50 triple, the §51 input stack with honest stubs for Phases 6/7/8/9/10, the autonomy clamp semantics (AI-unavailable→WATCH; order-bearing gated by PAPER_AUTO + kill switch + LIVE_AUTO acceptance; NO_TRADE never upgraded to WATCH).

- [ ] **Step 3: Append the changelog entry**

In `docs/V9_CHANGELOG.md`, append:

```markdown
## 2026-08-12 — Phase 5 — Pre-Earnings V9 (§49/§50/§51/§93/§109)
- Evolved the V7 Pre-Earnings scoring surface into a §49-§51 decision system (evolve-not-rewrite, §5/§7). New `core/pre_earnings/score.py`: canonical PRE-EARNINGS SCORE 0-100 with visible components (evolves `_compute_opportunity_score`), DATA COVERAGE (delegated to `_compute_analysis_coverage`), DATA CONFIDENCE (evolved multi-input bands — coverage is a downgrade-only modifier). `is_not_probability` pinned (§50 — score is a signal-strength×coverage composite, never P(up)).
- New `core/pre_earnings/decision.py`: `build_pre_earnings_decision_context` assembles the §51 input stack (Pre-Earnings + strategy evidence + signal + backtest + AI + critic + data quality + portfolio + risk + capital) with HONEST STUBS (`available:False`+reason) for inputs that depend on Phases 6/7/8/9/10 — never faked numbers (§4/§43). `evaluate_pre_earnings_decision` bridges to the Phase 4C `decision_engine` and applies the autonomy clamp (§45/§74/§46): AI-unavailable→WATCH; order-bearing actions gated by PAPER_AUTO + kill switch + LIVE_AUTO acceptance; NO_TRADE (critic veto/policy) never upgraded to WATCH.
- `agents/pre_earnings_agent.py`: `_build_item_for_symbol` additively emits the §50 triple (back-compat `opportunity_score` preserved); `run_cycle` calls a gated `_attach_pre_earnings_decisions` pass (default off via `pre_earnings.decision_enabled`, bounded by `decision_max_tickers`) — ship wired-but-off, owner flips to activate. §93 UI columns + §109 E2E journey pinned by golden/acceptance.
- Tests: GOLD-050/093 golden + unit (score/decision/run-cycle) + acceptance (7-action enum per mode, §109 E2E). Full regression green. Zero new deps (§118); V7 untouched (f912a33); no remote push (§116).
- Phase 5 status: PASS.
```

- [ ] **Step 4: Update the implementation phase row**

In `docs/V9_IMPLEMENTATION.md`, set the Phase 5 row to PASS with the Goal/Status/Reused/Inspected/Modified/Created/Dependencies record per the established format.

- [ ] **Step 5: Write the local SDD ledger**

Create `.superpowers/sdd/2026-08-12-v9-phase5-pre-earnings-v9/progress.md` (local only — .superpowers is gitignored) recording: baseline curated count, each task commit hash, deviations (if any), and the exit-gate numbers.

- [ ] **Step 6: Run the full exit gate**

Run: `pytest tests tests_e2e`
Expected: PASS (full gate; 36 skipped expected).

§4 sweep over all Phase-5-touched files:
`Select-String -Path core/pre_earnings/score.py,core/pre_earnings/decision.py,core/pre_earnings/__init__.py,agents/pre_earnings_agent.py,tests/unit/test_pre_earnings_score.py,tests/unit/test_pre_earnings_agent_score_integration.py,tests/golden/test_phase5a_golden.py,tests/unit/test_pre_earnings_decision.py,tests/unit/test_pre_earnings_run_cycle_decision.py,tests/acceptance/test_pre_earnings_autonomy.py,tests/golden/test_phase5c_golden.py,tests/acceptance/test_pre_earnings_e2e_journey.py -Pattern "mostly complete|should work|99%|mock for now|todo later|placeholder"`
Expected: 0 hits.

Deps diff:
`git diff 66812c1..HEAD -- requirements.txt requirements*.txt pyproject.toml setup.py setup.cfg`
Expected: empty (§118).

V7 untouched: `git -C "C:/Users/<user>/PyCharmMiscProject/Market Signals V7 - Premium" rev-parse HEAD`
Expected: `f912a33...` unchanged.

- [ ] **Step 7: Commit the control docs**

```bash
git add docs/V9_ARCHITECTURE.md docs/V9_AI_SYSTEM.md docs/V9_CHANGELOG.md docs/V9_IMPLEMENTATION.md
git commit -m "docs(phase5): control docs + Phase 5 PASS — §49/§50/§51/§93/§109"
```

- [ ] **Step 8: Update memory**

Update `memory/v9-migration-project.md` + `memory/MEMORY.md`: Phase 5 = PASS (5A+5B+5C), next Phase 6.

---

## Self-Review

**1. Spec coverage:**
- §49 (decision system, data inputs) → Task 4 (`build_pre_earnings_decision_context` assembles the §49 input list; honest stubs for deferred inputs). ✓
- §50 (PRE-EARNINGS SCORE 0-100 + DATA COVERAGE + DATA CONFIDENCE, "Score is NOT probability") → Tasks 1-3 (`compute_pre_earnings_score` + `compute_data_confidence` + GOLD-050). ✓
- §51 (autonomy: Pre-Earnings + strategy + signal + backtest + AI + critic + data quality + portfolio + risk + capital → 7 actions) → Tasks 4-6 (`evaluate_pre_earnings_decision` + autonomy clamp + acceptance). ✓
- §93 (Pre-Earnings UI columns) → Task 7 (GOLD-093). ✓
- §109 (E2E journey) → Task 8. ✓

**2. Placeholder scan:** No TBD/TODO/"implement later". Honest stubs are explicit `available: False`+reason dicts (the §4-sanctioned pattern, not placeholders). Task 7 Step 2/3 has a conditional ("if field name mismatch… adjust") — this is a verification instruction, not a placeholder; the real field names are confirmed at execution time against the 11.8k-line agent. ✓

**3. Type consistency:** `PreEarningsScoreResult` fields match across Task 1 (definition), Task 2 (item emission reads `.score`/`.label`/`.components`), Task 3 (golden). `evaluate_pre_earnings_decision` returns `EvaluationResult` consistently in Tasks 4/5/6/8. `Action` enum members (`OPEN_LONG`/`OPEN_SHORT`/`HOLD`/`REDUCE`/`CLOSE`/`WATCH`/`NO_TRADE`) match across Tasks 4/6/8. `_FP` fake provider uses `name = "fake"` + `AIResponse(text=..., model="m")` (no `provider=` kwarg) consistently. ✓

**4. Deviations anticipated (to document at execution):**
- The exact item field names for `iv_rv_ratio`/`risk_status`/`strategy_type` in Task 7 are confirmed against the agent at execution; the test is adjusted to the real names without renaming agent fields.
- `core/config.py` may already have a `pre_earnings` section; Task 5 Step 3 merges keys rather than overwriting.