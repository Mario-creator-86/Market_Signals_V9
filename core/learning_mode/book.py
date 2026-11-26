"""Learning mode: the trades Opus proposed, the signals they become, and the record of their results.

State lives in ``data/learning_mode/``: ``state.json`` (pending ideas, which
movers were asked when, which headlines were already shown to Opus) and
``closed.jsonl`` (one line per closed learning trade). The capital state keeps
only the last 300 closed positions of all strategies, so progress toward
10,000 trades is counted from this ledger.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from core.learning_mode.settings import (
    LEARNING_SIGNAL_MODE,
    LEARNING_SOURCE,
    MOVER_PREFIX,
    NEWS_PREFIX,
    learning_config,
)

LOCAL_TZ = ZoneInfo("Europe/Bucharest")
ADMISSION_SCORE = 95          # orders learning ideas first in the cycle; not a probability
CONFIDENCE_BANDS = (("0.55-0.65", 0.0, 0.65), ("0.65-0.75", 0.65, 0.75), ("0.75+", 0.75, 9.0))


def _parse(value: Any) -> Optional[datetime]:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _is_learning_strategy(strategy: Any) -> bool:
    return str(strategy or "").upper().startswith((MOVER_PREFIX, NEWS_PREFIX))


def _learning_block(position: Dict[str, Any]) -> Dict[str, Any]:
    for holder in (position.get("decision_context"), position.get("signal")):
        if isinstance(holder, dict) and isinstance(holder.get("learning"), dict):
            return holder["learning"]
    return {}


def build_learning_signal(entry: Dict[str, Any], cfg: Dict[str, Any], *, now: datetime) -> Dict[str, Any]:
    """The paper-trading signal for one of Opus's ideas, in the shape the autonomous pipeline expects."""
    side = str(entry["side"]).upper()
    action = "BUY" if side == "LONG" else "SHORT"
    price = float(entry["price"])
    stop, target = float(entry["stop_pct"]) / 100.0, float(entry["target_pct"]) / 100.0
    sl = price * (1.0 + stop) if action == "SHORT" else price * (1.0 - stop)
    tp = price * (1.0 - target) if action == "SHORT" else price * (1.0 + target)
    prefix = NEWS_PREFIX if entry["source"] == "news" else MOVER_PREFIX
    strategy = f"{prefix}{side}"
    crypto = "CRYPTO" in str(entry.get("asset_class") or "").upper()
    headlines = [dict(h) for h in (entry.get("headlines") or []) if isinstance(h, dict)][:6]
    reasons = [
        f"Learning mode ({entry['source']}): Opus chose {side} on {entry['ticker']} with confidence "
        f"{float(entry['confidence']):.2f}.",
        f"Entry {price:.6g}; stop {entry['stop_pct']:g}%, target {entry['target_pct']:g}%, "
        f"closed after {entry['hold_hours']} h at the latest.",
        f"Why: {entry.get('why') or 'not given'}",
        "Paper learning trade: small fixed size, measured toward 10,000 trades; not a validated edge.",
    ]
    learning = {key: entry.get(key) for key in ("source", "side", "confidence", "stop_pct", "target_pct",
                                                 "hold_hours", "why", "model", "created_at", "headline_ids")}
    learning["opus_price"] = entry.get("opus_price", entry.get("price"))
    return {
        "id": entry["id"],
        "ticker": entry["ticker"],
        "action": action,
        "type": action,
        "direction": side,
        "status": "Open",
        # The time the entry price was observed (Opus's answer, or the scanner snapshot it was
        # re-anchored to): the data-age gates of Risk and Capital judge that price, not the hand-out.
        "created_at": (_parse(entry.get("price_observed_at")) or _parse(entry.get("created_at")) or now)
        .astimezone(LOCAL_TZ).isoformat(),
        "source": "autonomous_monitor",
        "autonomous_source": LEARNING_SOURCE,
        "autonomous_candidate_origin": LEARNING_SOURCE,
        "strategy_type": strategy,
        "strategy_id": strategy,
        "strategy_status": "RESEARCH",
        "strategy_evidence": {"lifecycle_status": "RESEARCH", "source": "learning_mode", "strategy_id": strategy},
        "entry_strategy": f"learning_{entry['source']}",
        "signal_mode": LEARNING_SIGNAL_MODE,
        "variant": "AUTO",
        "strategy_source": "LEARNING_MODE_V1",
        "entry_policy": "ai_chosen_paper_learning",
        "asset_class": "CRYPTO_FUTURES" if crypto else "US_STOCKS",
        "entry": round(price, 8),
        "entry_price": round(price, 8),
        "price": round(price, 8),
        "sl": round(sl, 8),
        "tp": round(tp, 8),
        "risk_reward_ratio": round(target / stop, 4) if stop > 0 else None,
        "time_stop_minutes": int(entry["hold_hours"]) * 60,
        "signal_score": ADMISSION_SCORE,
        "score": ADMISSION_SCORE,
        "raw_signal_score": ADMISSION_SCORE,
        "signal_score_source": "learning_mode_admission",
        "validation_score_status": "unavailable",
        "average_daily_volume_usd": entry.get("dollar_volume"),
        "learning_size_pct": float(cfg.get("position_pct_of_equity", 1.0)),
        "learning": learning,
        "news_headlines": [h.get("title") for h in headlines if h.get("title")],
        "top_headlines": headlines,
        "decision_reasons": reasons,
        "reasons": reasons,
    }


class LearningBook:
    """Pending ideas, ask bookkeeping and the closed-trade ledger of learning mode. Thread-safe."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._state_path = self.root / "state.json"
        self._ledger_path = self.root / "closed.jsonl"
        self._lock = threading.RLock()
        self._state = self._load_state()
        self._recorded = {row.get("position_id") for row in self._ledger()}

    # --- persistence -----------------------------------------------------------------------------
    def _load_state(self) -> Dict[str, Any]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        for key in ("seen_headlines", "mover_asked"):
            data[key] = data.get(key) if isinstance(data.get(key), dict) else {}
        for key in ("ideas", "reviewed"):
            data[key] = data.get(key) if isinstance(data.get(key), list) else []
        return data

    def _save_state(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        scratch = self._state_path.with_name("state.json.tmp")
        scratch.write_text(json.dumps(self._state, default=str), encoding="utf-8")
        os.replace(scratch, self._state_path)

    def _ledger(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        try:
            with self._ledger_path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except OSError:
            pass
        return rows

    # --- ideas -----------------------------------------------------------------------------------
    def add_idea(self, idea: Dict[str, Any], *, ticker: str, source: str, price: float, asset_class: str,
                 headlines: Iterable[Dict[str, Any]], model: str, now: datetime,
                 dollar_volume: Optional[float] = None) -> Dict[str, Any]:
        entry = {
            **idea,
            "id": f"learn_{source}_{ticker}_{idea['side']}_{int(now.timestamp())}".lower(),
            "ticker": str(ticker).upper(),
            "source": source,
            "price": float(price),
            "asset_class": asset_class,
            "headlines": [dict(h) for h in headlines if isinstance(h, dict)][:6],
            "model": model,
            "created_at": now.astimezone(timezone.utc).isoformat(),
            "dollar_volume": dollar_volume,
        }
        with self._lock:
            self._state["ideas"].append(entry)
            self._save_state()
        return entry

    def candidates(self, exec_cfg: Dict[str, Any], now: datetime,
                   reprice: Optional[Callable[[str], Any]] = None,
                   open_learning: Optional[int] = None) -> List[Dict[str, Any]]:
        """Signals for the ideas still waiting (younger than the idea TTL, not yet reviewed). Paper only.

        ``reprice(ticker)`` gives the latest scanner price: the idea is anchored to it,
        with Opus's stop, target and hold unchanged, or dropped when the price moved
        more than ``max_price_drift_pct`` from the one Opus judged (seen live: a 3x ETF
        moved 3.2% in the two minutes before the cycle, and the price check refused it).
        """
        cfg = learning_config(exec_cfg)
        if str((exec_cfg or {}).get("mode", "paper")).lower() != "paper" or not cfg.get("enabled", True):
            return []
        if open_learning is not None and open_learning >= int(cfg.get("max_open_positions", 15) or 0):
            return []                                   # learning mode's share of open positions is used up
        ttl = timedelta(minutes=float(cfg["idea_ttl_minutes"]))
        max_drift = float(cfg.get("max_price_drift_pct", 3.0)) / 100.0
        with self._lock:
            reviewed = set(self._state["reviewed"])
            alive: List[Dict[str, Any]] = []
            for entry in self._state["ideas"]:
                if entry.get("id") in reviewed or (_parse(entry.get("created_at")) or now) + ttl < now:
                    continue
                latest, observed = None, None
                if reprice is not None:
                    try:
                        answer = reprice(entry["ticker"])
                        if isinstance(answer, tuple):
                            answer, observed = answer
                        latest = float(answer or 0.0)
                    except Exception:  # noqa: BLE001 - a failed lookup keeps the price Opus judged
                        latest, observed = None, None
                if latest and latest > 0:
                    judged = float(entry.get("opus_price", entry["price"]))
                    if abs(latest / judged - 1.0) > max_drift:
                        continue                                    # the premise changed: dropped
                    entry["opus_price"], entry["price"] = judged, latest
                    entry["price_observed_at"] = (observed or now).isoformat()
                alive.append(entry)
            if alive != self._state["ideas"] or reprice is not None:
                self._state["ideas"] = alive
                self._state["reviewed"] = [i for i in self._state["reviewed"] if i in {e["id"] for e in alive}]
                self._save_state()
            return [build_learning_signal(entry, cfg, now=now) for entry in alive]

    def note_reviewed(self, idea_id: Any) -> None:
        with self._lock:
            self._state["ideas"] = [e for e in self._state["ideas"] if e.get("id") != idea_id]
            self._save_state()

    def write_status(self, payload: Dict[str, Any]) -> None:
        """Publish the worker's latest status for the API process (``status.json``)."""
        self.root.mkdir(parents=True, exist_ok=True)
        scratch = self.root / "status.json.tmp"
        scratch.write_text(json.dumps(payload, default=str), encoding="utf-8")
        os.replace(scratch, self.root / "status.json")

    def pending_count(self, now: Optional[datetime] = None, ttl_minutes: Optional[float] = None) -> int:
        """Ideas still waiting for the cycle; with ``now``, only those younger than ``ttl_minutes``."""
        with self._lock:
            ideas = list(self._state["ideas"])
        if now is None:
            return len(ideas)
        ttl = timedelta(minutes=float(ttl_minutes if ttl_minutes is not None else 4))
        return sum(1 for e in ideas if (_parse(e.get("created_at")) or now) + ttl >= now)

    # --- asking ----------------------------------------------------------------------------------
    def may_ask_mover(self, ticker: str, now: datetime, cooldown_minutes: float) -> bool:
        with self._lock:
            last = _parse(self._state["mover_asked"].get(str(ticker).upper()))
        return last is None or now - last >= timedelta(minutes=float(cooldown_minutes))

    def note_mover_asked(self, ticker: str, now: datetime) -> None:
        with self._lock:
            asked = self._state["mover_asked"]
            asked[str(ticker).upper()] = now.isoformat()
            cutoff = now - timedelta(days=1)
            self._state["mover_asked"] = {k: v for k, v in asked.items() if (_parse(v) or now) >= cutoff}
            self._save_state()

    def unseen_headlines(self, rows: Iterable[Dict[str, Any]], now: datetime, *, max_age_minutes: float,
                         min_impact: float) -> List[Dict[str, Any]]:
        """Headlines Opus has not seen yet: fresh, important enough, and not from the future."""
        out: List[Dict[str, Any]] = []
        misdated: List[str] = []
        with self._lock:
            seen = set(self._state["seen_headlines"])
        for row in rows:
            if not isinstance(row, dict) or row.get("is_duplicate"):
                continue                                    # the news agent already marked it as a copy
            key = str(row.get("id") or row.get("story_fingerprint") or row.get("title") or "").strip()
            story = str(row.get("story_fingerprint") or "").strip()
            published = _parse(row.get("published_at"))
            if not key or key in seen or (story and story in seen) or published is None:
                continue
            age = (now - published).total_seconds() / 60.0
            if age < -1:
                # A clock ahead of now (often a local time read as UTC): it would later be shown
                # to Opus as brand new when it is hours old. Set aside for good.
                misdated.extend(x for x in (key, story) if x)
                continue
            try:
                impact = float(row.get("impact_score") or 0)
            except (TypeError, ValueError):
                impact = 0.0
            if age > float(max_age_minutes) or impact < float(min_impact):
                continue
            seen.update(x for x in (key, story) if x)        # one story once per batch
            out.append({**row, "id": key, "age_minutes": int(round(max(0.0, age)))})
        if misdated:
            with self._lock:
                for key in misdated:
                    self._state["seen_headlines"][key] = now.isoformat()
                self._save_state()
        return out

    def note_headlines_seen(self, rows: Iterable[Dict[str, Any]], now: datetime) -> None:
        with self._lock:
            seen = self._state["seen_headlines"]
            for row in rows:
                if isinstance(row, dict) and row.get("id"):
                    seen[str(row["id"])] = now.isoformat()
                    if row.get("story_fingerprint"):
                        seen[str(row["story_fingerprint"])] = now.isoformat()
            cutoff = now - timedelta(days=2)
            self._state["seen_headlines"] = {k: v for k, v in seen.items() if (_parse(v) or now) >= cutoff}
            self._save_state()

    # --- results ---------------------------------------------------------------------------------
    @staticmethod
    def opened_today(positions: Iterable[Dict[str, Any]], now: datetime) -> int:
        """Learning positions opened on the local calendar day of ``now`` (open or already closed)."""
        today = now.astimezone(LOCAL_TZ).date()
        count = 0
        for position in positions or []:
            if not isinstance(position, dict) or not _is_learning_strategy(position.get("strategy_type")):
                continue
            opened = _parse(position.get("opened_at"))
            if opened is not None and opened.astimezone(LOCAL_TZ).date() == today:
                count += 1
        return count

    def sync_closed(self, positions: Iterable[Dict[str, Any]]) -> int:
        """Append newly closed learning trades to the ledger; how many were new."""
        new_rows: List[Dict[str, Any]] = []
        with self._lock:
            for position in positions or []:
                if not isinstance(position, dict) or not _is_learning_strategy(position.get("strategy_type")):
                    continue
                pid = position.get("position_id")
                if str(position.get("status") or "").upper() != "CLOSED" or not pid or pid in self._recorded:
                    continue
                block = _learning_block(position)
                margin = float(position.get("margin_used") or position.get("usd_size") or 0.0)
                net = float(position.get("net_pnl") or 0.0)
                strategy = str(position.get("strategy_type")).upper()
                new_rows.append({
                    "position_id": pid,
                    "ticker": position.get("ticker"),
                    "strategy_type": strategy,
                    "side": strategy.rsplit("_", 1)[-1],
                    "source": block.get("source") or ("news" if strategy.startswith(NEWS_PREFIX) else "mover"),
                    "confidence": block.get("confidence"),
                    "model": block.get("model"),
                    "opened_at": position.get("opened_at"),
                    "closed_at": position.get("closed_at"),
                    "close_reason": position.get("close_reason"),
                    "net_pnl": round(net, 6),
                    "pnl_pct": round(net / margin * 100.0, 4) if margin > 0 else None,
                })
                self._recorded.add(pid)
            if new_rows:
                self.root.mkdir(parents=True, exist_ok=True)
                with self._ledger_path.open("a", encoding="utf-8") as handle:
                    for row in new_rows:
                        handle.write(json.dumps(row, default=str) + "\n")
        return len(new_rows)

    def progress(self, cfg: Dict[str, Any], positions: Iterable[Dict[str, Any]] = ()) -> Dict[str, Any]:
        """Where learning mode stands: closed of the target, results by source, side and confidence."""
        rows = self._ledger()

        def summary(group: List[Dict[str, Any]]) -> Dict[str, Any]:
            nets = [float(r.get("net_pnl") or 0.0) for r in group]
            return {"closed": len(group), "net_pnl": round(sum(nets), 4),
                    "win_rate_pct": round(100.0 * sum(1 for n in nets if n > 0) / len(nets), 1) if nets else None}

        def band(row: Dict[str, Any]) -> str:
            try:
                value = float(row.get("confidence"))
            except (TypeError, ValueError):
                return "unknown"                            # opened before positions kept Opus's decision
            return next(name for name, low, high in CONFIDENCE_BANDS if low <= value < high)

        open_now = [p for p in positions or [] if isinstance(p, dict) and _is_learning_strategy(p.get("strategy_type"))
                    and str(p.get("status") or "").upper() == "OPEN"]
        total = summary(rows)
        every = max(1, int(cfg.get("checkpoint_every", 1000) or 1000))
        return {
            "target": int(cfg.get("target_closed_trades", 10_000)),
            "closed": total["closed"],
            "open": len(open_now),
            "net_pnl": total["net_pnl"],
            "win_rate_pct": total["win_rate_pct"],
            "checkpoint": total["closed"] // every,
            "checkpoint_every": every,
            "by_source": {name: summary([r for r in rows if r.get("source") == name]) for name in ("mover", "news")},
            "by_side": {name: summary([r for r in rows if r.get("side") == name]) for name in ("LONG", "SHORT")},
            "by_confidence": {name: summary([r for r in rows if band(r) == name]) for name, _, _ in CONFIDENCE_BANDS},
            "recent": list(reversed(rows[-20:])),
            "pending_ideas": self.pending_count(),
        }


__all__ = ["ADMISSION_SCORE", "LearningBook", "build_learning_signal"]
