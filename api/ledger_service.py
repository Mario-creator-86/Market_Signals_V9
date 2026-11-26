"""Phase 13 API service — Decision Ledger, no-trade evaluation, Model Lab, replay.

Bounded, read-mostly projections over `core.decision_ledger`. Bars for §90/§89
evaluation come from the Phase 1 point-in-time historical store; a ticker
without stored bars stays honestly INCONCLUSIVE — nothing is imputed.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

from core.decision_ledger.model_lab import MODEL_LAB_ROLES, ModelLab, validate_role
from core.decision_ledger.no_trade_eval import evaluate_no_trade_decisions
from core.decision_ledger.replay import (
    DEFAULT_MOVE_THRESHOLD_PCT, run_ai_replay,
)
from core.decision_ledger.store import get_decision_ledger_store

logger = logging.getLogger(__name__)


class LedgerServiceError(ValueError):
    """Bounded, caller-facing error (422-class)."""


def _bars_from_historical(ticker: str) -> List[Dict[str, Any]]:
    """Read OHLCV bars from the Phase 1 PIT store (never a live provider)."""
    try:
        from core.data_model.store import get_historical_store

        store = get_historical_store()
        records = store.query("ohlcv", ticker)
    except Exception:
        logger.exception("historical bar read failed for %s", ticker)
        return []
    bars = []
    for record in records or []:
        payload = record if isinstance(record, dict) else getattr(record, "to_dict", lambda: record)()
        if isinstance(payload, dict):
            bars.append(payload)
    return bars


# The PIT store starts empty in a fresh deployment, which left every §90
# evaluation permanently INCONCLUSIVE. Backfill real daily bars via the shared
# stock data client (bounded per call) and persist them into the PIT store so
# later evaluations are local and deterministic.
_EOD_BACKFILL_MAX_TICKERS_PER_CALL = 20

# Tickers the EOD provider has no data for (e.g. crypto symbols Yahoo does not
# list) were refetched on every call, ~3.5s each, making the endpoint take
# over a minute. Remember empty backfills for a while instead.
_EOD_BACKFILL_EMPTY_TTL_SECONDS = 6 * 3600
_eod_backfill_empty_until: Dict[str, float] = {}


def _bars_from_eod_backfill(ticker: str) -> List[Dict[str, Any]]:
    try:
        from core.stock_data_client import get_stock_data_client

        frame = get_stock_data_client().get_historical_eod_light(ticker)
    except Exception:
        logger.warning("EOD backfill fetch failed for %s", ticker)
        return []
    if frame is None or getattr(frame, "empty", True):
        return []
    bars: List[Dict[str, Any]] = []
    try:
        import pandas as pd

        for idx, row in frame.iterrows():
            close = row.get("Close")
            if close is None or pd.isna(close):
                continue
            ts = idx.isoformat() if hasattr(idx, "isoformat") else str(idx)
            bars.append({"symbol": ticker, "date": ts, "close": float(close)})
    except Exception:
        logger.warning("EOD backfill parse failed for %s", ticker)
        return []
    if not bars:
        return []
    try:
        from core.data_model.store import get_historical_store

        store = get_historical_store()
        for bar in bars:
            try:
                store.append(
                    dict(bar),
                    entity_type="ohlcv",
                    symbol=ticker,
                    provider="yahoo_eod",
                    available_from=bar["date"],
                )
            except Exception:
                continue  # transient lock/unique conflict: persistence is best-effort
    except Exception:
        pass
    return bars


def _bars_for_ticker_with_backfill(ticker: str, backfill_budget: Dict[str, int]) -> List[Dict[str, Any]]:
    bars = _bars_from_historical(ticker)
    if len(bars) >= 10:
        return bars
    if backfill_budget["remaining"] <= 0:
        return bars
    if _eod_backfill_empty_until.get(ticker, 0.0) > time.time():
        return bars
    backfill_budget["remaining"] -= 1
    backfill_budget["used"] += 1
    fetched = _bars_from_eod_backfill(ticker)
    if not fetched:
        _eod_backfill_empty_until[ticker] = time.time() + _EOD_BACKFILL_EMPTY_TTL_SECONDS
    return fetched if len(fetched) > len(bars) else bars


def list_ledger_decisions(
    config: Any,
    *,
    limit: int = 50,
    ticker: Optional[str] = None,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    store = get_decision_ledger_store(getattr(config, "config", config) if config is not None else None)
    entries = store.list_entries(limit=limit, ticker=ticker, source=source)
    return {
        "decisions": [entry.eleven_questions() for entry in entries],
        "count": len(entries),
        "limit": max(1, min(500, int(limit or 50))),
        "total_entries": store.count_entries(),
        "replay_entries": store.count_entries(source="REPLAY"),
        "coverage": "PERSISTENT_LEDGER" if entries else "EMPTY",
    }


def get_ledger_decision(config: Any, decision_id: str) -> Dict[str, Any]:
    identifier = str(decision_id or "").strip()
    if not identifier:
        raise LedgerServiceError("decision_id is required")
    store = get_decision_ledger_store(getattr(config, "config", config) if config is not None else None)
    entry = store.get_entry(identifier)
    if entry is None:
        raise KeyError(identifier)
    return {
        "entry": entry.to_dict(),
        "eleven_questions": entry.eleven_questions(),
        "outcomes": [outcome.to_dict() for outcome in store.list_outcomes(identifier)],
    }


def get_no_trade_evaluation(
    config: Any,
    *,
    threshold_pct: float = DEFAULT_MOVE_THRESHOLD_PCT,
    horizon_sessions: Optional[int] = None,
    limit: int = 200,
    backfill: Optional[bool] = None,
) -> Dict[str, Any]:
    store = get_decision_ledger_store(getattr(config, "config", config) if config is not None else None)
    entries = store.list_entries(limit=max(1, min(500, int(limit or 200))))
    tickers = sorted({entry.ticker for entry in entries if entry.ticker})
    backfill_allowed = bool(backfill) if backfill is not None else (
        os.getenv("TEST_MODE", "").strip() != "1"
    )
    backfill_budget = {
        "remaining": _EOD_BACKFILL_MAX_TICKERS_PER_CALL if backfill_allowed else 0,
        "used": 0,
    }
    bars_by_ticker = {
        ticker: _bars_for_ticker_with_backfill(ticker, backfill_budget)
        for ticker in tickers
    }
    report = evaluate_no_trade_decisions(
        entries, bars_by_ticker, threshold_pct=threshold_pct, horizon_sessions=horizon_sessions,
    )
    report["bars_source"] = (
        "point_in_time_historical_store+eod_backfill"
        if backfill_allowed else "point_in_time_historical_store"
    )
    report["tickers_with_bars"] = sum(1 for bars in bars_by_ticker.values() if bars)
    report["bars_backfilled_this_call"] = backfill_budget["used"]
    report["bars_backfill_budget"] = _EOD_BACKFILL_MAX_TICKERS_PER_CALL if backfill_allowed else 0
    report["acceptance_performance"] = _acceptance_performance(entries)
    return report


def _acceptance_performance(entries: List[Any]) -> Dict[str, Any]:
    """Realized P&L of ACCEPTED decisions, from their position_close outcomes.

    This is the other half of the decision scorecard: rejection quality comes
    from the no-trade evaluation above, acceptance quality from positions the
    engine closed and wrote back as `position_close` afterward facts
    (core.capital_manager._record_closed_position_afterward)."""
    by_strategy: Dict[str, Dict[str, Any]] = {}
    total = wins = 0
    sum_pnl = 0.0
    excluded_count = 0
    excluded_pnl_usd = 0.0
    for entry in entries:
        afterward = getattr(entry, "afterward", None)
        if not isinstance(afterward, dict) or str(afterward.get("basis") or "") != "position_close":
            continue
        pnl = afterward.get("forward_return_pct")
        if pnl is None:
            continue
        detail = afterward.get("detail") if isinstance(afterward.get("detail"), dict) else {}
        if detail.get("evidence_excluded") is True:
            # Exit delayed by an outage: a real paper result, not a measurement
            # of the decision. Left out of the scorecard and reported below.
            excluded_count += 1
            try:
                excluded_pnl_usd += float(detail.get("pnl_usd") or 0.0)
            except (TypeError, ValueError):
                pass
            continue
        strategy = str(entry.strategy or "unknown")
        bucket = by_strategy.setdefault(strategy, {"n": 0, "wins": 0, "sum_pnl_pct": 0.0})
        bucket["n"] += 1
        bucket["sum_pnl_pct"] += float(pnl)
        if float(pnl) > 0:
            bucket["wins"] += 1
            wins += 1
        total += 1
        sum_pnl += float(pnl)
    for bucket in by_strategy.values():
        n = bucket.pop("n")
        bucket["win_rate_pct"] = round(bucket.pop("wins") / n * 100.0, 1) if n else None
        bucket["avg_pnl_pct"] = round(bucket.pop("sum_pnl_pct") / n, 3) if n else None
        bucket["n"] = n
    return {
        "evaluated": total,
        "wins": wins,
        "win_rate_pct": round(wins / total * 100.0, 1) if total else None,
        "avg_pnl_pct": round(sum_pnl / total, 3) if total else None,
        "by_strategy": by_strategy,
        "excluded_trades": {
            "count": excluded_count,
            "total_pnl_usd": round(excluded_pnl_usd, 4),
            "net_pnl": round(excluded_pnl_usd, 4),
            "reasons": {"exit_delayed_by_outage": excluded_count} if excluded_count else {},
        },
    }


def get_model_lab(config: Any, *, role: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
    if role is not None:
        try:
            role = validate_role(role)
        except ValueError as exc:
            raise LedgerServiceError(str(exc)) from exc
    store = get_decision_ledger_store(getattr(config, "config", config) if config is not None else None)
    lab = ModelLab(store)
    summary = lab.role_summary(role=role, limit=limit)
    runs = store.list_model_lab_runs(limit=limit, role=role)
    return {
        "roles_available": list(MODEL_LAB_ROLES),
        "summary": summary,
        "runs": [
            {
                "run_id": run.get("run_id"),
                "role": run.get("role"),
                "created_at": run.get("created_at"),
                "inputs_sha256": run.get("inputs_sha256"),
                "decision_distribution": run.get("decision_distribution"),
                "inter_model_agreement": run.get("inter_model_agreement"),
                "results": [
                    {
                        "provider": result.get("provider"),
                        "model": result.get("model"),
                        "ai_available": result.get("ai_available"),
                        "valid": result.get("valid"),
                        "action": result.get("action"),
                        "validation_stage": result.get("validation_stage"),
                    }
                    for result in run.get("results") or []
                ],
            }
            for run in runs
        ],
        "note": "Per-role comparisons only — no global AI score (spec §88).",
    }


def replay_decision(
    config: Any,
    decision_id: str,
    *,
    model: Optional[str] = None,
    services: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """§89 replay — an audit artifact; it can never place an order."""
    identifier = str(decision_id or "").strip()
    if not identifier:
        raise LedgerServiceError("decision_id is required")
    cfg = getattr(config, "config", config) if config is not None else None
    store = get_decision_ledger_store(cfg)
    return run_ai_replay(identifier, store=store, config=cfg or {}, services=services, model=model)


__all__ = [
    "LedgerServiceError", "list_ledger_decisions", "get_ledger_decision",
    "get_no_trade_evaluation", "get_model_lab", "replay_decision",
]
