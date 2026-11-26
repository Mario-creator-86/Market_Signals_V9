"""Bounded, evidence-first payload for the Phase 12 AI Trading Cockpit.

The cockpit is a read model.  It does not start workers, call brokers, refresh
providers, select trading values, or mutate owner configuration.  Missing
evidence remains ``None``/EMPTY/UNVERIFIED instead of becoming a plausible
looking fallback value.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from api.actions_service import get_strategy_performance
from api.agent_runtime_service import snapshot_agent_runtimes
from api.agents_service import CONTROL_PLANE_AGENTS, build_control_plane_payload
from api.ai_config_service import list_ai_providers
from api.dashboard_service import get_dashboard_payload
from api.engine_service import is_engine_running
from api.execution_service import get_execution_control
from api.state_service import get_capital_state, get_shared_config
from core.config import DATA_DIR
from core.sqlite_store import DB_PATH as _SQLITE_DB_PATH
from core.autonomous_candidates import assess_pre_earnings_candidate
from core.data_quality import get_health_dashboard
from core.decision_ledger.types import decision_critic_evidence
from core.market_hours import us_equity_market_status
from core.observability import snapshot_agent_status
from core.strategy_registry import StrategyRegistry, StrategyRegistryError


logger = logging.getLogger("MarketSignalsCockpit")

SCHEMA_VERSION = 1
_EARNINGS_DISPLAY_LIMIT = 20
_LEADERBOARD_MIN_SAMPLE = 10  # same floor as research_factory.weakness DEFAULT_THRESHOLDS["min_sample"]
ASYNC_STATES = frozenset({"LOADING", "SUCCESS", "EMPTY", "ERROR"})
_DB_PATH = Path(_SQLITE_DB_PATH)  # one rule for the database path: core.sqlite_store
_PRE_EARNINGS_PATH = DATA_DIR / "pre_earnings.json"


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def checked_iso(moment: Optional[datetime] = None) -> str:
    return (moment or datetime.now(timezone.utc)).isoformat()


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _integer(value: Any) -> Optional[int]:
    number = _finite(value)
    return int(number) if number is not None else None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(value: Any, now: datetime) -> Optional[int]:
    parsed = _parse_dt(value)
    if parsed is None:
        return None
    return max(0, int((now - parsed).total_seconds()))


def _pre_earnings_freshness(
    pre_earnings: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    status = _mapping(pre_earnings.get("status"))
    inputs = _mapping(status.get("last_inputs_used"))
    updated_at = status.get("last_success_time") or status.get("last_run_time")
    age_seconds = _age_seconds(updated_at, now)
    refresh_seconds = max(30, _integer(pre_earnings.get("refresh_seconds")) or 600)
    stale = age_seconds is None or age_seconds > refresh_seconds * 2
    return {
        "state": "UNKNOWN" if age_seconds is None else ("STALE" if stale else "FRESH"),
        "updated_at": updated_at,
        "age_seconds": age_seconds,
        "refresh_seconds": refresh_seconds,
        "news_age_seconds": _integer(inputs.get("news_cache_age_seconds")),
        "calendar_age_seconds": _integer(inputs.get("earnings_cache_age_seconds")),
        "last_error": status.get("last_error"),
    }


def _human_age(seconds: Any) -> str:
    value = _finite(seconds)
    if value is None:
        return "an unknown time"
    if value < 90:
        return f"{int(max(0, value))} seconds"
    if value < 5400:
        return f"{int(round(value / 60))} minutes"
    if value < 172800:
        return f"{int(round(value / 3600))} hours"
    return f"{int(round(value / 86400))} days"


def _watchlist_earnings(dashboard: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Watchlist-calendar earnings within 7 days (the source of the
    "Earnings in Nd" risk rows) so the Pre-Earnings card can reference the
    same events instead of contradicting them."""
    rows: list[dict[str, Any]] = []
    for item in _rows(dashboard.get("dashboard_earnings_7d")):
        ticker = str(item.get("ticker") or "").upper().strip()
        days = _integer(item.get("earnings_days"))
        if not ticker or days is None or days < 0:
            continue
        rows.append({
            "ticker": ticker,
            "days": days,
            "date": item.get("earnings_date") if item.get("earnings_date") not in (None, "", "N/A") else None,
            "session": str(item.get("earnings_when_desc") or item.get("earnings_when") or "").strip() or None,
        })
    rows.sort(key=lambda row: (row["days"], row["ticker"]))
    return rows[:10]


def _upcoming_earnings_note(freshness: Mapping[str, Any], watchlist_rows: list[dict[str, Any]]) -> str:
    """Empty-state text that tells the truth about WHY the matrix is empty."""
    state = str(freshness.get("state") or "UNKNOWN").upper()
    if state == "STALE":
        note = (
            f"The Pre-Earnings scan is out of date: its last successful refresh was "
            f"{_human_age(freshness.get('age_seconds'))} ago (expected every "
            f"{_human_age(freshness.get('refresh_seconds'))}), and every event it held has since passed. "
            "Upcoming events appear after the next scan completes; use Refresh to start one."
        )
    elif state == "UNKNOWN":
        note = "No Pre-Earnings scan has completed yet, so upcoming events cannot be listed. Use Refresh to start one."
    else:
        note = "The latest Pre-Earnings scan found no upcoming earnings event."
    if watchlist_rows:
        listed = ", ".join(f"{row['ticker']} ({row['days']}d)" for row in watchlist_rows[:6])
        note += f" The watchlist calendar shows earnings within 7 days for {listed} (see Current risks)."
    return note


def _compact_decisions_section(section: Mapping[str, Any], limit: int = 12) -> dict[str, Any]:
    """Overview copy of the ledger: headline fields only.

    The full eleven-question rows (prompt, validation, risk checks) live once
    under the top-level ``decision_ledger`` key; repeating them here doubled
    the payload for a card that shows six one-line rows.
    """
    keys = ("decision_id", "event_id", "ticker", "timestamp", "event_type", "status",
            "decision_status", "what_happened", "strategy", "position_id")
    compact = {key: value for key, value in section.items() if key != "items"}
    compact["items"] = [
        {key: row.get(key) for key in keys if row.get(key) is not None}
        for row in _rows(section.get("items"))[:limit]
    ]
    return compact


def _section(
    items: Iterable[Mapping[str, Any]] = (),
    *,
    error: Optional[str] = None,
    note: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    clean_items = [dict(item) for item in items if isinstance(item, Mapping)]
    state = "ERROR" if error else ("SUCCESS" if clean_items else "EMPTY")
    payload: dict[str, Any] = {"state": state, "items": clean_items, "count": len(clean_items)}
    if error:
        payload["message"] = error
    elif note and not clean_items:
        payload["message"] = note
    if extra:
        payload.update(dict(extra))
    return payload


def _service(
    service_id: str,
    label: str,
    status: str,
    *,
    message: str,
    checked_at: Optional[str] = None,
    evidence: Optional[Mapping[str, Any]] = None,
    action: Optional[str] = None,
) -> dict[str, Any]:
    normalized = str(status or "UNVERIFIED").strip().upper()
    allowed = {"HEALTHY", "DEGRADED", "UNHEALTHY", "UNVERIFIED", "UNAVAILABLE", "DISABLED", "NOT_CONFIGURED"}
    if normalized not in allowed:
        normalized = "UNVERIFIED"
    payload = {
        "id": service_id,
        "label": label,
        "status": normalized,
        "message": str(message or "No check result is available."),
        "checked_at": checked_at,
        "evidence": dict(evidence or {}),
    }
    # "What should I do about it?" - attached only to checks that need attention.
    if action and normalized in {"UNHEALTHY", "DEGRADED", "UNVERIFIED", "UNAVAILABLE"}:
        payload["action"] = str(action)
    return payload


def _safe_source(
    label: str,
    loader: Callable[[], Any],
    fallback: Any,
) -> tuple[Any, Optional[str]]:
    try:
        return loader(), None
    except Exception:
        logger.error("Cockpit source failed: %s", label, exc_info=True)
        return fallback, f"{label} is unavailable."


def _read_pre_earnings_cache() -> dict[str, Any]:
    payload: dict[str, Any] = {}
    try:
        from agents.pre_earnings_agent import get_pre_earnings_agent_if_ready

        agent = get_pre_earnings_agent_if_ready()
        if agent is not None:
            state = agent.get_state()
            payload = dict(state) if isinstance(state, Mapping) else {}
    except Exception:
        logger.debug("Pre-Earnings in-memory snapshot unavailable", exc_info=True)

    if not payload:
        try:
            raw = json.loads(_PRE_EARNINGS_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return {}
        payload = dict(raw) if isinstance(raw, Mapping) else {}
    if not payload:
        return {}
    # API and engine are separate processes. Apply this final cache-only truth
    # gate to both the in-memory and file-backed paths; other API routes can
    # initialize a local Pre-Earnings reader before the Cockpit is requested.
    try:
        from agents.pre_earnings_agent import PreEarningsAgent, detect_already_reported_symbols

        config = get_shared_config()
        root = _mapping(getattr(config, "config", {}))
        pre_cfg = _mapping(root.get("pre_earnings"))
        current = datetime.now(timezone.utc)
        active_items = _rows(payload.get("items"))
        # The worker may have persisted a snapshot milliseconds before a result
        # release entered the same snapshot's news evidence. Re-run the bounded,
        # cache-only result detector over embedded headlines so an obvious
        # "reported results" item cannot remain active until the next 10m cycle.
        news_index: dict[str, list[dict[str, Any]]] = {}
        earnings_index: dict[str, list[dict[str, Any]]] = {}
        for item in active_items:
            symbol = str(item.get("symbol") or "").upper().strip()
            if not symbol:
                continue
            news_index[symbol] = _rows(item.get("top_news"))
            earnings_index[symbol] = [{"symbol": symbol, "date": item.get("earnings_time_utc")}]
        detected = detect_already_reported_symbols(news_index, earnings_index, now=current)
        for item in active_items:
            symbol = str(item.get("symbol") or "").upper().strip()
            evidence = _mapping(detected.get(symbol))
            if not evidence:
                continue
            flags = _mapping(item.get("flags"))
            flags["already_reported"] = True
            flags["already_reported_evidence"] = [
                str(value) for value in (evidence.get("headlines") or []) if str(value).strip()
            ][:5]
            item["flags"] = flags
        survivors, reported = PreEarningsAgent._filter_reported_items(active_items, pre_cfg, current)
        payload["items"] = survivors
        existing_reported = _rows(payload.get("reported_today"))
        seen = {
            (str(item.get("symbol") or "").upper(), str(item.get("earnings_time_utc") or ""))
            for item in existing_reported
        }
        for item in reported:
            key = (str(item.get("symbol") or "").upper(), str(item.get("earnings_time_utc") or ""))
            if key not in seen:
                existing_reported.append(item)
                seen.add(key)
        payload["reported_today"] = existing_reported
    except Exception:
        logger.warning("Cockpit could not apply the persisted Pre-Earnings reported filter", exc_info=True)
    return payload


def _database_check(path: Path = _DB_PATH) -> dict[str, Any]:
    checked_at = datetime.now(timezone.utc).isoformat()
    if not path.exists():
        return _service(
            "database", "Database", "UNAVAILABLE", checked_at=checked_at,
            message="SQLite database file is missing.",
            evidence={"path": str(path)},
        )
    try:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2.0) as connection:
            connection.execute("SELECT 1").fetchone()
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        return _service(
            "database", "Database", "HEALTHY", checked_at=checked_at,
            message="Read-only SQLite check passed.",
            evidence={
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "journal_mode": journal_mode[0] if journal_mode else None,
            },
        )
    except Exception:
        logger.error("Cockpit database check failed", exc_info=True)
        return _service(
            "database", "Database", "UNHEALTHY", checked_at=checked_at,
            message="Read-only SQLite check failed.",
            evidence={"path": str(path)},
        )


def _pre_earnings_prefilter(
    item: Mapping[str, Any],
    execution_cfg: Optional[Mapping[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    return assess_pre_earnings_candidate(
        dict(item),
        0.0,
        dict(execution_cfg or {}),
        require_price=False,
        require_entry_window=True,
        now=now,
    )


def _apply_pre_earnings_prefilter(row: dict[str, Any], prefilter: Mapping[str, Any]) -> None:
    row["autonomous_prefilter"] = dict(prefilter)
    for key in ("ranking_eligible", "executable_now", "eligibility_scope", "entry_window", "window_open", "window_reason"):
        row[key] = prefilter.get(key)


def _pre_earnings_row(
    item: Mapping[str, Any],
    execution_cfg: Optional[Mapping[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
    include_prefilter: bool = True,
) -> dict[str, Any]:
    row = dict(item)
    components = _mapping(row.get("components"))
    flags = _mapping(row.get("flags"))
    ai = _mapping(row.get("ai_decision")) or _mapping(row.get("ollama"))
    blockers = [str(value) for value in (row.get("recommendation_blockers") or []) if str(value).strip()]
    bias_score = _finite(row.get("final_direction_bias_score"))
    if bias_score is None:
        bias_score = _finite(row.get("direction_bias_score"))
    score = _finite(row.get("trader_score"))
    score_kind = "TRADER_SCORE_NOT_PROBABILITY"
    if score is None:
        score = _finite(row.get("pre_earnings_score"))
        score_kind = "PRE_EARNINGS_SCORE_NOT_PROBABILITY"
    expected_move = _finite(row.get("range_proxy_pct"))
    if expected_move is None:
        # Legacy producer alias is a price-range proxy, not an options forecast.
        expected_move = _finite(row.get("expected_move_pct"))
    historical_move = _finite(row.get("historical_median_move_pct"))
    if historical_move is None:
        historical_move = _finite(row.get("avg_move_historical"))

    iv_rv_ratio = _finite(row.get("iv_rv_ratio"))
    if iv_rv_ratio is None:
        options = _mapping(row.get("options_snapshot"))
        iv_rv_ratio = _finite(options.get("iv_rv_ratio"))

    eligible = row.get("recommendation_eligible") is True
    direction = str(row.get("direction_label") or "INSUFFICIENT_DATA").upper()
    strategy = "PRE_EARNINGS_DIRECTIONAL" if eligible and direction in {"LONG", "SHORT"} else None
    if blockers:
        recommendation_status = "BLOCKED"
    elif eligible:
        recommendation_status = "ELIGIBLE"
    else:
        recommendation_status = "WATCH"
    # The autonomous prefilter is evaluated only for rows that are displayed
    # (the matrix shows 20 of several hundred candidates); ranking, counts and
    # the opportunity list never read it.
    prefilter = _pre_earnings_prefilter(row, execution_cfg, now=now) if include_prefilter else {}

    return {
        "ticker": str(row.get("symbol") or "").upper(),
        "earnings_date": row.get("earnings_time_utc"),
        "session": str(row.get("session") or "UNKNOWN").upper(),
        "event_status": str(row.get("event_status") or "UNKNOWN").upper(),
        "event_verified": not bool(flags.get("event_unverified", False)),
        "event_source": row.get("event_source"),
        "event_source_url": row.get("event_source_url"),
        "event_verified_at": row.get("event_verified_at"),
        "score": score,
        "score_kind": score_kind if score is not None else None,
        "data_confidence": row.get("data_confidence"),
        "coverage_score": _finite(row.get("coverage_score")),
        "expected_move_pct": expected_move,
        "expected_move_source": row.get("range_proxy_source"),
        "range_proxy_pct": expected_move,
        "range_proxy_source": row.get("range_proxy_source"),
        "historical_move_pct": historical_move,
        "historical_sample_size": _integer(row.get("historical_sample_size")),
        "iv_rv_ratio": iv_rv_ratio,
        "iv_rv_state": "AVAILABLE" if iv_rv_ratio is not None else "UNAVAILABLE",
        "bias": direction,
        "bias_score": bias_score,
        "strategy": strategy,
        "ai_decision": ai.get("decision") or ai.get("verdict"),
        "ai_confidence": _finite(ai.get("decision_confidence") if "decision_confidence" in ai else ai.get("confidence")),
        "ai_model": ai.get("model"),
        "ai_generated_at": ai.get("generated_at"),
        # Backward-compatible alias.  This is a recommendation result, not the
        # Risk Agent verdict; the UI uses the explicit field below.
        "risk_status": recommendation_status,
        "recommendation_status": recommendation_status,
        "recommendation_eligible": eligible,
        "blockers": blockers,
        "autonomous_prefilter": prefilter,
        "ranking_eligible": prefilter.get("ranking_eligible"),
        "executable_now": prefilter.get("executable_now"),
        "eligibility_scope": prefilter.get("eligibility_scope"),
        "entry_window": prefilter.get("entry_window"),
        "window_open": prefilter.get("window_open"),
        "window_reason": prefilter.get("window_reason"),
        "why": [str(value) for value in (row.get("why") or []) if str(value).strip()][:8],
        "what_to_watch": [str(value) for value in (row.get("what_to_watch") or []) if str(value).strip()][:8],
        "already_reported": bool(flags.get("already_reported", False)),
    }


def _compact_position(position: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(position)
    explainability = _mapping(row.get("explainability"))
    return {
        "position_id": row.get("position_id"),
        "ticker": str(row.get("ticker") or "").upper(),
        "direction": str(row.get("direction") or "").upper(),
        "status": str(row.get("status") or "UNKNOWN").upper(),
        "strategy": row.get("strategy_type"),
        "notional_usd": _finite(row.get("usd_size")),
        "margin_usd": _finite(row.get("margin_used")),
        "leverage": _finite(row.get("leverage")),
        "entry_price": _finite(row.get("entry_price")),
        "current_price": _finite(row.get("current_price")),
        "unrealized_pnl": _finite(row.get("unrealized_pnl")),
        # Mark-to-market P&L before fees; equity must use this one because the
        # cash balance already had the entry commission taken out.
        "unrealized_pnl_gross": _finite(
            row.get("unrealized_pnl_gross")
            if row.get("unrealized_pnl_gross") is not None
            else row.get("current_pnl")
        ),
        "unrealized_pnl_pct": _finite(row.get("unrealized_pnl_pct")),
        "stop_loss_price": _finite(row.get("stop_loss_price")),
        "take_profit_price": _finite(row.get("take_profit_price")),
        "opened_at": row.get("opened_at"),
        "decision_status": row.get("decision_status"),
        "summary": explainability.get("summary") or row.get("entry_reason"),
    }


_CAPITAL_LEDGER_WINDOW_ROWS = 100  # Capital publishes its most recent 100 ledger rows


def _trading_day_zone() -> tuple[Any, str]:
    """Day boundary used for "today" figures.

    The Risk Agent's daily-loss limit counts closes on the owner's app day
    (``core.config.BUCHAREST_TZ``). The cockpit must use the SAME boundary:
    with a UTC boundary a position closed at 01:30 local time was "yesterday"
    here while Risk already counted it against today's loss limit.
    """
    try:
        from core.config import BUCHAREST_TZ

        return BUCHAREST_TZ, str(getattr(BUCHAREST_TZ, "zone", None) or "Europe/Bucharest")
    except Exception:
        return timezone.utc, "UTC"


def _daily_realized_pnl(history: list[dict[str, Any]], now: datetime) -> tuple[Optional[float], int]:
    total = 0.0
    count = 0
    zone, _zone_name = _trading_day_zone()
    today = now.astimezone(zone).date()
    for entry in history:
        if str(entry.get("type") or "").upper() != "CLOSE_POSITION":
            continue
        timestamp = _parse_dt(entry.get("timestamp"))
        if timestamp is None or timestamp.astimezone(zone).date() != today:
            continue
        metadata = _mapping(entry.get("metadata"))
        pnl = _finite(metadata.get("net_pnl"))
        if pnl is None:
            pnl = _finite(entry.get("amount"))
        if pnl is None:
            continue
        total += pnl
        count += 1
    return (round(total, 6), count) if count else (None, 0)


def _capital_summary(capital: Mapping[str, Any], now: datetime) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    positions = [
        _compact_position(item)
        for item in _rows(capital.get("positions"))
        if str(item.get("status") or "").upper() == "OPEN"
    ]
    positions.sort(
        key=lambda item: (
            _finite(item.get("unrealized_pnl")) is not None,
            _finite(item.get("unrealized_pnl")) or 0.0,
            _finite(item.get("unrealized_pnl_pct")) or 0.0,
        ),
        reverse=True,
    )
    deployable = _finite(capital.get("balance"))
    cash = _finite(capital.get("raw_cash_balance"))
    margin = sum(value for value in (_finite(item.get("margin_usd")) for item in positions) if value is not None)
    unrealized = sum(value for value in (_finite(item.get("unrealized_pnl")) for item in positions) if value is not None)
    notional = sum(value for value in (_finite(item.get("notional_usd")) for item in positions) if value is not None)
    # Same rule as core.capital_manager.resolve_capital_equity, so the cockpit
    # and the Capital tab show one equity figure.
    unrealized_for_equity = sum(
        value
        for value in (
            _finite(item.get("unrealized_pnl_gross"))
            if _finite(item.get("unrealized_pnl_gross")) is not None
            else _finite(item.get("unrealized_pnl"))
            for item in positions
        )
        if value is not None
    )
    equity = (deployable + margin + unrealized_for_equity) if deployable is not None else None
    exposure_pct = (notional / equity * 100.0) if equity is not None and equity > 0 else None
    history_rows = _rows(capital.get("history"))
    daily_pnl, daily_trades = _daily_realized_pnl(history_rows, now)
    day_zone, day_zone_name = _trading_day_zone()
    trading_day = now.astimezone(day_zone).date()
    # Capital publishes a bounded ledger window. If even its OLDEST row is from
    # today, earlier closes of the day may have scrolled out of the window —
    # the sum is then a lower bound, and the UI must say so.
    history_times = [value for value in (_parse_dt(row.get("timestamp")) for row in history_rows) if value is not None]
    daily_window_truncated = bool(
        len(history_rows) >= _CAPITAL_LEDGER_WINDOW_ROWS
        and history_times
        and min(history_times).astimezone(day_zone).date() == trading_day
    )
    critic_decisions = [
        {
            "decision_id": item.get("decision_id"),
            "signal_id": item.get("signal_id"),
            "ticker": str(item.get("ticker") or "").upper(),
            "status": str(item.get("status") or "").upper(),
            "ai_critic": decision_critic_evidence(item),
        }
        for item in _rows(capital.get("decisions"))
        if str(item.get("status") or "").upper() in {"ACCEPTED", "EXECUTED"}
        and decision_critic_evidence(item)
    ]
    return {
        "deployable_cash": deployable,
        "raw_cash": cash,
        "estimated_equity": round(equity, 6) if equity is not None else None,
        "open_margin": round(margin, 6),
        "notional_exposure": round(notional, 6),
        "notional_exposure_pct": round(exposure_pct, 4) if exposure_pct is not None else None,
        "unrealized_pnl": round(unrealized, 6),
        "daily_realized_pnl": daily_pnl,
        "daily_closed_trades": daily_trades,
        "daily_pnl_basis": (
            f"Realized net P/L of positions closed today ({day_zone_name} day, "
            "same boundary as the Risk daily-loss limit)"
        ),
        "daily_pnl_day": trading_day.isoformat(),
        "daily_pnl_timezone": day_zone_name,
        "daily_pnl_window_truncated": daily_window_truncated,
        "open_positions": len(positions),
        "win_rate": _finite(capital.get("win_rate")),
        "closed_trades": _integer(capital.get("total_closed_trades")),
        "ai_critic_decisions": critic_decisions,
    }, positions


def _opportunity_rows(capital: Mapping[str, Any], earnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    opportunity_state = _mapping(capital.get("opportunities"))
    rows: list[dict[str, Any]] = []
    for item in _rows(opportunity_state.get("signals")):
        rows.append({
            "source": "CAPITAL_PIPELINE",
            "ticker": str(item.get("ticker") or "").upper(),
            "direction": str(item.get("direction") or item.get("action") or "").upper(),
            "score": _finite(next((item.get(k) for k in ("score", "signal_score") if item.get(k) is not None), None)),
            "strategy": item.get("strategy_type") or item.get("strategy_id"),
            "status": item.get("decision_status") or "ELIGIBLE",
            "reason": item.get("reason"),
            "ai_critic": decision_critic_evidence(item) or None,
        })
    for item in earnings:
        if item.get("recommendation_eligible") is not True or item.get("already_reported") is True:
            continue
        rows.append({
            "source": "PRE_EARNINGS",
            "ticker": item.get("ticker"),
            "direction": item.get("bias"),
            "score": item.get("score"),
            "score_kind": item.get("score_kind"),
            "strategy": item.get("strategy"),
            "status": item.get("risk_status"),
            "reason": (item.get("why") or [None])[0],
        })
    rows.sort(key=lambda value: (_finite(value.get("score")) is not None, _finite(value.get("score")) or 0.0), reverse=True)
    return rows[:12]


_RECONCILIATION_ACTION = (
    "Open Agents → Risk Manager and press “Reconcile broker”. If the same conflict "
    "returns, open the Capital tab and use “Record orphaned-fill rollback” on the "
    "reconciliation banner (an audit record only; cash and positions are not changed). "
    "New Paper Auto entries stay blocked until then."
)


def _reconciliation_conflict_text(conflict: Mapping[str, Any]) -> str:
    """One operator-readable sentence per paper-ledger conflict code."""
    code = str(conflict.get("code") or "").strip().lower()
    order_id = str(conflict.get("order_intent_id") or "").strip() or "unknown order"
    position_id = str(conflict.get("position_id") or "").strip() or "no position id"
    if code == "filled_order_position_missing":
        return (
            f"Paper order {order_id} is recorded as FILLED, but its position "
            f"({position_id}) does not exist in the Capital ledger."
        )
    if code == "filled_close_position_still_open":
        return f"Close order {order_id} is recorded as FILLED, but position {position_id} is still open."
    if code == "capital_position_order_missing":
        return f"Capital position {position_id} refers to order {order_id}, which is missing from the order ledger."
    if code == "inflight_order_unresolved":
        action = str(conflict.get("action") or "").strip() or "order"
        return f"{action} order {order_id} was left in flight and could not be matched to a Capital position."
    if code == "reconciliation_error":
        return f"Reconciliation could not run: {str(conflict.get('error') or 'unknown error')[:160]}"
    return f"Unclassified conflict {code or 'unknown'} (order {order_id}, position {position_id})."


def _reconciliation_summary(reconciliation: Mapping[str, Any]) -> dict[str, Any]:
    """Status + readable conflict list shared by the risk card and the health tile."""
    status = str(reconciliation.get("status") or "UNVERIFIED").upper()
    conflicts = [dict(item) for item in (reconciliation.get("conflicts") or []) if isinstance(item, Mapping)]
    details = [_reconciliation_conflict_text(item) for item in conflicts[:5]]
    healthy = status in {"HEALTHY", "RECONCILED"}
    if healthy:
        text = "Paper order ledger and Capital positions agree."
    elif details:
        more = f" (+{len(conflicts) - len(details)} more)" if len(conflicts) > len(details) else ""
        text = " ".join(details) + more
    elif status == "UNVERIFIED":
        text = "Paper reconciliation has not run yet, so the order ledger is unverified."
    else:
        text = f"Paper reconciliation reported {status.replace('_', ' ').lower()} without conflict details."
    return {
        "status": status,
        "healthy": healthy,
        "conflict_count": len(conflicts),
        "conflicts": conflicts[:5],
        "text": text,
        "checked_at": reconciliation.get("checked_at"),
    }


def _risk_rows(
    dashboard: Mapping[str, Any],
    execution: Mapping[str, Any],
    positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if execution.get("kill_switch_active") is True:
        rows.append({
            "severity": "CRITICAL", "type": "KILL_SWITCH", "title": "Kill Switch active",
            "detail": "New autonomous Paper risk is blocked.",
            "action": "Release the Kill Switch (top right) when you want Paper Auto to resume.",
        })
    reconciliation = _mapping(execution.get("reconciliation"))
    reconciliation_blocks = False
    if str(reconciliation.get("status") or "").upper() not in {"HEALTHY", "RECONCILED"}:
        summary = _reconciliation_summary(reconciliation)
        reconciliation_blocks = True
        rows.append({
            "severity": "CRITICAL",
            "type": "EXECUTION_RECONCILIATION",
            "title": (
                f"Paper order ledger conflict ({summary['conflict_count']})"
                if summary["conflict_count"] else "Paper order ledger is not verified"
            ),
            "detail": summary["text"],
            "status_code": summary["status"],
            "action": _RECONCILIATION_ACTION,
        })
    readiness = _mapping(execution.get("readiness"))
    for reason in readiness.get("reasons") or []:
        reason_text = str(reason)
        if reconciliation_blocks and "EXECUTION_STATE_CONFLICT" in reason_text.upper():
            # Same fact as the reconciliation row above — say it once, in words.
            rows.append({
                "severity": "HIGH", "type": "PAPER_READINESS", "title": "Paper Auto cannot open new positions",
                "detail": "Blocked by the paper order ledger conflict above.",
                "action": "Resolve the ledger conflict; Paper Auto resumes automatically afterwards.",
            })
            continue
        rows.append({"severity": "HIGH", "type": "PAPER_READINESS", "title": "Paper Auto blocked", "detail": reason_text})
    for item in _rows(dashboard.get("dashboard_risk_flags")):
        rows.append({
            "severity": str(item.get("severity") or "MEDIUM").upper(),
            "type": item.get("type") or "MARKET_RISK",
            "ticker": item.get("ticker"),
            "title": item.get("title") or "Market risk",
            "detail": item.get("detail"),
        })
    for position in positions:
        if position.get("stop_loss_price") is None:
            rows.append({
                "severity": "HIGH",
                "type": "POSITION_PROTECTION",
                "ticker": position.get("ticker"),
                "title": "Open position has no recorded stop",
                "detail": position.get("position_id"),
            })
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in rows:
        key = "|".join(str(item.get(name) or "") for name in ("type", "ticker", "title", "detail"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    severity = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
    unique.sort(key=lambda item: severity.get(str(item.get("severity") or "LOW").upper(), 0), reverse=True)
    return unique[:20]


_MAJOR_NEWS_MAX_AGE_SECONDS = 72 * 3600  # spans a weekend; anything older is history, not "major news"
_NARRATIVE_STALE_AFTER_SECONDS = 3 * 3600  # the news agent refreshes the narrative every ~15 minutes


def _news_title_key(title: Any) -> str:
    """Normalized headline used to collapse re-posts of the same story."""
    return " ".join("".join(ch if ch.isalnum() else " " for ch in str(title or "").casefold()).split())


def _major_news(
    dashboard: Mapping[str, Any],
    now: Optional[datetime] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Current high/critical-impact items only.

    The upstream list is ranked by impact with no time window, so two-month-old
    CRITICAL items used to fill the card ahead of this week's news, and the
    same post published twice appeared twice. Items older than the window,
    undated items, flagged duplicates and repeated headlines are dropped; the
    counts of what was dropped are returned so the card can say so.
    """
    current = now or datetime.now(timezone.utc)
    rows = []
    seen_titles: set[str] = set()
    dropped = {"stale": 0, "undated": 0, "duplicates": 0}
    candidates = sorted(
        _rows(dashboard.get("dashboard_news_items")),
        key=lambda item: (_finite(item.get("impact_score")) or 0.0, str(item.get("published_at") or "")),
        reverse=True,
    )
    for item in candidates:
        impact = str(item.get("impact") or "").lower()
        if impact not in {"critical", "high"}:
            continue
        age_seconds = _age_seconds(item.get("published_at"), current)
        if age_seconds is None:
            dropped["undated"] += 1
            continue
        if age_seconds > _MAJOR_NEWS_MAX_AGE_SECONDS:
            dropped["stale"] += 1
            continue
        title_key = _news_title_key(item.get("title"))
        if item.get("is_duplicate") is True or (title_key and title_key in seen_titles):
            dropped["duplicates"] += 1
            continue
        if title_key:
            seen_titles.add(title_key)
        rows.append({
            "id": item.get("id"),
            "ticker": item.get("ticker"),
            "title": item.get("title"),
            "source": item.get("source"),
            "url": item.get("url"),
            "published_at": item.get("published_at"),
            "impact": impact.upper(),
            "impact_score": _finite(item.get("impact_score")),
            "sentiment": item.get("sentiment"),
            "sentiment_source": _mapping(item.get("sentiment_quality")).get("source"),
            "summary": str(item.get("summary") or "")[:400] or None,
        })
    # The dashboard already filters its news pool with the same window; add
    # what it dropped so the card can say how many old items were withheld.
    upstream = _mapping(dashboard.get("dashboard_news_meta"))
    return rows[:12], {
        "window_hours": int(_MAJOR_NEWS_MAX_AGE_SECONDS // 3600),
        "dropped_stale": dropped["stale"] + (_integer(upstream.get("dropped_stale")) or 0),
        "dropped_undated": dropped["undated"] + (_integer(upstream.get("dropped_undated")) or 0),
        "dropped_duplicates": dropped["duplicates"] + (_integer(upstream.get("dropped_duplicates")) or 0),
    }


def _narrative_freshness(narrative: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """FRESH / STALE / UNKNOWN for the market narrative, from its own timestamp."""
    generated_at = narrative.get("generated_at")
    age_seconds = _age_seconds(generated_at, now)
    if age_seconds is None:
        state = "UNKNOWN"
    else:
        state = "STALE" if age_seconds > _NARRATIVE_STALE_AFTER_SECONDS else "FRESH"
    return {
        "state": state,
        "generated_at": generated_at,
        "stale_after_seconds": _NARRATIVE_STALE_AFTER_SECONDS,
    }


def _sentiment_section(dashboard: Mapping[str, Any], now: Optional[datetime] = None) -> dict[str, Any]:
    narrative = _mapping(dashboard.get("dashboard_market_narrative"))
    if not narrative or narrative.get("available") is False:
        return _section(note="No source-backed market narrative is available yet.")
    freshness = _narrative_freshness(narrative, now or datetime.now(timezone.utc))
    item = {
        "current_tone": narrative.get("tone"),
        "risk_regime": narrative.get("risk_regime"),
        "generated_at": narrative.get("generated_at"),
        # A narrative is "current" only while its own timestamp is recent; an
        # old one is shown as the last recorded tone, never as today's.
        "freshness": freshness["state"],
        "stale_after_seconds": freshness["stale_after_seconds"],
        "top_catalysts": [str(value) for value in (narrative.get("top_catalysts") or []) if str(value).strip()][:6],
        "what_matters_today": [str(value) for value in (narrative.get("what_matters_today") or []) if str(value).strip()][:6],
        "prior_tone": None,
        "shift": None,
        "shift_state": "UNAVAILABLE",
    }
    return _section(
        [item],
        note="Current source-backed tone is available; a prior comparable snapshot was not found.",
        extra={"freshness": freshness["state"]},
    )


_EVIDENCE_CACHE: dict[str, Any] = {"at": 0.0, "ledger": None, "factory": None}
_EVIDENCE_TTL_SECONDS = 10.0


def _evidence_cache_fresh() -> bool:
    import time as _time

    return (_EVIDENCE_CACHE["at"] > 0
            and _time.monotonic() - _EVIDENCE_CACHE["at"] < _EVIDENCE_TTL_SECONDS)


def _ledger_evidence() -> dict[str, Any]:
    """Phase 13 — persistent Decision Ledger availability summary (bounded)."""
    if _evidence_cache_fresh() and _EVIDENCE_CACHE["ledger"] is not None:
        return dict(_EVIDENCE_CACHE["ledger"])
    try:
        import time as _time

        from core.decision_ledger.store import get_decision_ledger_store

        store = get_decision_ledger_store(None)
        evidence = {
            "available": True,
            "total_entries": store.count_entries(),
            "replay_entries": store.count_entries(source="REPLAY"),
            "status_counts": store.count_entries_by_status(),
        }
        _EVIDENCE_CACHE["ledger"] = evidence
        _EVIDENCE_CACHE["at"] = _time.monotonic()
        return dict(evidence)
    except Exception:
        logger.exception("Decision ledger availability check failed")
        return {"available": False, "total_entries": 0, "replay_entries": 0}


def _ledger_decision_rows() -> tuple[list[dict[str, Any]], str | None]:
    """§97 eleven-question rows from the persistent ledger (Phase 13)."""
    try:
        from core.decision_ledger.store import get_decision_ledger_store

        store = get_decision_ledger_store(None)
        rows = [entry.eleven_questions() for entry in store.list_entries(limit=30)]
        # Reflection lessons (TradingAgents adoption): join the decision_id-keyed
        # ai_reflections store so the UI can answer "what did the AI learn?".
        try:
            import json as _json

            from core.ai.reflection import REFLECTIONS_PATH

            lessons_by_id: dict[str, dict[str, Any]] = {}
            if REFLECTIONS_PATH.exists():
                with REFLECTIONS_PATH.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = _json.loads(line)
                        except ValueError:
                            continue
                        if row.get("decision_id") and row.get("lesson"):
                            lessons_by_id[str(row["decision_id"])] = {
                                "lesson": str(row["lesson"])[:400],
                                "pnl_pct": row.get("pnl_pct"),
                                "reflected_at": row.get("reflected_at"),
                            }
            for row in rows:
                lesson = lessons_by_id.get(str(row.get("decision_id") or ""))
                if lesson:
                    row["reflection"] = lesson
        except Exception:
            pass  # lessons are evidence garnish; never break the ledger feed
        return rows, None
    except Exception:
        logger.exception("Decision ledger read failed")
        return [], "Decision Ledger store is unavailable."


def _reconcile_ledger_rows(
    rows: Iterable[Mapping[str, Any]],
    capital: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Overlay later Capital facts on immutable point-in-time decisions.

    A ledger entry is appended when Risk accepts or rejects a proposal. A
    successful paper fill happens a few seconds later, so mutating that entry
    would destroy the original audit snapshot. The Cockpit read model joins the
    snapshot with the current Capital position instead.
    """
    by_decision_id: dict[str, Mapping[str, Any]] = {}
    by_signal_id: dict[str, Mapping[str, Any]] = {}
    for position in _rows(capital.get("positions")):
        context = _mapping(position.get("decision_context"))
        decision_id = str(context.get("decision_id") or "").strip()
        signal_id = str(
            position.get("source_signal_id")
            or position.get("signal_id")
            or context.get("signal_id")
            or ""
        ).strip()
        if decision_id:
            by_decision_id[decision_id] = position
        if signal_id:
            by_signal_id[signal_id] = position

    opened_by_signal_id: dict[str, Mapping[str, Any]] = {}
    closed_by_position_id: dict[str, Mapping[str, Any]] = {}
    for transaction in _rows(capital.get("history")):
        transaction_type = str(transaction.get("type") or "").upper()
        metadata = _mapping(transaction.get("metadata"))
        if transaction_type == "OPEN_POSITION":
            signal_id = str(metadata.get("signal_id") or "").strip()
            if signal_id:
                opened_by_signal_id[signal_id] = transaction
        elif transaction_type == "CLOSE_POSITION":
            position_id = str(metadata.get("position_id") or "").strip()
            if position_id:
                closed_by_position_id[position_id] = transaction

    reconciled: list[dict[str, Any]] = []
    for original in rows:
        item = dict(original)
        recorded_critic = _mapping(item.get("critic"))
        validation = _mapping(item.get("validation"))
        if recorded_critic.get("available") is not True and validation.get("ok") is False:
            item["critic"] = {
                "available": False,
                "skipped": True,
                "reason": "skipped_after_ai_validation_veto",
                "upstream_stage": validation.get("stage"),
                "upstream_reason": validation.get("reason"),
            }
        action = str(
            item.get("action")
            or str(item.get("what_happened") or "").split(" (", 1)[0]
            or ""
        ).upper()
        # Older immutable snapshots were appended before the required-AI
        # availability gate. A parser failure therefore carried the impossible
        # combination NO_TRADE + accepted even though no order was sent. Keep
        # the stored status as decision_status, but present the effective
        # operator outcome truthfully in the read model.
        if action == "NO_TRADE" and str(item.get("status") or "").lower() == "accepted":
            item["decision_status"] = "accepted"
            item["status"] = "rejected"
            item["event_type"] = "decision_rejected"
            item["what_happened"] = "NO_TRADE (rejected)"
        decision_id = str(item.get("decision_id") or "").strip()
        signal_id = str(
            item.get("signal_id")
            or _mapping(item.get("data_existed")).get("signal_id")
            or ""
        ).strip()
        position = by_decision_id.get(decision_id)
        executable_action = action.startswith("OPEN_") or action in {
            "BUY", "SELL", "LONG", "SHORT",
        }
        if not position and signal_id and executable_action:
            signal_position = by_signal_id.get(signal_id)
            signal_context = _mapping((signal_position or {}).get("decision_context"))
            signal_decision_id = str(signal_context.get("decision_id") or "").strip()
            # signal_id is hourly/deterministic for scanner candidates and can
            # legitimately recur after a rejected review. Never attach a newer
            # decision's position to an older immutable ledger entry.
            if signal_position and (
                not decision_id
                or not signal_decision_id
                or signal_decision_id == decision_id
            ):
                position = signal_position
        if not position and signal_id and executable_action:
            opened = opened_by_signal_id.get(signal_id)
            open_metadata = _mapping((opened or {}).get("metadata"))
            open_decision_id = str(open_metadata.get("decision_id") or "").strip()
            if decision_id and open_decision_id and open_decision_id != decision_id:
                opened = None
                open_metadata = {}
            position_id = str(open_metadata.get("position_id") or "").strip()
            closed = closed_by_position_id.get(position_id)
            if opened and closed:
                close_metadata = _mapping(closed.get("metadata"))
                position = {
                    "position_id": position_id,
                    "ticker": close_metadata.get("ticker") or open_metadata.get("ticker"),
                    "direction": close_metadata.get("direction"),
                    "status": "CLOSED",
                    "entry_price": close_metadata.get("entry_price"),
                    "exit_price": close_metadata.get("exit_price"),
                    "net_pnl": close_metadata.get("net_pnl"),
                    "close_reason": close_metadata.get("close_reason"),
                    "closed_at": closed.get("timestamp"),
                    "allocated_usd": abs(_finite(opened.get("amount")) or 0.0),
                    "signal_id": signal_id,
                }
        if not position:
            reconciled.append(item)
            continue

        context = _mapping(position.get("decision_context"))
        execution_context = _mapping(context.get("execution"))
        position_status = str(position.get("status") or "OPEN").upper()
        executed = dict(_mapping(item.get("executed")))
        executed.update({
            "state": "FILLED",
            "position_status": position_status,
            "position_id": position.get("position_id"),
            "order_intent_id": execution_context.get("order_intent_id"),
            "entry_price": position.get("entry_price"),
            "usd_size": position.get("usd_size"),
            "direction": position.get("direction"),
            "opened_at": position.get("opened_at"),
            "closed_at": position.get("closed_at"),
            "exit_price": position.get("exit_price"),
            "realized_pnl": position.get("net_pnl") or position.get("pnl"),
            "close_reason": position.get("close_reason"),
            "stop_loss_price": position.get("stop_loss_price"),
            "take_profit_price": position.get("take_profit_price"),
        })
        item["executed"] = {key: value for key, value in executed.items() if value is not None}
        item["position_id"] = position.get("position_id")
        item["decision_status"] = item.get("status")
        item["status"] = "executed" if position_status == "OPEN" else position_status.lower()
        action = (
            item.get("action")
            or str(item.get("what_happened") or "").split(" (", 1)[0]
            or "Position"
        )
        item["what_happened"] = f"{action} ({item['status']})"

        final_sizing = dict(_mapping(item.get("capital_allowed")))
        final_sizing.update(_mapping(context.get("sizing")))
        if _finite(final_sizing.get("usd_size")) in (None, 0):
            final_sizing["usd_size"] = position.get("usd_size")
        if _finite(final_sizing.get("invested_amount")) in (None, 0):
            final_sizing["invested_amount"] = position.get("margin_used") or position.get("usd_size")
        if _finite(final_sizing.get("usd_size")) in (None, 0) and _finite(position.get("allocated_usd")):
            final_sizing.pop("usd_size", None)
            final_sizing["allocated_usd"] = position.get("allocated_usd")
            final_sizing["allocation_basis"] = "recorded_open_cash_debit_including_costs"
        final_sizing["leverage"] = position.get("leverage") or final_sizing.get("leverage")
        item["capital_allowed"] = final_sizing

        recorded_critic = _mapping(item.get("critic"))
        position_critic = _mapping(context.get("ai_critic"))
        if recorded_critic.get("available") is not True and position_critic:
            item["critic"] = position_critic

        item["afterward"] = {
            "status": f"POSITION_{position_status}",
            "position_id": position.get("position_id"),
            "current_price": position.get("current_price"),
            "unrealized_pnl": position.get("unrealized_pnl"),
            "closed_at": position.get("closed_at"),
            "exit_price": position.get("exit_price"),
            "realized_pnl": position.get("net_pnl") or position.get("pnl"),
            "close_reason": position.get("close_reason"),
            "as_of": position.get("current_price_timestamp"),
        }
        reconciled.append(item)
    return reconciled


def _decision_rows(capital: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in _rows(capital.get("decisions"))[:30]:
        payload = _mapping(item.get("payload"))
        decision = _mapping(payload.get("decision"))
        risk = _mapping(payload.get("risk"))
        capital_evidence = _mapping(payload.get("capital"))
        ai = _mapping(payload.get("ai"))
        rows.append({
            "event_id": item.get("event_id"),
            "timestamp": item.get("timestamp"),
            "ticker": item.get("ticker"),
            "event_type": item.get("event_type"),
            "status": item.get("status"),
            "what_happened": item.get("summary") or item.get("event_type"),
            "data_existed": payload.get("data_evidence") or payload.get("inputs"),
            "strategy": item.get("strategy_type") or payload.get("strategy_type"),
            "ai_model": ai.get("model") or payload.get("ai_model"),
            "prompt": payload.get("prompt"),
            "ai_proposal": ai.get("proposal") or decision.get("action"),
            "critic": payload.get("critic"),
            "risk": risk or payload.get("risk_evaluation"),
            "capital_allowed": capital_evidence or payload.get("sizing"),
            "executed": payload.get("execution") or item.get("status"),
            "afterward": payload.get("outcome") or payload.get("afterward"),
            "position_id": item.get("position_id"),
            "signal_id": item.get("signal_id"),
        })
    return rows


def _continuous_scan_evidence() -> dict[str, Any]:
    """Continuous Pre-Earnings scanner summary (bounded, fail-safe)."""
    evidence: dict[str, Any] = {"enabled": False, "interval_seconds": 60,
                                "scanned": 0, "ai_verdicts": 0, "last_symbol": None}
    try:
        from core.pre_earnings.continuous import get_continuous_scanner

        status = get_continuous_scanner(None).status()
        evidence = {
            "enabled": bool(status.get("running")),
            "interval_seconds": status.get("interval_seconds", 60),
            "scanned": status.get("scanned", 0),
            "ai_verdicts": status.get("ai_verdicts", 0),
            "last_symbol": status.get("last_symbol"),
        }
    except Exception:
        logger.exception("continuous pre-earnings evidence read failed")
    return evidence


def _ai_decisions_section(
    capital: Mapping[str, Any],
    errors: Mapping[str, Any],
    *,
    governance: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """§97 — persistent-ledger-first; Phase 12 computed rows stay the fallback."""
    ledger_rows, ledger_error = _ledger_decision_rows()
    if ledger_rows:
        ledger_rows = _reconcile_ledger_rows(ledger_rows, capital)
        evidence = _ledger_evidence()
        return _section(
            ledger_rows,
            error=None,
            note="Persistent Decision Ledger rows (Phase 13).",
            extra={
                "coverage": "PERSISTENT_LEDGER",
                "source": "core.decision_ledger",
                "total_entries": evidence.get("total_entries", len(ledger_rows)),
                "status_counts": evidence.get("status_counts", {}),
                "governance": dict(governance or {}),
            },
        )
    # The compacted capital decisions list strips payloads, so the fallback
    # reads the FULL audit records directly — otherwise 8 of the 11 questions
    # would render "not captured" although the evidence exists server-side.
    fallback_capital = capital
    try:
        from core.trade_audit import get_trade_audit

        full_rows = get_trade_audit(limit=30)
        if full_rows:
            fallback_capital = {"decisions": full_rows}
    except Exception:
        logger.exception("full trade-audit fallback read failed")
    computed = _decision_rows(fallback_capital)
    if computed:
        return _section(
            computed,
            error=errors.get("capital"),
            note="No persistent ledger entry yet — computed Phase 12 audit rows.",
            extra={"coverage": "PARTIAL_PHASE12_FALLBACK"},
        )
    return _section(
        [],
        error=ledger_error or errors.get("capital"),
        note="No decision audit event is available.",
        extra={"coverage": "EMPTY"},
    )


def _decision_replay_section() -> dict[str, Any]:
    """§89 — replay availability + the latest replay entries."""
    evidence = _ledger_evidence()
    if not evidence.get("available"):
        return _section(error="Decision Ledger store is unavailable.")
    try:
        from core.decision_ledger.store import get_decision_ledger_store

        replays = [
            {
                "replay_decision_id": entry.decision_id,
                "replay_of": entry.replay_of,
                "ticker": entry.ticker,
                "action": entry.action,
                "ai_available": bool((entry.ai or {}).get("available")),
                "model": (entry.ai or {}).get("model"),
                "created_at": entry.created_at,
            }
            for entry in get_decision_ledger_store(None).list_entries(limit=10, source="REPLAY")
        ]
    except Exception:
        logger.exception("Replay section read failed")
        return _section(error="Decision replay read failed.")
    return _section(
        replays,
        note="No point-in-time replay has been recorded yet.",
        extra={"replay_entries_total": evidence.get("replay_entries", 0)},
    )


def _model_lab_section() -> dict[str, Any]:
    """§88 — per-role Model Lab summary (never a global score)."""
    try:
        from api.ledger_service import get_model_lab

        summary = get_model_lab(None, limit=10)
    except Exception:
        logger.exception("Model Lab section read failed")
        return _section(error="Model Lab is unavailable.")
    roles = summary.get("summary", {}).get("roles") or {}
    runs = summary.get("runs") or []
    return _section(
        runs,
        note="No model comparison has been recorded yet. Comparisons are kept per AI role.",
        extra={"roles_with_runs": sorted(roles.keys())},
    )


def _autonomous_repair_section() -> dict[str, Any]:
    """§112 card data: repair cases with the full agent journey + watchdog state."""
    rows: List[Dict[str, Any]] = []
    watchdog: Dict[str, Any] = {}
    try:
        from core.development_factory.watchdog import RepairCaseStore, case_summary_rows

        rows = case_summary_rows(RepairCaseStore(), limit=12)
    except Exception:
        logger.exception("autonomous repair section read failed")
        return _section([], error="Autonomous repair store is unavailable.")
    try:
        from core.development_factory.watchdog import get_repair_watchdog

        watchdog = get_repair_watchdog(None).status()
    except Exception:
        watchdog = {}
    return _section(
        rows,
        note="No problem has been detected for the coding agent yet.",
        extra={
            "watchdog": watchdog,
            "chain": "detector → coding_agent → code_review_agent → security_review_agent → test_agent → auto-apply + post-check",
            "policy": "independent reviewers' PASS applies the fix automatically; "
                      "a failing post-apply check rolls it back (§81/§112)",
        },
    )


def _strategy_lab(performance: Mapping[str, Any], registry: StrategyRegistry) -> dict[str, Any]:
    perf_rows = _rows(performance.get("entries"))
    try:
        records = [record.to_dict() for record in registry.list_records()]
        events = [event.to_dict() for event in registry.history()]
        registry_error = None
    except StrategyRegistryError:
        logger.error("Strategy registry read failed", exc_info=True)
        records, events = [], []
        registry_error = "Strategy Registry is unavailable."

    latest_by_strategy: dict[str, dict[str, Any]] = {}
    for row in perf_rows:
        strategy_id = str(row.get("strategy_id") or "").upper()
        previous = latest_by_strategy.get(strategy_id)
        if previous is None or (_parse_dt(row.get("last_run")) or _EPOCH) > (_parse_dt(previous.get("last_run")) or _EPOCH):
            latest_by_strategy[strategy_id] = row

    try:
        from core.strategy_v2.performance_cache import STRATEGY_RESEARCH_CACHE_PATH, StrategyPerformanceCache

        research_cache = StrategyPerformanceCache(STRATEGY_RESEARCH_CACHE_PATH)
        research_entries = (research_cache.load() or {}).get("entries", {})
        for key, value in research_entries.items():
            if isinstance(value, dict):
                parts = str(key).split("|")
                sid = parts[2].upper() if len(parts) > 2 else ""
                if sid and (sid not in latest_by_strategy or not latest_by_strategy[sid].get("validation_status")):
                    latest_by_strategy[sid] = {
                        "strategy_id": sid,
                        "validation_status": value.get("validation_status") or "research_only",
                        "walk_forward": value.get("walk_forward"),
                        "validation_as_of": value.get("validation_as_of") or value.get("last_run"),
                        "sample_size": value.get("sample_size", 0),
                        "expectancy_R": value.get("expectancy_R", 0.0),
                        "profit_factor": value.get("profit_factor", 0.0),
                    }
    except Exception:
        logger.exception("strategy-lab research cache read failed")

    # §79/§95 real drift: measured by the Phase 16 drift monitor over closed
    # trades, not re-derived from registry status. REVALIDATION-triggering
    # verdicts surface; measurement never changes risk.
    drift_by_strategy: dict[str, str] = {}
    try:
        from core.research_factory.drift import detect_drift
        from core.research_factory.weakness import load_closed_trades

        for report in detect_drift(load_closed_trades()):
            drift_by_strategy[report.strategy_id.upper()] = (
                "DRIFT_SUSPECTED" if report.verdict == "DRIFT_SUSPECTED"
                else ("STABLE" if report.verdict == "STABLE" else report.verdict)
            )
    except Exception:
        logger.exception("strategy-lab drift measurement failed")

    lifecycle = []
    for record in records:
        strategy_id = str(record.get("strategy_id") or "").upper()
        perf = latest_by_strategy.get(strategy_id, {})
        status = str(record.get("status") or "RESEARCH").upper()
        lifecycle.append({
            **record,
            "oos_verdict": perf.get("validation_status") or None,
            "robustness": _mapping(perf.get("walk_forward")) or None,
            "last_validated": perf.get("validation_as_of"),
            "drift": drift_by_strategy.get(strategy_id, "NOT_MEASURED") if status != "DEGRADED" else "DEGRADED",
            "sample_size": _integer(perf.get("sample_size")),
            "expectancy_r": _finite(perf.get("expectancy_R")),
        })

    leaderboard = []
    for row in perf_rows:
        leaderboard.append({
            "strategy_id": row.get("strategy_id"),
            "symbol": row.get("symbol"),
            "direction": row.get("direction"),
            "validation_status": row.get("validation_status") or "unverified",
            "sample_size": _integer(row.get("sample_size")),
            # Below the research minimum nothing may be claimed: a single lucky
            # trade is an anecdote, not a rank-1 strategy.
            "sample_sufficient": (_integer(row.get("sample_size")) or 0) >= _LEADERBOARD_MIN_SAMPLE,
            "expectancy_r": _finite(row.get("expectancy_R")),
            "profit_factor": _finite(row.get("profit_factor")),
            "max_drawdown_pct": _finite(row.get("max_drawdown")),
            "last_run": row.get("last_run"),
        })
    leaderboard.sort(
        key=lambda item: (
            str(item.get("validation_status") or "").lower() not in {"unverified", ""},
            item.get("sample_sufficient") is True,
            # 0.0 is a real expectancy and must not sort as "missing".
            _finite(item.get("expectancy_r")) if _finite(item.get("expectancy_r")) is not None else float("-inf"),
            _integer(item.get("sample_size")) or 0,
        ),
        reverse=True,
    )
    history = [
        event for event in events
        if str(event.get("action") or "").upper() in {"PROMOTE", "DEMOTE", "TRANSITION"}
    ]
    # §87 rollback history comes from the Phase 16 promotion records (real
    # ROLLED_BACK promotions) plus registry DEMOTE events — a substring scan of
    # registry actions can never surface rollbacks (they are not emitted there).
    rollback_history: list[dict[str, Any]] = [
        {
            "strategy_id": event.get("strategy_id"),
            "version": event.get("version"),
            "action": str(event.get("action") or "").upper(),
            "at": event.get("occurred_at") or event.get("created_at"),
            "detail": event.get("details"),
        }
        for event in events if str(event.get("action") or "").upper() == "DEMOTE"
    ]
    try:
        from core.promotion.records import PromotionRecordStore

        for record in PromotionRecordStore().list_records(limit=100):
            if record.status == "ROLLED_BACK":
                rollback_history.append({
                    "strategy_id": record.strategy_id,
                    "version": record.new_version,
                    "action": "ROLLED_BACK",
                    "at": record.rolled_back_at or record.promotion_time,
                    "detail": {"restored_version": record.previous_version,
                               "reason": record.rollback_reason,
                               "promotion_id": record.promotion_id},
                })
    except Exception:
        logger.exception("promotion rollback history read failed")
    state = "ERROR" if registry_error else ("SUCCESS" if lifecycle or leaderboard else "EMPTY")
    return {
        "state": state,
        "message": registry_error or (
            "No registered lifecycle records exist; performance rows remain unverified evidence."
            if not lifecycle else "Lifecycle records are evidence-backed by the Strategy Registry."
        ),
        "records": lifecycle,
        "champions": [item for item in lifecycle if item.get("status") == "CHAMPION"],
        "challengers": [item for item in lifecycle if item.get("status") == "CHALLENGER"],
        "shadow": [item for item in lifecycle if item.get("status") == "SHADOW"],
        "degraded": [item for item in lifecycle if item.get("status") == "DEGRADED"],
        "leaderboard": leaderboard[:30],
        "leaderboard_min_sample": _LEADERBOARD_MIN_SAMPLE,
        "promotion_history": [item for item in history if str(item.get("action") or "").upper() in {"PROMOTE", "DEMOTE"}],
        "rollback_history": rollback_history,
        "ai_quant_commentary": {
            "state": "EMPTY",
            "text": None,
            "message": "No owner-selected Quant Analyst model has produced commentary for this evidence.",
        },
    }


def _research_factory_evidence() -> dict[str, Any]:
    """Phase 14 — real Research Factory store evidence (bounded, fail-safe)."""
    if _evidence_cache_fresh() and _EVIDENCE_CACHE["factory"] is not None:
        return dict(_EVIDENCE_CACHE["factory"])
    evidence = {"available": False, "hypotheses": 0, "experiments": 0,
                "status_counts": {}, "shadow": 0, "revalidations": 0}
    try:
        from core.research_factory.experiments import ExperimentRegistry
        from core.research_factory.hypothesis import HypothesisStore

        store = HypothesisStore()
        registry = ExperimentRegistry(hypothesis_store=store)
        counts = registry.status_counts()
        evidence.update({
            "available": True,
            "hypotheses": store.count(),
            "experiments": sum(counts.values()),
            "status_counts": counts,
            "shadow": counts.get("SHADOW", 0),
        })
    except Exception:
        logger.exception("Research Factory evidence read failed")
    try:
        from core.research_factory.drift import detect_drift, revalidation_recommendations
        from core.research_factory.weakness import load_closed_trades

        evidence["revalidations"] = len(revalidation_recommendations(detect_drift(load_closed_trades())))
    except Exception:
        pass
    import time as _time

    _EVIDENCE_CACHE["factory"] = evidence
    _EVIDENCE_CACHE["at"] = _time.monotonic()
    return dict(evidence)


def _research_section(agents: Mapping[str, Any], performance: Mapping[str, Any]) -> dict[str, Any]:
    worker = _mapping(_mapping(agents.get("agents")).get("strategy_research"))
    factory = _research_factory_evidence()
    activity = []
    if factory.get("available") and factory.get("experiments"):
        try:
            from core.research_factory.experiments import ExperimentRegistry
            from core.research_factory.hypothesis import HypothesisStore

            registry = ExperimentRegistry(hypothesis_store=HypothesisStore())
            for record in registry.list_records(limit=8):
                activity.append({
                    "hypothesis": record.hypothesis_id,
                    # What was tested, in words; bare hyp_/exp_ ids tell the owner nothing.
                    "strategy": record.strategy_id,
                    "description": str(record.description or "")[:200] or None,
                    "experiment": record.experiment_id,
                    "status": record.status,
                    "tests": len(record.events),
                    "validation": (record.evidence or {}).get("evaluation", {}).get("outcome_class", "governed lifecycle"),
                    "last_run": record.updated_at,
                })
        except Exception:
            logger.exception("Research Factory activity read failed")
    if not activity:
        for row in sorted(_rows(performance.get("entries")), key=lambda item: str(item.get("last_run") or ""), reverse=True)[:8]:
            activity.append({
                "hypothesis": row.get("strategy_id"),
                "experiment": "strategy_performance_observation",
                "status": row.get("validation_status") or "unverified",
                "tests": _integer(row.get("sample_size")),
                "validation": row.get("validation_method"),
                "last_run": row.get("last_run"),
            })
    capabilities = [
        {"name": "Hypotheses and experiments", "status": "AVAILABLE" if factory.get("experiments") else ("EMPTY" if factory.get("available") else "OBSERVATION_ONLY"), "phase": "PHASE_14"},
        {"name": "Weakness detection and drift", "status": "AVAILABLE" if factory.get("available") else "OBSERVATION_ONLY", "phase": "PHASE_14"},
        {"name": "Coding Agent", "status": "AVAILABLE", "phase": "PHASE_15"},
        {"name": "Review Agent", "status": "AVAILABLE", "phase": "PHASE_15"},
        {"name": "Shadow / promotion / rejection / rollback", "status": "AVAILABLE", "phase": "PHASE_16"},
    ]
    message = "Governed hypotheses, experiments and drift verdicts from the Research Factory."
    if not factory.get("available"):
        message = "Research Factory stores are unavailable; only observed strategy-performance activity is shown."
    elif not factory.get("experiments"):
        message = "Research Factory is ready; no experiment has been recorded yet. Turn the auto-cycle on to start one."
    auto_cycle: Dict[str, Any] = {"enabled": False}
    try:
        from core.research_factory.scheduler import get_research_scheduler

        sched = get_research_scheduler(None).status()
        last = _mapping(sched.get("last_result"))
        auto_cycle = {
            "enabled": bool(sched.get("running")),
            "last_run": sched.get("last_run"),
            "cycles_completed": sched.get("cycles_completed"),
            "interval_seconds": sched.get("interval_seconds"),
            "last_weaknesses": _integer(last.get("weaknesses_detected")),
            "last_experiments": _integer(last.get("experiments")),
            "last_error": str(sched.get("last_error") or ""),
        }
        # Karpathy autoresearch adoption: KEEP/DISCARD ledger + current best.
        try:
            from core.research_factory.experiments import DEFAULT_EXPERIMENTS_PATH, ExperimentRegistry

            registry = ExperimentRegistry(DEFAULT_EXPERIMENTS_PATH)
            counts = registry.status_counts()
            applied = registry.list_records(status="APPLIED", limit=500)
            latest_applied = applied[-1] if applied else None
            auto_cycle["autoresearch"] = {
                "keep": int(counts.get("APPLIED", 0)),
                "discard": int(counts.get("FAILED", 0)) + int(counts.get("REJECTED", 0)),
                "open": int(counts.get("PROPOSED", 0)) + int(counts.get("IN_PROGRESS", 0)),
                "current_best": (
                    {
                        "experiment_id": latest_applied.experiment_id,
                        "strategy_id": latest_applied.strategy_id,
                        "description": str(latest_applied.description or "")[:160],
                        "updated_at": latest_applied.updated_at,
                    }
                    if latest_applied is not None else None
                ),
            }
        except Exception:
            pass
    except Exception:
        pass
    return {
        "state": "SUCCESS" if activity or worker else "EMPTY",
        "message": message,
        "worker": worker or None,
        "activity": activity,
        "capabilities": capabilities,
        "factory": factory,
        "auto_cycle": auto_cycle,
    }


def _sec_health(config: Any, checked_at: str) -> dict[str, Any]:
    root = _mapping(getattr(config, "config", {}))
    settings = _mapping(root.get("sec"))
    identity_env = str(settings.get("identity_env") or "EDGAR_IDENTITY")
    if not str(os.getenv(identity_env, "")).strip():
        return _service(
            "sec", "SEC", "NOT_CONFIGURED", checked_at=checked_at,
            message=f"{identity_env} is not configured; no live SEC check is claimed.",
            evidence={"identity_configured": False},
        )
    path = Path(settings.get("cache_path") or (DATA_DIR / "sec_filings_cache.json"))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = _mapping(payload.get("entries"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        entries = {}
    if not entries:
        return _service(
            "sec", "SEC", "UNVERIFIED", checked_at=checked_at,
            message="SEC identity exists, but no cache-backed successful read is available.",
            evidence={"identity_configured": True, "cache_entries": 0},
            action="No SEC filing has been fetched yet. Open a ticker's filings once, or check the engine log for SEC errors.",
        )
    fetched = [_parse_dt(_mapping(value).get("fetched_at")) for value in entries.values()]
    latest = max((value for value in fetched if value is not None), default=None)
    return _service(
        "sec", "SEC", "HEALTHY", checked_at=checked_at,
        message="SEC identity and cache-backed filing evidence are available.",
        evidence={"identity_configured": True, "cache_entries": len(entries), "last_fetch": latest.isoformat() if latest else None},
    )


_SHARED_FRESHNESS_MAX_AGE_SECONDS = 15 * 60  # calendar/news evidence this fresh proves the data path works


def _market_provider_from_shared_freshness(pre_earnings: Mapping[str, Any], now: datetime) -> Optional[dict[str, Any]]:
    """Derive market-provider evidence from the SHARED Pre-Earnings snapshot.

    The quality registry is process-local; when the engine subprocess performs
    market fetching, the API process holds no stream. The Pre-Earnings state
    file is shared across processes, and its calendar/news evidence ages prove
    the market-data path delivered successfully — labeled as DERIVED, never
    presented as the primary verified stream."""
    freshness = _pre_earnings_freshness(pre_earnings, now)
    if freshness.get("state") == "ERROR":
        return {"level": "DEGRADED", "checked_at": checked_iso(now),
                "message": "Market-provider evidence derived from the shared Pre-Earnings snapshot reports an error.",
                "evidence": {"quality": "DERIVED_ERROR", "source": "pre_earnings_shared_state", **freshness}}
    # The calendar/news ages were measured WHEN THE SNAPSHOT WAS WRITTEN. Add
    # the snapshot's own age, otherwise a weeks-old snapshot that recorded
    # "news 2s old" would still prove a live market-data path today.
    snapshot_age = _finite(freshness.get("age_seconds"))
    if snapshot_age is None:
        return None
    ages = [value + snapshot_age for value in (
        _finite(freshness.get("calendar_age_seconds")),
        _finite(freshness.get("news_age_seconds")),
    ) if value is not None and value >= 0]
    if ages and min(ages) <= _SHARED_FRESHNESS_MAX_AGE_SECONDS:
        return {"level": "VERIFIED", "checked_at": checked_iso(now),
                "message": "Market-provider evidence derived from fresh shared calendar/news data (cross-process).",
                "evidence": {"quality": "DERIVED_VERIFIED", "source": "pre_earnings_shared_state",
                             "freshest_age_seconds": round(min(ages), 1)}}
    return None  # no shared evidence → the honest UNAVAILABLE stands


_AI_PROBE_CACHE: dict[str, Any] = {"at": 0.0, "result": None}
_AI_PROBE_TTL_SECONDS = 60.0
_AI_PROBE_TIMEOUT_SECONDS = 3.0


def _probe_primary_ai_provider(config: Any) -> Optional[dict[str, Any]]:
    """Real short-timeout availability probe of the primary LOCAL AI provider.

    Returns None when the primary provider is a cloud one (an honest
    UNVERIFIED is better than burning a cloud call in a health tile). TTL
    cached so cockpit rebuilds don't hammer the local server."""
    import time as _time

    root = _mapping(getattr(config, "config", {}))
    providers = _mapping(root.get("ai")).get("providers") or {}
    primary_name = str(_mapping(root.get("ai")).get("primary") or "").strip() or "ollama"
    primary = _mapping(providers.get(primary_name))
    if not primary or str(primary.get("type") or "") != "ollama":
        return None
    now_monotonic = _time.monotonic()
    if _AI_PROBE_CACHE["result"] is not None and now_monotonic - _AI_PROBE_CACHE["at"] < _AI_PROBE_TTL_SECONDS:
        cached = dict(_AI_PROBE_CACHE["result"])
        cached["checked_at"] = checked_iso(datetime.now(timezone.utc))
        return cached
    import urllib.error
    import urllib.request

    base = str(primary.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
    try:
        req = urllib.request.Request(f"{base}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=_AI_PROBE_TIMEOUT_SECONDS) as resp:
            ok = resp.status == 200
            detail = f"GET {base}/api/tags -> {resp.status}"
    except urllib.error.HTTPError as exc:
        ok, detail = False, f"HTTP {exc.code} from {base}"
    except Exception as exc:  # noqa: BLE001 — probe must never break the payload
        ok, detail = False, f"{type(exc).__name__}: {str(exc)[:80]}"
    result = {"provider": primary_name, "ok": ok, "detail": detail}
    _AI_PROBE_CACHE["at"] = now_monotonic
    _AI_PROBE_CACHE["result"] = result
    probed = dict(result)
    probed["checked_at"] = checked_iso(datetime.now(timezone.utc))
    return probed


def _system_health(
    *,
    config: Any,
    agents: Mapping[str, Any],
    execution: Mapping[str, Any],
    pre_earnings: Mapping[str, Any],
    providers: list[dict[str, Any]],
    data_health: Mapping[str, Any],
    database: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    checked_at = now.isoformat()
    runtime = _mapping(agents.get("agents"))
    streams = _mapping(data_health.get("streams"))
    services: list[dict[str, Any]] = [
        _service("backend", "Backend", "HEALTHY", checked_at=checked_at, message="Cockpit API request completed."),
        dict(database),
    ]

    engine_up = bool(_mapping(agents.get("bot")).get("engine_running"))
    enabled_workers = [value for value in runtime.values() if isinstance(value, Mapping) and value.get("enabled") is True]
    worker_errors = [str(value.get("last_error")) for value in enabled_workers if str(value.get("last_error") or "").strip()]
    scheduler_status = "HEALTHY" if engine_up and not worker_errors else ("DEGRADED" if engine_up else "UNHEALTHY")
    services.append(_service(
        "scheduler", "Scheduler", scheduler_status, checked_at=checked_at,
        message=(
            "Engine and enabled workers are running without a reported error." if scheduler_status == "HEALTHY"
            else ("The engine is not running, so no agent is scanning or managing positions." if not engine_up
                  else f"{len(worker_errors)} enabled worker(s) report an error: {worker_errors[0][:160]}")
        ),
        evidence={"engine_running": engine_up, "enabled_workers": len(enabled_workers), "worker_errors": worker_errors[:5]},
        action=(
            "Start the engine from the Agents tab (or restart the server)." if not engine_up
            else "Open the Agents tab to see which worker failed and its last error."
        ),
    ))

    market_stream = _mapping(streams.get("yf_prices")) or _mapping(streams.get("yf_chart"))
    market_level = str(market_stream.get("level") or "").upper()
    market_evidence = {"quality": market_level, "source": market_stream.get("source")}
    market_checked_at = market_stream.get("timestamp") or checked_at
    market_message = ("Latest provider evidence is verified." if market_level == "VERIFIED"
                      else "No fresh verified market-provider evidence is available.")
    if market_level not in {"VERIFIED", "ESTIMATED", "STALE"}:
        # Cross-process fallback (§98 no-fake-status): the quality registry is
        # process-local, so when the ENGINE subprocess performs the fetching the
        # API process holds no stream. Derive evidence from the shared
        # Pre-Earnings snapshot freshness (calendar/news ages) instead of
        # declaring a false UNAVAILABLE.
        derived = _market_provider_from_shared_freshness(pre_earnings, now)
        if derived is not None:
            market_level = derived["level"]
            market_evidence = derived["evidence"]
            market_checked_at = derived["checked_at"]
            market_message = derived["message"]
    services.append(_service(
        "market_provider", "Market provider",
        "HEALTHY" if market_level == "VERIFIED" else ("DEGRADED" if market_level in {"ESTIMATED", "STALE"} else "UNAVAILABLE"),
        checked_at=market_checked_at,
        message=market_message,
        evidence=market_evidence,
        action="Prices on other tabs may be old. Check the internet connection and the engine log for provider errors.",
    ))

    pre_status = _mapping(pre_earnings.get("status"))
    pre_error = str(pre_status.get("last_error") or "").strip()
    pre_items = _rows(pre_earnings.get("items"))
    pre_freshness = _pre_earnings_freshness(pre_earnings, now)
    pre_stale = pre_freshness.get("state") == "STALE"
    pre_age_known = pre_freshness.get("age_seconds") is not None
    if pre_error:
        earnings_status = "UNHEALTHY"
        earnings_message = f"Pre-Earnings worker reports an error: {pre_error[:160]}"
    elif pre_stale and pre_age_known:
        # A snapshot exists but is past its refresh horizon. This is a stale
        # source (with or without rows left in it), not "no snapshot".
        earnings_status = "DEGRADED"
        earnings_message = (
            f"Pre-Earnings snapshot is stale: last successful refresh {_human_age(pre_freshness.get('age_seconds'))} ago "
            f"(expected every {_human_age(pre_freshness.get('refresh_seconds'))})."
        )
    elif pre_items:
        earnings_status = "HEALTHY"
        earnings_message = "Pre-Earnings snapshot is available."
    elif pre_age_known:
        earnings_status = "HEALTHY"
        earnings_message = "Pre-Earnings scan is current; it holds no upcoming earnings event."
    else:
        earnings_status = "UNAVAILABLE"
        earnings_message = "No earnings snapshot is available."
    services.append(_service(
        "earnings", "Earnings", earnings_status,
        checked_at=pre_status.get("last_success_time") or pre_status.get("last_run_time"),
        message=earnings_message,
        evidence={"items": len(pre_items), "last_error_present": bool(pre_error), **pre_freshness},
        action="Press Refresh on the cockpit (starts a Pre-Earnings scan) and check that the Pre-Earnings agent is enabled in Agents.",
    ))

    options_values = [
        _finite(_mapping(item.get("components")).get("options"))
        for item in pre_items
    ]
    options_available = sum(1 for value in options_values if value is not None)
    services.append(_service(
        "options", "Options", "HEALTHY" if options_available else "UNAVAILABLE", checked_at=checked_at,
        message="Options evidence is present in the earnings snapshot." if options_available else "No IV/options evidence is present in the current earnings snapshot.",
        evidence={"covered_items": options_available, "total_items": len(pre_items)},
    ))

    news_runtime = _mapping(runtime.get("news_tab"))
    news_error = str(news_runtime.get("last_error") or "").strip()
    news_ready = bool(news_runtime.get("ready") or news_runtime.get("running"))
    services.append(_service(
        "news", "News", "UNHEALTHY" if news_error else ("HEALTHY" if news_ready else "UNAVAILABLE"),
        checked_at=news_runtime.get("last_success") or news_runtime.get("last_run"),
        message="News worker is ready without a reported error." if news_ready and not news_error else (f"News worker reports an error: {news_error[:160]}" if news_error else "News worker is not ready."),
        evidence={"enabled": news_runtime.get("enabled"), "running": news_runtime.get("running")},
        action="Open the Agents tab and check the News agent; news and sentiment on this page stay old until it runs.",
    ))
    services.append(_sec_health(config, checked_at))

    ai_stream = _mapping(streams.get("ollama_availability"))
    ai_verified = str(ai_stream.get("level") or "").upper() == "VERIFIED" and ai_stream.get("value") is True
    configured = [row for row in providers if row.get("enabled") is True and (row.get("has_key") is True or row.get("type") == "ollama")]
    ai_evidence = {"configured": [row.get("name") for row in configured], "verified_provider": ai_stream.get("source") if ai_verified else None}
    ai_checked_at = ai_stream.get("timestamp") or checked_at
    ai_message = ("A provider availability check is verified." if ai_verified
                  else ("Provider configuration exists, but no verified availability check is current." if configured
                        else "No AI provider is configured."))
    if not ai_verified and configured:
        # Cross-process fallback: probe the primary local provider directly
        # (short timeout, TTL-cached) so the tile reflects a REAL check even
        # when the availability stream lives in the engine subprocess.
        probed = _probe_primary_ai_provider(config)
        if probed is not None:
            ai_verified = probed["ok"]
            ai_evidence = {"configured": [row.get("name") for row in configured],
                           "verified_provider": probed["provider"] if probed["ok"] else None,
                           "probe": probed["detail"]}
            ai_checked_at = probed["checked_at"]
            ai_message = ("Local AI provider answered a live availability probe." if probed["ok"]
                          else f"Local AI provider probe failed: {probed['detail']}")
    ai_status = "HEALTHY" if ai_verified else ("UNVERIFIED" if configured else "NOT_CONFIGURED")
    services.append(_service(
        "ai_providers", "AI providers", ai_status,
        checked_at=ai_checked_at,
        message=ai_message,
        evidence=ai_evidence,
        action=(
            "Start the local AI server (Ollama) or fix the provider in Settings; AI reviews are skipped until it answers."
            if configured else "Configure an AI provider in Settings; no AI review can run without one."
        ),
    ))

    root = _mapping(getattr(config, "config", {}))
    ml_records = _rows(_mapping(root.get("ml")).get("registry"))
    # Check that at least one registered model has its artifact file on disk
    trained_count = 0
    for rec in ml_records:
        artifact = str(rec.get("artifact_path", "")).strip()
        if artifact and Path(artifact).exists():
            trained_count += 1
        elif not artifact:
            trained_count += 1  # legacy record without path → count as present
    ml_status = "HEALTHY" if trained_count > 0 else ("UNVERIFIED" if ml_records else "UNAVAILABLE")
    ml_message = (
        f"{trained_count} trained ML model(s) registered and verified on disk."
        if trained_count > 0
        else (
            "ML registry has records but artifact files are missing — retrain needed."
            if ml_records
            else "No persisted ML model is registered."
        )
    )
    services.append(_service(
        "ml_model", "ML model", ml_status, checked_at=checked_at,
        message=ml_message,
        evidence={"configured_records": len(ml_records), "trained_on_disk": trained_count},
    ))

    # MLflow — live probe (short timeout, not part of the critical path)
    mlflow_uri = str(os.getenv("MLFLOW_TRACKING_URI", "")).strip()
    mlflow_healthy = False
    if mlflow_uri:
        try:
            from core.mlflow_server import probe as _mlflow_probe
            mlflow_healthy = _mlflow_probe(mlflow_uri, timeout=2.0)
        except Exception:
            pass
    services.append(_service(
        "mlflow", "MLflow", "HEALTHY" if mlflow_healthy else ("UNVERIFIED" if mlflow_uri else "NOT_CONFIGURED"),
        checked_at=checked_at,
        message=(
            f"MLflow tracking server is live at {mlflow_uri}." if mlflow_healthy
            else ("MLflow is configured but did not answer the /health probe." if mlflow_uri
                  else "MLflow is not configured.")
        ),
        evidence={"configured": bool(mlflow_uri), "uri": mlflow_uri, "live": mlflow_healthy},
        action="The MLflow tracking server is not answering; restart the server so it is started again (model training still works without it).",
    ))

    # RD-Agent — live probe
    rdagent_healthy = False
    rdagent_enabled = False
    try:
        from core.rdagent_worker import is_enabled as _rdagent_enabled, probe_rdagent as _probe_rdagent
        rdagent_enabled = _rdagent_enabled(config)
        if rdagent_enabled:
            rdagent_healthy = _probe_rdagent(config, timeout=2.0)
    except Exception:
        pass
    services.append(_service(
        "rd_agent", "RD-Agent worker",
        "HEALTHY" if rdagent_healthy else ("UNVERIFIED" if rdagent_enabled else "NOT_CONFIGURED"),
        checked_at=checked_at,
        message=(
            "RD-Agent worker is live and answering health probe." if rdagent_healthy
            else ("RD-Agent is enabled but the worker is not yet responding." if rdagent_enabled
                  else "RD-Agent is not configured.")
        ),
        evidence={"configured": rdagent_enabled, "live": rdagent_healthy},
        action="The RD-Agent worker is enabled but not answering; it may still be starting. If this persists, restart the server.",
    ))

    reconciliation = _mapping(execution.get("reconciliation"))
    reconciliation_status = str(reconciliation.get("status") or "UNVERIFIED").upper()
    reconciliation_summary = _reconciliation_summary(reconciliation)

    paper_status = "HEALTHY" if reconciliation_status in {"HEALTHY", "RECONCILED"} else ("UNHEALTHY" if reconciliation_status == "EXECUTION_STATE_CONFLICT" else "UNVERIFIED")
    services.append(_service(
        "paper_broker", "Paper Broker", paper_status,
        checked_at=reconciliation.get("checked_at"),
        message=(
            "Paper ledger reconciliation is healthy." if paper_status == "HEALTHY"
            else f"{reconciliation_summary['text']} New Paper Auto entries are blocked."
        ),
        evidence={
            "reconciliation_status": reconciliation_status,
            "conflict_count": reconciliation_summary["conflict_count"],
            "conflicts": reconciliation_summary["conflicts"],
        },
        action=_RECONCILIATION_ACTION,
    ))
    services.append(_service(
        "live_broker", "Live broker", "NOT_CONFIGURED", checked_at=checked_at,
        message="Live execution is intentionally unavailable in this Paper Trading build.",
        evidence={"live_execution_available": bool(execution.get("live_execution_available", False))},
    ))

    research_runtime = _mapping(runtime.get("strategy_research"))
    research_error = str(research_runtime.get("last_error") or "").strip()
    research_status = "UNHEALTHY" if research_error else ("HEALTHY" if research_runtime.get("running") else ("DISABLED" if research_runtime.get("enabled") is False else "UNAVAILABLE"))
    services.append(_service(
        "research_worker", "Research worker", research_status,
        checked_at=research_runtime.get("last_success") or research_runtime.get("last_run"),
        message="Strategy research worker is running." if research_status == "HEALTHY" else (f"Strategy research worker reports an error: {research_error[:160]}" if research_error else "Strategy research worker is not running."),
        evidence={"enabled": research_runtime.get("enabled"), "running": research_runtime.get("running")},
        action="Open the Agents tab and check the Strategy Research agent.",
    ))

    counts = Counter(str(item.get("status") or "UNVERIFIED") for item in services)
    critical_bad = any(
        item.get("status") == "UNHEALTHY" and item.get("id") in {"backend", "database", "paper_broker", "scheduler"}
        for item in services
    )
    # §98 aggregate semantics: DEGRADED means something ACTIVE is broken or
    # unverifiable (DEGRADED/UNHEALTHY/UNVERIFIED). Optional services that are
    # deliberately NOT_CONFIGURED and capability/data gaps (UNAVAILABLE — e.g.
    # no options evidence in the snapshot, no persisted ML model) stay visible
    # per-tile and in coverage_gaps; they are not system failures.
    overall = "UNHEALTHY" if critical_bad else (
        "DEGRADED" if any(
            item.get("status") in {"DEGRADED", "UNHEALTHY", "UNVERIFIED"} for item in services
        ) else "HEALTHY"
    )
    coverage_gaps = [
        {"id": item.get("id"), "name": item.get("label") or item.get("id")}
        for item in services if item.get("status") == "UNAVAILABLE"
    ]
    # The checks that actually drive the aggregate, worst first, each with its
    # message and the owner action - so "Unhealthy" is never shown without
    # saying WHICH check failed and what to do.
    severity_rank = {"UNHEALTHY": 0, "DEGRADED": 1, "UNVERIFIED": 2}
    failing = sorted(
        (
            {
                "id": item.get("id"),
                "label": item.get("label") or item.get("id"),
                "status": item.get("status"),
                "message": item.get("message"),
                "action": item.get("action"),
            }
            for item in services if item.get("status") in severity_rank
        ),
        key=lambda item: severity_rank.get(str(item.get("status")), 9),
    )
    if failing:
        names = ", ".join(str(item["label"]) for item in failing[:4])
        summary = f"{len(failing)} of {len(services)} checks need attention: {names}" + (" and more." if len(failing) > 4 else ".")
    else:
        summary = f"All {len(services) - len(coverage_gaps)} active checks pass" + (
            f"; {len(coverage_gaps)} optional data source(s) have no data." if coverage_gaps else "."
        )
    return {
        "state": "SUCCESS",
        "overall": overall,
        "summary": summary,
        "failing": failing,
        "services": services,
        "counts": dict(counts),
        "coverage_gaps": coverage_gaps,
        "checked_at": checked_at,
    }


def _entry_switch_blockers(agents: Mapping[str, Any], execution_cfg: Mapping[str, Any]) -> list[str]:
    """Switches outside the risk policy that stop every automatic paper entry."""
    blockers: list[str] = []
    bot = _mapping(_mapping(agents).get("bot"))
    # No bot status (agents source failed) is unknown, not "off".
    if bot and bot.get("running") is not True:
        blockers.append("Bot master switch is off")
    if _mapping(execution_cfg).get("autonomous_candidates_enabled") is not True:
        blockers.append("Internal candidate worker is off (agents.execution.autonomous_candidates_enabled)")
    return blockers


def build_cockpit_payload(
    *,
    config: Any,
    dashboard: Mapping[str, Any],
    capital_payload: Mapping[str, Any],
    pre_earnings: Mapping[str, Any],
    agents: Mapping[str, Any],
    execution: Mapping[str, Any],
    providers: list[dict[str, Any]],
    strategy_performance: Mapping[str, Any],
    data_health: Mapping[str, Any],
    database: Mapping[str, Any],
    registry: Optional[StrategyRegistry] = None,
    source_errors: Optional[Mapping[str, str]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    errors = dict(source_errors or {})
    root_config = _mapping(getattr(config, "config", {}))
    execution_cfg = _mapping(_mapping(root_config.get("agents")).get("execution"))
    pre_config = _mapping(root_config.get("pre_earnings"))
    capital = _mapping(capital_payload.get("capital")) or _mapping(capital_payload)
    capital_summary, positions = _capital_summary(capital, current)
    pre_items = _rows(pre_earnings.get("items"))
    earnings_rows = [
        _pre_earnings_row(item, execution_cfg, now=current, include_prefilter=False)
        for item in pre_items
    ]
    raw_item_by_row = {id(row): item for row, item in zip(earnings_rows, pre_items)}
    pre_freshness = _pre_earnings_freshness(pre_earnings, current)
    pre_ranking = _mapping(pre_earnings.get("ranking"))
    pre_calendar_universe = _mapping(pre_earnings.get("calendar_universe"))
    calendar_reporter_count = max(
        len(earnings_rows),
        _integer(pre_calendar_universe.get("reporter_count"))
        or _integer(pre_ranking.get("universe_event_count"))
        or 0,
    )
    upcoming = [
        item for item in earnings_rows
        if item.get("already_reported") is not True
        and item.get("event_status") != "REPORTED"
    ]
    for item in upcoming:
        event_time = _parse_dt(item.get("earnings_date"))
        if event_time is None:
            item["schedule_status"] = "DATE_UNAVAILABLE"
        elif event_time.date() < current.date():
            item["schedule_status"] = "PAST_DATE_UNRESOLVED"
        elif item.get("event_verified") is True:
            item["schedule_status"] = "DATE_VERIFIED"
        else:
            item["schedule_status"] = "DATE_ESTIMATED"
    watchlist_earnings = _watchlist_earnings(dashboard)
    watchlist_tickers = {row["ticker"] for row in watchlist_earnings}
    # Watchlist names first: the "Earnings in Nd" risk rows come from the
    # watchlist calendar, so those tickers must be visible in the 20-row matrix
    # instead of being buried among hundreds of same-day calendar reporters.
    upcoming.sort(key=lambda item: (
        str(item.get("ticker") or "").upper() not in watchlist_tickers,
        _parse_dt(item.get("earnings_date")) is None,
        str(item.get("earnings_date") or "9999"),
        -(_finite(item.get("score")) if _finite(item.get("score")) is not None else -1),
    ))
    # Only the displayed slice needs the (comparatively expensive) autonomous
    # prefilter and Paper Auto trace.
    displayed_earnings = upcoming[:_EARNINGS_DISPLAY_LIMIT]
    for item in displayed_earnings:
        _apply_pre_earnings_prefilter(
            item,
            _pre_earnings_prefilter(raw_item_by_row.get(id(item), {}), execution_cfg, now=current),
        )
    scanned_tickers = {str(item.get("ticker") or "").upper() for item in upcoming}
    for row in watchlist_earnings:
        row["in_scan"] = row["ticker"] in scanned_tickers
    dated_count = sum(1 for item in upcoming if _parse_dt(item.get("earnings_date")) is not None)
    verified_date_count = sum(1 for item in upcoming if item.get("schedule_status") == "DATE_VERIFIED")
    unavailable_date_count = sum(1 for item in upcoming if item.get("schedule_status") == "DATE_UNAVAILABLE")

    market = us_equity_market_status(current)
    market_payload = {
        "exchange": market.exchange,
        "is_open": market.is_open,
        "session": getattr(market.session, "value", str(market.session)),
        "reason": market.reason,
        "now_et": market.now_et.isoformat(),
        "open_time_et": market.open_time_et.isoformat(),
        "close_time_et": market.close_time_et.isoformat(),
        "is_half_day": market.is_half_day,
    }
    regime = _mapping(dashboard.get("dashboard_regime"))
    narrative = _mapping(dashboard.get("dashboard_market_narrative"))
    if narrative.get("available") is False:
        regime_available = False
    else:
        regime_available = bool(regime.get("regime") or narrative.get("risk_regime"))
    narrative_freshness = _narrative_freshness(narrative, current) if regime_available else {"state": "UNKNOWN"}

    risk_constitution = _mapping(_mapping(agents.get("config")).get("risk_constitution"))
    risk_status = "ACTIVE" if risk_constitution.get("status") == "active" else str(risk_constitution.get("status") or "UNCONFIGURED").upper()
    ai_health = _mapping(data_health.get("streams")).get("ollama_availability")
    ai_health = _mapping(ai_health)
    ai_verified = str(ai_health.get("level") or "").upper() == "VERIFIED" and ai_health.get("value") is True
    configured_providers = [
        row.get("name") for row in providers
        if row.get("enabled") is True and (row.get("has_key") is True or row.get("type") == "ollama")
    ]
    ai_config = _mapping(root_config.get("ai"))
    ai_assignment = _mapping(ai_config.get("assignment"))
    ollama_config = _mapping(root_config.get("ollama"))
    global_ai_model = ai_assignment.get("all") or ollama_config.get("model")
    readiness = _mapping(execution.get("readiness"))

    open_position_by_ticker = {
        str(item.get("ticker") or "").upper(): item
        for item in positions
        if str(item.get("ticker") or "").strip()
    }
    autonomy_mode = str(execution.get("mode") or "ANALYZE").upper()
    readiness_reasons = [str(value) for value in (readiness.get("reasons") or []) if str(value).strip()]
    for item in displayed_earnings:
        ticker = str(item.get("ticker") or "").upper()
        prefilter = _mapping(item.get("autonomous_prefilter"))
        execution_blockers = [dict(value) for value in _rows(prefilter.get("blockers"))]
        if autonomy_mode != "PAPER_AUTO":
            execution_blockers.append({
                "code": "paper_auto_mode_required",
                "message": f"Autonomy mode is {autonomy_mode}; PAPER_AUTO is required.",
                "actual": autonomy_mode,
                "required": "PAPER_AUTO",
            })
        if execution.get("kill_switch_active") is True:
            execution_blockers.append({
                "code": "kill_switch_active",
                "message": "Kill Switch blocks every new autonomous order.",
                "actual": True,
                "required": False,
            })
        if readiness and readiness.get("ready") is not True:
            execution_blockers.append({
                "code": "paper_readiness_blocked",
                "message": "Paper execution readiness is blocked"
                + (f": {', '.join(readiness_reasons[:4])}." if readiness_reasons else "."),
                "actual": readiness_reasons or False,
                "required": True,
            })
        if not market.is_open:
            execution_blockers.append({
                "code": "regular_session_required",
                "message": f"US equities can open only during the regular session ({market.reason}).",
                "actual": getattr(market.session, "value", str(market.session)),
                "required": "REGULAR",
            })
        position = open_position_by_ticker.get(ticker)
        blocker_codes = {
            str(blocker.get("code") or "") for blocker in execution_blockers
        }
        if position:
            trace_status = "POSITION_OPEN"
            why_no_position = None
        elif execution_blockers:
            trace_status = (
                "WAITING_FOR_ENTRY_WINDOW"
                if blocker_codes == {"outside_pre_earnings_window"}
                else "BLOCKED"
            )
            why_no_position = execution_blockers[0].get("message")
        else:
            trace_status = "READY_FOR_AI_RISK_CAPITAL"
            why_no_position = "Visible prefilters pass; the next autonomous cycle must still pass AI critic, Risk, Capital and Paper Broker."
        prefilter_ok = bool(prefilter.get("ranking_eligible"))
        window_open = bool(prefilter.get("window_open"))
        downstream_state = "PENDING" if not execution_blockers else "NOT_REACHED"
        item["paper_auto_trace"] = {
            "status": trace_status,
            "why_no_position": why_no_position,
            "position_id": position.get("position_id") if position else None,
            "blockers": execution_blockers,
            "first_blocker": execution_blockers[0] if execution_blockers else None,
            "stages": [
                {"name": "Pre-Earnings recommendation", "status": "PASS" if item.get("recommendation_eligible") else str(item.get("recommendation_status") or "WATCH")},
                {"name": "Autonomous prefilter", "status": "PASS" if prefilter_ok else "BLOCKED"},
                {"name": "Pre-Earnings entry window", "status": "PASS" if window_open else ("WAITING" if prefilter_ok else "BLOCKED")},
                {"name": "US regular session", "status": "PASS" if market.is_open else "BLOCKED"},
                {"name": "AI + independent critic", "status": "PASS" if position else downstream_state},
                {"name": "Risk Gate", "status": "PASS" if position else downstream_state},
                {"name": "Capital allocation", "status": "PASS" if position else downstream_state},
                {"name": "Paper order / fill / position", "status": "PASS" if position else downstream_state},
            ],
        }

    strategy_lab = _strategy_lab(strategy_performance, registry or StrategyRegistry())
    active_constitution = _mapping(risk_constitution.get("active"))
    lifecycle_records = _rows(strategy_lab.get("records"))
    approved_strategies = sum(
        1 for row in lifecycle_records
        if str(row.get("status") or "").upper() in {"APPROVED", "CHAMPION"}
    )
    ledger_governance = {
        "minimum_cash_pct": active_constitution.get("minimum_cash_pct"),
        "max_liquidity_participation_pct": active_constitution.get("max_liquidity_participation_pct"),
        "block_data_conflicts": active_constitution.get("block_data_conflicts"),
        "require_approved_strategy": active_constitution.get("require_approved_strategy"),
        "approved_strategies": approved_strategies,
        "strategy_total": len(lifecycle_records),
    }
    health = _system_health(
        config=config,
        agents=agents,
        execution=execution,
        pre_earnings=pre_earnings,
        providers=providers,
        data_health=data_health,
        database=database,
        now=current,
    )

    # One truth for "AI status": the KPI reuses the System Health tile, which
    # also runs the live local-provider probe. Deriving it separately from the
    # process-local stream showed "Unverified" beside a HEALTHY tile.
    ai_service = next((item for item in health.get("services", []) if item.get("id") == "ai_providers"), {})
    ai_header_status = str(ai_service.get("status") or (
        "HEALTHY" if ai_verified else ("UNVERIFIED" if configured_providers else "NOT_CONFIGURED")
    ))
    ai_header_source = _mapping(ai_service.get("evidence")).get("verified_provider") or (
        ai_health.get("source") if ai_verified else None
    )
    major_news_rows, major_news_meta = _major_news(dashboard, current)
    decision_ledger = _ai_decisions_section(capital, errors, governance=ledger_governance)
    sections = {
        "opportunities": _section(
            _opportunity_rows(capital, earnings_rows),
            error=errors.get("capital") or errors.get("pre_earnings"),
            note="No current setup clears the real Capital or Pre-Earnings eligibility gates.",
        ),
        "risks": _section(
            _risk_rows(dashboard, execution, positions),
            error=errors.get("dashboard") or errors.get("execution"),
            note="No current risk evidence was reported.",
        ),
        "upcoming_earnings": _section(
            displayed_earnings,
            error=errors.get("pre_earnings"),
            note=_upcoming_earnings_note(pre_freshness, watchlist_earnings),
            extra={
                "count": len(upcoming),
                "source_freshness": pre_freshness,
                "stale": pre_freshness.get("state") != "FRESH",
                "watchlist_earnings": watchlist_earnings,
                "schedule_counts": {
                    "active_candidates": len(upcoming),
                    "analyzed_candidates": len(earnings_rows),
                    "calendar_reporters": calendar_reporter_count,
                    "light_scanned": _integer(pre_ranking.get("light_scanned_count")) or calendar_reporter_count,
                    "deep_scored": _integer(pre_ranking.get("deep_scored_count")) or len(earnings_rows),
                    "ai_reviewed": _integer(pre_ranking.get("ai_reviewed_count")) or 0,
                    "reported_today": len(_rows(pre_earnings.get("reported_today"))),
                    "displayed_candidates": len(displayed_earnings),
                    "dated": dated_count,
                    "verified_dates": verified_date_count,
                    "date_unavailable": unavailable_date_count,
                },
                "refresh_policy": {
                    "full_refresh_seconds": _integer(pre_earnings.get("refresh_seconds")) or _integer(pre_config.get("refresh_seconds")) or 600,
                    "continuous_scan_enabled": bool(_mapping(pre_config.get("continuous_scan")).get("enabled", False)),
                    "continuous_scan_seconds": _integer(_mapping(pre_config.get("continuous_scan")).get("interval_seconds")),
                    "max_deep_results": _integer(pre_config.get("max_results")),
                    "ai_max_tickers_per_cycle": _integer(pre_config.get("ollama_max_tickers")),
                    "news_change_reanalysis": bool(pre_config.get("news_change_reanalysis", False)),
                },
            },
        ),
        "sentiment_shifts": _sentiment_section(dashboard, current) if not errors.get("dashboard") else _section(error=errors["dashboard"]),
        "major_news": _section(
            major_news_rows,
            error=errors.get("dashboard"),
            note=(
                f"No high/critical-impact news was published in the last {major_news_meta['window_hours']} hours"
                + (f" ({major_news_meta['dropped_stale']} older item(s) are not shown as current)." if major_news_meta["dropped_stale"] else ".")
            ),
            extra=major_news_meta,
        ),
        "ai_decisions": _compact_decisions_section(decision_ledger),
        "decision_replay": _decision_replay_section(),
        "model_lab": _model_lab_section(),
        "active_positions": _section(
            positions,
            error=errors.get("capital"),
            note="No Capital position is open.",
        ),
        "strategy_degradation": _section(
            strategy_lab.get("degraded") or [],
            error=strategy_lab.get("message") if strategy_lab.get("state") == "ERROR" else None,
            note="No Strategy Registry record is currently marked DEGRADED.",
        ),
        "research_activity": _research_section(agents, strategy_performance),
        "autonomous_repair": _autonomous_repair_section(),
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "state": "SUCCESS",
        "generated_at": current.isoformat(),
        "header": {
            "version": "V9",
            "market": market_payload,
            "autonomy": {
                "mode": str(execution.get("mode") or "ANALYZE").upper(),
                # The policy readiness alone said "Paper entry ready" while the
                # bot was stopped; the switches that also stop every automatic
                # entry are part of the answer (same gates as the Agents tab).
                "ready": bool(readiness.get("ready")) and not _entry_switch_blockers(agents, execution_cfg),
                "reasons": [str(value) for value in (readiness.get("reasons") or [])]
                + (_entry_switch_blockers(agents, execution_cfg) if readiness else []),
                # evaluated=False marks an execution-source failure: readiness
                # was never computed — the UI must say "unknown", not "0 blockers".
                "evaluated": bool(readiness),
            },
            "ai": {
                "status": ai_header_status,
                "configured_providers": configured_providers,
                "verified_source": ai_header_source,
                "global_model": global_ai_model,
            },
            "regime": {
                "available": regime_available,
                "value": (regime.get("regime") or narrative.get("risk_regime")) if regime_available else None,
                "sentiment": regime.get("sentiment") if regime_available else None,
                "confidence": _finite(regime.get("confidence")) if regime_available else None,
                "generated_at": narrative.get("generated_at") if regime_available else None,
                # STALE = the narrative behind this regime is older than its
                # refresh horizon; the KPI must show its age, not "current".
                "freshness": narrative_freshness.get("state") if regime_available else None,
            },
            "capital": capital_summary,
            "risk": {
                "status": risk_status,
                "constitution_fingerprint": risk_constitution.get("fingerprint"),
                "readiness": bool(readiness.get("ready")),
            },
            "kill_switch_active": bool(execution.get("kill_switch_active", False)),
            "reduce_fraction_pct": execution.get("reduce_fraction_pct"),
        },
        "sections": sections,
        "strategy_lab": strategy_lab,
        "decision_ledger": decision_ledger,
        # 2026-09-03 field audit: cockpit.js reads these from the TOP level
        # (cockpit.js:630/632) — they were only under "sections", so the
        # Point-in-time replays and Model Lab cards could never show data.
        "decision_replay": sections.get("decision_replay"),
        "model_lab": sections.get("model_lab"),
        "continuous_scan": _continuous_scan_evidence(),
        "research_factory": sections["research_activity"],
        "system_health": health,
        "source_freshness": {"pre_earnings": pre_freshness},
        "source_errors": errors,
        "deferred": {},
    }


_SLOW_BUILD_LOG_SECONDS = 2.0


def _load_sources_concurrently(
    loaders: Mapping[str, tuple[str, Callable[[], Any], Any]],
) -> tuple[dict[str, Any], dict[str, Optional[str]], dict[str, float]]:
    """Run the independent source loaders side by side.

    The sources are separate read models (files, SQLite, in-memory agents,
    one bulk quote call inside Capital) and used to run one after another, so
    a slow source delayed every other one. Each loader keeps its own
    ``_safe_source`` isolation: a failure or a slow source never corrupts the
    rest. The caller's request context (cache-only policy) is copied into
    every worker so no loader may start a remote refresh.
    """
    import contextvars
    import time as _time
    from concurrent.futures import ThreadPoolExecutor

    values: dict[str, Any] = {}
    errors: dict[str, Optional[str]] = {}
    timings: dict[str, float] = {}

    def run(key: str) -> tuple[str, Any, Optional[str], float]:
        label, loader, fallback = loaders[key]
        started = _time.perf_counter()
        value, error = _safe_source(label, loader, fallback)
        return key, value, error, _time.perf_counter() - started

    with ThreadPoolExecutor(max_workers=max(1, min(6, len(loaders))), thread_name_prefix="cockpit-src") as pool:
        futures = [pool.submit(contextvars.copy_context().run, run, key) for key in loaders]
        for future in futures:
            key, value, error, seconds = future.result()
            values[key] = value
            errors[key] = error
            timings[key] = round(seconds, 3)
    return values, errors, timings


def get_cockpit_payload() -> dict[str, Any]:
    """Collect already-available read models and build one cache-first payload."""
    from core.request_context import get_request_context, request_context

    # Background rebuild threads (stale-while-revalidate, warm-up) carry no
    # request context. Without one the dashboard builder defaults to
    # allow_remote=True and refreshes the whole watchlist from the quote
    # provider inside this "read model". Pin the cache-only policy here so the
    # cockpit never triggers provider I/O, whichever thread builds it.
    existing = get_request_context()
    if existing is not None and bool(getattr(existing, "cache_only", False)):
        return _collect_cockpit_payload()
    with request_context(reason="COCKPIT_READ_MODEL", path="/api/cockpit", cache_only=True):
        return _collect_cockpit_payload()


def _collect_cockpit_payload() -> dict[str, Any]:
    import time as _time

    build_started = _time.perf_counter()
    config = get_shared_config()

    def load_agents() -> dict[str, Any]:
        from core.bot_controller import BotController

        engine_running = is_engine_running()
        bot_status = BotController.get_instance(config_manager=config).get_control_status()
        runtimes = snapshot_agent_runtimes(
            config=config,
            observed_status=snapshot_agent_status(),
            engine_running=engine_running,
            agent_names=CONTROL_PLANE_AGENTS,
        )
        return build_control_plane_payload(
            config=config,
            bot_status=bot_status,
            agents_status=runtimes,
            engine_running=engine_running,
        )

    loaders: dict[str, tuple[str, Callable[[], Any], Any]] = {
        "dashboard": ("Dashboard data", get_dashboard_payload, {}),
        "capital": (
            "Capital data",
            lambda: get_capital_state(live_prices=False, include_opportunities=True, include_decisions=True),
            {},
        ),
        "pre_earnings": ("Pre-Earnings data", _read_pre_earnings_cache, {}),
        "execution": ("Paper execution data", lambda: get_execution_control(config), {}),
        "providers": (
            "AI provider configuration",
            lambda: list_ai_providers(_mapping(getattr(config, "config", {}))),
            [],
        ),
        "strategy_performance": ("Strategy performance", get_strategy_performance, {"entries": []}),
        "data_health": (
            "Data health",
            lambda: get_health_dashboard().get_live_dashboard(),
            {"streams": {}},
        ),
        "agents": ("Agent runtime data", load_agents, {}),
    }
    values, source_errors, timings = _load_sources_concurrently(loaders)
    database = _database_check()
    errors = {key: value for key, value in source_errors.items() if value}
    sources_done = _time.perf_counter()
    payload = build_cockpit_payload(
        config=config,
        dashboard=_mapping(values.get("dashboard")),
        capital_payload=_mapping(values.get("capital")),
        pre_earnings=_mapping(values.get("pre_earnings")),
        agents=_mapping(values.get("agents")),
        execution=_mapping(values.get("execution")),
        providers=_rows(values.get("providers")),
        strategy_performance=_mapping(values.get("strategy_performance")),
        data_health=_mapping(values.get("data_health")),
        database=database,
        source_errors=errors,
    )
    total = _time.perf_counter() - build_started
    if total >= _SLOW_BUILD_LOG_SECONDS:
        # Evidence for the next slow first load: which source the time went to.
        logger.warning(
            "Cockpit build took %.1fs (sources %.1fs, assemble %.1fs); per source: %s",
            total, sources_done - build_started, _time.perf_counter() - sources_done,
            ", ".join(f"{key}={seconds:.2f}s" for key, seconds in sorted(timings.items(), key=lambda kv: -kv[1])),
        )
    return payload


__all__ = [
    "ASYNC_STATES",
    "SCHEMA_VERSION",
    "build_cockpit_payload",
    "get_cockpit_payload",
]
