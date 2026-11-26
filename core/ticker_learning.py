"""Per-ticker, per-direction learning & safety adjustments.

Pure, deterministic layer that derives an entry adjustment from the *closed* trade
history that already exists on the capital manager. No new persisted state, so it can
never drift from real outcomes.

Rationale: the autonomous entry score saturates (nearly every strong mover clamps to
~95), so it does not differentiate. This layer supplies the differentiation:

- Consecutive LOSSES on a ticker+direction  -> raise the entry bar + shrink size, and
  after enough losses BLOCK the direction for a cooldown (derived statelessly from the
  timestamp of the most recent loss).
- Consecutive WINS on a ticker+direction     -> widen the R:R target (and allow more
  leverage, but only once the global strategy is proven profitable — the caller gates
  that against ``force_one_x_until_profitable``).

Both directions are handled symmetrically. See
``docs/superpowers/specs/2026-07-04-ticker-learning-safety-design.md``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# Defaults; every value is overridable from ``agents.execution.ticker_learning`` in config.
_DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "window": 20,               # max recent closed trades considered per ticker+direction
    "loss_streak_reduce": 3,    # L>=this  -> size x0.5, bar 90
    "loss_streak_tighten": 4,   # L>=this  -> size x0.25, bar 93
    "loss_streak_block": 5,     # L>=this  -> block for cooldown, then probation
    "block_cooldown_hours": 6,
    "win_streak_boost": 3,      # W>=this  -> R:R x1.25, lev +1
    "win_streak_boost2": 5,     # W>=this  -> R:R x1.5,  lev +2
    "max_leverage_cap": 5,
    "size_mult_l3": 0.5,
    "size_mult_l4": 0.25,
    "min_score_l3": 90.0,
    "min_score_l4": 93.0,
    "rr_mult_w3": 1.25,
    "rr_mult_w5": 1.5,
    "baseline_min_score": 78.0,  # fallback when execution cfg has no scanner_min_score
}


def _norm_dir(value: Any) -> str:
    """Map BUY/LONG -> LONG and SELL/SHORT -> SHORT (uppercase)."""
    d = str(value or "").upper().strip()
    if d in ("BUY", "LONG"):
        return "LONG"
    if d in ("SELL", "SHORT"):
        return "SHORT"
    return d


def _norm_ticker(value: Any) -> str:
    return str(value or "").upper().strip()


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse an ISO timestamp to an aware datetime (assume UTC if naive)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _num(cfg: Dict[str, Any], key: str) -> Any:
    """Config value with default fallback."""
    if isinstance(cfg, dict) and key in cfg and cfg[key] is not None:
        return cfg[key]
    return _DEFAULTS[key]


def _baseline(min_score: float) -> Dict[str, Any]:
    return {
        "streak_type": "NONE",
        "streak_len": 0,
        "win_rate": 0.0,
        "action": "ALLOW",
        "size_mult": 1.0,
        "min_score_required": float(min_score),
        "leverage_bonus": 0,
        "rr_mult": 1.0,
        "cooldown_until": None,
        "reason": "",
    }


def get_ticker_adjustment(
    ticker: str,
    direction: str,
    closed_positions: List[Dict[str, Any]],
    cfg: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return the entry adjustment for ``ticker`` on ``direction``.

    ``cfg`` is the execution agent config (may hold a ``ticker_learning`` block and
    ``scanner_min_score``). ``closed_positions`` is the raw capital-manager position list
    (any status; only matching CLOSED trades are used). Deterministic given its inputs.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    learn = cfg.get("ticker_learning", {})
    learn = learn if isinstance(learn, dict) else {}

    baseline_min_score = float(cfg.get("scanner_min_score", _num(learn, "baseline_min_score")) or _num(learn, "baseline_min_score"))
    base = _baseline(baseline_min_score)

    if not bool(_num(learn, "enabled")):
        return base

    tkr = _norm_ticker(ticker)
    dirn = _norm_dir(direction)
    if not tkr or dirn not in ("LONG", "SHORT"):
        return base

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # Collect matching CLOSED trades with a decisive (non-zero) pnl.
    rows: List[Dict[str, Any]] = []
    for pos in closed_positions or []:
        if not isinstance(pos, dict):
            continue
        if str(pos.get("status", "")).upper() != "CLOSED":
            continue
        if _norm_ticker(pos.get("ticker") or pos.get("symbol")) != tkr:
            continue
        if _norm_dir(pos.get("direction")) != dirn:
            continue
        try:
            pnl = float(pos.get("pnl", 0.0) or 0.0)
        except Exception:
            pnl = 0.0
        if pnl == 0.0:
            continue  # breakeven: neutral, ignored
        rows.append({"pnl": pnl, "ts": _parse_ts(pos.get("closed_at"))})

    if not rows:
        return base

    # Most recent first. Undated rows sort to the bottom (treated as oldest).
    rows.sort(key=lambda r: (r["ts"] or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    window = max(1, int(_num(learn, "window")))
    rows = rows[:window]

    wins = sum(1 for r in rows if r["pnl"] > 0)
    win_rate = round(wins / len(rows) * 100.0, 1) if rows else 0.0

    # Walk the consecutive same-outcome streak from the most recent trade.
    top_win = rows[0]["pnl"] > 0
    # The streak is anchored at the most recent trade (rows[0] after the descending
    # sort). The 5-loss cooldown is derived from THAT trade's timestamp (the most
    # recent loss), per the design spec's "Stateless cooldown" section — NOT from the
    # oldest trade in the streak. Setting it once here (rather than overwriting inside
    # the loop) avoids using the oldest streak trade's timestamp, which would make the
    # cooldown land in the past and the BLOCK gate inert.
    last_streak_ts = rows[0]["ts"]
    streak_len = 0
    for r in rows:
        if (r["pnl"] > 0) == top_win:
            streak_len += 1
        else:
            break
    streak_type = "WIN" if top_win else "LOSS"

    adj = _baseline(baseline_min_score)
    adj["streak_type"] = streak_type
    adj["streak_len"] = streak_len
    adj["win_rate"] = win_rate

    if streak_type == "LOSS":
        L = streak_len
        block_n = int(_num(learn, "loss_streak_block"))
        tighten_n = int(_num(learn, "loss_streak_tighten"))
        reduce_n = int(_num(learn, "loss_streak_reduce"))
        if L >= block_n:
            # Cooldown derived from the most recent loss — no persisted state.
            cooldown_until = None
            if last_streak_ts is not None:
                cooldown_until = last_streak_ts + timedelta(hours=float(_num(learn, "block_cooldown_hours")))
            adj["size_mult"] = float(_num(learn, "size_mult_l4"))
            adj["min_score_required"] = float(_num(learn, "min_score_l4"))
            adj["cooldown_until"] = cooldown_until.isoformat() if cooldown_until else None
            if cooldown_until is not None and now < cooldown_until:
                adj["action"] = "BLOCK"
                mins = max(0, int((cooldown_until - now).total_seconds() // 60))
                adj["reason"] = f"{L}x LOSS {dirn} on {tkr} - blocked ~{mins}m (cooldown)"
            else:
                adj["action"] = "REDUCE"  # probation
                adj["reason"] = f"{L}x LOSS {dirn} on {tkr} - probation size x{adj['size_mult']:g}, bar {adj['min_score_required']:g}"
        elif L >= tighten_n:
            adj["action"] = "REDUCE"
            adj["size_mult"] = float(_num(learn, "size_mult_l4"))
            adj["min_score_required"] = float(_num(learn, "min_score_l4"))
            adj["reason"] = f"{L}x LOSS {dirn} on {tkr} - size x{adj['size_mult']:g}, bar {adj['min_score_required']:g}"
        elif L >= reduce_n:
            adj["action"] = "REDUCE"
            adj["size_mult"] = float(_num(learn, "size_mult_l3"))
            adj["min_score_required"] = float(_num(learn, "min_score_l3"))
            adj["reason"] = f"{L}x LOSS {dirn} on {tkr} - size x{adj['size_mult']:g}, bar {adj['min_score_required']:g}"
    else:  # WIN
        W = streak_len
        boost2_n = int(_num(learn, "win_streak_boost2"))
        boost_n = int(_num(learn, "win_streak_boost"))
        if W >= boost2_n:
            adj["rr_mult"] = float(_num(learn, "rr_mult_w5"))
            adj["leverage_bonus"] = 2
            adj["reason"] = f"{W}x WIN {dirn} on {tkr} - R:R x{adj['rr_mult']:g}, lev +2"
        elif W >= boost_n:
            adj["rr_mult"] = float(_num(learn, "rr_mult_w3"))
            adj["leverage_bonus"] = 1
            adj["reason"] = f"{W}x WIN {dirn} on {tkr} - R:R x{adj['rr_mult']:g}, lev +1"

    return adj
