"""Why did real paper trades do worse than a replay of the same entries?

usage: execution_gap.py <data dir>   (after ai_scorecard_study.py has written research/ai_scorecard_study.json)

For every taken crypto candidate that has a recorded real close, compares the
real trade (entry, exit, holding time, close reason, from the decision ledger)
with the replay (entry at the reviewed price, stop or target if touched,
otherwise exit after two hours) and splits the difference into parts.
Read-only.
"""
import datetime as dt
import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

DATA = Path(sys.argv[1])
COST = 2 * 0.0004 + 0.00025 + 0.00025


def parse(text):
    try:
        return dt.datetime.fromisoformat(str(text))
    except ValueError:
        return None


def mean(values):
    return statistics.fmean(values) * 100 if values else float("nan")


def main():
    replays = {r["id"]: r for r in json.loads((DATA / "research" / "ai_scorecard_study.json").read_text(encoding="utf-8"))}
    db = sqlite3.connect(f"file:{(DATA / 'decision_ledger.db').as_posix()}?mode=ro", uri=True)
    rows = []
    for decision_id, payload in db.execute("SELECT decision_id, outcome_payload FROM decision_ledger_outcomes"):
        replay = replays.get(decision_id)
        if not replay or replay["group"] != "taken":
            continue
        detail = (json.loads(payload).get("detail") or {})
        try:
            entry, exit_px = float(detail["entry"]), float(detail["exit"])
        except (KeyError, TypeError, ValueError):
            continue
        opened, closed = parse(detail.get("opened_at")), parse(detail.get("closed_at"))
        sign = 1 if replay["direction"] == "LONG" else -1
        decided = dt.datetime.fromtimestamp(replay["ts"] / 1000, dt.timezone.utc)
        rows.append({
            "id": decision_id, "ticker": replay["ticker"], "direction": replay["direction"], "sign": sign,
            "reviewed_entry": replay["entry"], "real_entry": entry, "real_exit": exit_px,
            "stop": replay["stop"], "target": replay["target"],
            "real_gross": sign * (exit_px / entry - 1), "replay_net": replay["rule"], "replay_reason": replay["reason"],
            "real_reason": str(detail.get("close_reason") or ""),
            "entry_slip": sign * (entry / replay["entry"] - 1),          # positive = paid more than the reviewed price
            "delay_s": (opened - decided).total_seconds() if opened else None,
            "held_min": (closed - opened).total_seconds() / 60 if opened and closed else None,
            "pnl_usd": detail.get("pnl_usd"),
        })
    print(f"taken crypto trades with a real close and a replay: {len(rows)}")
    real_net = [r["real_gross"] - COST for r in rows]
    print(f"real, price move minus the same {COST * 100:.2f}% costs: {mean(real_net):+.2f}% per trade")
    print(f"replay of the same candidates:                    {mean([r['replay_net'] for r in rows]):+.2f}% per trade")
    print(f"gap: {mean(real_net) - mean([r['replay_net'] for r in rows]):+.2f} points")

    print("\n1. Entry: real fill against the price the AI reviewed")
    slips = [r["entry_slip"] for r in rows]
    delays = [r["delay_s"] for r in rows if r["delay_s"] is not None]
    print(f"   mean entry slippage {mean(slips):+.3f}% (median {statistics.median(slips) * 100:+.3f}%), "
          f"worse than 0.2%: {sum(s > 0.002 for s in slips)} of {len(slips)}")
    if delays:
        print(f"   time from review to fill: median {statistics.median(delays):.0f}s, 90th percentile "
              f"{sorted(delays)[int(len(delays) * 0.9)]:.0f}s")

    print("\n2. Holding time (the replay exits after 120 minutes at the latest)")
    held = [r["held_min"] for r in rows if r["held_min"] is not None]
    buckets = Counter("<=125 min" if h <= 125 else "125-240 min" if h <= 240 else "4-24 h" if h <= 1440 else "> 24 h" for h in held)
    print(f"   median {statistics.median(held):.0f} min; " + ", ".join(f"{k}: {v}" for k, v in buckets.most_common()))
    for label, lo, hi in (("<=125 min", 0, 125), ("125-240 min", 125, 240), ("4-24 h", 240, 1440), ("> 24 h", 1440, 1e9)):
        part = [r for r in rows if r["held_min"] is not None and lo < r["held_min"] <= hi]
        if part:
            print(f"   {label:12} n={len(part):3} real {mean([r['real_gross'] - COST for r in part]):+.2f}% vs replay "
                  f"{mean([r['replay_net'] for r in part]):+.2f}%")

    print("\n3. How the trades ended: real close reason against the replay's")
    table = defaultdict(list)
    for r in rows:
        table[(r["real_reason"] or "?", r["replay_reason"])].append(r)
    for (real_reason, replay_reason), part in sorted(table.items(), key=lambda kv: -len(kv[1]))[:14]:
        print(f"   real {real_reason:28} replay {replay_reason:6} n={len(part):3} real {mean([r['real_gross'] - COST for r in part]):+.2f}% "
              f"replay {mean([r['replay_net'] for r in part]):+.2f}%")

    print("\n4. Stops: real exit against the stop level, for trades that closed on a stop")
    stopped = [r for r in rows if "stop" in r["real_reason"].lower() and "time" not in r["real_reason"].lower()]
    if stopped:
        beyond = [r["sign"] * (r["stop"] / r["real_exit"] - 1) for r in stopped]   # positive = exit worse than the stop level
        print(f"   n={len(stopped)}; exit beyond the stop level by {mean(beyond):+.3f}% on average "
              f"(median {statistics.median(beyond) * 100:+.3f}%); worse than 0.5%: {sum(b > 0.005 for b in beyond)}")
        planned = [r["sign"] * (r["stop"] / r["reviewed_entry"] - 1) for r in stopped]
        actual = [r["real_gross"] for r in stopped]
        print(f"   planned loss at the stop {mean(planned):+.2f}%, realised {mean(actual):+.2f}%")

    print("\n5. Targets: trades the replay closed at the target")
    winners = [r for r in rows if r["replay_reason"] == "target"]
    if winners:
        print(f"   n={len(winners)}; replay {mean([r['replay_net'] for r in winners]):+.2f}%, real "
              f"{mean([r['real_gross'] - COST for r in winners]):+.2f}%; real close reasons: "
              + ", ".join(f"{k}: {v}" for k, v in Counter(r["real_reason"] for r in winners).most_common(5)))

    print("\n6. Biggest gaps (real minus replay)")
    for r in sorted(rows, key=lambda r: (r["real_gross"] - COST) - r["replay_net"])[:8]:
        print(f"   {r['ticker']:12} {r['direction']:5} real {((r['real_gross'] - COST) * 100):+6.2f}% ({r['real_reason']}, held "
              f"{(r['held_min'] or 0):.0f} min) vs replay {r['replay_net'] * 100:+6.2f}% ({r['replay_reason']}); entry slip {r['entry_slip'] * 100:+.2f}%")


if __name__ == "__main__":
    main()
