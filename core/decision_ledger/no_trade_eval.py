"""Phase 13 §90 — measure rejection quality of recorded no-trade decisions.

Without this, V9 cannot know whether its filters are too conservative.
Partition recorded NO_TRADE/rejected decisions against subsequent price
action (deterministic, threshold-explicit):

  correctly_rejected             the move after rejection was adverse
  incorrectly_rejected_profitable the move was favorable beyond threshold —
                                 the filter may be too conservative
  inconclusive                   not enough post-decision data yet

No probability language, no imputation (§32 spirit): missing future bars
stay inconclusive.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from core.decision_ledger.replay import evaluate_decision_outcome
from core.decision_ledger.types import (
    OUTCOME_ADVERSE, OUTCOME_FAVORABLE, LedgerEntry, LedgerOutcome,
)

NO_TRADE_ACTIONS = frozenset({"NO_TRADE", "WATCH", "REJECTED"})


def is_no_trade_entry(entry: LedgerEntry) -> bool:
    if entry.source == "REPLAY":
        return False  # replays are audit artifacts, not live rejections
    if str(entry.action).upper() in NO_TRADE_ACTIONS:
        return True
    return str(entry.status).lower() == "rejected"


def evaluate_no_trade_decisions(
    entries: Iterable[LedgerEntry],
    bars_by_ticker: Dict[str, List[Dict[str, Any]]],
    *,
    threshold_pct: float = 1.0,
    horizon_sessions: Optional[int] = None,
) -> Dict[str, Any]:
    total = 0
    correctly_rejected: List[Dict[str, Any]] = []
    incorrectly_rejected: List[Dict[str, Any]] = []
    inconclusive: List[Dict[str, Any]] = []

    for entry in entries:
        if not is_no_trade_entry(entry):
            continue
        total += 1
        bars = bars_by_ticker.get(entry.ticker) or []
        outcome = evaluate_decision_outcome(
            entry, bars, horizon_sessions=horizon_sessions, threshold_pct=threshold_pct,
        )
        record = {
            "decision_id": entry.decision_id,
            "ticker": entry.ticker,
            "action": entry.action,
            "outcome": outcome.outcome,
            "forward_return_pct": outcome.forward_return_pct,
            "bars_used": outcome.bars_used,
        }
        if outcome.outcome == OUTCOME_ADVERSE:
            correctly_rejected.append(record)
        elif outcome.outcome == OUTCOME_FAVORABLE:
            incorrectly_rejected.append(record)
        else:
            inconclusive.append(record)

    evaluated = len(correctly_rejected) + len(incorrectly_rejected)
    return {
        "total_recorded": total,
        "evaluated": evaluated,
        "correctly_rejected": len(correctly_rejected),
        "incorrectly_rejected_profitable": len(incorrectly_rejected),
        "inconclusive": len(inconclusive),
        "rejection_precision": round(len(correctly_rejected) / evaluated, 6) if evaluated else None,
        "threshold_pct": float(threshold_pct),
        "horizon_sessions": horizon_sessions,
        "records": {
            "correctly_rejected": correctly_rejected,
            "incorrectly_rejected_profitable": incorrectly_rejected,
            "inconclusive": inconclusive,
        },
    }


__all__ = ["evaluate_no_trade_decisions", "is_no_trade_entry", "NO_TRADE_ACTIONS"]
