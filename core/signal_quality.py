"""
core/signal_quality.py  —  Stage 7: Signal Quality Metrics
===========================================================

Pure-function module that computes signal-level quality metrics
from a list of closed signals (no I/O, no side effects).

Metrics produced:
  - win_rate       — fraction of WIN outcomes, 7d + 30d windows
  - profit_factor  — gross_profit / gross_loss (R-multiples)
  - avg_R          — average R-multiple per trade
  - max_drawdown   — maximum peak-to-trough cumulative-R drop
  - sample_size    — number of closed signals in window
  - expectancy_R   — weighted avg R: win_rate * avg_win_R - loss_rate * avg_loss_R

WIN/LOSS cost model (conservative, realistic):
  A WIN trade achieves its TP.     R = +tp_pct / sl_pct (capped at 5 R)
  A LOSS/LOST trade hits its SL.   R = -1.0 (always full risk unit)
  A NEUTRAL / unknown status:      R = 0.0  (excluded from return metrics)

Internal degradation alerts:
  If win_rate_30d < ALERT_WIN_RATE_THRESHOLD     → 'win_rate_degraded'
  If profit_factor_30d < ALERT_PROFIT_FACTOR_MIN → 'profit_factor_low'
  If max_drawdown_30d > ALERT_MAX_DRAWDOWN_PCT   → 'drawdown_excessive'

Rules:
  - Stateless: takes a list of dicts, returns a dict.
  - Deterministic: always returns the full schema even with empty input.
  - Does not import any project module → safe to test without app startup.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

# ── Alert thresholds ────────────────────────────────────────────────────
ALERT_WIN_RATE_THRESHOLD: float = 0.48   # < 48% → warn
ALERT_PROFIT_FACTOR_MIN: float = 1.0     # < 1.0 → not profitable
ALERT_MAX_DRAWDOWN_PCT: float = 25.0     # > 25% equity drawdown → excessive

_EMPTY_WINDOW: Dict[str, Any] = {
    "win_rate": None,
    "profit_factor": None,
    "avg_R": None,
    "expectancy_R": None,
    "max_drawdown": None,
    "wins": 0,
    "losses": 0,
    "sample_size": 0,
    "alerts": [],
}


# ── Helpers ──────────────────────────────────────────────────────────────

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


_WIN_TOKENS = frozenset({"WIN", "WON", "TAKE_PROFIT", "TP", "PROFIT"})
_LOSS_TOKENS = frozenset({"LOSS", "LOST", "STOP_LOSS", "SL"})
_BREAKEVEN_TOKENS = frozenset({"BREAKEVEN", "BREAK_EVEN", "BE", "FLAT"})


def _classify_outcome_token(raw: Any) -> Optional[str]:
    """Map a raw outcome label to WIN/LOSS/BREAKEVEN, or None if unrecognized."""
    token = str(raw or "").strip().upper().replace("-", "_").replace(" ", "_")
    if token in _WIN_TOKENS:
        return "WIN"
    if token in _LOSS_TOKENS:
        return "LOSS"
    if token in _BREAKEVEN_TOKENS:
        return "BREAKEVEN"
    return None


def _r_multiple(signal: Dict[str, Any]) -> float:
    """
    R-multiple for a closed signal.
    WIN signals:  R = min(tp_pct / sl_pct, 5.0)   capped at 5 R
    LOSS signals: R = -1.0                          full risk unit
    Other:        R = 0.0                           excluded
    """
    status = _normalized_outcome(signal)

    if status == "WIN":
        tp_pct = abs(_safe_float(signal.get("tp_pct"), 0.0))
        sl_pct = abs(_safe_float(signal.get("sl_pct"), 0.0))
        if tp_pct > 0 and sl_pct > 0:
            return min(tp_pct / sl_pct, 5.0)

        entry = _safe_float(signal.get("entry") or signal.get("entry_price"), 0.0)
        tp_price = _safe_float(signal.get("tp") or signal.get("take_profit_price"), 0.0)
        sl_price = _safe_float(signal.get("sl") or signal.get("stop_loss_price"), 0.0)
        if entry > 0 and tp_price > 0 and sl_price > 0:
            reward = abs(tp_price - entry)
            risk = abs(entry - sl_price)
            if risk > 0:
                return min(reward / risk, 5.0)
        return 1.0  # default 1:1 if SL unknown
    elif status == "LOSS":
        return -1.0
    elif status in ("BREAKEVEN", "BE"):
        return 0.0
    else:
        return float("nan")   # unknown → exclude


def _normalized_outcome(signal: Dict[str, Any]) -> str:
    """Normalize live, legacy, and paper-trading outcome vocabulary."""
    for key in ("result", "outcome", "status", "close_reason"):
        outcome = _classify_outcome_token(signal.get(key))
        if outcome is not None:
            return outcome

    for key in ("net_pnl", "realized_pnl", "pnl", "pnl_pct"):
        if key not in signal or signal.get(key) is None:
            continue
        pnl = _safe_float(signal.get(key), float("nan"))
        if math.isnan(pnl):
            continue
        if pnl > 0:
            return "WIN"
        if pnl < 0:
            return "LOSS"
        return "BREAKEVEN"
    return "UNKNOWN"


def _filter_window(signals: List[Dict[str, Any]], days: int) -> List[Dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = []
    for s in signals:
        ts = _parse_iso(s.get("closed_at") or s.get("created_at"))
        if ts is None or ts >= cutoff:
            result.append(s)
    return result


# ── Core metric computation ──────────────────────────────────────────────

def compute_quality_metrics(signals: List[Dict[str, Any]], days: int) -> Dict[str, Any]:
    """
    Compute quality metrics for the signals in the last `days` days.
    Returns a full dict with all keys (None when sample too small).
    """
    windowed = _filter_window(signals, days)

    wins = 0
    losses = 0
    r_values: List[float] = []

    for s in windowed:
        r = _r_multiple(s)
        if math.isnan(r):
            continue   # skip unknown status
        if r > 0:
            wins += 1
        elif r < 0:
            losses += 1
        r_values.append(r)

    sample_size = len(r_values)
    if sample_size == 0:
        return dict(_EMPTY_WINDOW, sample_size=0)

    # Win rate
    win_rate = wins / sample_size

    # Profit factor = sum of positive R / abs(sum of negative R)
    gross_profit = sum(r for r in r_values if r > 0)
    gross_loss = abs(sum(r for r in r_values if r < 0))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    # Average R
    avg_R = sum(r_values) / sample_size

    # Expectancy R = win_rate * avg_win_R - loss_rate * avg_loss_R
    win_rs = [r for r in r_values if r > 0]
    loss_rs = [abs(r) for r in r_values if r < 0]
    avg_win_R = sum(win_rs) / len(win_rs) if win_rs else 0.0
    avg_loss_R = sum(loss_rs) / len(loss_rs) if loss_rs else 0.0
    loss_rate = losses / sample_size
    expectancy_R = (win_rate * avg_win_R) - (loss_rate * avg_loss_R)

    # Max drawdown (on cumulative R equity curve)
    max_drawdown = _compute_max_drawdown(r_values)

    # Alerts
    alerts: List[str] = []
    if win_rate < ALERT_WIN_RATE_THRESHOLD:
        alerts.append("win_rate_degraded")
    if profit_factor is not None and profit_factor < ALERT_PROFIT_FACTOR_MIN:
        alerts.append("profit_factor_low")
    if max_drawdown is not None and max_drawdown > ALERT_MAX_DRAWDOWN_PCT:
        alerts.append("drawdown_excessive")

    return {
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 3) if profit_factor is not None else None,
        "avg_R": round(avg_R, 3),
        "expectancy_R": round(expectancy_R, 4),
        "max_drawdown": round(max_drawdown, 2) if max_drawdown is not None else None,
        "wins": wins,
        "losses": losses,
        "sample_size": sample_size,
        "alerts": alerts,
    }


def _compute_max_drawdown(r_values: List[float]) -> Optional[float]:
    """
    Maximum peak-to-trough drawdown on the cumulative-R equity curve.
    Returns as a percentage of peak (0-100).
    Returns None if fewer than 2 trades.
    """
    if len(r_values) < 2:
        return None

    # Use one risk unit of starting equity per evaluated trade. Starting at zero
    # makes a normal loss sequence report impossible drawdowns above 100%.
    starting_equity = float(len(r_values))
    equity = [starting_equity]
    for r in r_values:
        equity.append(equity[-1] + r)

    peak = equity[0]
    max_dd = 0.0

    for val in equity:
        if val > peak:
            peak = val
        drawdown = peak - val
        if drawdown > max_dd:
            max_dd = drawdown

    base = max(abs(peak), 1.0)
    return min(100.0, (max_dd / base) * 100.0)


# ── Aggregation across dimensions ────────────────────────────────────────

def compute_quality_report(
    closed_signals: List[Dict[str, Any]],
    breakdown_by: str = "risk_mode",
) -> Dict[str, Any]:
    """
    Full quality report with 7d + 30d global windows and per-dimension breakdown.

    breakdown_by: 'risk_mode' | 'action' | 'ticker'
    """
    global_7d = compute_quality_metrics(closed_signals, 7)
    global_30d = compute_quality_metrics(closed_signals, 30)

    # All-time global
    global_all = compute_quality_metrics(closed_signals, 365 * 10)

    # Per-dimension breakdown (30d)
    breakdown: Dict[str, Any] = {}
    window_30d = _filter_window(closed_signals, 30)
    dimension_values = sorted(set(
        str(s.get(breakdown_by) or "unknown") for s in window_30d
    ))
    for dim_val in dimension_values:
        subset = [s for s in window_30d if str(s.get(breakdown_by) or "unknown") == dim_val]
        breakdown[dim_val] = compute_quality_metrics(subset, 30)

    # Collect all active alerts across windows
    all_alerts: List[str] = []
    for label, a in [("7d", global_7d["alerts"]), ("30d", global_30d["alerts"])]:
        for alert in a:
            tagged = f"{alert}:{label}"
            if tagged not in all_alerts:
                all_alerts.append(tagged)

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "global": {
            "7d": global_7d,
            "30d": global_30d,
            "all_time": global_all,
        },
        "breakdown_by": breakdown_by,
        "breakdown": breakdown,
        "active_alerts": all_alerts,
        "alert_thresholds": {
            "min_win_rate": ALERT_WIN_RATE_THRESHOLD,
            "min_profit_factor": ALERT_PROFIT_FACTOR_MIN,
            "max_drawdown_pct": ALERT_MAX_DRAWDOWN_PCT,
        },
    }
