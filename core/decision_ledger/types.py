"""Phase 13 §97 — immutable Decision Ledger evidence contracts.

Every trading decision — executed, rejected, or NO_TRADE (§90) — becomes one
persistent ledger entry answering the eleven audit questions from recorded
evidence only. Numbers stay app-authoritative (§43): the ledger records what
services computed, it never recomputes or trusts AI-supplied values.

External News/SEC text never enters the ledger verbatim: snapshots keep the
Phase 10 UNTRUSTED_EXTERNAL_DATA digests already applied upstream.
"""
from __future__ import annotations


def decision_critic_evidence(row):
    """Return AI Critic evidence from a decision's decision_context (top-level ai_critic fallback)."""
    if not isinstance(row, dict):
        return {}
    context = row.get("decision_context")
    critic = context.get("ai_critic") if isinstance(context, dict) else None
    if not isinstance(critic, dict):
        critic = row.get("ai_critic")
    return dict(critic) if isinstance(critic, dict) else {}

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SOURCE_SIGNAL_PATH = "SIGNAL_PATH"
SOURCE_PRE_EARNINGS_PATH = "PRE_EARNINGS_PATH"
SOURCE_REPLAY = "REPLAY"
_LEDGER_SOURCES = frozenset({SOURCE_SIGNAL_PATH, SOURCE_PRE_EARNINGS_PATH, SOURCE_REPLAY})

PROMPT_TEXT_CAP = 20000


def sha256_digest(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8", errors="replace")).hexdigest()


def json_digest(value: Any) -> str:
    return sha256_digest(json.dumps(value, ensure_ascii=True, sort_keys=True, default=str))


def _sanitize_nonfinite(node: Any) -> Any:
    """Historical rows may hold inf/NaN (e.g. the pre-fix liquidity check);
    §99 consumers need JSON-safe values, so non-finite floats become None."""
    if isinstance(node, dict):
        return {key: _sanitize_nonfinite(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_sanitize_nonfinite(item) for item in node]
    if isinstance(node, float) and (node != node or node in (float("inf"), float("-inf"))):
        return None
    return node


def _clean(value: Any) -> Any:
    """JSON-safe, digest-stable projection for arbitrary evidence objects."""
    return json.loads(json.dumps(_sanitize_nonfinite(value), ensure_ascii=True, sort_keys=True, default=str))


def bounded_text(value: Any, cap: int = PROMPT_TEXT_CAP) -> str:
    text = str(value or "")
    if len(text) <= cap:
        return text
    return text[:cap] + f"...[truncated {len(text) - cap} chars]"


def decision_projection(decision: Any) -> Dict[str, Any]:
    """§41 Decision → bounded dict (dataclass or already-decoded mapping)."""
    if decision is None:
        return {"available": False, "reason": "no_decision_recorded"}
    if isinstance(decision, Dict):
        raw = dict(decision)
    else:
        raw = {
            f: getattr(decision, f)
            for f in (
                "action", "ticker", "horizon", "confidence", "strategy_id", "thesis",
                "supporting_evidence", "opposing_evidence", "risk_flags", "data_quality",
                "requires_more_data", "rejected_fields",
            )
            if hasattr(decision, f)
        }
    action = raw.get("action")
    return _clean({
        "action": str(getattr(action, "value", action) or ""),
        "ticker": raw.get("ticker"),
        "horizon": str(getattr(raw.get("horizon"), "value", raw.get("horizon")) or "")
        if raw.get("horizon") is not None else None,
        "confidence": raw.get("confidence"),
        "strategy_id": raw.get("strategy_id"),
        "thesis": bounded_text(raw.get("thesis"), 4000),
        "supporting_evidence": raw.get("supporting_evidence") or [],
        "opposing_evidence": raw.get("opposing_evidence") or [],
        "risk_flags": raw.get("risk_flags") or [],
        "data_quality": raw.get("data_quality"),
        "requires_more_data": raw.get("requires_more_data"),
        "rejected_fields": raw.get("rejected_fields") or [],
    })


def validation_projection(validation: Any) -> Dict[str, Any]:
    """§42 ValidationResult → bounded dict."""
    if validation is None:
        return {"available": False, "reason": "no_validation_recorded"}
    stage = getattr(validation, "stage", None)
    checks = getattr(validation, "checks", None) or {}
    return _clean({
        "ok": bool(getattr(validation, "ok", False)),
        "stage": str(getattr(stage, "value", stage) or ""),
        "reason": bounded_text(getattr(validation, "reason", ""), 2000),
        "checks": checks,
    })


def critic_projection(critic: Any) -> Dict[str, Any]:
    """§44 CriticVerdict → bounded dict."""
    if critic is None:
        return {"available": False, "reason": "critic_not_run"}
    return _clean({
        "available": True,
        "veto": bool(getattr(critic, "veto", False)),
        "model_veto": bool(getattr(critic, "model_veto", False)),
        "categories": list(getattr(critic, "categories", []) or []),
        "severity": getattr(critic, "severity", None),
        "quality_score": getattr(critic, "quality_score", None),
        "veto_threshold": getattr(critic, "veto_threshold", None),
        "size_factor": getattr(critic, "size_factor", None),
        "policy_reason": bounded_text(getattr(critic, "policy_reason", ""), 500),
        "challenges": bounded_text(getattr(critic, "challenges", ""), 4000)
        if not isinstance(getattr(critic, "challenges", None), (list, tuple))
        else list(getattr(critic, "challenges", []) or []),
        "rationale_digest": sha256_digest(str(getattr(critic, "rationale", "") or "")),
        "rationale": bounded_text(getattr(critic, "rationale", ""), 4000),
        "provider": bounded_text(getattr(critic, "provider", ""), 100),
        "model": bounded_text(getattr(critic, "model", ""), 200),
        "prompt_version": bounded_text(getattr(critic, "prompt_version", ""), 100),
    })


@dataclass(frozen=True)
class LedgerEntry:
    """One §97 record. `outcome` is None until §89 evaluation appends one."""
    decision_id: str
    created_at: str
    ticker: str
    action: str
    horizon: str
    source: str
    status: str
    strategy: str
    ai: Dict[str, Any]
    ai_proposal: Dict[str, Any]
    validation: Dict[str, Any]
    critic: Dict[str, Any]
    risk: Dict[str, Any]
    capital_allowed: Dict[str, Any]
    executed: Dict[str, Any]
    data_existed: Dict[str, Any] = field(default_factory=dict)
    afterward: Optional[Dict[str, Any]] = None
    debate: Optional[Dict[str, Any]] = None
    replay_of: Optional[str] = None
    pit_as_of: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return _clean({
            "decision_id": self.decision_id,
            "created_at": self.created_at,
            "ticker": self.ticker,
            "action": self.action,
            "horizon": self.horizon,
            "source": self.source,
            "status": self.status,
            "strategy": self.strategy,
            "ai": self.ai,
            "ai_proposal": self.ai_proposal,
            "validation": self.validation,
            "critic": self.critic,
            "risk": self.risk,
            "capital_allowed": self.capital_allowed,
            "executed": self.executed,
            "data_existed": self.data_existed,
            "afterward": self.afterward,
            "debate": self.debate,
            "replay_of": self.replay_of,
            "pit_as_of": self.pit_as_of,
        })

    @staticmethod
    def from_dict(raw: Dict[str, Any]) -> "LedgerEntry":
        known = {f for f in LedgerEntry.__dataclass_fields__}
        payload = {k: v for k, v in (raw or {}).items() if k in known}
        payload.setdefault("data_existed", {})
        return LedgerEntry(**payload)

    def eleven_questions(self) -> Dict[str, Any]:
        """§97 — the Decision Ledger questions, answered from this record."""
        ai = dict(self.ai) if isinstance(self.ai, dict) else {}
        # The full prompt text belongs to the detail view, not the row summary.
        prompt_text = ai.pop("prompt_text", None)
        prompt_summary = {
            "version": ai.get("prompt_version") or "",
            "sha256": ai.get("prompt_sha256") or "",
            "chars": ai.get("prompt_chars") or 0,
        }
        if prompt_text:
            prompt_summary["preview"] = bounded_text(prompt_text, 200)
        if not ai.get("consulted", True):
            prompt_summary["note"] = "AI review not consulted for this decision"
        return _clean({
            "decision_id": self.decision_id,
            "timestamp": self.created_at,
            "ticker": self.ticker,
            "event_type": f"decision_{self.status}",
            "status": self.status,
            "what_happened": f"{self.action} ({self.status})",
            "data_existed": self.data_existed,
            "strategy": self.strategy,
            "ai_model": ai,
            "prompt": prompt_summary,
            "ai_proposal": self.ai_proposal,
            "validation": self.validation,
            "critic": self.critic,
            "risk": self.risk,
            "capital_allowed": self.capital_allowed,
            "executed": self.executed,
            "afterward": self.afterward,
            "debate": self.debate,
            "position_id": (self.executed or {}).get("position_id"),
            "signal_id": (self.data_existed or {}).get("signal_id"),
        })


@dataclass(frozen=True)
class LedgerOutcome:
    """§89 — a deterministic after-the-fact evaluation of one entry.

    Appended (never mutated): a later evaluation with more future data
    becomes a new row; readers take the latest per decision.
    """
    decision_id: str
    evaluated_at: str
    outcome: str                     # FAVORABLE / ADVERSE / FLAT / INCONCLUSIVE
    basis: str                       # evaluation description, e.g. "forward_return_5_sessions"
    forward_return_pct: Optional[float] = None
    bars_used: int = 0
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return _clean({
            "decision_id": self.decision_id,
            "evaluated_at": self.evaluated_at,
            "outcome": self.outcome,
            "basis": self.basis,
            "forward_return_pct": self.forward_return_pct,
            "bars_used": self.bars_used,
            "detail": self.detail,
        })


OUTCOME_FAVORABLE = "FAVORABLE"
OUTCOME_ADVERSE = "ADVERSE"
OUTCOME_FLAT = "FLAT"
OUTCOME_INCONCLUSIVE = "INCONCLUSIVE"
_LEDGER_OUTCOMES = frozenset({OUTCOME_FAVORABLE, OUTCOME_ADVERSE, OUTCOME_FLAT, OUTCOME_INCONCLUSIVE})


def validate_source(source: str) -> str:
    if source not in _LEDGER_SOURCES:
        raise ValueError(f"invalid ledger source: {source}")
    return source


def validate_outcome(outcome: str) -> str:
    if outcome not in _LEDGER_OUTCOMES:
        raise ValueError(f"invalid ledger outcome: {outcome}")
    return outcome


__all__ = [
    "LedgerEntry", "LedgerOutcome", "SOURCE_SIGNAL_PATH", "SOURCE_PRE_EARNINGS_PATH",
    "SOURCE_REPLAY", "OUTCOME_FAVORABLE", "OUTCOME_ADVERSE", "OUTCOME_FLAT",
    "OUTCOME_INCONCLUSIVE", "PROMPT_TEXT_CAP", "sha256_digest", "json_digest",
    "bounded_text", "decision_projection", "validation_projection", "critic_projection",
    "validate_source", "validate_outcome",
]
