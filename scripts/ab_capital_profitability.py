"""scripts/ab_capital_profitability.py
======================================
A/B quantification of candidate Capital-profitability fixes on the 281
autonomous closed trades in capital.json (CLAUDE.md §4 requires before/after
math for any change that alters which trades win/lose).

Read-only analysis. Does NOT mutate capital.json. Run:

    python scripts/ab_capital_profitability.py

Outputs (printed to stdout):
  1. Baseline: WR, PF, net PnL, payoff, LONG vs SHORT split.
  2. Re-entry lockout after a STOP-LOSS (per-ticker, N hours) -- net PnL delta
     and trades blocked, for N in {1,2,4,6,12,24,48}h.
  3. Re-entry lockout after ANY close (stop OR TP) -- same sweep.
  4. Score-threshold raise (signal_score >= X) -- WR + net PnL at X in
     {78,80,82,85,88,90}.
  5. Direction-conditional score threshold (higher bar for LONG since LONG
     WR << SHORT WR).
  6. Per-ticker repeat-close table (top offenders) so the lockout rationale
     is visible.

Excludes close_reason == "Manual close" (the user's manual trades -- not part
of the autonomous pipeline under review).
"""
from __future__ import annotations

import collections
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAP = ROOT / "capital.json"


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except Exception:
        return None


def _score(p: dict) -> float | None:
    snap = p.get("score_snapshot") or {}
    if not isinstance(snap, dict):
        return None
    v = snap.get("signal_score")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _pnl(p: dict) -> float:
    v = p.get("net_pnl")
    if v is None:
        v = p.get("pnl")
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _is_loss(p: dict) -> bool:
    return _pnl(p) < 0.0


def _is_stop(p: dict) -> bool:
    return str(p.get("close_reason") or "").lower().startswith("stop")


def _is_tp(p: dict) -> bool:
    return str(p.get("close_reason") or "").lower().startswith("take_profit") or \
           str(p.get("close_reason") or "").lower().startswith("take profit")


def load_closed() -> list[dict]:
    data = json.loads(CAP.read_text(encoding="utf-8"))
    pos = data.get("positions", [])
    closed = [
        p for p in pos
        if p.get("status") == "CLOSED"
        and str(p.get("close_reason") or "") != "Manual close"
    ]
    return closed


def stats(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0}
    wins = [t for t in trades if _pnl(t) > 0]
    losses = [t for t in trades if _pnl(t) < 0]
    gross_w = sum(_pnl(t) for t in wins)
    gross_l = abs(sum(_pnl(t) for t in losses))
    wr = len(wins) / n
    pf = gross_w / gross_l if gross_l > 0 else float("inf")
    net = sum(_pnl(t) for t in trades)
    avg_win = gross_w / len(wins) if wins else 0.0
    avg_loss = gross_l / len(losses) if losses else 0.0
    payoff = avg_win / avg_loss if avg_loss > 0 else float("inf")
    return {
        "n": n, "wins": len(wins), "losses": len(losses),
        "wr": wr, "pf": pf, "net": net, "payoff": payoff,
        "avg_win": avg_win, "avg_loss": avg_loss,
    }


def _fmt(d: dict) -> str:
    if d.get("n", 0) == 0:
        return "n=0"
    return (
        f"n={d['n']:>4}  WR={d['wr']*100:5.1f}%  PF={d['pf']:5.3f}  "
        f"net={d['net']:+9.2f}  payoff={d['payoff']:4.2f}  "
        f"avgW={d['avg_win']:+.3f} avgL={d['avg_loss']:+.3f}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2 + 3: Re-entry lockout simulation
# ─────────────────────────────────────────────────────────────────────────────

def simulate_lockout(trades: list[dict], block_hours: float, after_any_close: bool) -> dict:
    """Per-ticker timeline. After a qualifying close, block re-entry on the
    same ticker for `block_hours`. A trade is BLOCKED if its opened_at falls
    within the lockout window of the most recent qualifying close on that
    ticker. Blocked trades are removed from the population; we then report
    the surviving population's stats + the sum of PnL we avoided (or gave up).

    Assumption: trades are processed in opened_at order; a blocked trade does
    NOT extend or reset the lockout window (it never opened). The lockout
    window is set by the most recent ACTUAL close.
    """
    by_ticker: dict[str, list[dict]] = collections.defaultdict(list)
    for t in trades:
        by_ticker[str(t.get("ticker") or "")].append(t)

    kept: list[dict] = []
    blocked: list[dict] = []
    for ticker, tl in by_ticker.items():
        tl.sort(key=lambda p: _parse_dt(p.get("opened_at")) or datetime.min)
        unlocked_until: datetime | None = None  # opened_at >= this is allowed
        for p in tl:
            od = _parse_dt(p.get("opened_at"))
            if unlocked_until is not None and od is not None and od < unlocked_until:
                blocked.append(p)
                continue
            kept.append(p)
            cd = _parse_dt(p.get("closed_at"))
            qualifies = after_any_close or _is_stop(p)
            if qualifies and cd is not None:
                from datetime import timedelta
                unlocked_until = cd + timedelta(hours=block_hours)
            # If the close did not qualify (e.g. TP under after_stop=False),
            # leave the existing lockout window intact (no reset).
    kept_stats = stats(kept)
    blocked_pnl = sum(_pnl(p) for p in blocked)
    blocked_wins = sum(1 for p in blocked if _pnl(p) > 0)
    blocked_losses = sum(1 for p in blocked if _pnl(p) < 0)
    return {
        "kept": kept_stats, "blocked_n": len(blocked),
        "blocked_pnl": blocked_pnl,
        "blocked_wins": blocked_wins, "blocked_losses": blocked_losses,
        "kept_net": kept_stats.get("net", 0.0),
    }


def sweep_lockout(trades: list[dict], after_any_close: bool, label: str) -> None:
    print(f"\n=== {label} ===")
    base = stats(trades)
    print(f"baseline:                  {_fmt(base)}")
    for h in [1, 2, 4, 6, 8, 12, 24, 48]:
        r = simulate_lockout(trades, h, after_any_close)
        ks = r["kept"]
        delta = ks["net"] - base["net"]
        print(
            f"  block {h:>2}h: kept={_fmt(ks)}  "
            f"blocked={r['blocked_n']:>3} (W{r['blocked_wins']}/L{r['blocked_losses']})  "
            f"avoided_pnl={r['blocked_pnl']:+8.2f}  net_delta={delta:+8.2f}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4 + 5: Score-threshold filtering
# ─────────────────────────────────────────────────────────────────────────────

def sweep_score_threshold(trades: list[dict]) -> None:
    print("\n=== Score-threshold raise (signal_score >= X) ===")
    base = stats(trades)
    print(f"baseline (all):            {_fmt(base)}")
    for x in [78, 80, 82, 85, 88, 90]:
        kept = [t for t in trades if (s := _score(t)) is not None and s >= x]
        if not kept:
            continue
        ks = stats(kept)
        delta = ks["net"] - base["net"]
        print(f"  >= {x}: {_fmt(ks)}  net_delta={delta:+8.2f}")


def sweep_direction_conditional(trades: list[dict]) -> None:
    print("\n=== Direction-conditional score bar (LONG needs higher score) ===")
    base = stats(trades)
    long_base = stats([t for t in trades if t.get("direction") == "LONG"])
    short_base = stats([t for t in trades if t.get("direction") == "SHORT"])
    print(f"LONG  baseline:  {_fmt(long_base)}")
    print(f"SHORT baseline:  {_fmt(short_base)}")
    print(f"all   baseline:  {_fmt(base)}")
    for long_x in [82, 85, 88]:
        for short_x in [78, 80]:
            kept = [
                t for t in trades
                if (s := _score(t)) is not None
                and s >= (long_x if t.get("direction") == "LONG" else short_x)
            ]
            if not kept:
                continue
            ks = stats(kept)
            delta = ks["net"] - base["net"]
            print(
                f"  LONG>={long_x} SHORT>={short_x}: {_fmt(ks)}  "
                f"net_delta={delta:+8.2f}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# 6: Per-ticker repeat-close table
# ─────────────────────────────────────────────────────────────────────────────

def per_ticker_repeats(trades: list[dict], top: int = 15) -> None:
    print("\n=== Per-ticker repeat closes (top offenders) ===")
    by_ticker: dict[str, list[dict]] = collections.defaultdict(list)
    for t in trades:
        by_ticker[str(t.get("ticker") or "")].append(t)
    rows = []
    for ticker, tl in by_ticker.items():
        if len(tl) < 2:
            continue
        tl.sort(key=lambda p: _parse_dt(p.get("opened_at")) or datetime.min)
        net = sum(_pnl(p) for p in tl)
        stops = sum(1 for p in tl if _is_stop(p))
        tps = sum(1 for p in tl if _is_tp(p))
        rows.append((ticker, len(tl), stops, tps, net))
    rows.sort(key=lambda r: r[1], reverse=True)
    print(f"  {'ticker':<12} {'n':>4} {'stops':>5} {'tps':>4} {'net_pnl':>10}")
    for ticker, n, stops, tps, net in rows[:top]:
        print(f"  {ticker:<12} {n:>4} {stops:>5} {tps:>4} {net:>+10.2f}")
    # Aggregate of the repeat offenders
    repeat_net = sum(r[4] for r in rows)
    print(f"  --- repeat-offender aggregate net PnL: {repeat_net:+.2f} "
          f"across {len(rows)} tickers ({sum(r[1] for r in rows)} trades)")


# ─────────────────────────────────────────────────────────────────────────────
# Global consecutive-loss pause (gate 8 analog) + combination
# ─────────────────────────────────────────────────────────────────────────────

def simulate_global_loss_pause(trades: list[dict], after_losses: int,
                               pause_hours: float) -> dict:
    """Process ALL trades in global opened_at order. After `after_losses`
    consecutive losing closes, pause ALL trading for `pause_hours`. A trade
    whose opened_at falls in a pause window is blocked. A blocked trade does
    NOT count toward the consecutive-loss counter (it never opened/closed).

    Mirrors confidence_gates gate 8 (consecutive_loss_cooldown) so the sweep
    shows the marginal value of tuning that gate vs adding the per-ticker
    lockout.
    """
    order = sorted(trades, key=lambda p: _parse_dt(p.get("opened_at")) or datetime.min)
    from datetime import timedelta
    kept: list[dict] = []
    blocked: list[dict] = []
    consec = 0
    pause_until: datetime | None = None
    for p in order:
        od = _parse_dt(p.get("opened_at"))
        if pause_until is not None and od is not None and od < pause_until:
            blocked.append(p)
            continue
        kept.append(p)
        if _is_loss(p):
            consec += 1
            if consec >= after_losses:
                cd = _parse_dt(p.get("closed_at"))
                if cd is not None:
                    pause_until = cd + timedelta(hours=pause_hours)
        else:
            consec = 0
    ks = stats(kept)
    blocked_pnl = sum(_pnl(p) for p in blocked)
    return {
        "kept": ks, "blocked_n": len(blocked),
        "blocked_pnl": blocked_pnl,
        "kept_net": ks.get("net", 0.0),
    }


def simulate_combined(trades: list[dict], lockout_hours: float,
                       after_losses: int, pause_hours: float) -> dict:
    """Per-ticker lockout (after ANY close) AND global consecutive-loss pause,
    applied together. A trade is blocked if it trips EITHER rule. Both rules
    use closed_at to set their windows; a blocked trade extends neither.
    """
    by_ticker: dict[str, list[dict]] = collections.defaultdict(list)
    for t in trades:
        by_ticker[str(t.get("ticker") or "")].append(t)

    # Precompute per-ticker lockout windows (set by actual closes only).
    from datetime import timedelta
    # We need global ordering for the loss-pause, so instead simulate in
    # global opened_at order while tracking per-ticker unlock times.
    order = sorted(trades, key=lambda p: _parse_dt(p.get("opened_at")) or datetime.min)
    unlock_by_ticker: dict[str, datetime] = {}
    pause_until: datetime | None = None
    consec = 0
    kept: list[dict] = []
    blocked: list[dict] = []
    for p in order:
        od = _parse_dt(p.get("opened_at"))
        ticker = str(p.get("ticker") or "")
        t_unlock = unlock_by_ticker.get(ticker)
        ticker_blocked = (t_unlock is not None and od is not None and od < t_unlock)
        global_blocked = (pause_until is not None and od is not None and od < pause_until)
        if ticker_blocked or global_blocked:
            blocked.append(p)
            continue
        kept.append(p)
        cd = _parse_dt(p.get("closed_at"))
        # per-ticker lockout after ANY close
        if cd is not None:
            unlock_by_ticker[ticker] = cd + timedelta(hours=lockout_hours)
        # global loss pause
        if _is_loss(p):
            consec += 1
            if consec >= after_losses and cd is not None:
                pause_until = cd + timedelta(hours=pause_hours)
        else:
            consec = 0
    ks = stats(kept)
    blocked_pnl = sum(_pnl(p) for p in blocked)
    return {
        "kept": ks, "blocked_n": len(blocked),
        "blocked_pnl": blocked_pnl,
        "kept_net": ks.get("net", 0.0),
    }


def sweep_global_pause(trades: list[dict]) -> None:
    print("\n=== Global consecutive-loss pause (gate-8 tuning) ===")
    base = stats(trades)
    print(f"baseline:                  {_fmt(base)}")
    for k in [2, 3]:
        for ph in [1, 2, 4, 6, 8]:
            r = simulate_global_loss_pause(trades, k, ph)
            ks = r["kept"]
            delta = ks["net"] - base["net"]
            print(
                f"  after {k} losses, pause {ph}h: kept={_fmt(ks)}  "
                f"blocked={r['blocked_n']:>3}  avoided={r['blocked_pnl']:+8.2f}  "
                f"net_delta={delta:+8.2f}"
            )


def sweep_combined(trades: list[dict]) -> None:
    print("\n=== COMBINED: per-ticker lockout (after any close) + global loss pause ===")
    base = stats(trades)
    print(f"baseline:                  {_fmt(base)}")
    # Best per-ticker candidate (8h after any close) + a couple of global pauses.
    for lockout in [6, 8]:
        for k, ph in [(2, 2), (2, 4), (3, 4)]:
            r = simulate_combined(trades, lockout, k, ph)
            ks = r["kept"]
            delta = ks["net"] - base["net"]
            print(
                f"  lockout {lockout}h + pause after {k}L/{ph}h: "
                f"kept={_fmt(ks)}  blocked={r['blocked_n']:>3}  "
                f"avoided={r['blocked_pnl']:+8.2f}  net_delta={delta:+8.2f}"
            )


def main() -> int:
    closed = load_closed()
    print(f"Loaded {len(closed)} autonomous closed trades "
          f"(Manual close excluded).")

    base = stats(closed)
    print(f"\n=== BASELINE ===\n{_fmt(base)}")
    long_s = stats([t for t in closed if t.get("direction") == "LONG"])
    short_s = stats([t for t in closed if t.get("direction") == "SHORT"])
    print(f"LONG:  {_fmt(long_s)}")
    print(f"SHORT: {_fmt(short_s)}")

    sweep_lockout(closed, after_any_close=False,
                 label="Re-entry lockout AFTER STOP-LOSS only (per-ticker)")
    sweep_lockout(closed, after_any_close=True,
                 label="Re-entry lockout AFTER ANY CLOSE (per-ticker)")
    sweep_score_threshold(closed)
    sweep_direction_conditional(closed)
    per_ticker_repeats(closed)
    sweep_global_pause(closed)
    sweep_combined(closed)
    return 0


if __name__ == "__main__":
    sys.exit(main())