# Phase 4B — Decision Schema & Validation Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the §41 structured decision schema (`Action` enum + `Decision` dataclass + `parse_decision`) and the §42 validation pipeline (`PARSE→SCHEMA→SEMANTIC→AUTHORITATIVE DATA CHECK→POLICY→RISK GATE`) that composes the existing decision/risk/capital functions as stages, strips §43 non-authoritative AI numbers, and guarantees AI prose alone never produces an order.

**Architecture:** Two new pure-additive modules under `core/ai/`: `decision.py` (the §41 `Action` enum + frozen `Decision` + `parse_decision` evolving `ollama_decision.parse_trade_review`'s brace-match/coerce pattern) and `pipeline.py` (`ValidationResult` + six injectable stage functions + `run_pipeline`). The pipeline COMPOSES existing functions — `evaluate_capital_entry`, `evaluate_autonomous_performance`, `evaluate_autonomous_news`, `RiskAgent.evaluate_signal`, `risk_limits.calculate_trade_risk_pct`, `compute_size_factor`, `resolve_deployable_balance`, `should_veto_trade` — without modifying any of them. App-calculated §43 numbers (allowed capital, position size, portfolio risk) live in a side channel (`ValidationResult.checks["authoritative"]`), never in the `Decision` (whose §41 field list excludes them). No existing file's behavior changes; the `ollama_decision`→`generate_with_fallback` bridge and the `agent_manager` rewire are 4C.

**Tech Stack:** Python 3.14.3, stdlib only (`dataclasses`, `enum`, `json`, `typing`, `re`), pytest. Zero new deps (§118).

## Global Constraints

- **Spec:** `prompt_to_follow.md` is the sole prompt to follow. §41 (AI output schema + 7 actions), §42 (6-stage pipeline; AI prose alone must never trigger an order), §43 (AI does not supply authoritative price/P&L/IV/EPS/portfolio value/allowed capital/position size — app services calculate them), §4 (no forbidden success language: no "mostly complete"/"should work"/"99%"/"mock for now"/"TODO later"), §5/§7 (evolve + preserve, no rewrite), §106 (no secrets), §115 (no test weakening), §117 (V7 read-only), §118/§25 (no new deps, pin deps).
- **V7** at `C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium` is READ-ONLY (HEAD `f912a33`) — never modify.
- **No remote push** (§116). Commit to `master` only.
- **Existing pinned tests must stay green:** `tests/test_ollama_decision.py` (19), `tests/test_risk_agent.py` (10), `tests/unit/test_capital_entry_guard.py` (10), `tests/unit/test_autonomous_trade_guard.py` (13), `tests/unit/test_autonomous_candidates.py` (16), `tests/unit/test_autonomous_control.py` (1), `tests/unit/test_risk_settings.py` (4). These files are NOT modified.
- **No existing file's behavior changes in 4B.** Only `core/config.py` (additive `DEFAULT_CONFIG["ai"]["decision"]` key) and `config.example.json` (mirror) and `core/ai/__init__.py` (additive re-exports) are touched — all purely additive.
- **§4 sweep:** every task ends with `grep -riE "mostly complete|should work|99%|mock for now|todo later|placeholder"` over the new/changed files returning 0 hits.
- **Baseline gate:** curated selection `tests/unit tests/integration tests/contract tests/golden tests/perf` must stay 1811 green (the 4A gate); full `pytest tests tests_e2e` must stay green (4A: 2323 passed/0 failed/36 skipped). Each task commit keeps regression green.

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `core/ai/decision.py` | `Action` enum (7 §41 actions), `Decision` frozen dataclass (§41 fields), `AUTHORITATIVE_FIELDS` set (§43), `parse_decision(raw)->Decision` (brace-match + coerce + strip/reject), small parse helpers | **Create** |
| `core/ai/pipeline.py` | `ValidationResult` + `StageResult` frozen dataclasses, 6 stage-name constants + `STAGE_ORDER`, `default_services(config)` lazy real-function lookup, six stage functions, `run_pipeline(raw_decision, signal, positions, *, config, services=None)->ValidationResult` | **Create** |
| `core/config.py` | Add `DEFAULT_CONFIG["ai"]["decision"]` block (additive; backfilled by `deep_merge`) | **Modify** (additive) |
| `config.example.json` | Mirror `ai.decision` block | **Modify** (additive) |
| `core/ai/__init__.py` | Re-export `Action`, `Decision`, `parse_decision`, `AUTHORITATIVE_FIELDS`, `ValidationResult`, `StageResult`, `run_pipeline`, stage constants; extend `__all__` | **Modify** (additive) |
| `tests/unit/test_ai_decision.py` | Unit tests for `decision.py` | **Create** |
| `tests/unit/test_ai_pipeline.py` | Unit tests for `pipeline.py` (injectable fake services/stages) | **Create** |
| `tests/golden/test_ai_decision_pipeline_golden.py` | Golden §42 ordering + §43 strip + no-order-on-AI-prose | **Create** |
| `docs/superpowers/plans/2026-08-12-v9-phase4b-decision-schema-and-validation-pipeline.md` | This plan | **Create** (committed at plan step) |
| `.superpowers/sdd/2026-08-12-v9-phase4b-decision-schema-and-validation-pipeline/progress.md` | SDD ledger | **Create** at execution |

---

## Interfaces (cross-task contract)

**Task 1 produces (consumed by Tasks 2–5):**
- `Action(str, Enum)` with members `WATCH, NO_TRADE, OPEN_LONG, OPEN_SHORT, HOLD, REDUCE, CLOSE`.
- `ALLOWED_ACTIONS: frozenset[str]` = `{a.value for a in Action}`.
- `AUTHORITATIVE_FIELDS: frozenset[str]` = `{"price","pnl","iv","eps","portfolio_value","allowed_capital","position_size"}` (§43).
- `Decision` frozen dataclass: fields `action: Action`, `ticker: str=""`, `horizon: str=""`, `confidence: float=0.0`, `strategy_id: str=""`, `thesis: str=""`, `supporting_evidence: list=field(default_factory=list)`, `opposing_evidence: list=field(default_factory=list)`, `risk_flags: list=field(default_factory=list)`, `data_quality: float=0.0`, `requires_more_data: bool=False`, `raw: dict=field(default_factory=dict)`, `rejected_fields: list=field(default_factory=list)`.

**Task 2 produces (consumed by Task 3):**
- `parse_decision(raw: Any, *, strict_schema: bool=False) -> Decision` — brace-match + `json.loads` + per-field coerce (evolving `parse_trade_review`); drops `AUTHORITATIVE_FIELDS` keys into `rejected_fields`; in `strict_schema=True` raises `DecisionSchemaError` if any authoritative field is present.

**Task 3 produces (consumed by Tasks 4–5):**
- `ValidationResult(ok: bool, decision: Optional[Decision], stage: str, reason: str="", checks: dict=field(default_factory=dict))`.
- `StageResult(passed: bool, decision: Optional[Decision], reason: str="", checks: dict=field(default_factory=dict))`.
- Stage-name constants `PARSE, SCHEMA, SEMANTIC, AUTHORITATIVE_DATA_CHECK, POLICY, RISK_GATE` and `STAGE_ORDER` tuple.
- `default_services(config) -> dict` — lazy real-function lookup (returns callables).
- `run_pipeline(raw_decision, signal, positions, *, config, services=None) -> ValidationResult`.

**Tasks 4–5 produce:** the real `AUTHORITATIVE_DATA_CHECK`, `POLICY`, `RISK_GATE` stage callables, wired as defaults inside `default_services`.

---

## Task 1: `core/ai/decision.py` — Action enum + Decision dataclass + constants

**Files:**
- Create: `core/ai/decision.py`
- Test: `tests/unit/test_ai_decision.py`

**Interfaces:**
- Consumes: nothing (stdlib only).
- Produces: `Action`, `ALLOWED_ACTIONS`, `AUTHORITATIVE_FIELDS`, `Decision` (full field list above).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_ai_decision.py
"""Unit tests for core.ai.decision — §41 Action enum + Decision schema (Phase 4B)."""
import pytest

from core.ai.decision import (
    ALLOWED_ACTIONS,
    AUTHORITATIVE_FIELDS,
    Action,
    Decision,
)


def test_action_enum_has_exactly_the_seven_spec_actions():
    # §41 allowed list, verbatim
    assert {a.value for a in Action} == {
        "WATCH", "NO_TRADE", "OPEN_LONG", "OPEN_SHORT",
        "HOLD", "REDUCE", "CLOSE",
    }


def test_action_values_match_allow_set():
    assert ALLOWED_ACTIONS == {a.value for a in Action}


def test_authoritative_fields_cover_spec_43_list():
    # §43: price, P/L, IV, EPS, portfolio value, allowed capital, position size
    assert AUTHORITATIVE_FIELDS == frozenset({
        "price", "pnl", "iv", "eps",
        "portfolio_value", "allowed_capital", "position_size",
    })


def test_decision_defaults_and_frozen():
    d = Decision(action=Action.WATCH)
    assert d.action == Action.WATCH
    assert d.ticker == ""
    assert d.confidence == 0.0
    assert d.supporting_evidence == []
    assert d.requires_more_data is False
    assert d.rejected_fields == []
    # frozen
    with pytest.raises(Exception):
        d.ticker = "NVDA"
    # default factories are per-instance (not shared)
    a = Decision(action=Action.WATCH)
    b = Decision(action=Action.WATCH)
    a.supporting_evidence.append("x")
    assert b.supporting_evidence == []


def test_decision_field_names_match_spec_41_schema():
    # §41 example schema fields, verbatim
    expected = {
        "action", "ticker", "horizon", "confidence", "strategy_id",
        "thesis", "supporting_evidence", "opposing_evidence", "risk_flags",
        "data_quality", "requires_more_data",
    }
    assert expected.issubset({f.name for f in __import__("dataclasses").fields(Decision)})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_ai_decision.py -v`
Expected: FAIL with `ImportError: cannot import name 'ALLOWED_ACTIONS' ...` (module does not exist).

- [ ] **Step 3: Write minimal implementation**

```python
# core/ai/decision.py
"""Phase 4B — §41 structured decision schema.

The §41 action vocabulary (WATCH/NO_TRADE/OPEN_LONG/OPEN_SHORT/HOLD/REDUCE/CLOSE)
and the frozen Decision dataclass. parse_decision (Task 2) evolves the legacy
ollama_decision.parse_trade_review brace-match + coerce pattern into the new
§41 schema and enforces §43 (AI numbers are not authoritative).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List


class Action(str, Enum):
    """§41 allowed actions (verbatim from prompt_to_follow.md)."""

    WATCH = "WATCH"
    NO_TRADE = "NO_TRADE"
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"


ALLOWED_ACTIONS: frozenset[str] = frozenset(a.value for a in Action)

# §43 — AI does not supply authoritative values for these; app services calculate them.
AUTHORITATIVE_FIELDS: frozenset[str] = frozenset({
    "price", "pnl", "iv", "eps",
    "portfolio_value", "allowed_capital", "position_size",
})


@dataclass(frozen=True)
class Decision:
    """§41 validated decision schema. Authoritative numbers are intentionally absent
    (§43) — they live in the pipeline's ValidationResult.checks['authoritative'] side
    channel, computed by app services."""

    action: Action
    ticker: str = ""
    horizon: str = ""
    confidence: float = 0.0
    strategy_id: str = ""
    thesis: str = ""
    supporting_evidence: List[str] = field(default_factory=list)
    opposing_evidence: List[str] = field(default_factory=list)
    risk_flags: List[str] = field(default_factory=list)
    data_quality: float = 0.0
    requires_more_data: bool = False
    # metadata (not §41 schema fields — audit/parse provenance only)
    raw: dict = field(default_factory=dict)
    rejected_fields: List[str] = field(default_factory=list)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_ai_decision.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: §4 sweep + commit**

```bash
grep -riE "mostly complete|should work|99%|mock for now|todo later|placeholder" core/ai/decision.py tests/unit/test_ai_decision.py
# expected: 0 hits
git add core/ai/decision.py tests/unit/test_ai_decision.py
git commit -m "feat(phase4b): Action enum + Decision schema + §43 authoritative-field set (§41/§43)"
```

---

## Task 2: `parse_decision(raw)` — PARSE+SCHEMA evolving `parse_trade_review`

**Files:**
- Modify: `core/ai/decision.py` (add `parse_decision` + helpers + `DecisionSchemaError`)
- Test: `tests/unit/test_ai_decision.py` (append)

**Interfaces:**
- Consumes: `Action`, `ALLOWED_ACTIONS`, `AUTHORITATIVE_FIELDS`, `Decision` (Task 1).
- Produces: `parse_decision(raw, *, strict_schema=False) -> Decision`, `DecisionSchemaError(Exception)`.

- [ ] **Step 1: Write the failing tests (append to `tests/unit/test_ai_decision.py`)**

```python
from core.ai.decision import parse_decision, DecisionSchemaError


def test_parse_decision_valid_json_coerces_fields():
    raw = """
    {
      "action": "OPEN_LONG",
      "ticker": "NVDA",
      "horizon": "SWING",
      "confidence": 1.7,
      "strategy_id": "momentum_v3",
      "thesis": "Momentum continuation.",
      "supporting_evidence": ["rsi>70", "volume up"],
      "opposing_evidence": ["overbought"],
      "risk_flags": ["thin liquidity"],
      "data_quality": 1.5,
      "requires_more_data": false
    }
    """
    d = parse_decision(raw)
    assert d.action == Action.OPEN_LONG
    assert d.ticker == "NVDA"
    assert d.horizon == "SWING"
    assert d.confidence == 1.0          # clamped to [0,1]
    assert d.data_quality == 1.0         # clamped to [0,1]
    assert d.requires_more_data is False
    assert d.supporting_evidence == ["rsi>70", "volume up"]


def test_parse_decision_accepts_dict_passthrough():
    d = parse_decision({"action": "WATCH", "ticker": "AAPL"})
    assert d.action == Action.WATCH
    assert d.ticker == "AAPL"


def test_parse_decision_invalid_action_falls_back_to_no_trade():
    d = parse_decision({"action": "BUY", "ticker": "AAPL"})
    assert d.action == Action.NO_TRADE
    assert d.ticker == "AAPL"


def test_parse_decision_invalid_json_falls_back_to_no_trade():
    d = parse_decision("not json at all")
    assert d.action == Action.NO_TRADE
    assert d.raw.get("parse_error") == "invalid_json"


def test_parse_decision_strips_authoritative_fields_and_records_them():
    raw = {
        "action": "OPEN_LONG",
        "ticker": "NVDA",
        "confidence": 0.8,
        "price": 850.0,            # §43 — not authoritative from AI
        "position_size": 12000,    # §43
        "allowed_capital": 50000,  # §43
        "portfolio_value": 100000,# §43
    }
    d = parse_decision(raw)
    # §41 schema has no such fields
    assert not hasattr(d, "price")
    assert "price" in d.rejected_fields
    assert "position_size" in d.rejected_fields
    assert "allowed_capital" in d.rejected_fields
    assert "portfolio_value" in d.rejected_fields
    # kept fields survive
    assert d.ticker == "NVDA"
    assert d.confidence == 0.8


def test_parse_decision_strict_schema_rejects_authoritative_fields():
    raw = {"action": "OPEN_LONG", "ticker": "NVDA", "price": 850.0}
    with pytest.raises(DecisionSchemaError) as exc:
        parse_decision(raw, strict_schema=True)
    assert "price" in str(exc.value)


def test_parse_decision_brace_matches_embedded_json():
    # prose around the JSON, like the legacy ollama response shape
    raw = 'Here is my decision: {"action":"HOLD","ticker":"TSLA","confidence":0.5} ok?'
    d = parse_decision(raw)
    assert d.action == Action.HOLD
    assert d.ticker == "TSLA"
    assert d.confidence == 0.5


def test_parse_decision_truncates_long_thesis_and_lists():
    raw = {"action": "NO_TRADE", "thesis": "x" * 2000,
           "risk_flags": ["a", "b", "c", "d", "e", "f", "g", "h"]}
    d = parse_decision(raw)
    assert len(d.thesis) <= 1000
    assert len(d.risk_flags) <= 8
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `python -m pytest tests/unit/test_ai_decision.py -v`
Expected: the 8 new tests FAIL with `ImportError: cannot import name 'parse_decision'`.

- [ ] **Step 3: Write the implementation (append to `core/ai/decision.py`)**

```python
import json
from typing import Optional


class DecisionSchemaError(ValueError):
    """Raised when strict_schema=True rejects AI-supplied authoritative fields (§43)."""


def _extract_json(raw_text: str) -> Optional[str]:
    """Brace-matching JSON extractor — evolves ollama_decision._extract_json_object.
    Returns the substring from the first '{' through its matching closing '}' at
    depth 0, or None if there is no balanced object."""
    text = raw_text or ""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for idx, ch in enumerate(text[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]
    return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        f = float(value)
        if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
            return default
        return f
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _list_str(value: Any, *, limit: int = 8, item_limit: int = 300) -> list:
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value[:limit]:
        s = str(item).strip()[:item_limit]
        if s:
            out.append(s)
    return out


def parse_decision(raw: Any, *, strict_schema: bool = False) -> Decision:
    """Parse an AI raw decision (dict or JSON-in-prose string) into a §41 Decision.

    Evolves ollama_decision.parse_trade_review: dict passthrough or brace-match +
    json.loads, then per-field coerce/clamp. §43 authoritative fields
    (price/pnl/iv/eps/portfolio_value/allowed_capital/position_size) are dropped
    from the schema and recorded in rejected_fields; in strict_schema mode their
    presence raises DecisionSchemaError instead."""
    if isinstance(raw, dict):
        parsed = raw
        raw_meta = {}
    else:
        snippet = _extract_json(str(raw or ""))
        if not snippet:
            return Decision(action=Action.NO_TRADE, raw={"parse_error": "invalid_json"})
        try:
            parsed = json.loads(snippet)
        except Exception:
            return Decision(action=Action.NO_TRADE, raw={"parse_error": "invalid_json"})
        if not isinstance(parsed, dict):
            return Decision(action=Action.NO_TRADE, raw={"parse_error": "invalid_json"})
        raw_meta = {"parse_error": ""}

    if not isinstance(parsed, dict):
        return Decision(action=Action.NO_TRADE, raw={"parse_error": "invalid_json"})

    # §43 — strip / reject authoritative AI-supplied numbers
    rejected = []
    for field_name in AUTHORITATIVE_FIELDS:
        if field_name in parsed:
            rejected.append(field_name)
    if rejected and strict_schema:
        raise DecisionSchemaError(
            "AI supplied authoritative field(s) forbidden by §43: " + ", ".join(sorted(rejected))
        )
    # never carry authoritative fields into the Decision, regardless of strict mode
    for field_name in AUTHORITATIVE_FIELDS:
        parsed.pop(field_name, None)

    action_raw = str(parsed.get("action") or "").upper().strip()
    action = Action(action_raw) if action_raw in ALLOWED_ACTIONS else Action.NO_TRADE

    return Decision(
        action=action,
        ticker=str(parsed.get("ticker") or "").strip()[:32],
        horizon=str(parsed.get("horizon") or "").strip()[:32],
        confidence=round(_clamp(_safe_float(parsed.get("confidence"), 0.0), 0.0, 1.0), 4),
        strategy_id=str(parsed.get("strategy_id") or "").strip()[:64],
        thesis=str(parsed.get("thesis") or "").strip()[:1000],
        supporting_evidence=_list_str(parsed.get("supporting_evidence"), limit=8),
        opposing_evidence=_list_str(parsed.get("opposing_evidence"), limit=8),
        risk_flags=_list_str(parsed.get("risk_flags"), limit=8),
        data_quality=round(_clamp(_safe_float(parsed.get("data_quality"), 0.0), 0.0, 1.0), 4),
        requires_more_data=bool(parsed.get("requires_more_data")) if isinstance(parsed.get("requires_more_data"), bool) else False,
        raw=raw_meta,
        rejected_fields=sorted(rejected),
    )
```

- [ ] **Step 4: Run tests to verify all pass**

Run: `python -m pytest tests/unit/test_ai_decision.py -v`
Expected: PASS (5 + 8 = 13 tests).

- [ ] **Step 5: §4 sweep + regression + commit**

```bash
python -m pytest tests/unit/test_ai_decision.py tests/test_ollama_decision.py -q
# expected: 13 + 19 green (legacy parse_trade_review untouched)
grep -riE "mostly complete|should work|99%|mock for now|todo later|placeholder" core/ai/decision.py tests/unit/test_ai_decision.py
git add core/ai/decision.py tests/unit/test_ai_decision.py
git commit -m "feat(phase4b): parse_decision — PARSE+SCHEMA evolving parse_trade_review, §43 strip/reject (§41/§42/§43)"
```

---

## Task 3: `core/ai/pipeline.py` — ValidationResult + 6 injectable stages + run_pipeline (PARSE/SCHEMA/SEMANTIC real; AUTHORITATIVE/POLICY/RISK_GATE injectable defaults)

**Files:**
- Create: `core/ai/pipeline.py`
- Test: `tests/unit/test_ai_pipeline.py`

**Interfaces:**
- Consumes: `Action`, `Decision`, `parse_decision`, `DecisionSchemaError` (Tasks 1–2).
- Produces: `ValidationResult`, `StageResult`, `PARSE/SCHEMA/SEMANTIC/AUTHORITATIVE_DATA_CHECK/POLICY/RISK_GATE`, `STAGE_ORDER`, `default_services(config)`, `run_pipeline(...)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_ai_pipeline.py
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
    res = run_pipeline(raw, signal={}, positions=[], config={"ai": {"decision": {"strict_schema": True}}})
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
    # AUTHORITATIVE/POLICY/RISK_GATE default to pass-through in Task 3
    res = run_pipeline(raw, signal={}, positions=[], config={})
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

    services = {SEMANTIC: fail_semantic}
    res = run_pipeline("ignored", signal={}, positions=[], config={}, services=services)
    assert res.ok is False
    assert res.stage == SEMANTIC
    assert res.reason == "bad semantics"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_ai_pipeline.py -v`
Expected: FAIL with `ImportError: No module named 'core.ai.pipeline'`.

- [ ] **Step 3: Write the implementation**

```python
# core/ai/pipeline.py
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
from typing import Any, Callable, Dict, Iterable, List, Optional

from core.ai.decision import Action, Decision, DecisionSchemaError, parse_decision

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
    strict = bool(ctx.config.get("ai", {}).get("decision", {}).get("strict_schema", False))
    try:
        decision = parse_decision(ctx.raw_decision, strict_schema=strict)
    except DecisionSchemaError as exc:
        return StageResult(passed=False, decision=None, reason=str(exc),
                            checks={"stage": PARSE})
    if decision.raw.get("parse_error") == "invalid_json":
        return StageResult(passed=False, decision=decision,
                            reason="PARSE: invalid_json (no JSON object found)",
                            checks={"stage": PARSE})
    ctx.decision = decision
    return StageResult(passed=True, decision=decision)


def stage_schema(ctx: _StageContext) -> StageResult:
    # §43 authoritative fields are already stripped/rejected inside parse_decision.
    # If any were rejected and strict_schema is on, PARSE already raised. Here we
    # only record the rejection audit (non-strict path) — schema passed.
    decision = ctx.decision
    if decision is None:
        return StageResult(passed=False, reason="SCHEMA: no decision from PARSE")
    if decision.rejected_fields:
        ctx.checks["rejected_authoritative_fields"] = list(decision.rejected_fields)
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


def stage_authoritative_data_check(ctx: _StageContext) -> StageResult:
    """Wired fully in Task 4. Task 3 default: pass-through (no authoritative recalc)
    so PARSE/SCHEMA/SEMANTIC + ordering + injectability are testable in isolation."""
    return StageResult(passed=True, decision=ctx.decision)


def stage_policy(ctx: _StageContext) -> StageResult:
    """Wired fully in Task 5. Task 3 default: pass-through."""
    return StageResult(passed=True, decision=ctx.decision)


def stage_risk_gate(ctx: _StageContext) -> StageResult:
    """Wired fully in Task 5. Task 3 default: pass-through."""
    return StageResult(passed=True, decision=ctx.decision)


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_ai_pipeline.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: §4 sweep + regression + commit**

```bash
python -m pytest tests/unit/test_ai_pipeline.py tests/unit/test_ai_decision.py -q
grep -riE "mostly complete|should work|99%|mock for now|todo later|placeholder" core/ai/pipeline.py tests/unit/test_ai_pipeline.py
git add core/ai/pipeline.py tests/unit/test_ai_pipeline.py
git commit -m "feat(phase4b): §42 pipeline — ValidationResult + 6 injectable stages + run_pipeline (PARSE/SCHEMA/SEMANTIC) (§42)"
```

---

## Task 4: AUTHORITATIVE_DATA_CHECK stage — compose `evaluate_capital_entry` + §43 recalc

**Files:**
- Modify: `core/ai/pipeline.py` (replace `stage_authoritative_data_check` pass-through)
- Test: `tests/unit/test_ai_pipeline.py` (append)

**Interfaces:**
- Consumes (real, lazy-imported inside the stage): `core.capital_entry_guard.evaluate_capital_entry(signal, positions, config, *, perf_cache=None, news_pulse=None)`, `core.capital_manager.resolve_deployable_balance(cm)`, `core.position_sizing.compute_size_factor(score, cfg=None, ...)`, `core.risk_limits.calculate_trade_risk_pct(*, notional_usd, stop_loss_fraction, equity)`, `agents.risk_agent.RiskAgent(config_manager).evaluate_signal(signal)`. All injectable via `services` keys `"evaluate_capital_entry"`, `"resolve_deployable_balance"`, `"compute_size_factor"`, `"calculate_trade_risk_pct"`, `"risk_agent"` for tests.
- Produces: a real `stage_authoritative_data_check` that fails on `allowed=False` and stores app-calculated §43 numbers in `ctx.checks["authoritative"]`.

- [ ] **Step 1: Write the failing tests (append)**

```python
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
    res = run_pipeline(raw, signal={}, positions=[], config={}, services=services)
    assert res.ok is True
    assert res.checks["authoritative"]["capital_entry"]["status"] == "validated"
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `python -m pytest tests/unit/test_ai_pipeline.py -v`
Expected: the 3 new tests FAIL (pass-through stage returns ok=True with no `authoritative` check / no failure on blocked entry).

- [ ] **Step 3: Replace the pass-through `stage_authoritative_data_check` in `core/ai/pipeline.py`**

```python
def _real_authoritative_services(config: Dict[str, Any]) -> Dict[str, Any]:
    """Lazy real §43 service callables. Imported here (not at module top) so that
    importing core.ai.pipeline never triggers heavy/coupled imports."""
    from core.capital_entry_guard import evaluate_capital_entry
    from core.capital_manager import resolve_deployable_balance
    from core.position_sizing import compute_size_factor
    from core.risk_limits import calculate_trade_risk_pct
    from agents.risk_agent import RiskAgent
    from core.config import ConfigManager

    cm = ConfigManager()
    return {
        "evaluate_capital_entry": evaluate_capital_entry,
        "resolve_deployable_balance": lambda _cm: resolve_deployable_balance(cm),
        "compute_size_factor": compute_size_factor,
        "calculate_trade_risk_pct": calculate_trade_risk_pct,
        "risk_agent": RiskAgent(cm),
    }


_AUTHORITATIVE_SERVICE_KEYS = (
    "evaluate_capital_entry", "resolve_deployable_balance",
    "compute_size_factor", "calculate_trade_risk_pct", "risk_agent",
)


def _ensure_authoritative_services(ctx: _StageContext) -> None:
    """Lazily fill any missing §43 service callables with the real implementations,
    cached on ctx.services. Tests that inject fakes for all five keys never build the
    real block (no ConfigManager/CapitalManager constructed)."""
    if all(k in ctx.services and ctx.services[k] is not None for k in _AUTHORITATIVE_SERVICE_KEYS):
        return
    if ctx.checks.get("_real_svc_built"):
        return
    try:
        ctx.services.update(_real_authoritative_services(ctx.config))
    except Exception:
        pass
    ctx.checks["_real_svc_built"] = True


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
    if not risk_eval.get("accepted", True):
        return StageResult(
            passed=False, decision=ctx.decision,
            reason=f"AUTHORITATIVE: portfolio risk rejected — {risk_eval.get('reason','')}",
            checks={"authoritative": authoritative},
        )
    return StageResult(passed=True, decision=ctx.decision, checks={"authoritative": authoritative})
```

(The `stage_policy`/`stage_risk_gate` pass-throughs from Task 3 are replaced in Task 5; they read `ctx.services["risk_agent"]` / `ctx.services["should_veto_trade"]` and also call `_ensure_authoritative_services` is NOT needed there because POLICY/RISK_GATE only need `risk_agent` + `should_veto_trade`, which `_ensure_authoritative_services` already provides. Task 5 calls `_ensure_authoritative_services(ctx)` at the top of `stage_policy` so `risk_agent`/`should_veto_trade` resolve consistently.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_ai_pipeline.py -v`
Expected: PASS (7 + 3 = 10 tests). Note: with the merge + lazy-resolve design, only `test_run_pipeline_no_trade_action_passes_semantic_without_ticker` (which injects NO services) reaches the real AUTHORITATIVE stage (NO_TRADE passes SEMANTIC). It must now inject the five §43 service fakes to stay hermetic. The other Task-3 tests either short-circuit before AUTHORITATIVE (`test_run_pipeline_first_failing_stage_short_circuits`) or inject a fake AUTHORITATIVE *stage* (`test_run_pipeline_stages_are_injectable_and_called_in_order`), so they never build real services and need no change. Document this as a faithful test adjustment, not a weakening.

Update `test_run_pipeline_no_trade_action_passes_semantic_without_ticker` to pass a `services` dict with the five AUTHORITATIVE fakes + a veto fake:
```python
    class _RA:
        def evaluate_signal(self, s):
            return {"accepted": True, "reason": "passed", "checks": [], "metrics": {}}
    services = {
        "evaluate_capital_entry": lambda s, p, c=None, **k: {"allowed": True, "check": "complete", "status": "validated", "reason": "ok", "checks": {}},
        "resolve_deployable_balance": lambda cm: 0.0,
        "compute_size_factor": lambda score, cfg=None, **k: {"size_factor": 0.0},
        "calculate_trade_risk_pct": lambda **k: 0.0,
        "risk_agent": _RA(),
        "should_veto_trade": lambda review, config=None: False,
    }
    res = run_pipeline(raw, signal={}, positions=[], config={}, services=services)
```
Re-run; all 10 pass hermetically (no real ConfigManager/CapitalManager constructed).

- [ ] **Step 5: §4 sweep + regression + commit**

```bash
python -m pytest tests/unit/test_ai_pipeline.py tests/unit/test_capital_entry_guard.py tests/unit/test_autonomous_trade_guard.py tests/test_risk_agent.py -q
grep -riE "mostly complete|should work|99%|mock for now|todo later|placeholder" core/ai/pipeline.py tests/unit/test_ai_pipeline.py
git add core/ai/pipeline.py tests/unit/test_ai_pipeline.py
git commit -m "feat(phase4b): AUTHORITATIVE_DATA_CHECK stage — compose evaluate_capital_entry + §43 recalc (§42/§43)"
```

---

## Task 5: POLICY + RISK_GATE stages — `RiskAgent.evaluate_signal` + `risk_limits` + `should_veto_trade` (unchanged)

**Files:**
- Modify: `core/ai/pipeline.py` (replace `stage_policy` + `stage_risk_gate` pass-throughs)
- Test: `tests/unit/test_ai_pipeline.py` (append)

**Interfaces:**
- Consumes: `agents.risk_agent.RiskAgent.evaluate_signal(signal)`, `core.risk_limits.calculate_trade_risk_pct(*, notional_usd, stop_loss_fraction, equity)`, `core.ollama_decision.should_veto_trade(review, config)` (UNCHANGED — called via a composition adapter that maps a `Decision` to the legacy `review` shape). Injectable via `services["risk_agent"]`, `services["calculate_trade_risk_pct"]`, `services["should_veto_trade"]`.
- Produces: real `stage_policy` + `stage_risk_gate`.

- [ ] **Step 1: Write the failing tests (append)**

```python
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
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `python -m pytest tests/unit/test_ai_pipeline.py -v`
Expected: the 4 new tests FAIL (pass-through POLICY/RISK_GATE return ok=True without checking risk_agent or veto).

- [ ] **Step 3: Replace `stage_policy` and `stage_risk_gate` in `core/ai/pipeline.py`**

```python
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
        "recommendation": _ACTION_TO_RECOMMENDATION.get(decision.action, "approve"),
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
        from core.ollama_decision import should_veto_trade as veto_fn  # lazy real
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
```

Also extend `_real_authoritative_services` and `_AUTHORITATIVE_SERVICE_KEYS` to include `"should_veto_trade"`:
```python
_AUTHORITATIVE_SERVICE_KEYS = (
    "evaluate_capital_entry", "resolve_deployable_balance",
    "compute_size_factor", "calculate_trade_risk_pct",
    "risk_agent", "should_veto_trade",
)
```
and in `_real_authoritative_services` add (lazy import inside the function):
```python
    from core.ollama_decision import should_veto_trade
    ...
    "should_veto_trade": should_veto_trade,
```
(The lazy `from core.ollama_decision import should_veto_trade` inside `stage_risk_gate` remains as a defensive fallback for the case where `should_veto_trade` was not injected and `_ensure_authoritative_services` could not build the real block.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_ai_pipeline.py -v`
Expected: PASS (10 + 4 = 14 tests).

- [ ] **Step 5: §4 sweep + regression + commit**

```bash
python -m pytest tests/unit/test_ai_pipeline.py tests/test_ollama_decision.py tests/test_risk_agent.py tests/unit/test_risk_settings.py -q
grep -riE "mostly complete|should work|99%|mock for now|todo later|placeholder" core/ai/pipeline.py tests/unit/test_ai_pipeline.py
git add core/ai/pipeline.py tests/unit/test_ai_pipeline.py
git commit -m "feat(phase4b): POLICY + RISK_GATE stages — RiskAgent + risk_limits + should_veto_trade unchanged (§42)"
```

---

## Task 6: Config additions + `core/ai/__init__.py` re-exports

**Files:**
- Modify: `core/config.py` (add `DEFAULT_CONFIG["ai"]["decision"]`)
- Modify: `config.example.json` (mirror)
- Modify: `core/ai/__init__.py` (additive re-exports + `__all__`)
- Test: `tests/unit/test_ai_decision_config.py` (create)

**Interfaces:**
- Consumes: `Action`, `Decision`, `parse_decision`, `ValidationResult`, `StageResult`, `run_pipeline`, stage constants (Tasks 1–5).
- Produces: `DEFAULT_CONFIG["ai"]["decision"]` with `strict_schema`/`critic_enabled`/`critic_force_no_trade_on_fail`; public re-exports.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_ai_decision_config.py
"""Phase 4B config + re-export contract tests."""
from core.config import DEFAULT_CONFIG
import core.ai as ai


def test_ai_decision_config_block_present_with_spec_defaults():
    decision = DEFAULT_CONFIG["ai"]["decision"]
    # §42 strict_schema (reject AI authoritative numbers, not silently strip)
    assert decision["strict_schema"] is False
    # §44 critic (wired in 4C; config lands here so the pipeline can read it)
    assert decision["critic_enabled"] is True
    assert decision["critic_force_no_trade_on_fail"] is True


def test_ai_module_reexports_4b_symbols():
    for name in [
        "Action", "Decision", "parse_decision", "AUTHORITATIVE_FIELDS",
        "ValidationResult", "StageResult", "run_pipeline",
        "PARSE", "SCHEMA", "SEMANTIC", "AUTHORITATIVE_DATA_CHECK", "POLICY", "RISK_GATE",
        "STAGE_ORDER", "default_services",
    ]:
        assert hasattr(ai, name), f"core.ai must re-export {name}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_ai_decision_config.py -v`
Expected: FAIL (`DEFAULT_CONFIG["ai"] has no key 'decision'` / missing re-exports).

- [ ] **Step 3: Make the changes**

In `core/config.py`, inside the `"ai": { ... }` block (after `"all_unavailable_no_trade": True,`), add:
```python
    "decision": {
        "strict_schema": False,             # §43 — reject (not silently strip) AI authoritative-number fields when True
        "critic_enabled": True,              # §44 — can be disabled in low autonomy modes; forced on in LIVE_AUTO (4C)
        "critic_force_no_trade_on_fail": True # §44 — a failing critic verdict forces NO_TRADE
    },
```

In `config.example.json`, inside the `"ai": { ... }` block (after `"all_unavailable_no_trade": true`), add:
```json
    "decision": {
        "strict_schema": false,
        "critic_enabled": true,
        "critic_force_no_trade_on_fail": true
    },
```

In `core/ai/__init__.py`, add imports:
```python
from core.ai.decision import (
    ALLOWED_ACTIONS, AUTHORITATIVE_FIELDS, Action, Decision, DecisionSchemaError, parse_decision,
)
from core.ai.pipeline import (
    AUTHORITATIVE_DATA_CHECK, PARSE, POLICY, RISK_GATE, SCHEMA, SEMANTIC, STAGE_ORDER,
    StageResult, ValidationResult, default_services, run_pipeline,
)
```
and extend `__all__` with the new names (alphabetical): `AUTHORITATIVE_DATA_CHECK`, `AUTHORITATIVE_FIELDS`, `ALLOWED_ACTIONS`, `Action`, `Decision`, `DecisionSchemaError`, `PARSE`, `POLICY`, `RISK_GATE`, `SCHEMA`, `SEMANTIC`, `STAGE_ORDER`, `StageResult`, `ValidationResult`, `default_services`, `parse_decision`, `run_pipeline`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_ai_decision_config.py tests/unit/test_ai_decision.py tests/unit/test_ai_pipeline.py -q`
Expected: PASS.

- [ ] **Step 5: §4 sweep + regression + commit**

```bash
python -m pytest tests/unit tests/contract -q
grep -riE "mostly complete|should work|99%|mock for now|todo later|placeholder" core/config.py config.example.json core/ai/__init__.py tests/unit/test_ai_decision_config.py
git add core/config.py config.example.json core/ai/__init__.py tests/unit/test_ai_decision_config.py
git commit -m "feat(phase4b): ai.decision config block + core.ai re-exports (§42/§44 config)"
```

---

## Task 7: Golden §42/§43 + control docs + exit gate

**Files:**
- Create: `tests/golden/test_ai_decision_pipeline_golden.py`
- Modify: `docs/V9_ARCHITECTURE.md` (add a Phase 4B note: §42 pipeline + §43 side-channel convention)
- Modify: `docs/V9_CONTROL_DOCS_INDEX.md` (if it indexes architecture notes — add the 4B line; otherwise skip)
- Create: `.superpowers/sdd/2026-08-12-v9-phase4b-decision-schema-and-validation-pipeline/progress.md` (ledger)

- [ ] **Step 1: Write the golden tests**

```python
# tests/golden/test_ai_decision_pipeline_golden.py
"""Golden tests for the Phase 4B §42 validation pipeline + §43 non-authoritative numbers.

GOLD-042 — §42 ordering: every decision passes PARSE -> SCHEMA -> SEMANTIC ->
AUTHORITATIVE DATA CHECK -> POLICY -> RISK GATE in that exact order; the first
failing stage short-circuits; AI prose alone never produces an order.

GOLD-043 — §43 non-authoritative numbers: AI-supplied price/P&L/IV/EPS/portfolio
value/allowed_capital/position_size are stripped (or rejected in strict mode) and
the validated numbers come from app services (resolve_deployable_balance,
compute_size_factor, calculate_trade_risk_pct, RiskAgent.evaluate_signal).
"""
import pytest

from core.ai.decision import Action, Decision
from core.ai.pipeline import (
    AUTHORITATIVE_DATA_CHECK, PARSE, POLICY, RISK_GATE, SCHEMA, SEMANTIC,
    STAGE_ORDER, run_pipeline,
)

pytestmark = pytest.mark.golden


def _full_fake_services():
    class _RA:
        def evaluate_signal(self, s):
            return {"accepted": True, "reason": "passed", "checks": [], "metrics": {}}
    return {
        "evaluate_capital_entry": lambda s, p, c=None, **k: {"allowed": True, "check": "complete", "status": "validated", "reason": "ok", "checks": {}},
        "resolve_deployable_balance": lambda cm: 50000.0,
        "compute_size_factor": lambda score, cfg=None, **k: {"size_factor": 0.5},
        "calculate_trade_risk_pct": lambda **k: 0.3,
        "risk_agent": _RA(),
        "should_veto_trade": lambda review, config=None: False,
    }


def test_gold042_pipeline_stage_order_is_exactly_spec_42():
    assert STAGE_ORDER == (
        "PARSE", "SCHEMA", "SEMANTIC",
        "AUTHORITATIVE_DATA_CHECK", "POLICY", "RISK_GATE",
    )


def test_gold042_ai_prose_alone_never_orders():
    # pure prose, no JSON — must resolve to NO_TRADE at PARSE, never an open action
    res = run_pipeline("The chart looks great, I think we should buy NVDA aggressively!",
                       signal={}, positions=[], config={}, services=_full_fake_services())
    assert res.ok is False
    assert res.stage == PARSE
    assert res.decision.action == Action.NO_TRADE


def test_gold042_first_failing_stage_short_circuits_in_order():
    # invalid action -> PARSE produces NO_TRADE (parse success) -> passes; force a
    # SEMANTIC fail (OPEN_LONG, no ticker) and confirm the stage is SEMANTIC
    res = run_pipeline({"action": "OPEN_LONG", "confidence": 0.8},
                       signal={}, positions=[], config={}, services=_full_fake_services())
    assert res.ok is False
    assert res.stage == SEMANTIC


def test_gold043_ai_authoritative_numbers_are_stripped_not_used():
    raw = {"action": "OPEN_LONG", "ticker": "NVDA", "confidence": 0.8,
           "price": 1.0, "pnl": 2.0, "iv": 3.0, "eps": 4.0,
           "portfolio_value": 5.0, "allowed_capital": 999999, "position_size": 888888}
    res = run_pipeline(raw, signal={"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95,
                                     "signal_score": 0.8, "strategy_type": "SCANNER_AI_MOMENTUM"},
                       positions=[], config={}, services=_full_fake_services())
    assert res.ok is True
    auth = res.checks["authoritative"]
    # §43: app-service numbers, NOT the AI's 999999 / 888888
    assert auth["allowed_capital"] == 50000.0
    assert auth["size_factor"] == 0.5
    assert auth["trade_risk_pct"] == 0.3
    # the Decision schema has no authoritative fields at all
    for forbidden in ("price", "pnl", "iv", "eps", "portfolio_value", "allowed_capital", "position_size"):
        assert not hasattr(res.decision, forbidden)


def test_gold043_strict_schema_rejects_authoritative_fields():
    raw = {"action": "OPEN_LONG", "ticker": "NVDA", "confidence": 0.8, "price": 850.0}
    res = run_pipeline(raw, signal={}, positions=[],
                       config={"ai": {"decision": {"strict_schema": True}}},
                       services=_full_fake_services())
    assert res.ok is False
    assert res.stage == SCHEMA
    assert "price" in res.reason


def test_gold042_vetoed_risk_gate_forces_no_trade_valid_outcome():
    svc = _full_fake_services()
    svc["should_veto_trade"] = lambda review, config=None: True
    raw = {"action": "OPEN_LONG", "ticker": "NVDA", "confidence": 0.9}
    res = run_pipeline(raw, signal={"ticker": "NVDA", "entry": 100, "tp": 110, "sl": 95,
                                     "signal_score": 0.9, "strategy_type": "SCANNER_AI_MOMENTUM"},
                       positions=[], config={}, services=svc)
    # §44: a valid final result can be NO_TRADE
    assert res.ok is True
    assert res.decision.action == Action.NO_TRADE
    assert res.checks["risk_gate"]["vetoed"] is True
```

- [ ] **Step 2: Run the golden tests**

Run: `python -m pytest tests/golden/test_ai_decision_pipeline_golden.py -v`
Expected: PASS (6 golden tests).

- [ ] **Step 3: Update control docs**

Append to `docs/V9_ARCHITECTURE.md` a short Phase 4B section:
```markdown
## Phase 4B — §41 decision schema + §42 validation pipeline + §43 non-authoritative numbers

- `core/ai/decision.py`: `Action` enum (7 §41 actions), frozen `Decision` (§41 fields only),
  `parse_decision` (evolves `ollama_decision.parse_trade_review`; strips §43 authoritative fields,
  rejects them in `strict_schema` mode).
- `core/ai/pipeline.py`: `run_pipeline` runs the §42 stages
  PARSE→SCHEMA→SEMANTIC→AUTHORITATIVE_DATA_CHECK→POLICY→RISK_GATE, composing the existing
  `evaluate_capital_entry` / `RiskAgent.evaluate_signal` / `risk_limits.calculate_trade_risk_pct`
  / `should_veto_trade` (all unchanged). First failing stage short-circuits; AI prose alone
  yields NO_TRADE at PARSE and never orders.
- §43 convention: AI-supplied price/P&L/IV/EPS/portfolio_value/allowed_capital/position_size are
  NOT carried in the `Decision`. App-calculated values live in
  `ValidationResult.checks["authoritative"]` (allowed_capital, size_factor, trade_risk_pct,
  portfolio_risk), computed from `resolve_deployable_balance` / `compute_size_factor` /
  `calculate_trade_risk_pct` / `RiskAgent.evaluate_signal`.
- The `ollama_decision`→`generate_with_fallback` bridge and the `agent_manager` rewire are
  Phase 4C; 4B is pure-additive (no existing file's behavior changes).
```

- [ ] **Step 4: Create the SDD ledger**

Create `.superpowers/sdd/2026-08-12-v9-phase4b-decision-schema-and-validation-pipeline/progress.md` with the header (mode, base, baseline) and a Task 1–7 checklist mirroring this plan (to be filled as execution proceeds).

- [ ] **Step 5: Full regression gate + §4 sweep + protected-source/deps diff + commit**

```bash
# curated gate (comparable to 4A's 1811)
python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf -q
# full suite
python -m pytest tests tests_e2e -q
# §4 sweep over all 4B-touched files
grep -riE "mostly complete|should work|99%|mock for now|todo later|placeholder" core/ai/decision.py core/ai/pipeline.py core/ai/__init__.py core/config.py config.example.json tests/unit/test_ai_decision.py tests/unit/test_ai_pipeline.py tests/unit/test_ai_decision_config.py tests/golden/test_ai_decision_pipeline_golden.py
# V7 untouched + deps diff empty
git diff --stat f912a33 -- ../ 2>/dev/null || true
# confirm V7 not touched
git -C "C:/Users/<user>/PyCharmMiscProject/Market Signals V7 - Premium" status --short | head
git add tests/golden/test_ai_decision_pipeline_golden.py docs/V9_ARCHITECTURE.md .superpowers/sdd/2026-08-12-v9-phase4b-decision-schema-and-validation-pipeline/progress.md
git commit -m "test(phase4b): golden §42/§43 + control docs — Phase 4B PASS"
```

- [ ] **Step 6: Final review (inline, controller-as-reviewer)** — review the full 4B diff against this plan, document any deviations (strict improvements only) in the SDD ledger, and update the memory `v9-migration-project.md` Phase 4 row to "IN PROGRESS (4C remains)".

---

## Self-Review

**1. Spec coverage:**
- §41 (7 actions + decision schema) → Task 1 (`Action`, `Decision`) + Task 2 (`parse_decision`). ✓
- §42 (6-stage pipeline; AI prose alone never orders) → Task 3 (`run_pipeline` + `STAGE_ORDER`), Task 4 (AUTHORITATIVE), Task 5 (POLICY/RISK_GATE), Task 7 (golden GOLD-042 ordering + prose-never-orders). ✓
- §43 (AI numbers not authoritative) → Task 2 (strip/reject), Task 4 (AUTHORITATIVE recalc + side channel), Task 7 (golden GOLD-043). ✓
- §4 (no forbidden language) → §4 sweep every task. ✓
- §5/§7 (evolve, preserve) → pipeline composes existing functions; `should_veto_trade` unchanged (Task 5 test pins the unchanged signature); legacy `ollama_decision` untouched. ✓
- §106/§115/§117/§118 → no secrets, no test weakening, V7 read-only, stdlib-only. ✓
- §44 (critic) — config lands in Task 6 (`critic_enabled`/`critic_force_no_trade_on_fail`); the critic *workflow* is 4C (design §5; `loop.py` docstring). ✓ (config here, workflow 4C — explicitly out of 4B scope per design §7/out-of-scope.)
- §45/§46/§48 autonomy + `agent_manager` rewire + `ollama_decision` bridge — 4C (design §5; this plan's Architecture line states so). ✓

**2. Placeholder scan:** No TBD/TODO/"implement later"/"add validation"/"similar to Task N". Every code step contains runnable code. The Task 3 pass-through stages are explicitly labeled as wired in Tasks 4–5 (not placeholders — they are real, tested identity stages replaced in later tasks, a TDD progression). ✓

**3. Type consistency:**
- `Decision` fields consistent across Tasks 1–5 (`action/ticker/horizon/confidence/strategy_id/thesis/supporting_evidence/opposing_evidence/risk_flags/data_quality/requires_more_data/raw/rejected_fields`).
- `parse_decision(raw, *, strict_schema=False)` signature identical in Task 2 interface, Task 3 `stage_parse`, Task 7 golden.
- `StageResult(passed, decision, reason, checks)` and `ValidationResult(ok, decision, stage, reason, checks)` consistent throughout.
- `should_veto_trade(review, config)` called UNCHANGED (Task 5 test pins the real signature).
- `calculate_trade_risk_pct(*, notional_usd, stop_loss_fraction, equity)` called keyword-only (Task 4), matching the audit.
- `evaluate_capital_entry(signal, positions, config, *, perf_cache, news_pulse)` called positionally for the first three (Task 4), matching the audit.
- `STAGE_ORDER` tuple identical in Task 3 and Task 7 golden. ✓

**4. Scope check:** 4B is one cohesive subsystem (decision schema + pipeline). 4C is separate (critic + autonomy + rewire) and explicitly deferred. Each task produces a green, testable commit. ✓

No issues found. Plan is complete.