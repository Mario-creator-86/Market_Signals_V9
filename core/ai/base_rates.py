"""Realized strategy base rates for the AI decision context (P2, 2026-09-02).

History finding that motivated this: the §41 reviewer reasoned about quality
from signal scores alone while the system's own realized record told a
different story (pre-earnings accepts ran ~18% win rate, avg -2.2%/trade,
while scanner entries were net profitable). Feeding the model the system's
own closed-trade base rates per strategy makes "edge marginal" a measured
statement instead of an impression.

Evidence-only by design (owner spec): never a veto, never a size input on
its own — the existing confidence/gap-risk gates own the decisions.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from core.sqlite_store import DB_PATH

logger = logging.getLogger("AgentManager")

_CACHE_TTL_SECONDS = 600.0
_MAX_POSITIONS_SCANNED = 600
_MIN_SAMPLE = 3  # below this the bucket is not prompt-worthy
_SMALL_SAMPLE = 30

_cache: Dict[str, Any] = {"at": 0.0, "by_strategy": {}}
_lock = threading.Lock()


def _family(strategy_type: str) -> str:
    s = str(strategy_type or "").upper().strip()
    if s.startswith("PRE_EARNINGS"):
        return "PRE_EARNINGS"
    if s.startswith("SCANNER"):
        return "SCANNER"
    return s.split("_")[0] if s else ""


def _position_pnl_pct(pos: Dict[str, Any]) -> Optional[float]:
    """Price P&L % of one closed position, or None when it cannot be known.

    A position without a recognisable direction is skipped: it used to be
    counted as LONG, which turns a winning short into a loss in the base rates
    the reviewer is told to calibrate on. One unreadable row is skipped too,
    instead of discarding every base rate.
    """
    try:
        entry = float(pos.get("entry_price") or 0.0)
        exit_price = float(pos.get("exit_price") or 0.0)
    except (TypeError, ValueError):
        return None
    if not (entry > 0 and exit_price > 0) or entry == float("inf") or exit_price == float("inf"):
        return None
    direction = str(pos.get("direction") or "").upper().strip()
    raw_pct = (exit_price - entry) / entry * 100.0
    if direction in ("LONG", "BUY"):
        return raw_pct
    if direction in ("SHORT", "SELL"):
        return -raw_pct
    return None


def _is_late_exit(pos: Dict[str, Any]) -> bool:
    """True when the exit was executed long after it was due (an outage), not by the strategy.

    The same rule the Capital evidence gate uses (core.portfolio.evidence): a
    position already marked ``evidence_excluded``, or one whose close came long
    after its exit was due, or - for old positions without first-touch tracking -
    whose exit lies several times the planned distance beyond its stop or target.
    Such a trade is a real paper result but does not measure the strategy.
    """
    try:
        from core.portfolio.evidence import assess_exit_delay, is_evidence_excluded

        if is_evidence_excluded(pos):
            return True
        return bool(assess_exit_delay(pos, closed_at=pos.get("closed_at"), exit_price=pos.get("exit_price")))
    except Exception:
        return False


def _closed_pnl_pct() -> List[Dict[str, Any]]:
    """(strategy_type, pnl_pct) for closed capital positions from the kv store."""
    try:
        con = sqlite3.connect(str(DB_PATH), timeout=2.0)
        try:
            row = con.execute(
                "SELECT value FROM kv_store WHERE key='capital_state'"
            ).fetchone()
        finally:
            con.close()
        if not row:
            return []
        data = json.loads(row[0])
        positions = data.get("positions", []) if isinstance(data, dict) else []
        out: List[Dict[str, Any]] = []
        for pos in (positions or [])[-_MAX_POSITIONS_SCANNED:]:
            if not isinstance(pos, dict) or str(pos.get("status") or "").upper() != "CLOSED":
                continue
            row_pnl = _position_pnl_pct(pos)
            if row_pnl is None:
                continue
            out.append({"strategy_type": str(pos.get("strategy_type") or ""), "pnl_pct": row_pnl,
                        "late_exit": _is_late_exit(pos)})
        return out
    except Exception:
        return []


def _summarize(values: List[float]) -> Optional[Dict[str, Any]]:
    if len(values) < _MIN_SAMPLE:
        return None  # below small-sample floor: not prompt-worthy
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v <= 0]
    return {
        "n": len(values),
        "win_rate_pct": round(len(wins) / len(values) * 100.0, 1),
        "avg_pnl_pct": round(sum(values) / len(values), 2),
        "avg_win_pct": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss_pct": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "small_sample": len(values) < _SMALL_SAMPLE,
    }


def _compute_all() -> Dict[str, Dict[str, Any]]:
    rows = _closed_pnl_pct()
    if not rows:
        return {}
    by_exact: Dict[str, List[float]] = {}
    by_family: Dict[str, List[float]] = {}
    late: Dict[str, List[float]] = {}
    for row in rows:
        st = row["strategy_type"]
        if row.get("late_exit"):
            # Kept out of the averages, reported separately so nothing is hidden.
            late.setdefault(st, []).append(row["pnl_pct"])
            by_exact.setdefault(st, [])
            continue
        by_exact.setdefault(st, []).append(row["pnl_pct"])
        fam = _family(st)
        if fam:
            by_family.setdefault(fam, []).append(row["pnl_pct"])
    out: Dict[str, Dict[str, Any]] = {}
    for st in by_exact:
        payload: Dict[str, Any] = {
            "strategy": st,
            "exact": _summarize(by_exact.get(st, [])),
            "family": _summarize(by_family.get(_family(st), [])),
            "note": (
                "The system's own realized outcomes for this strategy (closed paper "
                "trades). Evidence for calibration only — never a veto and never a "
                "size input on its own. Treat small samples with explicit caution."
            ),
        }
        excluded = late.get(st) or []
        if excluded:
            payload["excluded_late_exits"] = {
                "n": len(excluded),
                "avg_pnl_pct": round(sum(excluded) / len(excluded), 2),
                "note": (
                    "Trades of this strategy whose exit was executed long after it was due (the app was "
                    "not monitoring, so the close came late and far beyond the stop or target). They are "
                    "real paper results but not a measurement of the strategy, and are left out above."
                ),
            }
        if payload["exact"] or payload["family"]:
            out[st] = payload
    return out


def strategy_base_rates(strategy_type: str) -> Dict[str, Any]:
    """Realized base rates for one strategy type (cached 10 min, fail-open)."""
    with _lock:
        now = time.monotonic()
        if now - float(_cache["at"]) >= _CACHE_TTL_SECONDS:
            try:
                _cache["by_strategy"] = _compute_all()
            except Exception:
                logger.exception("base rate computation failed")
                _cache["by_strategy"] = {}
            _cache["at"] = now
        return dict(_cache["by_strategy"].get(str(strategy_type or "")) or {})


__all__ = ["strategy_base_rates"]
