"""Phase 13 §88 — Model Lab: compare AI models on identical PIT snapshots.

Per-role only (pre_earnings / market_analyst / news_analyst / quant /
risk_critic / research / coding / review): §88 explicitly forbids one
meaningless global AI score. Every model in one run receives the SAME
frozen snapshot and prompt contract (`inputs_sha256` pinned per run);
per-model results record provider/model, availability, validity, decision
distribution and digests. Providers are injected by the caller (API layer
builds them from owner config); the lab never invents a provider result —
an unavailable provider is recorded as unavailable.
"""
from __future__ import annotations

from copy import deepcopy
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from core.ai.decision_engine import evaluate_trade_decision
from core.decision_ledger.store import DecisionLedgerStore
from core.decision_ledger.types import json_digest

MODEL_LAB_ROLES = (
    "pre_earnings", "market_analyst", "news_analyst", "quant",
    "risk_critic", "research", "coding", "review",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_role(role: str) -> str:
    normalized = str(role or "").lower().strip()
    if normalized not in MODEL_LAB_ROLES:
        raise ValueError(f"invalid model lab role: {role}")
    return normalized


class ModelLab:
    """Runs one §88 comparison: N candidate models over ONE frozen snapshot."""

    def __init__(self, store: DecisionLedgerStore):
        self.store = store

    def run(
        self,
        *,
        role: str,
        signal: Dict[str, Any],
        config: Dict[str, Any],
        candidates: Sequence[Dict[str, Any]],
        risk_result: Optional[Dict[str, Any]] = None,
        services: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """`candidates`: [{"provider": AIProvider, "model": str}, ...].

        Identical inputs are enforced by construction: one signal dict, one
        prompt builder, per-candidate model override only.
        """
        normalized_role = validate_role(role)
        if not candidates:
            raise ValueError("model lab run requires at least one candidate")
        # The lab compares models on IDENTICAL, deterministic inputs. The
        # caller may pre-attach flow_evidence on the signal (evidence wins);
        # the lab itself never triggers the live evidence fetch — otherwise
        # per-candidate fetch timing would make prompts diverge and the
        # inputs digest nondeterministic. The deepcopy additionally isolates
        # caller-owned signal mutation inside this run.
        frozen_signal = deepcopy(signal or {})
        inputs_digest = json_digest({"role": normalized_role, "signal": frozen_signal, "risk": risk_result or {}})
        run_id = f"lab_{uuid.uuid4().hex[:12]}"
        results: List[Dict[str, Any]] = []
        decisions: List[str] = []

        for candidate in candidates:
            provider = candidate.get("provider")
            model = str(candidate.get("model") or "")
            if provider is None:
                results.append({
                    "provider": str(candidate.get("name") or "unknown"),
                    "model": model,
                    "ai_available": False,
                    "error": "provider_not_injected",
                })
                continue
            run_services = dict(services or {})
            run_services["primary_provider"] = provider
            run_services["primary_model"] = model or None
            run_services["skip_flow_evidence"] = True
            result = evaluate_trade_decision(
                frozen_signal, risk_result, config=config, services=run_services,
            )
            action = str(getattr(result.decision.action, "value", result.decision.action))
            decisions.append(action)
            results.append({
                "provider": str(getattr(provider, "name", "") or "unknown"),
                "model": result.model or model,
                "ai_available": bool(result.review.get("available")),
                "valid": bool(result.validation.ok) if result.validation is not None else False,
                "validation_stage": str(result.validation.stage) if result.validation is not None else "",
                "action": action,
                "confidence": getattr(result.decision, "confidence", None),
                "critic_veto": bool(result.critic.veto) if result.critic is not None else False,
                "prompt_version": result.prompt_version,
                "response_digest": json_digest({"prompt": result.prompt or "", "model": result.model or model}),
                "fallback_used": str(result.review.get("fallback_used") or ""),
            })

        run = {
            "run_id": run_id,
            "role": normalized_role,
            "created_at": _utc_now_iso(),
            "inputs_sha256": inputs_digest,
            "candidates": len(results),
            "decision_distribution": _distribution(decisions),
            "inter_model_agreement": _agreement(decisions),
        }
        created = self.store.append_model_lab_run(run, results)
        return {**run, "results": results, "created": created}

    def role_summary(self, *, role: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
        """Per-role aggregated counts over stored runs — no global blend."""
        runs = self.store.list_model_lab_runs(limit=limit, role=role)
        by_role: Dict[str, Dict[str, Any]] = {}
        for run in runs:
            role_key = str(run.get("role") or "")
            bucket = by_role.setdefault(
                role_key, {"runs": 0, "results": 0, "ai_available": 0, "valid": 0, "actions": {}}
            )
            bucket["runs"] += 1
            for result in run.get("results") or []:
                bucket["results"] += 1
                if result.get("ai_available"):
                    bucket["ai_available"] += 1
                if result.get("valid"):
                    bucket["valid"] += 1
                action = str(result.get("action") or "")
                if action:
                    bucket["actions"][action] = bucket["actions"].get(action, 0) + 1
        return {"roles": by_role, "runs_total": len(runs)}


def _distribution(decisions: List[str]) -> Dict[str, int]:
    distribution: Dict[str, int] = {}
    for action in decisions:
        distribution[action] = distribution.get(action, 0) + 1
    return distribution


def _agreement(decisions: List[str]) -> Optional[float]:
    if len(decisions) < 2:
        return None
    counts = _distribution(decisions)
    top = max(counts.values())
    return round(top / len(decisions), 6)


__all__ = ["ModelLab", "MODEL_LAB_ROLES", "validate_role"]
