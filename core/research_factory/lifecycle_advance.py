"""Phase 8+14 — evidence-based lifecycle advancement for the Strategy Registry.

Closes the loop the owner asked for ("strategies run continuously and a better
one is sought"): the research worker accumulates walk-forward evidence per
strategy in `strategy_research_cache`; this module AGGREGATES that evidence
per strategy and advances the registry lifecycle accordingly:

  RESEARCH  -> CHALLENGER  walk-forward evidence accumulated (>= MIN_SYMBOLS
                           strategies-by-symbol rows with >= MIN_WINDOWS
                           windows, fresh) — the strategy is under active
                           challenge evaluation;
  CHALLENGER -> SHADOW     aggregate out-of-sample expectancy > 0 over a
                           combined sample >= MIN_SAMPLE — ready for shadow
                           comparison;
  SHADOW    -> CHAMPION    NEVER here: §63 promotion stays behind the locked
                           Phase 8/16 evidence gates (owner-visible, no
                           automatic champion on one better backtest).

Fail-open: any error aborts the advancement without touching the registry.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from core.strategy_registry.store import StrategyRegistry
from core.strategy_registry.types import StrategyStatus

logger = logging.getLogger(__name__)

MIN_SYMBOLS_FOR_CHALLENGER = 3     # distinct ticker rows with fresh walk-forward
MIN_WINDOWS_FOR_CHALLENGER = 3     # walk-forward windows per row
MIN_SAMPLE_FOR_SHADOW = 30         # combined sample size
MIN_STRATEGY_ROWS = 2              # rows participating at all
MAX_EVIDENCE_AGE_HOURS = 72
PROMOTABLE_VALIDATION_STATUSES = frozenset({"validated", "verified", "passed", "pass"})
REQUIRED_VALIDATION_METHOD = "faithful_v2_walk_forward"


def _load_research_rows() -> List[Dict[str, Any]]:
    try:
        from core.strategy_v2.performance_cache import STRATEGY_RESEARCH_CACHE_PATH

        import json

        with open(STRATEGY_RESEARCH_CACHE_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        entries = payload.get("entries") if isinstance(payload, dict) else payload
        if isinstance(entries, dict):
            rows = []
            for key, value in entries.items():
                if not isinstance(value, dict):
                    continue
                row = dict(value)
                row["_key"] = key
                # Identity lives in the `symbol|asset_class|strategy|direction` key.
                parts = str(key).split("|")
                if len(parts) >= 4:
                    row.setdefault("symbol", parts[0])
                    row.setdefault("asset_class", parts[1])
                    row.setdefault("strategy_id", parts[2])
                    row.setdefault("direction", parts[3])
                rows.append(row)
            return rows
    except Exception:
        logger.exception("research cache read failed for lifecycle advancement")
    return []


def _aggregate_by_strategy(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    per_strategy: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        validation_status = str(row.get("validation_status") or "").strip().lower()
        validation_method = str(row.get("validation_method") or "").strip().lower()
        if (
            validation_status not in PROMOTABLE_VALIDATION_STATUSES
            or validation_method != REQUIRED_VALIDATION_METHOD
        ):
            continue
        explicit_fresh = row.get("is_fresh")
        if isinstance(explicit_fresh, bool):
            fresh = explicit_fresh
        else:
            try:
                stamp = datetime.fromisoformat(str(row.get("last_run") or "").replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                age_hours = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds() / 3600
                fresh = 0 <= age_hours <= MAX_EVIDENCE_AGE_HOURS
            except (TypeError, ValueError):
                fresh = False
        if not fresh:
            continue
        try:
            row_sample = int(row.get("sample_size") or 0)
        except (TypeError, ValueError):
            row_sample = 0
        if row_sample <= 0:
            continue
        strategy_id = str(row.get("strategy_id") or "").upper().strip()
        if not strategy_id:
            continue
        bucket = per_strategy.setdefault(
            strategy_id,
            {"symbols": set(), "wf_fresh_symbols": 0, "sample_size": 0,
             "test_expectancy_sum": 0.0, "test_rows": 0},
        )
        symbol = str(row.get("symbol") or "").strip()
        if symbol:
            bucket["symbols"].add(symbol)
        walk = row.get("walk_forward") if isinstance(row.get("walk_forward"), dict) else {}
        windows = int(walk.get("windows") or 0)
        if windows >= MIN_WINDOWS_FOR_CHALLENGER and fresh:
            bucket["wf_fresh_symbols"] += 1
        bucket["sample_size"] += row_sample
        test_exp = walk.get("avg_test_expectancy_R")
        try:
            bucket["test_expectancy_sum"] += float(test_exp or 0.0)
            bucket["test_rows"] += 1
        except (TypeError, ValueError):
            pass
    return per_strategy


def advance_registry_lifecycles(registry: StrategyRegistry | None = None) -> List[Dict[str, Any]]:
    """Advance RESEARCH/CHALLENGER records on aggregated evidence.

    Returns the list of transitions performed (empty when none qualify)."""
    try:
        registry = registry or StrategyRegistry()
        rows = _load_research_rows()
        per_strategy = _aggregate_by_strategy(rows)
        transitions: List[Dict[str, Any]] = []
        auto_advanced = {
            f"{event.strategy_id}@{event.version}"
            for event in registry.history()
            if event.action == "TRANSITION"
            and event.from_status is StrategyStatus.RESEARCH
            and event.to_status is StrategyStatus.CHALLENGER
            and str(event.details.get("actor") or "") == "research_lifecycle_advancer"
        }
        for record in registry.list_records():
            strategy_id = record.strategy_id.upper()
            evidence = per_strategy.get(strategy_id)
            challenger_ready = bool(
                evidence
                and evidence["sample_size"] >= MIN_SAMPLE_FOR_SHADOW
                and (
                    evidence["wf_fresh_symbols"] >= MIN_SYMBOLS_FOR_CHALLENGER
                    or len(evidence["symbols"]) >= MIN_STRATEGY_ROWS
                )
            )
            if (
                record.status is StrategyStatus.CHALLENGER
                and record.key in auto_advanced
                and not challenger_ready
            ):
                updated = registry.governance_repair_to_research(
                    record.key,
                    actor="research_lifecycle_guard",
                    reason=(
                        "automatic CHALLENGER state lacked fresh, positive-sample "
                        "faithful_v2_walk_forward evidence"
                    ),
                )
                transitions.append({
                    "strategy_id": record.strategy_id,
                    "version": record.version,
                    "from": record.status.value,
                    "to": updated.status.value,
                    "reason": "invalid automatic lifecycle evidence repaired",
                    "evidence": evidence or {},
                    "repair": True,
                })
                continue
            if evidence is None:
                continue
            target = None
            reason = ""
            if record.status is StrategyStatus.RESEARCH and challenger_ready:
                target = StrategyStatus.CHALLENGER
                reason = (
                    f"walk-forward evidence on {evidence['wf_fresh_symbols']} fresh symbol rows "
                    f"({len(evidence['symbols'])} symbols tracked)"
                )
            elif record.status is StrategyStatus.CHALLENGER:
                avg_test_exp = (
                    evidence["test_expectancy_sum"] / evidence["test_rows"]
                    if evidence["test_rows"] else 0.0
                )
                if evidence["sample_size"] >= MIN_SAMPLE_FOR_SHADOW and avg_test_exp > 0:
                    target = StrategyStatus.SHADOW
                    reason = (
                        f"aggregate OOS expectancy {avg_test_exp:.3f}R over sample "
                        f"{evidence['sample_size']} — ready for shadow comparison"
                    )
            if target is None:
                continue
            evidence_payload = {
                "symbols_tracked": len(evidence["symbols"]),
                "wf_fresh_symbol_rows": evidence["wf_fresh_symbols"],
                "aggregate_sample_size": evidence["sample_size"],
                "avg_test_expectancy_R": round(
                    evidence["test_expectancy_sum"] / evidence["test_rows"], 6
                ) if evidence["test_rows"] else None,
            }
            updated = registry.transition(
                record.key,
                target,
                actor="research_lifecycle_advancer",
                reason=f"{reason} | evidence={evidence_payload}",
            )
            transitions.append({
                "strategy_id": record.strategy_id,
                "version": record.version,
                "from": record.status.value,
                "to": updated.status.value,
                "reason": reason,
                "evidence": evidence_payload,
            })
            logger.info("Strategy lifecycle advanced: %s %s -> %s (%s)",
                        record.strategy_id, record.status.value, updated.status.value, reason)
        return transitions
    except Exception:
        logger.exception("lifecycle advancement failed (fail-open)")
        return []


__all__ = ["advance_registry_lifecycles"]
