"""Shared same-instrument direction conflict checks.

Every entry path must agree on one invariant: a paper-capital portfolio cannot
hold LONG and SHORT positions for the same instrument at the same time. A
reversal is an explicit close-then-open operation, never an accidental hedge
created by two independent strategy agents.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional


def normalize_direction(value: Any) -> str:
    side = str(value or "").upper().strip()
    if side in {"BUY", "LONG", "OPEN_LONG"}:
        return "LONG"
    if side in {"SELL", "SHORT", "OPEN_SHORT"}:
        return "SHORT"
    return ""


def normalize_ticker(value: Any) -> str:
    # BTCUSDT, BTC-USDT and BTC/USDT are the same instrument as BTC-USD.
    text = str(value or "").upper().strip()
    return re.sub(r"[-/]?USDT$", "-USD", text)


def position_type(position: Dict[str, Any]) -> str:
    explicit = str((position or {}).get("position_type") or "").lower().strip()
    if explicit:
        return explicit
    if bool((position or {}).get("research_only")):
        return "research_cohort"
    return "paper_signal"


def opposing_open_positions(
    positions: Iterable[Dict[str, Any]],
    *,
    ticker: Any,
    proposed_direction: Any,
    comparable_position_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return open positions on the same ticker with the opposite direction."""
    symbol = normalize_ticker(ticker)
    direction = normalize_direction(proposed_direction)
    wanted_type = str(comparable_position_type or "").lower().strip()
    if not symbol or not direction:
        return []

    conflicts: List[Dict[str, Any]] = []
    for raw in positions or []:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("status") or "").upper().strip() != "OPEN":
            continue
        if normalize_ticker(raw.get("ticker") or raw.get("symbol")) != symbol:
            continue
        if wanted_type and position_type(raw) != wanted_type:
            continue
        existing_direction = normalize_direction(raw.get("direction") or raw.get("action"))
        if existing_direction and existing_direction != direction:
            conflicts.append(dict(raw))
    return conflicts


def conflict_summary(conflicts: Iterable[Dict[str, Any]], *, ticker: Any, proposed_direction: Any) -> str:
    rows = list(conflicts or [])
    existing = sorted({normalize_direction(row.get("direction")) for row in rows if normalize_direction(row.get("direction"))})
    ids = [str(row.get("position_id") or "").strip() for row in rows if str(row.get("position_id") or "").strip()]
    return (
        f"Opposite open position for {normalize_ticker(ticker)}: "
        f"existing {','.join(existing) or 'UNKNOWN'}, proposed {normalize_direction(proposed_direction) or 'UNKNOWN'}"
        + (f" ({', '.join(ids[:3])})" if ids else "")
        + "; close/reverse explicitly before opening the new direction"
    )
