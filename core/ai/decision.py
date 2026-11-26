"""Phase 4B — §41 structured decision schema.

The §41 action vocabulary (WATCH/NO_TRADE/OPEN_LONG/OPEN_SHORT/HOLD/REDUCE/CLOSE)
and the frozen Decision dataclass. parse_decision (Task 2) evolves the legacy
ollama_decision.parse_trade_review brace-match + coerce pattern into the new
§41 schema and enforces §43 (AI numbers are not authoritative).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional


class Action(str, Enum):
    """§41 allowed actions (verbatim from prompt_to_follow.md)."""

    WATCH = "WATCH"
    NO_TRADE = "NO_TRADE"
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"


ALLOWED_ACTIONS: frozenset = frozenset(a.value for a in Action)

# §43 — AI does not supply authoritative values for these; app services calculate them.
AUTHORITATIVE_FIELDS: frozenset = frozenset({
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
    # `true`/`false` is a wrong type for a number, not 1.0/0.0.
    if isinstance(value, bool):
        return default
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
