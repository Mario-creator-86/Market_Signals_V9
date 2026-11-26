"""Continuous, staged Pre-Earnings analysis.

The scanner processes one active 30-day candidate per interval, nearest
earnings event first.  The owner's watchlist reporters go to the front of the
queue (still nearest first among themselves): with one symbol per interval a
400-name pass takes hours, and an alphabetical tie-break inside a busy
earnings day left watched names reporting tomorrow unanalysed until after
they had reported.  Each stage has a strict dependency chain:

  1. refresh calendar, news, estimates, history and market context;
  2. wait until the targeted rebuilt row is atomically persisted;
  3. run AI against that refreshed row only.

Progress is checkpointed, so a service restart resumes the current pass
instead of repeatedly starving later events. A per-symbol failure is recorded
and never stops subsequent stages. This component has no trading capability.
"""
from __future__ import annotations

import logging
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import DATA_DIR

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 60
MIN_INTERVAL_SECONDS = 30          # floor: the local LLM call alone takes seconds
STARTUP_DELAY_SECONDS = 45         # let the agent warm its caches first
HISTORY_LIMIT = 50
DEFAULT_REFRESH_WAIT_SECONDS = 180
MIN_REFRESH_WAIT_SECONDS = 30
MAX_REFRESH_WAIT_SECONDS = 300
DEFAULT_CHECKPOINT_PATH = DATA_DIR / "pre_earnings_continuous.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _calendar_day(value: Any) -> Any:
    """Return the displayed event day without shifting it across time zones."""
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def _watchlist_symbols(agent: Any) -> set:
    """Owner watchlist as the agent sees it; empty when unavailable."""
    getter = getattr(agent, "_get_watchlist_symbols", None)
    if not callable(getter):
        return set()
    try:
        return {
            str(symbol or "").upper().strip()
            for symbol in (getter() or [])
            if str(symbol or "").strip()
        }
    except Exception:
        logger.debug("continuous scanner watchlist read failed", exc_info=True)
        return set()


def _eligible_symbols(agent: Any) -> List[str]:
    """Active, scored 30-day candidates: watchlist first, then nearest event first."""
    try:
        state = getattr(agent, "_state", {}) or {}
        watchlist = _watchlist_symbols(agent)
        items = state.get("items") or []
        universe = state.get("calendar_universe") or {}
        start_day = _calendar_day(universe.get("window_start"))
        end_day = _calendar_day(universe.get("window_end"))
        if start_day is None:
            start_day = _calendar_day(state.get("as_of_utc"))
        if start_day is not None and end_day is None:
            lookahead_days = max(1, int(state.get("lookahead_days") or 30))
            end_day = start_day + timedelta(days=lookahead_days)
        rows: List[tuple] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper().strip()
            if not symbol:
                continue
            status = str(item.get("event_status") or "UPCOMING").upper().strip()
            flags = item.get("flags") if isinstance(item.get("flags"), dict) else {}
            if status not in {"", "UPCOMING", "DATE_CONFLICT"} or flags.get("already_reported"):
                continue
            event_dt = _parse_iso(item.get("earnings_time_utc"))
            if start_day is not None and end_day is not None:
                event_day = _calendar_day(item.get("earnings_time_utc"))
                if event_day is None or not (start_day <= event_day <= end_day):
                    continue
            rows.append((
                0 if (symbol in watchlist or item.get("is_watchlist")) else 1,
                event_dt or datetime.max.replace(tzinfo=timezone.utc),
                symbol,
            ))
        rows.sort()
        return list(dict.fromkeys(row[2] for row in rows))
    except Exception:
        logger.exception("continuous pre-earnings universe read failed")
        return []


def _default_checkpoint_path() -> Path:
    override = os.environ.get("MS_PRE_EARNINGS_CONTINUOUS_PATH", "").strip()
    return Path(override) if override else DEFAULT_CHECKPOINT_PATH


class ContinuousPreEarningsScanner:
    """Daemon thread: one ticker per interval, data refresh + AI verdict."""

    def __init__(self, *, interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
                 startup_delay: int = STARTUP_DELAY_SECONDS,
                 refresh_wait_seconds: int = DEFAULT_REFRESH_WAIT_SECONDS,
                 checkpoint_path: Optional[Path] = None):
        self.interval_seconds = max(MIN_INTERVAL_SECONDS, int(interval_seconds or DEFAULT_INTERVAL_SECONDS))
        self.startup_delay = max(0, int(startup_delay))
        self.refresh_wait_seconds = max(
            MIN_REFRESH_WAIT_SECONDS,
            min(MAX_REFRESH_WAIT_SECONDS, int(refresh_wait_seconds or DEFAULT_REFRESH_WAIT_SECONDS)),
        )
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path is not None else None
        self._stop = threading.Event()
        self._scan_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._status_lock = threading.Lock()
        checkpoint = self._load_checkpoint()
        self._completed_symbols = {
            str(symbol or "").upper().strip()
            for symbol in (checkpoint.get("completed_symbols") or [])
            if str(symbol or "").strip()
        }
        self._completed_cycles = max(0, int(checkpoint.get("completed_cycles") or 0))
        self._status: Dict[str, Any] = {
            "running": False,
            "enabled": False,
            "interval_seconds": self.interval_seconds,
            "refresh_wait_seconds": self.refresh_wait_seconds,
            "scanned": 0,
            "ai_verdicts": 0,
            "last_symbol": None,
            "last_run": None,
            "last_error": "",
            "stage": "idle",
            "current_symbol": None,
            "current_event_utc": None,
            "cycle_total": 0,
            "cycle_processed": len(self._completed_symbols),
            "cycle_remaining": 0,
            "completed_cycles": self._completed_cycles,
            "next_symbol": None,
            "recent": [],  # bounded history of per-symbol outcomes
        }

    def _load_checkpoint(self) -> Dict[str, Any]:
        if self.checkpoint_path is None:
            return {}
        try:
            payload = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_checkpoint(self) -> None:
        if self.checkpoint_path is None:
            return
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "completed_symbols": sorted(self._completed_symbols),
            "completed_cycles": self._completed_cycles,
            "updated_at": _utc_now_iso(),
        }
        fd, tmp = tempfile.mkstemp(dir=str(self.checkpoint_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.checkpoint_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ------------------------------------------------------------------ control

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="pre-earnings-continuous-scanner", daemon=True,
        )
        self._thread.start()
        with self._status_lock:
            self._status["running"] = True
            self._status["enabled"] = True
        logger.info("Continuous Pre-Earnings scanner started (interval=%ss)", self.interval_seconds)
        return True

    def stop(self) -> bool:
        was = self._status.get("running")
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None
        with self._status_lock:
            self._status["running"] = False
            self._status["enabled"] = False
        return bool(was)

    def status(self) -> Dict[str, Any]:
        with self._status_lock:
            return dict(self._status)

    # -------------------------------------------------------------------- loop

    def _loop(self) -> None:
        self._stop.wait(self.startup_delay)
        while not self._stop.is_set():
            try:
                self.scan_next()
            except Exception:  # noqa: BLE001 — the scanner must survive failures
                logger.exception("continuous pre-earnings scan crashed (scanner continues)")
            if self._stop.wait(self.interval_seconds):
                break

    def _record(self, symbol: str, outcome: Dict[str, Any], symbols: List[str]) -> None:
        active = set(symbols)
        self._completed_symbols.intersection_update(active)
        if symbol in active:
            self._completed_symbols.add(symbol)
        processed = len(self._completed_symbols)
        cycle_complete = bool(symbols) and processed >= len(symbols)
        if cycle_complete:
            self._completed_cycles += 1
            next_symbol = symbols[0] if symbols else None
            checkpoint_completed: set[str] = set()
        else:
            next_symbol = next((candidate for candidate in symbols if candidate not in self._completed_symbols), None)
            checkpoint_completed = set(self._completed_symbols)
        with self._status_lock:
            self._status["scanned"] += 1
            if outcome.get("ai") == "ok":
                self._status["ai_verdicts"] += 1
            self._status["last_symbol"] = symbol
            self._status["last_run"] = _utc_now_iso()
            self._status["last_error"] = "" if outcome.get("ok") else str(outcome.get("error") or "")[:200]
            self._status["stage"] = "idle"
            self._status["current_symbol"] = None
            self._status["current_event_utc"] = None
            self._status["cycle_total"] = len(symbols)
            self._status["cycle_processed"] = processed
            self._status["cycle_remaining"] = max(0, len(symbols) - processed)
            self._status["completed_cycles"] = self._completed_cycles
            self._status["next_symbol"] = next_symbol
            self._status["recent"] = (
                [{"symbol": symbol, **{k: v for k, v in outcome.items() if k != "symbol"}}]
                + self._status["recent"]
            )[:HISTORY_LIMIT]
        self._completed_symbols = checkpoint_completed
        self._save_checkpoint()

    def _set_stage(self, stage: str, *, symbol: str, event_utc: Any = None) -> None:
        with self._status_lock:
            self._status["stage"] = stage
            self._status["current_symbol"] = symbol
            self._status["current_event_utc"] = event_utc

    def _wait_for_data_refresh(self, agent: Any, symbol: str,
                               started_at: datetime) -> Dict[str, Any]:
        """Wait until targeted rescore publishes the refreshed row."""
        if not hasattr(agent, "get_refresh_status"):
            return {"ok": True, "status": "synchronous"}
        deadline = time.monotonic() + self.refresh_wait_seconds
        started_at = started_at.replace(microsecond=0)
        last_exc = ""
        while time.monotonic() < deadline and not self._stop.is_set():
            try:
                refresh = agent.get_refresh_status() or {}
                status = refresh.get("status") if isinstance(refresh.get("status"), dict) else refresh
                completed_symbol = str(status.get("last_targeted_rescore_symbol") or "").upper().strip()
                completed_at = _parse_iso(status.get("last_targeted_rescore_time"))
                if completed_symbol == symbol and completed_at is not None and completed_at >= started_at:
                    snapshot = (
                        agent.get_symbol_analysis_snapshot(symbol)
                        if hasattr(agent, "get_symbol_analysis_snapshot")
                        else {"status": "ok", "symbol": symbol}
                    ) or {}
                    if str(snapshot.get("status") or "ok").lower() != "ok":
                        return {
                            "ok": False,
                            "status": "snapshot_failed",
                            "error": f"data_snapshot:{snapshot.get('status') or 'unknown'}",
                        }
                    return {"ok": True, "status": "fresh", "completed_at": completed_at.isoformat(),
                            "snapshot": snapshot}
            except Exception as exc:
                last_exc = f":{type(exc).__name__}"
                logger.debug("continuous scanner refresh polling failed for %s", symbol, exc_info=True)
            self._stop.wait(0.25)
        return {"ok": False, "status": "timeout",
                "error": f"data_refresh_timeout_after_{self.refresh_wait_seconds}s{last_exc}"}

    def scan_next(self) -> Dict[str, Any]:
        """One stage: nearest unprocessed symbol → fresh data → fresh AI."""
        if not self._scan_lock.acquire(blocking=False):
            with self._status_lock:
                current_symbol = self._status.get("current_symbol")
                stage = self._status.get("stage")
            return {
                "ok": False,
                "error": "scan_already_running",
                "symbol": current_symbol,
                "stage": stage,
                "data": "in_progress",
                "ai": "in_progress",
            }
        try:
            return self._scan_next_locked()
        finally:
            self._scan_lock.release()

    def _scan_next_locked(self) -> Dict[str, Any]:
        """Run one mutually-exclusive stage."""
        from agents.pre_earnings_agent import get_pre_earnings_agent

        agent = get_pre_earnings_agent()
        if hasattr(agent, "start"):
            try:
                agent.start()
            except Exception:
                logger.debug("continuous scanner could not start agent thread", exc_info=True)
        symbols = _eligible_symbols(agent)
        if not symbols:
            outcome = {"ok": False, "error": "no_pre_earnings_universe", "data": "skipped", "ai": "skipped"}
            self._record("-", outcome, [])
            return outcome
        self._completed_symbols.intersection_update(set(symbols))
        if len(self._completed_symbols) >= len(symbols):
            self._completed_symbols.clear()
        symbol = next(candidate for candidate in symbols if candidate not in self._completed_symbols)
        state_items = (getattr(agent, "_state", {}) or {}).get("items") or []
        current_item = next(
            (item for item in state_items if isinstance(item, dict)
             and str(item.get("symbol") or "").upper().strip() == symbol),
            {},
        )
        event_utc = current_item.get("earnings_time_utc")
        outcome: Dict[str, Any] = {"symbol": symbol, "ok": True, "data": "skipped", "ai": "skipped"}
        refresh_started = datetime.now(timezone.utc)
        self._set_stage("refreshing_data", symbol=symbol, event_utc=event_utc)
        try:
            if hasattr(agent, "refresh_symbol_analysis"):
                result = agent.refresh_symbol_analysis(symbol) or {}
                request_status = str(result.get("status") or "ok")
                coverage = result.get("coverage") if isinstance(result.get("coverage"), dict) else {}
                coverage_status = str(coverage.get("status") or "").lower()
                if request_status != "ok":
                    outcome["data"] = request_status
                    outcome["ok"] = False
                    outcome["error"] = f"refresh_symbol_analysis:{request_status}"
                elif coverage_status in {"accepted", "queued", "already_running"}:
                    waited = self._wait_for_data_refresh(agent, symbol, refresh_started)
                    outcome["data"] = str(waited.get("status") or "unknown")
                    if waited.get("ok"):
                        snapshot = waited.get("snapshot") if isinstance(waited.get("snapshot"), dict) else {}
                        outcome["data_verified_at"] = snapshot.get("data_verified_at") or waited.get("completed_at")
                        outcome["data_sources"] = snapshot.get("enrichment_value_sources") or {}
                    else:
                        outcome["ok"] = False
                        outcome["error"] = str(waited.get("error") or "data_refresh_failed")
                else:
                    outcome["data"] = "ok"
        except Exception as exc:  # noqa: BLE001
            outcome["data"] = "error"
            outcome["ok"] = False
            outcome["error"] = f"refresh:{type(exc).__name__}"
            logger.exception("continuous scanner data refresh failed for %s", symbol)
        try:
            if outcome.get("data") not in {"ok", "fresh", "synchronous"}:
                outcome["ai"] = "skipped_stale_data"
                raise RuntimeError("skip_ai_until_data_is_fresh")
            self._set_stage("running_ai", symbol=symbol, event_utc=event_utc)
            if hasattr(agent, "force_ai_for_symbol"):
                result = agent.force_ai_for_symbol(symbol) or {}
                outcome["ai"] = str(result.get("status") or "skipped")
                if outcome["ai"] == "ok":
                    verdict = (result.get("ai_decision") or {})
                    outcome["verdict"] = verdict.get("verdict")
                    outcome["confidence"] = verdict.get("confidence")
                    outcome["ai_generated_at"] = verdict.get("generated_at")
                    data_at = _parse_iso(outcome.get("data_verified_at"))
                    ai_at = _parse_iso(outcome.get("ai_generated_at"))
                    outcome["fresh_sequence_verified"] = bool(
                        data_at is None or (ai_at is not None and ai_at >= data_at)
                    )
                    if not outcome["fresh_sequence_verified"]:
                        outcome["ai"] = "stale_sequence"
                        outcome["ok"] = False
                        outcome["error"] = "ai_generated_before_data_refresh"
                elif outcome["ai"] in {"ai_unavailable", "ai_failed"}:
                    # Non-fatal for the loop: the AI may come back next round.
                    if outcome["ok"]:
                        outcome["error"] = f"force_ai:{outcome['ai']}"
        except Exception as exc:  # noqa: BLE001
            if str(exc) != "skip_ai_until_data_is_fresh":
                outcome["ai"] = "error"
                outcome["error"] = f"ai:{type(exc).__name__}"
                logger.exception("continuous scanner AI verdict failed for %s", symbol)
        self._record(symbol, outcome, symbols)
        logger.info("Continuous Pre-Earnings scan %s -> data=%s ai=%s", symbol, outcome.get("data"), outcome.get("ai"))
        return outcome


_SCANNER: Optional[ContinuousPreEarningsScanner] = None
_SCANNER_LOCK = threading.Lock()


def continuous_scan_config(config: Any) -> Dict[str, Any]:
    root = getattr(config, "config", config) if config is not None else {}
    section = ((root or {}).get("pre_earnings") or {}).get("continuous_scan") if isinstance(root, dict) else None
    if not isinstance(section, dict):
        section = {}
    return {
        "enabled": bool(section.get("enabled", False)),
        "interval_seconds": int(section.get("interval_seconds", DEFAULT_INTERVAL_SECONDS) or DEFAULT_INTERVAL_SECONDS),
        "refresh_wait_seconds": max(
            MIN_REFRESH_WAIT_SECONDS,
            min(MAX_REFRESH_WAIT_SECONDS, int(
                section.get("refresh_wait_seconds", DEFAULT_REFRESH_WAIT_SECONDS)
                or DEFAULT_REFRESH_WAIT_SECONDS
            )),
        ),
    }


def get_continuous_scanner(config: Any = None) -> ContinuousPreEarningsScanner:
    global _SCANNER
    with _SCANNER_LOCK:
        if _SCANNER is None:
            cfg = continuous_scan_config(config)
            _SCANNER = ContinuousPreEarningsScanner(
                interval_seconds=cfg["interval_seconds"],
                refresh_wait_seconds=cfg["refresh_wait_seconds"],
                checkpoint_path=_default_checkpoint_path(),
            )
        return _SCANNER


def shutdown_continuous_scanner() -> bool:
    global _SCANNER
    with _SCANNER_LOCK:
        if _SCANNER is not None and _SCANNER.status().get("running"):
            _SCANNER.stop()
            _SCANNER = None
            return True
        _SCANNER = None
        return False


__all__ = [
    "ContinuousPreEarningsScanner", "get_continuous_scanner",
    "continuous_scan_config", "shutdown_continuous_scanner",
    "DEFAULT_INTERVAL_SECONDS", "MIN_INTERVAL_SECONDS",
    "DEFAULT_REFRESH_WAIT_SECONDS", "MIN_REFRESH_WAIT_SECONDS",
]
