"""Does the AI reviewer add value? Approved vs rejected candidates, on the same yardstick.

usage: ai_scorecard_study.py <data dir with decision_ledger.db> <cache dir>

The decision ledger keeps every candidate the app reviewed: direction, entry,
stop, target, the AI's verdict and confidence, the critic's veto. For each
crypto candidate this replays what the SAME trade rule would have earned,
whether or not it was taken: entry at the recorded price, stop or target if
touched first (5-minute Binance bars; a bar that touches both counts as the
stop), otherwise exit after two hours. Costs: 0.13% round trip.

Candidates of one coin on one day and side are pooled before any statistic,
because the app re-reviews the same coin every cycle.
Read-only: the ledger is opened in read-only mode.
"""
import datetime as dt
import json
import math
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

DATA = Path(sys.argv[1])
CACHE = Path(sys.argv[2])
CACHE.mkdir(parents=True, exist_ok=True)
COST = 2 * 0.0004 + 0.00025 + 0.00025
TIME_STOP_MIN = 120
BAR = 5 * 60_000


def klines(symbol, start_ms):
    key = CACHE / f"k5_{symbol}_{start_ms}.json"
    if key.exists():
        return json.loads(key.read_text())
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=5m&startTime={start_ms}&limit=300"
    rows = []
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "market-research-script"})
            with urllib.request.urlopen(req, timeout=30) as r:
                rows = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])] for k in json.loads(r.read().decode())]
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (418, 429):
                raise SystemExit(f"rate limited by Binance ({exc.code}); stop and retry later")
            if exc.code == 400:
                break
            time.sleep(1.5 * (attempt + 1))
        except Exception:  # noqa: BLE001
            time.sleep(1.5 * (attempt + 1))
    key.write_text(json.dumps(rows))
    time.sleep(0.05)
    return rows


def dig(obj, *path):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def load_decisions():
    db = sqlite3.connect(f"file:{(DATA / 'decision_ledger.db').as_posix()}?mode=ro", uri=True)
    outcomes = {row[0]: json.loads(row[1]) for row in db.execute("SELECT decision_id, outcome_payload FROM decision_ledger_outcomes")}
    out = []
    for decision_id, created, ticker, status, strategy, entry_json in db.execute(
            "SELECT decision_id, created_at, ticker, status, strategy, entry_json FROM decision_ledger_entries WHERE source != 'REPLAY'"):
        if not str(ticker).endswith("-USD"):
            continue
        e = json.loads(entry_json)
        data = dig(e, "validation", "checks", "authoritative", "capital_entry", "checks", "data") or {}
        direction = str(data.get("direction") or ("LONG" if str(strategy).endswith("LONG") else "SHORT" if str(strategy).endswith("SHORT") else "")).upper()
        entry = dig(e, "data_existed", "entry") or data.get("entry")
        stop = data.get("stop") or dig(e, "risk", "risk_profile", "stop")
        target = data.get("target") or dig(e, "risk", "risk_profile", "target")
        try:
            entry, stop, target = float(entry), float(stop), float(target)
            ts = int(dt.datetime.fromisoformat(created).timestamp() * 1000)
        except (TypeError, ValueError):
            continue
        if direction not in ("LONG", "SHORT") or entry <= 0 or stop <= 0 or target <= 0:
            continue
        proposal = e.get("ai_proposal") or {}
        critic = e.get("critic") or {}
        ai_action = proposal.get("action")
        if status == "accepted":
            group = "taken"
        elif critic.get("veto") and ai_action in ("OPEN_LONG", "OPEN_SHORT", None) and critic.get("available"):
            group = "rejected by the critic"
        elif ai_action in ("NO_TRADE", "WATCH"):
            group = "rejected by the AI"
            if critic.get("veto"):
                group = "rejected by the critic"
        else:
            group = "rejected before the AI (rule gates)"
        out.append({"id": decision_id, "ts": ts, "ticker": ticker, "symbol": ticker[:-4].replace("-", "") + "USDT",
                    "direction": direction, "entry": entry, "stop": stop, "target": target, "group": group,
                    "confidence": proposal.get("confidence") if isinstance(proposal.get("confidence"), (int, float)) else None,
                    "ai_consulted": bool(dig(e, "ai", "consulted")), "month": created[:7],
                    "real": outcomes.get(decision_id)})
    return out


def replay(d):
    bars = klines(d["symbol"], d["ts"] // BAR * BAR)
    if len(bars) < 30 or abs(bars[0][1] / d["entry"] - 1) > 0.05:
        return None
    sign = 1 if d["direction"] == "LONG" else -1
    exit_px, reason = None, "time"
    for bar in bars[1:1 + TIME_STOP_MIN // 5]:
        _, _, high, low, close = bar
        hit_stop = low <= d["stop"] if sign == 1 else high >= d["stop"]
        hit_target = high >= d["target"] if sign == 1 else low <= d["target"]
        if hit_stop:
            exit_px, reason = d["stop"], "stop"
            break
        if hit_target:
            exit_px, reason = d["target"], "target"
            break
        exit_px = close
    if exit_px is None:
        return None
    idx24 = min(len(bars) - 1, 288)
    return {"rule": sign * (exit_px / d["entry"] - 1) - COST, "reason": reason,
            "h24": sign * (bars[idx24][4] / d["entry"] - 1) - COST if idx24 >= 280 else None}


def pooled(rows, key="rule"):
    groups = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            groups[(r["ticker"], r["ts"] // 86_400_000, r["direction"])].append(r[key])
    return [statistics.fmean(v) for v in groups.values()]


def line(label, rows, key="rule"):
    values = pooled(rows, key)
    if len(values) < 8:
        return f"{label:44} n={len(rows):5} pooled={len(values):4} (too few)"
    mean, sd = statistics.fmean(values), statistics.pstdev(values) or 1e-12
    gains, losses = sum(v for v in values if v > 0), -sum(v for v in values if v < 0)
    return (f"{label:44} n={len(rows):5} pooled={len(values):4} mean={mean * 100:+6.2f}% win={sum(v > 0 for v in values) / len(values) * 100:3.0f}% "
            f"PF={min(gains / losses if losses else 99, 99):5.2f} t={mean / (sd / math.sqrt(len(values))):+5.1f}")


def main():
    decisions = load_decisions()
    print(f"crypto candidates with levels in the ledger: {len(decisions)}")
    done = []
    for i, d in enumerate(decisions):
        result = replay(d)
        if result:
            done.append({**d, **result})
        if (i + 1) % 400 == 0:
            print(f"  replayed {i + 1}/{len(decisions)} (usable {len(done)})", flush=True)
    print(f"replayed with Binance prices: {len(done)}")
    (DATA / "research").mkdir(exist_ok=True)
    (DATA / "research" / "ai_scorecard_study.json").write_text(json.dumps(
        [{k: v for k, v in r.items() if k != "real"} for r in done]), encoding="utf-8")

    print("\nA. Same rule replayed for every candidate (stop/target/2h exit), net of costs")
    for group in ("taken", "rejected by the AI", "rejected by the critic", "rejected before the AI (rule gates)"):
        print("  " + line(group, [r for r in done if r["group"] == group]))
    rejected_all = [r for r in done if r["group"] != "taken"]
    print("  " + line("all rejected", rejected_all))

    print("\nB. By side")
    for side in ("LONG", "SHORT"):
        for group in ("taken", "rejected by the AI"):
            print("  " + line(f"{side} {group}", [r for r in done if r["group"] == group and r["direction"] == side]))

    print("\nC. By the AI's confidence (candidates the AI was asked about)")
    asked = [r for r in done if r["confidence"] is not None]
    for lo, hi in ((0.0, 0.4), (0.4, 0.6), (0.6, 0.75), (0.75, 1.01)):
        rows = [r for r in asked if lo <= r["confidence"] < hi]
        print("  " + line(f"confidence {lo:.2f}-{min(hi, 1.0):.2f} (taken {sum(r['group'] == 'taken' for r in rows)})", rows))

    print("\nD. By month")
    for month in sorted({r["month"] for r in done}):
        for group in ("taken", "rejected by the AI"):
            print("  " + line(f"{month} {group}", [r for r in done if r["month"] == month and r["group"] == group]))

    print("\nE. Held 24 hours instead of 2 (no stop), same candidates")
    for group in ("taken", "rejected by the AI", "rejected by the critic"):
        print("  " + line(group, [r for r in done if r["group"] == group], key="h24"))

    taken_real = [r for r in done if r["group"] == "taken" and r.get("real")]
    if taken_real:
        real = [float(r["real"].get("forward_return_pct") or 0.0) / 100 * (1 if r["direction"] == "LONG" else 1) for r in taken_real]
        print(f"\nF. Check: taken trades with a recorded real close: {len(taken_real)}; replay mean "
              f"{statistics.fmean(r['rule'] for r in taken_real) * 100:+.2f}% vs recorded forward return mean {statistics.fmean(real) * 100:+.2f}%")


if __name__ == "__main__":
    main()
