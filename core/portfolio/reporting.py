"""Read-only reporting helpers for the Capital tab.

Pure functions over stored position dictionaries. Nothing here opens, closes or
sizes a position; it only describes what is stored so the UI can label it
honestly (sample size and period of a win rate, whether a mark price is a live
tradable quote, why a position whose target was reached is still open).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

# CapitalManager persists every OPEN position but only the newest 300 closed
# ones, so any statistic computed from the stored state covers at most this many.
CLOSED_POSITION_RETENTION_LIMIT = 300


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: Any) -> Optional[datetime]:
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


def summarize_closed_trades(
    closed_positions: Iterable[Mapping[str, Any]],
    *,
    retention_limit: int = CLOSED_POSITION_RETENTION_LIMIT,
) -> dict[str, Any]:
    """Win rate with the sample, period and break-even bar it must be read against.

    ``breakeven_win_rate_pct`` is avg loss / (avg win + avg loss), the same
    definition as ``core.paper_performance.summarize_strategy``; it is ``None``
    unless the sample holds both wins and losses.

    Trades marked ``evidence_excluded`` (exit delayed by an outage) are left
    out of every figure and reported under ``excluded_trades`` with their
    count and total P&L.
    """
    from core.portfolio.evidence import excluded_trades_summary, split_evidence

    stored = [position for position in closed_positions or [] if isinstance(position, Mapping)]
    counted, excluded = split_evidence(stored)
    pnls: list[float] = []
    closed_times: list[datetime] = []
    for position in counted:
        pnl = _finite(position.get("pnl"))
        pnls.append(pnl if pnl is not None else 0.0)
        closed_at = _timestamp(position.get("closed_at"))
        if closed_at is not None:
            closed_times.append(closed_at)

    trades = len(pnls)
    wins = [value for value in pnls if value > 0]
    losses = [-value for value in pnls if value < 0]
    win_rate = round(len(wins) / trades * 100.0, 1) if trades else None
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    breakeven = (
        round(100.0 * avg_loss / (avg_win + avg_loss), 1)
        if avg_win and avg_loss
        else None
    )
    limit = max(0, int(retention_limit or 0))
    return {
        "trades": trades,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": win_rate,
        "breakeven_win_rate_pct": breakeven,
        "avg_win_usd": round(avg_win, 4) if avg_win is not None else None,
        "avg_loss_usd": round(avg_loss, 4) if avg_loss is not None else None,
        "net_pnl_usd": round(sum(pnls), 4) if trades else 0.0,
        # Gross profit / gross loss; None without a loss (never an invented ratio).
        "profit_factor": round(sum(wins) / sum(losses), 4) if losses and sum(losses) > 0 else None,
        "expectancy_usd": round(sum(pnls) / trades, 4) if trades else None,
        "period_start": min(closed_times).isoformat() if closed_times else None,
        "period_end": max(closed_times).isoformat() if closed_times else None,
        "retention_limit": limit,
        # True when older closed trades have already been trimmed from the stored
        # state, i.e. this is "the latest N", not the whole trading history.
        "window_is_capped": bool(limit and len(stored) >= limit),
        "excluded_trades": excluded_trades_summary(excluded),
        "pnl_basis": (
            "net realized P&L per closed position, research cohorts excluded; "
            "trades whose exit was delayed by an outage are listed under excluded_trades"
        ),
    }


def position_market_state(
    position: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Describe whether the position's mark is tradable now and any waiting exit.

    A US stock quoted while the regular session is closed is a last/indicative
    price: the exit path refuses to fill on it, so a position whose stop or
    target is already crossed legitimately stays open until the next session.
    """
    from core.market_hours import is_equity_market_hours_enforced, us_equity_market_status

    ticker = str(position.get("ticker") or position.get("symbol") or "").upper().strip()
    enforced = bool(ticker) and is_equity_market_hours_enforced(ticker)
    market_open = True
    reason = "always_open"
    if enforced:
        status = us_equity_market_status(now)
        market_open = bool(status.is_open)
        reason = str(status.reason or "")

    direction = str(position.get("direction") or "LONG").upper()
    is_long = direction != "SHORT"
    price = _finite(position.get("current_price"))
    stop = _finite(position.get("stop_loss_price"))
    target = _finite(position.get("take_profit_price"))
    trigger = ""
    if price is not None and price > 0:
        if target is not None and target > 0 and (price >= target if is_long else price <= target):
            trigger = "take_profit"
        elif stop is not None and stop > 0 and (price <= stop if is_long else price >= stop):
            trigger = "stop_loss"

    if not trigger:
        note = ""
    elif enforced and not market_open:
        note = (
            f"{'Target' if trigger == 'take_profit' else 'Stop'} already crossed on the last quote. "
            "Stock exits only fill in the US regular session (09:30-16:00 ET); "
            "this position closes at the first verified price after the market opens, "
            "which may differ from the price shown."
        )
    else:
        note = (
            f"{'Target' if trigger == 'take_profit' else 'Stop'} crossed; "
            "the exit fills on the next verified price check."
        )
    return {
        "market_hours_enforced": enforced,
        "market_open": market_open,
        "market_reason": reason,
        "price_is_tradable": market_open,
        "exit_trigger": trigger,
        "exit_note": note,
    }


__all__ = [
    "CLOSED_POSITION_RETENTION_LIMIT",
    "position_market_state",
    "summarize_closed_trades",
]
