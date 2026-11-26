"""Which closed paper trades count as strategy evidence.

A position whose exit was due but could not be executed for days (an outage)
closes far away from its stop or target. The result is a real paper result: it
stays in the ledger, in cash and in equity. It is not a measurement of the
strategy, so it carries an explicit, auditable marker and is left out of the
win rate, profit factor, expectancy, break-even comparison and the stopping
rule sample. Every summary that leaves trades out reports how many and their
total P&L.

Pure functions; nothing here opens, closes or sizes a position.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

EXIT_DELAY_EXCLUSION_REASON = "exit_delayed_by_outage"
DEFAULT_EXIT_DELAY_EXCLUSION_HOURS = 72.0
# Without a recorded first touch, an exit this many planned distances beyond
# the stop/target cannot be an on-time exit.
OVERSHOOT_MULTIPLE = 3.0

BASIS_FIRST_TOUCH = "first_touch_recorded"
BASIS_FIRST_TOUCH_UPPER_BOUND = "earliest_record_of_touch"
BASIS_TIME_STOP_DUE = "time_stop_due"
BASIS_OVERSHOOT = "exit_price_beyond_level"

SINCE_BASIS_OBSERVED = "observed"
# The exit condition was already recorded as met before first-touch tracking
# existed; the true first touch is earlier and unknown.
SINCE_BASIS_LEGACY = "legacy_earliest_record"

VALIDATION_STATUS_MEASURED = "measured"
VALIDATION_STATUS_UNAVAILABLE = "unavailable"
# Price-first autonomous candidates used to store this literal instead of a
# measured validation score (see core/autonomous_candidates.py).
_LEGACY_UNMEASURED_PLACEHOLDER = 55.0


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def exit_condition_kind(reason: Any) -> str:
    """Normalise an exit reason to the mechanical condition it stands for.

    '' for discretionary reasons (manual close, AI review): those are not a
    condition that, once met, fixes the strategy's result.
    """
    text = str(reason or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not text or text.startswith("manual"):
        return ""
    for prefix in ("take_profit", "stop_loss", "trailing_stop"):
        if text.startswith(prefix):
            return prefix
    if text.startswith(("scanner_time_stop", "regime_time_stop", "time_exit")):
        return "time_exit"
    return ""


def exit_delay_exclusion_hours(config: Any) -> float:
    """``capital_evidence.exit_delay_exclusion_hours`` (default 72, must be > 0)."""
    root = config if isinstance(config, Mapping) else getattr(config, "config", None)
    section = root.get("capital_evidence") if isinstance(root, Mapping) else None
    value = _finite(section.get("exit_delay_exclusion_hours")) if isinstance(section, Mapping) else None
    if value is None or value <= 0:
        return DEFAULT_EXIT_DELAY_EXCLUSION_HOURS
    return value


def _overshoot_multiple(position: Mapping[str, Any], exit_price: float) -> Tuple[Optional[float], str]:
    """How many planned distances the exit lies beyond the target or the stop."""
    entry = _finite(position.get("entry_price"))
    if entry is None or entry <= 0 or exit_price <= 0:
        return None, ""
    is_long = str(position.get("direction") or "LONG").upper() not in {"SHORT", "SELL"}
    target = _finite(position.get("take_profit_price"))
    stop = _finite(position.get("stop_loss_price"))
    best: Tuple[Optional[float], str] = (None, "")
    if target is not None and target > 0:
        planned = abs(target - entry)
        beyond = (exit_price - target) if is_long else (target - exit_price)
        if planned > 0 and beyond > 0:
            best = (beyond / planned, "take_profit")
    if stop is not None and stop > 0:
        planned = abs(entry - stop)
        beyond = (stop - exit_price) if is_long else (exit_price - stop)
        if planned > 0 and beyond > 0 and (best[0] is None or beyond / planned > best[0]):
            best = (beyond / planned, "stop_loss")
    return best


def exit_due_since(
    position: Mapping[str, Any],
    *,
    scanner_time_stop_minutes: float = 0.0,
) -> Optional[Tuple[datetime, str]]:
    """Earliest known moment the position's exit was due, with its basis.

    The single fact shared by the evidence rule (``assess_exit_delay``) and
    the Capital read model: the recorded first touch of the stop/target
    (``exit_condition_since``) or, for scanner positions, the strategy's own
    time stop (``opened_at`` + ``scanner_time_stop_minutes``), whichever is
    earlier. ``None`` when neither is known.
    """
    tracked = position.get("exit_condition_tracked") is True
    since = _timestamp(position.get("exit_condition_since"))
    since_basis = str(position.get("exit_condition_since_basis") or "").strip().lower()
    if since is not None and not since_basis:
        since_basis = SINCE_BASIS_OBSERVED if tracked else SINCE_BASIS_LEGACY
    candidates: List[Tuple[datetime, str]] = []
    if since is not None:
        candidates.append((
            since,
            BASIS_FIRST_TOUCH if since_basis == SINCE_BASIS_OBSERVED else BASIS_FIRST_TOUCH_UPPER_BOUND,
        ))
    minutes = _finite(scanner_time_stop_minutes) or 0.0
    opened = _timestamp(position.get("opened_at"))
    if (
        minutes > 0
        and opened is not None
        and str(position.get("strategy_type") or "").upper().startswith("SCANNER")
    ):
        candidates.append((opened + timedelta(minutes=minutes), BASIS_TIME_STOP_DUE))
    # A regime that was only tested with a time stop carries it on the position.
    from core.autonomous_candidates import position_time_stop_minutes

    regime_minutes = position_time_stop_minutes(position)
    if regime_minutes > 0 and opened is not None:
        candidates.append((opened + timedelta(minutes=regime_minutes), BASIS_TIME_STOP_DUE))
    return min(candidates, key=lambda item: item[0]) if candidates else None


def assess_exit_delay(
    position: Mapping[str, Any],
    *,
    closed_at: Any,
    exit_price: Any,
    threshold_hours: float = DEFAULT_EXIT_DELAY_EXCLUSION_HOURS,
    overshoot_multiple: float = OVERSHOOT_MULTIPLE,
    scanner_time_stop_minutes: float = 0.0,
) -> Dict[str, Any]:
    """Marker fields for a trade whose exit was delayed by an outage, else ``{}``.

    Branch 1 (time): the close happened more than ``threshold_hours`` after the
    earliest known moment the position's exit was due. Known moments are the
    recorded first touch of its stop/target (``exit_condition_since``) and, for
    scanner positions, the strategy's own time stop (``opened_at`` +
    ``scanner_time_stop_minutes``).

    Branch 2 (price): only when the first touch is unknown, i.e. the position
    predates first-touch tracking and no known moment is old enough. The exit
    lies beyond the target/stop by more than ``overshoot_multiple`` times the
    planned distance. A position tracked from its open is never judged by
    price alone: a genuine gap through the stop is strategy evidence.
    """
    closed = _timestamp(closed_at)
    price = _finite(exit_price)
    threshold = _finite(threshold_hours)
    if threshold is None or threshold <= 0:
        threshold = DEFAULT_EXIT_DELAY_EXCLUSION_HOURS
    tracked = position.get("exit_condition_tracked") is True
    since = _timestamp(position.get("exit_condition_since"))
    since_basis = str(position.get("exit_condition_since_basis") or "").strip().lower()
    if since is not None and not since_basis:
        since_basis = SINCE_BASIS_OBSERVED if tracked else SINCE_BASIS_LEGACY

    known_due = exit_due_since(position, scanner_time_stop_minutes=scanner_time_stop_minutes)

    base = {
        "evidence_excluded": True,
        "evidence_exclusion_reason": EXIT_DELAY_EXCLUSION_REASON,
        "evidence_exclusion_threshold_hours": round(threshold, 4),
    }
    if closed is not None and known_due is not None:
        due, basis = known_due
        delay_hours = (closed - due).total_seconds() / 3600.0
        if delay_hours > threshold:
            return {
                **base,
                "evidence_exclusion_basis": basis,
                "evidence_exclusion_delay_hours": round(delay_hours, 2),
                "evidence_exclusion_exit_due_since": due.isoformat(),
                "evidence_exclusion_note": (
                    f"Closed {delay_hours:.1f} h after its exit was due "
                    f"({basis.replace('_', ' ')}); limit {threshold:g} h."
                ),
            }
    first_touch_known = tracked or (since is not None and since_basis == SINCE_BASIS_OBSERVED)
    if first_touch_known or price is None:
        return {}
    multiple, level = _overshoot_multiple(position, price)
    limit = _finite(overshoot_multiple) or OVERSHOOT_MULTIPLE
    if multiple is not None and multiple > limit:
        return {
            **base,
            "evidence_exclusion_basis": BASIS_OVERSHOOT,
            # The first touch was never recorded, so the delay is not measurable.
            "evidence_exclusion_delay_hours": None,
            "evidence_exclusion_overshoot_multiple": round(multiple, 2),
            "evidence_exclusion_level": level,
            "evidence_exclusion_note": (
                f"First touch of the {level.replace('_', ' ')} was not recorded; the exit is "
                f"{multiple:.1f}x the planned distance beyond it (limit {limit:g}x)."
            ),
        }
    return {}


def is_evidence_excluded(trade: Any) -> bool:
    return isinstance(trade, Mapping) and trade.get("evidence_excluded") is True


def split_evidence(trades: Iterable[Any]) -> Tuple[List[Any], List[Any]]:
    """(counted, excluded) preserving order."""
    counted: List[Any] = []
    excluded: List[Any] = []
    for trade in trades or []:
        (excluded if is_evidence_excluded(trade) else counted).append(trade)
    return counted, excluded


def excluded_trades_summary(
    excluded: Iterable[Any],
    *,
    pnl_fields: Tuple[str, ...] = ("net_pnl", "pnl", "pnl_usd"),
) -> Dict[str, Any]:
    """Count and total P&L of the trades a summary left out, by reason."""
    count = 0
    total = 0.0
    reasons: Dict[str, int] = {}
    for trade in excluded or []:
        if not isinstance(trade, Mapping):
            continue
        count += 1
        for field in pnl_fields:
            value = _finite(trade.get(field))
            if value is not None:
                total += value
                break
        reason = str(trade.get("evidence_exclusion_reason") or "unspecified")
        reasons[reason] = reasons.get(reason, 0) + 1
    total = round(total, 4)
    return {
        "count": count,
        "total_pnl_usd": total,
        # Same number under the name the Capital tab reads.
        "net_pnl": total,
        "reasons": reasons,
    }


def validation_fields(position: Mapping[str, Any]) -> Dict[str, Any]:
    """The position's validation score as measured, or ``None`` + "unavailable".

    Reporting only: no gate reads this. Records written before the status
    existed stored a missing score as 0.0, and price-first autonomous
    candidates (scanner / pre-earnings), which have no validation at all,
    stored the literal 55.0.
    """
    decision = position.get("decision_context") if isinstance(position.get("decision_context"), Mapping) else {}
    confidence = decision.get("confidence") if isinstance(decision.get("confidence"), Mapping) else {}
    signal = decision.get("signal") if isinstance(decision.get("signal"), Mapping) else {}
    declared = str(
        confidence.get("validation_status")
        or confidence.get("validation_score_status")
        or signal.get("validation_score_status")
        or position.get("validation_status")
        or ""
    ).strip().lower()
    score = _finite(confidence.get("validation_score"))
    unavailable = {"validation_score": None, "validation_status": VALIDATION_STATUS_UNAVAILABLE}
    if declared == VALIDATION_STATUS_UNAVAILABLE or score is None:
        return unavailable
    if declared:
        return {"validation_score": score, "validation_status": declared}
    # Legacy record without a status.
    if score == 0.0:
        return unavailable
    strategy = str(position.get("strategy_type") or decision.get("strategy_type") or "").upper()
    price_first = strategy.startswith("SCANNER_AI_") or strategy.startswith("PRE_EARNINGS_")
    if price_first and score == _LEGACY_UNMEASURED_PLACEHOLDER:
        return unavailable
    return {"validation_score": score, "validation_status": VALIDATION_STATUS_MEASURED}


__all__ = [
    "BASIS_FIRST_TOUCH",
    "BASIS_FIRST_TOUCH_UPPER_BOUND",
    "BASIS_OVERSHOOT",
    "BASIS_TIME_STOP_DUE",
    "DEFAULT_EXIT_DELAY_EXCLUSION_HOURS",
    "EXIT_DELAY_EXCLUSION_REASON",
    "OVERSHOOT_MULTIPLE",
    "SINCE_BASIS_LEGACY",
    "SINCE_BASIS_OBSERVED",
    "assess_exit_delay",
    "excluded_trades_summary",
    "exit_condition_kind",
    "exit_due_since",
    "exit_delay_exclusion_hours",
    "is_evidence_excluded",
    "split_evidence",
    "validation_fields",
]
