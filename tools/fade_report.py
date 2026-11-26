"""Fade strategy live-test report.

Run any time during the multi-day fade test to compare the live results of the new
FADE entry strategy against the legacy MOMENTUM trades:

    python tools/fade_report.py            # all closed trades, split by strategy
    python tools/fade_report.py --since 2026-07-11   # only trades opened on/after a date

Fade trades are tagged (decision_context.entry_strategy == "fade") by the execution
agent; legacy momentum trades have no tag. This isolates the test cleanly.

Break-even reminder: with a realized R:R of X, break-even win rate = 1/(1+X). The fade
target is >50% WR AND profit factor > 1 net of costs.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TEST_MODE", "1")

from core.capital_manager import get_capital_manager  # noqa: E402


def _f(p, k, d=0.0):
    try:
        return float(p.get(k, d) or d)
    except Exception:
        return d


def _entry_strategy(p: dict) -> str:
    dc = p.get("decision_context") or {}
    val = dc.get("entry_strategy") or (dc.get("execution_context") or {}).get("entry_strategy")
    if val:
        return str(val).lower()
    # Legacy trades had no tag; infer from the entry_reason wording if present.
    reason = str(p.get("entry_reason") or "").lower()
    if "fading" in reason or "over-extension" in reason:
        return "fade"
    if "momentum" in reason:
        return "momentum"
    return "untagged"


def _report(rows: list[dict], label: str) -> None:
    if not rows:
        print(f"\n=== {label}: no trades ===")
        return
    wins = [p for p in rows if _f(p, "pnl") > 0]
    losses = [p for p in rows if _f(p, "pnl") < 0]
    n = len(rows)
    gw = sum(_f(p, "pnl") for p in wins)
    gl = sum(_f(p, "pnl") for p in losses)
    tot = sum(_f(p, "pnl") for p in rows)
    wr = len(wins) / n * 100 if n else 0.0
    avg_w = gw / len(wins) if wins else 0.0
    avg_l = gl / len(losses) if losses else 0.0
    pf = (gw / abs(gl)) if gl else float("inf")
    rr = abs(avg_w / avg_l) if avg_l else 0.0
    be_wr = 1 / (1 + rr) * 100 if rr else 0.0
    print(f"\n=== {label} ({n} closed) ===")
    print(f"  Win rate       : {wr:5.1f}%   ({len(wins)}W / {len(losses)}L)")
    print(f"  Net PnL        : ${tot:,.2f}")
    print(f"  Profit factor  : {pf:.2f}")
    print(f"  Avg win / loss : ${avg_w:,.2f} / ${avg_l:,.2f}   (R:R {rr:.2f})")
    print(f"  Expectancy/trade: ${tot / n:,.2f}")
    print(f"  Break-even WR  : {be_wr:.1f}%   ({'PROFITABLE' if wr > be_wr else 'below break-even'})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="Only trades opened on/after YYYY-MM-DD", default=None)
    args = ap.parse_args()

    cm = get_capital_manager()
    closed = [p for p in cm.get_positions() if str(p.get("status", "")).upper() == "CLOSED"]

    if args.since:
        cutoff = args.since[:10]
        closed = [p for p in closed if str(p.get("opened_at", ""))[:10] >= cutoff]
        print(f"(filtered to trades opened on/after {cutoff})")

    by_strat: dict[str, list] = defaultdict(list)
    for p in closed:
        by_strat[_entry_strategy(p)].append(p)

    print(f"Total closed trades analysed: {len(closed)}")
    print("Split by entry strategy:", {k: len(v) for k, v in by_strat.items()})

    _report(by_strat.get("fade", []), "FADE (new strategy)")
    _report(by_strat.get("momentum", []) + by_strat.get("untagged", []), "MOMENTUM / legacy (untagged)")


if __name__ == "__main__":
    main()
