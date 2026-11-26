"""The app's record of its own AI reviews, measured and fed back to the reviewer.

The decision ledger keeps every candidate the app reviewed - the ones it took
and the ones it rejected - with direction, entry, stop, target, the AI's verdict
and confidence, and the critic's veto. This module replays the SAME exit rule
(stop or target if touched, otherwise exit after the time limit) on the prices
that followed each candidate, so approved and rejected candidates are compared
on one yardstick, and the AI's stated confidence is checked against outcomes.

    python -m core.ai.scorecard            # rebuild data/research/ai_scorecard.json

``track_record_for(signal)`` returns the block for the candidate's strategy; the
agent manager attaches it to every review as ``ai_track_record``. It is
evidence only: never a veto, never a size input. Rebuilding needs prices from
the network, so it runs outside the decision path (scheduled); the decision
path only reads the saved file.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

BAR_MS = 5 * 60_000
DAY_MS = 86_400_000
DEFAULT_COST = 2 * 0.0004 + 0.00025 + 0.00025     # round trip, as in the research studies
DEFAULT_TIME_STOP_MINUTES = 120
_SMALL_SAMPLE = 30
_CONFIDENCE_RANGES = ((0.0, 0.4), (0.4, 0.6), (0.6, 0.75), (0.75, 1.0001))
GROUPS = ("taken", "rejected_by_ai", "rejected_by_critic", "rejected_by_rules")
EXIT_RULE = "same rule for every candidate: stop or target if touched first, otherwise exit after {minutes} minutes; net of costs"

BarsProvider = Callable[[str, int], List[List[float]]]


def scorecard_path() -> Path:
    from core.config import data_path

    return data_path("research", "ai_scorecard.json")


def replay_cache_path() -> Path:
    from core.config import data_path

    return data_path("research", "ai_replays.json")


# ------------------------------------------------------------------ replay (pure)
def replay_candidate(decision: Dict[str, Any], bars: List[List[float]], *,
                     time_stop_minutes: int = DEFAULT_TIME_STOP_MINUTES, cost: float = DEFAULT_COST) -> Optional[Dict[str, Any]]:
    """What the exit rule would have returned. ``bars`` are [ts, open, high, low, close], 5-minute.

    A bar that touches both levels counts as the stop. None when the bars are
    missing or do not start near the recorded entry (another instrument, bad data).
    """
    try:
        entry, stop, target = float(decision["entry"]), float(decision["stop"]), float(decision["target"])
    except (KeyError, TypeError, ValueError):
        return None
    limit = max(1, int(time_stop_minutes) // 5)
    if len(bars) < 6 or entry <= 0 or abs(float(bars[0][1]) / entry - 1) > 0.05:
        return None
    sign = 1 if str(decision.get("direction")).upper() == "LONG" else -1
    exit_price, reason = None, "time"
    for bar in bars[1:1 + limit]:
        high, low, close = float(bar[2]), float(bar[3]), float(bar[4])
        if (low <= stop) if sign == 1 else (high >= stop):
            exit_price, reason = stop, "stop"
            break
        if (high >= target) if sign == 1 else (low <= target):
            exit_price, reason = target, "target"
            break
        exit_price = close
    if exit_price is None:
        return None
    return {"net": sign * (exit_price / entry - 1) - float(cost), "reason": reason}


# ------------------------------------------------------------------ ledger
def _dig(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _read_decisions(ledger_path: Path, since_ms: int) -> List[Dict[str, Any]]:
    uri = Path(ledger_path).resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=15)
    try:
        rows = db.execute(
            "SELECT decision_id, created_at, ticker, status, strategy, entry_json FROM decision_ledger_entries "
            "WHERE source != 'REPLAY'"
        ).fetchall()
    finally:
        db.close()
    out: List[Dict[str, Any]] = []
    for decision_id, created, ticker, status, strategy, entry_json in rows:
        try:
            entry = json.loads(entry_json)
            ts = int(datetime.fromisoformat(str(created)).timestamp() * 1000)
        except (TypeError, ValueError):
            continue
        if ts < since_ms:
            continue
        data = _dig(entry, "validation", "checks", "authoritative", "capital_entry", "checks", "data") or {}
        strategy = str(strategy or "").upper()
        direction = str(data.get("direction") or "").upper() or (
            "LONG" if strategy.endswith("LONG") else "SHORT" if strategy.endswith("SHORT") else "")
        try:
            price = float(_dig(entry, "data_existed", "entry") or data.get("entry"))
            stop = float(data.get("stop") or _dig(entry, "risk", "risk_profile", "stop"))
            target = float(data.get("target") or _dig(entry, "risk", "risk_profile", "target"))
        except (TypeError, ValueError):
            continue
        if direction not in ("LONG", "SHORT") or min(price, stop, target) <= 0:
            continue
        proposal = entry.get("ai_proposal") if isinstance(entry.get("ai_proposal"), dict) else {}
        critic = entry.get("critic") if isinstance(entry.get("critic"), dict) else {}
        ai_action = proposal.get("action")
        # The AI's own verdict first: a rule basket is entered even when the AI advised against it
        # (advisory, paper only), and such an entry is not one the AI approved.
        if critic.get("available") and critic.get("veto"):
            group = "rejected_by_critic"
        elif ai_action in ("NO_TRADE", "WATCH"):
            group = "rejected_by_ai"
        elif str(status) == "accepted":
            group = "taken"
        else:
            group = "rejected_by_rules"
        confidence = proposal.get("confidence")
        out.append({
            "id": str(decision_id), "ts": ts, "ticker": str(ticker), "strategy": strategy, "direction": direction,
            "entry": price, "stop": stop, "target": target, "group": group,
            "confidence": float(confidence) if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else None,
        })
    return out


# ------------------------------------------------------------------ statistics
def _stat(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Reviews of one instrument on one day and side are one observation."""
    pooled = defaultdict(list)
    for row in rows:
        pooled[(row["ticker"], row["ts"] // DAY_MS, row["direction"])].append(row["net"])
    values = [statistics.fmean(v) for v in pooled.values()]
    if not values:
        return {"n": 0, "independent_n": 0, "mean_net_pct": None, "win_rate_pct": None, "profit_factor": None,
                "small_sample": True}
    gains, losses = sum(v for v in values if v > 0), -sum(v for v in values if v < 0)
    return {
        "n": len(rows), "independent_n": len(values),
        "mean_net_pct": round(statistics.fmean(values) * 100, 3),
        "win_rate_pct": round(sum(v > 0 for v in values) / len(values) * 100, 1),
        "profit_factor": round(gains / losses, 2) if losses > 0 else None,
        "small_sample": len(values) < _SMALL_SAMPLE,
    }


def _block(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    block: Dict[str, Any] = {group: _stat([r for r in rows if r["group"] == group]) for group in GROUPS}
    calibration = []
    for low, high in _CONFIDENCE_RANGES:
        bucket = [r for r in rows if r["confidence"] is not None and low <= r["confidence"] < high]
        stat = _stat(bucket)
        calibration.append({"range": f"{low:.2f}-{min(high, 1.0):.2f}", **stat,
                            "taken": sum(1 for r in bucket if r["group"] == "taken")})
    block["confidence"] = calibration
    return block


def build_scorecard(
    ledger_path: Path, *, bars_provider: BarsProvider, now_ms: Optional[int] = None,
    replay_cache_path: Optional[Path] = None, cost: float = DEFAULT_COST,
    time_stop_minutes: int = DEFAULT_TIME_STOP_MINUTES, min_age_minutes: int = 180,
    lookback_days: int = 180, max_new: int = 5000,
) -> Dict[str, Any]:
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    cache_path = Path(replay_cache_path) if replay_cache_path else None
    cache: Dict[str, Any] = {}
    if cache_path and cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
    decisions = _read_decisions(Path(ledger_path), now - lookback_days * DAY_MS)
    fetched = 0
    for decision in decisions:
        if decision["id"] in cache or now - decision["ts"] < min_age_minutes * 60_000:
            continue
        if fetched >= max_new:
            break
        bars = bars_provider(decision["ticker"], decision["ts"] // BAR_MS * BAR_MS)
        result = replay_candidate(decision, bars or [], time_stop_minutes=time_stop_minutes, cost=cost)
        cache[decision["id"]] = result["net"] if result else None
        fetched += 1
    if cache_path and fetched:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_name(cache_path.name + ".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, cache_path)

    rows = [{**d, "net": cache[d["id"]]} for d in decisions if cache.get(d["id"]) is not None]
    by_strategy: Dict[str, Any] = {}
    for strategy in sorted({r["strategy"] for r in rows}):
        by_strategy[strategy] = _block([r for r in rows if r["strategy"] == strategy])
    stamps = [r["ts"] for r in rows]
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()  # noqa: E731
    return {
        "generated_at": iso(now), "generated_at_ms": now,
        "exit_rule": EXIT_RULE.format(minutes=time_stop_minutes),
        "decisions_replayed": len(rows), "new_replays": fetched,
        "window": {"from": iso(min(stamps)), "to": iso(max(stamps))} if stamps else None,
        "by_strategy": by_strategy,
        "overall": _block(rows) if rows else None,
    }


def write_scorecard(card: Dict[str, Any], path: Optional[Path] = None) -> Path:
    target = Path(path) if path else scorecard_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(card, indent=1), encoding="utf-8")
    os.replace(tmp, target)
    return target


def load_scorecard(path: Optional[Path] = None, *, now_ms: Optional[int] = None, max_age_hours: float = 72.0) -> Optional[Dict[str, Any]]:
    """The saved scorecard, or None when it is missing, unreadable or too old to describe the present."""
    target = Path(path) if path else scorecard_path()
    try:
        card = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(card, dict):
        return None
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    try:
        if now - int(card.get("generated_at_ms") or 0) > max_age_hours * 3_600_000:
            return None
    except (TypeError, ValueError):
        return None
    return card


_cached: Dict[str, Any] = {"mtime": None, "card": None, "checked": 0.0}
_cache_lock = threading.Lock()


def _current_scorecard() -> Optional[Dict[str, Any]]:
    path = scorecard_path()
    with _cache_lock:
        if time.monotonic() - _cached["checked"] > 60:
            _cached["checked"] = time.monotonic()
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = None
            if mtime != _cached["mtime"]:
                _cached["mtime"], _cached["card"] = mtime, (load_scorecard(path) if mtime else None)
        return _cached["card"]


def _pct(stat: Dict[str, Any]) -> str:
    return f"{stat['mean_net_pct']:+.2f}% per trade (n={stat['independent_n']})"


def track_record_for(signal: Dict[str, Any], scorecard: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The measured record of past reviews of this candidate's strategy, for the reviewer."""
    card = scorecard if scorecard is not None else _current_scorecard()
    if not isinstance(card, dict):
        return None
    strategy = str((signal or {}).get("strategy_type") or (signal or {}).get("strategy_id") or "").upper()
    block = (card.get("by_strategy") or {}).get(strategy)
    if not isinstance(block, dict):
        return None
    taken, by_ai = block.get("taken") or {}, block.get("rejected_by_ai") or {}
    if (taken.get("independent_n") or 0) + (by_ai.get("independent_n") or 0) < 10:
        return None
    reading: List[str] = []
    if (taken.get("independent_n") or 0) >= 10:
        if taken["mean_net_pct"] < 0:
            reading.append(f"Candidates of this strategy that were approved lost on average after costs: {_pct(taken)}.")
        else:
            reading.append(f"Candidates of this strategy that were approved made {_pct(taken)}.")
    if (taken.get("independent_n") or 0) >= 10 and (by_ai.get("independent_n") or 0) >= 10:
        gap = by_ai["mean_net_pct"] - taken["mean_net_pct"]
        if gap > 0.1:
            reading.append(f"The candidates you rejected did better than the ones you approved ({_pct(by_ai)} against "
                           f"{_pct(taken)}): your approvals did not pick the winners.")
        elif gap < -0.1:
            reading.append(f"The candidates you approved did better than the ones you rejected ({_pct(taken)} against "
                           f"{_pct(by_ai)}): the rejections were worth something.")
        else:
            reading.append(f"Approved and rejected candidates did about the same ({_pct(taken)} against {_pct(by_ai)}): "
                           "the review did not separate winners from losers.")
    buckets = [b for b in block.get("confidence") or [] if (b.get("independent_n") or 0) >= 8]
    if len(buckets) >= 2:
        low, high = buckets[0], buckets[-1]
        if high["mean_net_pct"] <= low["mean_net_pct"]:
            reading.append(f"Higher stated confidence did not win more: confidence {high['range']} gave {_pct(high)}, "
                           f"confidence {low['range']} gave {_pct(low)}. Do not raise confidence without new, specific evidence.")
        else:
            reading.append(f"Higher stated confidence went with better results: confidence {high['range']} gave {_pct(high)}, "
                           f"confidence {low['range']} gave {_pct(low)}.")
    if any((block.get(g) or {}).get("small_sample") for g in ("taken", "rejected_by_ai")):
        reading.append("Some of these samples are small; treat the comparison as a tendency, not a law.")
    return {
        "what": "replay of past candidates of this strategy, taken or rejected, under one exit rule",
        "exit_rule": card.get("exit_rule"), "as_of": card.get("generated_at"),
        "taken": taken, "rejected_by_ai": by_ai,
        "rejected_by_critic": block.get("rejected_by_critic"), "rejected_by_rules": block.get("rejected_by_rules"),
        "confidence_calibration": block.get("confidence"),
        "reading": reading,
    }


# ------------------------------------------------------------------ rebuild (network, outside the decision path)
def _binance_bars(ticker: str, start_ms: int) -> List[List[float]]:
    """5-minute Binance spot bars for a crypto ticker ('ACE-USD'); [] for anything else."""
    if os.environ.get("MS_ENV") == "test" or not str(ticker).upper().endswith("-USD"):
        return []
    symbol = str(ticker).upper()[:-4].replace("-", "") + "USDT"
    url = f"https://api.binance.com/api/v3/klines?symbol={quote(symbol)}&interval=5m&startTime={start_ms}&limit=60"
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "market-research-script"})
            with urllib.request.urlopen(request, timeout=20) as response:
                rows = json.loads(response.read().decode("utf-8"))
            time.sleep(0.05)
            return [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])] for k in rows]
        except urllib.error.HTTPError as exc:
            if exc.code in (418, 429):
                raise RuntimeError(f"rate limited by Binance ({exc.code})") from None
            if exc.code == 400:
                return []
        except Exception:  # noqa: BLE001 - network trouble: retried, then raised so nothing wrong is cached
            if attempt == 2:
                raise
        time.sleep(1.5 * (attempt + 1))
    return []


def rebuild(now_ms: Optional[int] = None) -> Dict[str, Any]:
    from core.decision_ledger.store import decision_ledger_store_path

    card = build_scorecard(decision_ledger_store_path(), bars_provider=_binance_bars, now_ms=now_ms,
                           replay_cache_path=replay_cache_path())
    write_scorecard(card)
    return card


def main() -> int:
    try:
        card = rebuild()
    except Exception as exc:  # noqa: BLE001 - report and exit non-zero for the scheduler
        print(json.dumps({"failed": f"{exc.__class__.__name__}: {exc}"}))
        return 1
    overall = card.get("overall") or {}
    print(json.dumps({
        "generated_at": card["generated_at"], "decisions_replayed": card["decisions_replayed"],
        "new_replays": card["new_replays"], "strategies": sorted(card["by_strategy"]),
        "taken": overall.get("taken"), "rejected_by_ai": overall.get("rejected_by_ai"),
    }, indent=1))
    return 0


__all__ = ["build_scorecard", "load_scorecard", "rebuild", "replay_candidate", "track_record_for", "write_scorecard"]

if __name__ == "__main__":
    sys.exit(main())
