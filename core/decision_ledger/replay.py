"""Phase 13 §89 — point-in-time AI replay + deterministic outcome evaluation.

GOLD-002 contract: at historical time T the replay receives ONLY data
available at T (Phase 1 `HistoricalStore.as_of`, §29/§32 — anything without
a qualifying record renders UNAVAILABLE, never reconstructed from future
data). The replayed decision is recorded as a REPLAY ledger entry linked to
the original via `replay_of`, and can NEVER reach the execution broker
(the execution runtime only consumes SIGNAL/PRE_EARNINGS order intents;
replay entries carry no order linkage and are excluded from order paths).

Outcome evaluation (§89 "compare against the future result afterward") is
deterministic arithmetic over bars strictly AFTER T — appended as new
outcome rows, never overwriting history.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.ai.decision_engine import EvaluationResult, evaluate_trade_decision
from core.data_model.pit import UNAVAILABLE
from core.decision_ledger.store import DecisionLedgerStore
from core.decision_ledger.types import (
    LedgerEntry, LedgerOutcome, OUTCOME_ADVERSE, OUTCOME_FAVORABLE, OUTCOME_FLAT,
    OUTCOME_INCONCLUSIVE, SOURCE_REPLAY, bounded_text, json_digest,
)

logger = logging.getLogger(__name__)

HORIZON_SESSIONS = {"SHORT_TERM": 1, "SWING": 5, "MEDIUM_TERM": 10}
DEFAULT_HORIZON_SESSIONS = 5
DEFAULT_MOVE_THRESHOLD_PCT = 1.0

OHLCV_ENTITY = "ohlcv"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def horizon_sessions(horizon: str) -> int:
    return HORIZON_SESSIONS.get(str(horizon or "").upper(), DEFAULT_HORIZON_SESSIONS)


def build_pit_snapshot(
    ticker: str,
    as_of: datetime,
    *,
    store: DecisionLedgerStore,
    historical=None,
    entity_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """§89 inputs at T — per-entity `as_of` lookups with availability proofs.

    Every entity records the `available_from` of the record used (or the
    UNAVAILABLE marker), so tests can prove a T+1 bar is absent from a T
    snapshot without trusting the payload alone.
    """
    historical = historical if historical is not None else _default_historical()
    snapshot: Dict[str, Any] = {
        "ticker": str(ticker or "").upper(),
        "as_of": as_of.isoformat(),
        "entities": {},
        "inputs_sha256": "",
    }
    for entity_type in entity_types or [OHLCV_ENTITY]:
        record = historical.as_of(entity_type, snapshot["ticker"], as_of) \
            if hasattr(historical, "as_of") else UNAVAILABLE
        if record is UNAVAILABLE or record is None:
            snapshot["entities"][entity_type] = {"available": False, "value": "UNAVAILABLE"}
        else:
            payload = record if isinstance(record, dict) else getattr(record, "to_dict", lambda: record)()
            snapshot["entities"][entity_type] = {
                "available": True,
                "available_from": str(
                    (payload.get("pit") or {}).get("available_from")
                    if isinstance(payload, dict) else getattr(record, "available_from", "")
                ),
                "value": payload,
            }
    snapshot["inputs_sha256"] = json_digest(snapshot["entities"])
    return snapshot


def _default_historical():
    from core.data_model.store import get_historical_store

    return get_historical_store()


def bars_available_at(bars: List[Dict[str, Any]], as_of: datetime, *, time_key: str = "available_from") -> List[Dict[str, Any]]:
    """Bars whose availability timestamp is <= as_of (naive ISO strings treated as UTC)."""
    eligible = []
    for bar in bars or []:
        raw = bar.get(time_key) or bar.get("date") or bar.get("timestamp")
        if raw is None:
            continue
        try:
            stamp = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp <= as_of:
            eligible.append(bar)
    return eligible


def evaluate_decision_outcome(
    entry: LedgerEntry,
    bars: List[Dict[str, Any]],
    *,
    as_of: Optional[datetime] = None,
    horizon_sessions: Optional[int] = None,
    threshold_pct: float = DEFAULT_MOVE_THRESHOLD_PCT,
    close_key: str = "close",
) -> LedgerOutcome:
    """Deterministic §89 afterward-facts for one entry.

    Uses bars strictly AFTER the decision time only: the baseline close is
    the last close at-or-before T, the result close is `horizon_sessions`
    bars later. Insufficient data → INCONCLUSIVE (§32 spirit: never guessed).
    """
    anchor = as_of or _parse_ts(entry.pit_as_of or entry.created_at)
    sessions = int(horizon_sessions or HORIZON_SESSIONS.get(entry.horizon.upper(), DEFAULT_HORIZON_SESSIONS))
    prior = bars_available_at(bars, anchor)
    future = [bar for bar in (bars or []) if _bar_ts(bar) is not None and _bar_ts(bar) > anchor]
    future.sort(key=lambda bar: _bar_ts(bar))
    baseline_close = _close_of(prior[-1], close_key) if prior else None
    if baseline_close in (None, 0) or len(future) < sessions:
        return LedgerOutcome(
            decision_id=entry.decision_id, evaluated_at=_utc_now_iso(),
            outcome=OUTCOME_INCONCLUSIVE, basis=f"forward_return_{sessions}_sessions",
            bars_used=len(future),
            detail={"reason": "insufficient_post_decision_data",
                    "baseline_close": baseline_close, "future_bars": len(future)},
        )
    result_bar = future[sessions - 1]
    result_close = _close_of(result_bar, close_key)
    if result_close in (None, 0):
        return LedgerOutcome(
            decision_id=entry.decision_id, evaluated_at=_utc_now_iso(),
            outcome=OUTCOME_INCONCLUSIVE, basis=f"forward_return_{sessions}_sessions",
            bars_used=len(future),
            detail={"reason": "invalid_result_close"},
        )
    forward_return_pct = (float(result_close) - float(baseline_close)) / abs(float(baseline_close)) * 100.0
    long_biased = entry.action in ("OPEN_LONG", "OPEN", "LONG", "BUY")
    short_biased = entry.action in ("OPEN_SHORT", "SHORT", "SELL")
    raw_outcome = _classify(forward_return_pct, threshold_pct)
    if long_biased or short_biased:
        effective = raw_outcome if long_biased else _invert(raw_outcome)
    else:
        # Directionless decisions (WATCH/NO_TRADE/HOLD) are judged on magnitude
        # only — this is the §90 rejection-quality input, not a P/L claim.
        effective = OUTCOME_FLAT if abs(forward_return_pct) < threshold_pct else raw_outcome
    return LedgerOutcome(
        decision_id=entry.decision_id, evaluated_at=_utc_now_iso(),
        outcome=effective, basis=f"forward_return_{sessions}_sessions",
        forward_return_pct=round(forward_return_pct, 6), bars_used=len(future),
        detail={
            "baseline_close": float(baseline_close),
            "result_close": float(result_close),
            "result_bar_time": str(result_bar.get("available_from") or result_bar.get("date") or ""),
            "threshold_pct": float(threshold_pct),
        },
    )


def _classify(move_pct: float, threshold_pct: float) -> str:
    if move_pct >= threshold_pct:
        return OUTCOME_FAVORABLE
    if move_pct <= -threshold_pct:
        return OUTCOME_ADVERSE
    return OUTCOME_FLAT


def _invert(outcome: str) -> str:
    return {OUTCOME_FAVORABLE: OUTCOME_ADVERSE, OUTCOME_ADVERSE: OUTCOME_FAVORABLE}.get(outcome, outcome)


def _parse_ts(raw: str) -> datetime:
    stamp = datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _bar_ts(bar: Dict[str, Any]) -> Optional[datetime]:
    raw = bar.get("available_from") or bar.get("date") or bar.get("timestamp")
    if raw is None:
        return None
    try:
        return _parse_ts(raw)
    except ValueError:
        return None


def _close_of(bar: Dict[str, Any], close_key: str) -> Optional[float]:
    try:
        value = bar.get(close_key)
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def run_ai_replay(
    decision_id: str,
    *,
    store: DecisionLedgerStore,
    config: Dict[str, Any],
    services: Optional[Dict[str, Any]] = None,
    historical=None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """§89 — re-run the decision at its original point in time.

    Rebuilds the engine signal from the recorded entry's bounded evidence +
    the PIT snapshot, re-invokes evaluate_trade_decision (same prompt
    contract), and appends a REPLAY entry. The replay result is an audit
    artifact: it is NEVER sent to the execution broker and cannot create an
    order (§40/§42 still apply; replay entries carry no order linkage).
    """
    entry = store.get_entry(decision_id)
    if entry is None:
        raise KeyError(f"unknown decision_id: {decision_id}")
    anchor = _parse_ts(entry.pit_as_of or entry.created_at)
    snapshot = build_pit_snapshot(
        entry.ticker, anchor, store=store, historical=historical,
    )
    signal = {
        "ticker": entry.ticker,
        "entry": (entry.data_existed or {}).get("entry"),
        "signal_score": (entry.data_existed or {}).get("signal_score"),
        "horizon": entry.horizon,
        "strategy_type": entry.strategy,
        "autonomous_source": (entry.data_existed or {}).get("autonomous_source"),
        "data_quality": (entry.data_existed or {}).get("data_quality"),
        "thesis": f"AI replay of {entry.decision_id} at {anchor.isoformat()}",
        "replay_of": entry.decision_id,
    }
    replay_services = dict(services or {})
    if model:
        replay_services["primary_model"] = model
    result = evaluate_trade_decision(signal, None, config=config, services=replay_services or None)

    replay_entry = LedgerEntry(
        decision_id=f"dec_replay_{entry.decision_id}",
        created_at=_utc_now_iso(),
        ticker=entry.ticker,
        action=str(getattr(result.decision.action, "value", result.decision.action)),
        horizon=entry.horizon,
        source=SOURCE_REPLAY,
        status="replayed",
        strategy=entry.strategy,
        ai={
            "available": bool(result.review.get("available")),
            "provider": str(result.review.get("provider") or ""),
            "model": result.model,
            "prompt_version": result.prompt_version,
            "prompt_sha256": json_digest(result.prompt or ""),
            "prompt_text": bounded_text(result.prompt),
        },
        ai_proposal=_proposal_dict(result),
        validation=({"ok": result.validation.ok, "stage": str(result.validation.stage), "reason": result.validation.reason}
                    if result.validation is not None else {"available": False}),
        critic=({"available": True, "veto": result.critic.veto, "categories": list(result.critic.categories)}
                if result.critic is not None else {"available": False, "reason": "critic_not_run"}),
        risk={"note": "replay carries no order risk — audit artifact only"},
        capital_allowed={"allowed": False, "reason": "replay_never_executes"},
        executed={"order_intent_id": None, "state": "NONE", "note": "replay cannot reach the broker"},
        data_existed={
            "signal_id": (entry.data_existed or {}).get("signal_id", ""),
            "pit_snapshot": {k: v for k, v in snapshot["entities"].items()},
            "inputs_sha256": snapshot["inputs_sha256"],
            "captured_at": snapshot["as_of"],
        },
        replay_of=entry.decision_id,
        pit_as_of=anchor.isoformat(),
    )
    stored, created = store.append_entry(replay_entry)
    return {
        "replay_decision_id": stored.decision_id,
        "original_decision_id": entry.decision_id,
        "created": created,
        "action": stored.action,
        "ai_available": bool(result.review.get("available")),
        "model": result.model,
        "prompt_version": result.prompt_version,
        "inputs_sha256": snapshot["inputs_sha256"],
        "pit_as_of": anchor.isoformat(),
    }


def _proposal_dict(result: EvaluationResult) -> Dict[str, Any]:
    from core.decision_ledger.types import decision_projection

    return decision_projection(result.decision)


__all__ = [
    "build_pit_snapshot", "run_ai_replay", "evaluate_decision_outcome",
    "bars_available_at", "horizon_sessions", "HORIZON_SESSIONS",
    "DEFAULT_MOVE_THRESHOLD_PCT", "OHLCV_ENTITY",
]
