
import concurrent.futures
import copy
import hashlib
import json
import logging
import os
import re
import threading
import time
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agents.economic_calendar_agent import get_economic_calendar_agent
from agents.news_tab_agent import get_news_tab_agent_if_ready
from core.agent_toggles import is_agent_enabled
from core.config import DATA_DIR, HOTLIST_CACHE_PATH, STATE_PATH, ConfigManager
from core.agent_metrics import track_agent_run
from core.ai.role_resolution import call_timeout_for
from core.ollama_client import ollama
from core.observability import update_agent_status
from core.request_context import request_context
from core.market_hours import (
    EASTERN_TZ,
    earnings_session_reported,
    us_eastern_date,
    us_equity_market_status,
    us_equity_session_snapshot,
)

logger = logging.getLogger("PremarketScannerAgent")

PREMARKET_OUTPUT_PATH = DATA_DIR / "premarket.json"

POSITIVE_NEWS_TERMS = (
    "beats",
    "raises guidance",
    "upgrade",
    "acquisition",
    "partnership",
    "approval",
    "record",
)

NEGATIVE_NEWS_TERMS = (
    "misses",
    "cuts guidance",
    "downgrade",
    "investigation",
    "sec",
    "offering",
    "dilution",
    "lawsuit",
)

# Word-boundary regexes (audit #7): the old substring check (`term in txt`)
# fired "sec" inside "second"/"section"/"access", "record" inside "recorded",
# etc. `\b...\b` matches the term as a whole word only — plurals/variants are
# intentionally not matched (a crude lexicon; the false-positive fix matters
# more than catching "approvals"/"upgrades").
POSITIVE_NEWS_RE = tuple(re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE) for t in POSITIVE_NEWS_TERMS)
NEGATIVE_NEWS_RE = tuple(re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE) for t in NEGATIVE_NEWS_TERMS)

CATALYST_PATTERNS = {
    "EARNINGS": re.compile(r"\b(earnings|eps|revenue|guidance|quarter|results|beat|miss)\b", re.IGNORECASE),
    "OFFERING": re.compile(r"\b(offering|dilution|secondary)\b", re.IGNORECASE),
    "FDA": re.compile(r"\b(fda|approval|trial|phase\s?[1234])\b", re.IGNORECASE),
    "LAWSUIT": re.compile(r"\b(lawsuit|investigation|sec|probe|subpoena)\b", re.IGNORECASE),
    "M&A": re.compile(r"\b(acquisition|acquire|merger|buyout|deal)\b", re.IGNORECASE),
    "GUIDANCE": re.compile(r"\b(guidance|outlook|raises guidance|cuts guidance)\b", re.IGNORECASE),
    "MACRO": re.compile(r"\b(fed|cpi|ppi|inflation|rates?|gdp|payroll)\b", re.IGNORECASE),
    "UPGRADE": re.compile(r"\b(upgrade|initiates buy|overweight)\b", re.IGNORECASE),
    "DOWNGRADE": re.compile(r"\b(downgrade|underweight|sell rating)\b", re.IGNORECASE),
}

COMMODITY_EXCLUDED_SYMBOLS = {
    "GC=F", "SI=F",
    "XAUUSD", "XAGUSD",
    "XAU-USD", "XAG-USD",
    "XAUUSD=X", "XAGUSD=X",
    "GLD", "SLV", "IAU", "SIVR",
}


# The LLM answers "none"/"unknown" when it finds no catalyst. That is absence
# of information, not a catalyst tag: storing it produced chips like "NONE"
# next to real tags and fed "cat[NONE]" back into the aggregate pulse prompt.
PLACEHOLDER_CATALYSTS = {
    "", "NONE", "NULL", "N/A", "NA", "UNKNOWN", "UNSPECIFIED", "UNIDENTIFIED",
    "NONE_IDENTIFIED", "UNIDENTIFIED_GAP", "NO_CATALYST", "NOT_AVAILABLE",
}

# What the candidate prices actually are, by US session (see _describe_data_session).
DATA_KIND_PREMARKET = "PRE_MARKET_LIVE"      # real 04:00-09:30 ET pre-market quotes
DATA_KIND_AFTER_HOURS = "AFTER_HOURS_LIVE"   # 16:00-20:00 ET after-hours quotes
DATA_KIND_LAST_SESSION = "LAST_SESSION"      # market closed: last session's final prints
DATA_KIND_REGULAR = "REGULAR_SESSION"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _safe_float(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        if value is None:
            return default
        if isinstance(value, str):
            text = value.strip().replace("%", "").replace(",", "")
            if not text:
                return default
            return float(text)
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _pos_num(value: Any) -> Optional[float]:
    v = _safe_float(value, None)
    return v if v is not None and v > 0 else None


def _pick_metric(data: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        v = _pos_num(data.get(key))
        if v is not None:
            return v
    return None


def _normalize_mover_volume(mover: Any) -> Any:
    if not isinstance(mover, dict):
        return mover
    m = dict(mover)
    # Movers cache entries may nest quote/volume metrics one level down
    # (e.g. {"quote": {...}}); flatten so the volume plumbing sees them.
    flat = dict(m)
    for v in m.values():
        if isinstance(v, dict):
            for k, val in v.items():
                flat.setdefault(k, val)
    avg = _pick_metric(flat, "avg_volume", "average_volume", "avgVolume", "averageVolume", "averageDailyVolume", "average_daily_volume", "averageDailyVolume10Day", "averageDailyVolume3Month")
    # Only explicitly labelled extended-session volume is valid here.
    # regularMarketVolume/volume usually describe the completed regular
    # session and previously produced convincing but false pre-market ratios.
    pm = _pick_metric(flat, "premarket_volume", "premarketVolume", "premarket_vol", "pre_market_volume", "preMarketVolume", "extended_hours_volume", "extendedHoursVolume")
    rel = _safe_float(flat.get("premarket_vol_rel"), None)
    if rel is None and pm is not None and avg:
        rel = pm / avg
    if pm is not None:
        m["premarket_volume"] = pm
    if avg is not None:
        m["avg_volume"] = avg
    if rel is not None:
        m["premarket_vol_rel"] = round(float(rel), 4)
    return m


def score_gap_pct(gap_pct: float) -> int:
    gap_abs = abs(_safe_float(gap_pct, 0.0) or 0.0)
    if gap_abs >= 10:
        return 3
    if gap_abs >= 6:
        return 2
    if gap_abs >= 4:
        return 1
    return 0


def score_earnings_surprise(eps_surprise_pct: Optional[float], rev_surprise_pct: Optional[float]) -> int:
    score = 0

    eps = _safe_float(eps_surprise_pct, None)
    if eps is not None:
        if eps >= 20:
            score += 3
        elif eps >= 10:
            score += 2
        elif eps >= 0:
            score += 1
        elif eps <= -20:
            score -= 3
        elif eps <= -10:
            score -= 2
        else:
            score -= 1

    rev = _safe_float(rev_surprise_pct, None)
    if rev is not None:
        if rev >= 10:
            score += 2
        elif rev >= 0:
            score += 1
        elif rev <= -10:
            score -= 2
        else:
            score -= 1

    return int(score)


def score_news_lexicon(text_blob: str) -> int:
    txt = str(text_blob or "")
    score = 0
    for rx in POSITIVE_NEWS_RE:
        if rx.search(txt):
            score += 1
    for rx in NEGATIVE_NEWS_RE:
        if rx.search(txt):
            score -= 1
    return int(max(-3, min(3, score)))


def classify_direction(gap_pct: float, score_long: int, score_short: int) -> str:
    if score_long >= 4 and score_long > score_short:
        return "LONG"
    if score_short >= 4 and score_short > score_long:
        return "SHORT"
    return "WATCH"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _score_liquidity(avg_volume: Optional[float]) -> float:
    vol = _safe_float(avg_volume, None)
    if vol is None:
        return 45.0
    if vol >= 20_000_000:
        return 95.0
    if vol >= 10_000_000:
        return 86.0
    if vol >= 3_000_000:
        return 72.0
    if vol >= 1_000_000:
        return 58.0
    if vol >= 300_000:
        return 42.0
    return 26.0


def _score_volume_activity(premarket_vol_rel: Optional[float]) -> float:
    rel = _safe_float(premarket_vol_rel, None)
    if rel is None:
        return 45.0
    # Cap at 4x relative volume to avoid extreme spikes dominating.
    return float(_clamp(25.0 + (_clamp(rel, 0.0, 4.0) * 18.0), 0.0, 100.0))


def _score_catalyst_strength(
    earnings_score: int,
    news_score: int,
    catalysts: List[str],
    has_earnings: bool,
) -> float:
    score = 40.0
    score += float(_clamp(float(earnings_score) * 8.0, -28.0, 28.0))
    score += float(_clamp(float(news_score) * 9.0, -24.0, 24.0))
    score += float(min(24.0, len(catalysts) * 4.0))
    if has_earnings:
        score += 6.0
    return float(_clamp(score, 0.0, 100.0))


def _derive_premarket_direction(
    premarket_score_long: float,
    premarket_score_short: float,
    legacy_direction: str,
) -> str:
    diff = float(premarket_score_long) - float(premarket_score_short)
    if premarket_score_long >= 58.0 and diff >= 8.0:
        return "LONG"
    if premarket_score_short >= 58.0 and diff <= -8.0:
        return "SHORT"
    fallback = str(legacy_direction or "").upper()
    if fallback == "LONG" and diff >= 4.0:
        return "LONG"
    if fallback == "SHORT" and diff <= -4.0:
        return "SHORT"
    return "WATCH"

class PremarketScannerAgent:
    DEFAULT_CONFIG: Dict[str, Any] = {
        "refresh_seconds": 300,
        "startup_delay_seconds": 8,
        "external_call_budget": 24,
        "lookback_hours": 24,
        "min_gap_pct": 4.0,
        "max_results": 30,
        "timeout_seconds": 8,
        "cache_ttl_seconds": 300,
        "use_ollama": True,
        "ollama_max_tickers": 10,
        "ollama_timeout_seconds": 6,
        "ollama_workers": 4,
        # A catalyst answer is kept this long while the ticker's headlines, earnings
        # surprise, session and gap (in whole percent) are unchanged. 0 = every cycle.
        "ollama_reuse_minutes": 30,
        "aftermarket_max_symbols": 0,
        "aftermarket_workers": 6,
        "aftermarket_cache_ttl_seconds": 60,
        "news_refresh_batch_size": 50,
        "news_refresh_cooldown_seconds": 120,
        # Hard max-age for serving earnings as "current": if the earnings cache is
        # older than this, the payload surfaces a `earnings_stale` flag instead of
        # silently serving months-old data as current. Default 1 day.
        "earnings_max_age_seconds": 86400,
        # Premium Pre-Market (Task #14): drop tickers whose earnings release has
        # already fired (spent catalyst), surfaced in `reported_today`.
        "eliminate_reported": True,
        # Aggregate "Pre-Market Pulse" AI — one cached LLM call over the top
        # movers (overall sentiment + top long/short setups + risks). Cloud-only.
        "pulse_enabled": True,
        "pulse_ttl_seconds": 300,
        "pulse_timeout_seconds": 12,
        # While the US market is fully CLOSED (weekend / holiday / overnight)
        # nothing trades, so the candidate list is a last-session recap that
        # cannot change. Rebuild it (movers + news + earnings + AI calls) only
        # this often instead of every `refresh_seconds` — the old loop spent
        # ~288 full cycles of provider/LLM budget per closed day re-stamping
        # Friday's prices as "updated now".
        "closed_session_refresh_seconds": 21600,
    }

    ENV_MAP = {
        "PREMARKET_REFRESH_SECONDS": ("refresh_seconds", int),
        "PREMARKET_STARTUP_DELAY_SECONDS": ("startup_delay_seconds", int),
        "PREMARKET_EXTERNAL_CALL_BUDGET": ("external_call_budget", int),
        "PREMARKET_LOOKBACK_HOURS": ("lookback_hours", int),
        "PREMARKET_MIN_GAP_PCT": ("min_gap_pct", float),
        "PREMARKET_MAX_RESULTS": ("max_results", int),
        "PREMARKET_TIMEOUT_SECONDS": ("timeout_seconds", int),
        "PREMARKET_CACHE_TTL_SECONDS": ("cache_ttl_seconds", int),
        "PREMARKET_USE_OLLAMA": ("use_ollama", lambda v: str(v).strip().lower() in {"1", "true", "yes", "on"}),
        "PREMARKET_OLLAMA_MAX_TICKERS": ("ollama_max_tickers", int),
        "PREMARKET_OLLAMA_TIMEOUT_SECONDS": ("ollama_timeout_seconds", int),
        "PREMARKET_OLLAMA_WORKERS": ("ollama_workers", int),
        "PREMARKET_AFTERMARKET_MAX_SYMBOLS": ("aftermarket_max_symbols", int),
        "PREMARKET_AFTERMARKET_WORKERS": ("aftermarket_workers", int),
        "PREMARKET_AFTERMARKET_CACHE_TTL_SECONDS": ("aftermarket_cache_ttl_seconds", int),
        "PREMARKET_NEWS_REFRESH_BATCH_SIZE": ("news_refresh_batch_size", int),
        "PREMARKET_NEWS_REFRESH_COOLDOWN_SECONDS": ("news_refresh_cooldown_seconds", int),
        "PREMARKET_ELIMINATE_REPORTED": ("eliminate_reported", lambda v: str(v).strip().lower() in {"1", "true", "yes", "on"}),
        "PREMARKET_PULSE_ENABLED": ("pulse_enabled", lambda v: str(v).strip().lower() in {"1", "true", "yes", "on"}),
        "PREMARKET_PULSE_TTL_SECONDS": ("pulse_ttl_seconds", int),
        "PREMARKET_PULSE_TIMEOUT_SECONDS": ("pulse_timeout_seconds", int),
        "PREMARKET_CLOSED_SESSION_REFRESH_SECONDS": ("closed_session_refresh_seconds", int),
    }

    def __init__(self, config_manager: Optional[ConfigManager] = None):
        self.config_manager = config_manager or ConfigManager()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pending_refresh = False
        self._state = self._load_state()
        self._state_read_fallback = copy.deepcopy(self._state)
        self._news_refresh_lock = threading.Lock()
        self._news_refresh_inflight = False
        self._last_news_refresh_ts = 0.0
        self._cycle_inflight = False
        self._cycle_started = 0.0
        self._cycle_token = None
        self._force_full_cycle = False
        self._last_restart_attempt = 0.0

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="PremarketScannerAgent")
            self._thread.start()
            logger.info("PremarketScannerAgent started")

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3.0)
        self._persist_aftermarket_cache_if_needed(force=True)
        logger.info("PremarketScannerAgent stopped")

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def get_runtime_status(self) -> Dict[str, Any]:
        with self._lock:
            status = dict(self._state.get("status", {}) or {})
            running = bool(self._thread and self._thread.is_alive())
        return {
            "running": running,
            "last_run": status.get("last_run_time"),
            "last_success": status.get("last_success_time"),
            "last_error": status.get("last_error"),
            "next_run_in": status.get("next_run_in"),
        }

    def request_refresh(self, force: bool = True) -> Dict[str, Any]:
        """Wake the loop. `force=True` (a user pressing Refresh) rebuilds the
        payload even while the market is closed and the last-session recap is
        still current; `force=False` (endpoint self-heal) only wakes the loop
        and leaves the closed-session budget guard in charge."""
        if not self._is_enabled():
            return {"status": "disabled"}
        with self._lock:
            self._pending_refresh = True
            if force:
                self._force_full_cycle = True
        if force:
            # A manual refresh must not re-serve a stale pulse over the freshly
            # rebuilt candidate list — drop the cached pulse so the next cycle
            # recomputes it from the new book.
            self.invalidate_pulse_cache()
        self._wake_event.set()
        return {"status": "accepted"}

    def run_once(self) -> Dict[str, Any]:
        with self._lock:
            self._force_full_cycle = True
        return self.run_cycle()

    def _cycle_stuck_locked(self, limit: float) -> bool:
        """Caller must hold self._lock; True when the in-flight cycle exceeded limit seconds.

        A cycle whose start time was never recorded is not judged stuck.
        """
        started = getattr(self, "_cycle_started", None)
        if not getattr(self, "_cycle_inflight", False) or not started:
            return False
        return (time.monotonic() - started) > limit

    def _stuck_limit(self) -> float:
        return float(max(900, 3 * _safe_int(self._get_config().get("refresh_seconds", 300), 300)))

    def _ensure_loop_alive(self) -> None:
        """Lock-guarded, throttled restart of a dead scan loop thread."""
        if self._stop_event.is_set() or not self._is_enabled():
            return
        limit = self._stuck_limit()
        if not self._lock.acquire(timeout=0.15):
            return
        try:
            if self._thread is None or (self._thread.is_alive() and not self._cycle_stuck_locked(limit)):
                return
            now = time.monotonic()
            if now - self._last_restart_attempt < 60.0:
                return
            self._last_restart_attempt = now
            self._thread = None
        finally:
            self._lock.release()
        logger.warning("PremarketScannerAgent loop thread died or cycle hung while enabled; restarting")
        self.start()

    def _kick_if_stale(self, payload: Any, cfg: Dict[str, Any]) -> None:
        """Wake an idle-but-alive loop whose payload exceeds 3x refresh_seconds; never raises."""
        try:
            if self._stop_event.is_set() or not self._is_enabled() or self._cycle_inflight or not isinstance(payload, dict):
                return
            status = payload.get("status") or {}
            status = status if isinstance(status, dict) else {}
            now = _utc_now()
            refresh = max(30, _safe_int(cfg.get("refresh_seconds", 300), 300))
            nxt = _parse_iso(status.get("next_refresh_at_utc"))
            if nxt is not None and now < nxt <= now + timedelta(seconds=refresh):
                return
            last = _parse_iso(payload.get("as_of_utc")) or _parse_iso(status.get("last_run_time"))
            if last is None or (now - last).total_seconds() > 3 * refresh:
                self._pending_refresh = True
                self._wake_event.set()
        except Exception as exc:
            logger.debug("premarket stale-payload watchdog skipped: %s", exc)

    def get_state(self) -> Dict[str, Any]:
        acquired = self._lock.acquire(timeout=0.15)
        if acquired:
            try:
                payload = copy.deepcopy(self._state)
                self._state_read_fallback = copy.deepcopy(payload)
            finally:
                self._lock.release()
        else:
            payload = copy.deepcopy(self._state_read_fallback)
        cfg = self._get_config()
        self._ensure_loop_alive()
        self._kick_if_stale(payload, cfg)
        if not isinstance(payload, dict):
            payload = {}
        payload = self._sanitize_state_payload(payload, cfg)
        payload["refresh_seconds"] = int(cfg["refresh_seconds"])
        market_status_error = None
        try:
            market_open = self._is_us_market_open()
        except Exception as exc:
            market_status_error = f"{type(exc).__name__}: {exc}"
            logger.warning("premarket market-status lookup failed; using last known state: %s", market_status_error)
            market_open = bool((payload.get("flags") or {}).get("market_open"))
        payload = self._apply_market_open_state(payload, market_open)
        status = payload.get("status", {}) if isinstance(payload, dict) else {}
        if not isinstance(status, dict):
            status = {}
        status["market_state"] = "OPEN" if market_open else "CLOSED"
        if market_status_error:
            status["market_status_error"] = market_status_error
        # Clock-dependent session fields are recomputed at read time; the
        # data_* fields (what the candidate prices are) stay as the cycle
        # that built the candidates recorded them.
        stored_session = payload.get("session")
        if isinstance(stored_session, dict):
            try:
                payload["session"] = self._refresh_session_clock(stored_session, self._session_info())
            except Exception as exc:
                logger.warning("premarket session clock refresh failed: %s", exc)
                status["session_clock_error"] = f"{type(exc).__name__}: {exc}"
        last_run = _parse_iso(status.get("last_run_time"))
        next_refresh_at = _parse_iso(status.get("next_refresh_at_utc"))
        if self._is_enabled() and next_refresh_at:
            # Real countdown to the next data rebuild (closed sessions idle
            # for hours, so last_run + refresh_seconds would be a fiction).
            status["next_run_in"] = max(0, int((next_refresh_at - _utc_now()).total_seconds()))
        elif self._is_enabled() and last_run:
            elapsed = (_utc_now() - last_run).total_seconds()
            status["next_run_in"] = max(0, int(cfg["refresh_seconds"] - elapsed))
        elif self._is_enabled():
            status["next_run_in"] = int(cfg["refresh_seconds"])
        elif not self._is_enabled():
            status["next_run_in"] = None
            status["state"] = "PAUSED"
            status["reason"] = "agent_disabled"
        if market_open:
            status["state"] = "PAUSED"
            status["reason"] = "market_open_paused"
        elif status.get("state") in {None, "", "PAUSED"}:
            if status.get("last_error"):
                status["state"] = "ERROR"
                status["reason"] = "cycle_error"
            else:
                status["state"] = "OK"
                status["reason"] = None
        payload["status"] = status
        payload["as_of_utc"] = payload.get("as_of_utc") or status.get("last_success_time") or status.get("last_run_time")
        payload["disabled"] = (not self._is_enabled()) or bool(market_open)
        return self._sanitize_state_payload(payload, cfg)

    def run_cycle(self) -> Dict[str, Any]:
        """Singleflight wrapper (audit #11): only one cycle may run at a time.
        A second entry while one is in flight returns the current state instead
        of building a competing payload that would race on self._state
        (last-writer-wins non-determinism). The loop thread is the only steady
        caller, but run_once()/tests/refresh can overlap — this guard makes
        that safe."""
        limit = self._stuck_limit()
        with self._lock:
            cur = threading.current_thread()
            if cur.name == "PremarketScannerAgent" and self._thread is not None and cur is not self._thread:
                raise SystemExit("superseded premarket loop thread")
            if getattr(self, "_cycle_inflight", False) and not self._cycle_stuck_locked(limit):
                logger.debug("PremarketScannerAgent cycle skipped: already in flight.")
                return copy.deepcopy(self._state)
            self._cycle_inflight = True
            self._cycle_started = time.monotonic()
            self._cycle_token = token = object()
        try:
            return self._run_cycle_impl()
        except Exception as exc:
            # Never let a cycle exception escape: the loop thread would die and
            # as_of_utc would freeze at the last good payload while the agent
            # still reports enabled. Record the error and keep scanning.
            logger.exception("PremarketScannerAgent cycle crashed; loop stays alive: %s", exc)
            with self._lock:
                status = dict(self._state.get("status", {}) or {})
                status["last_error"] = str(exc)
                status["state"] = "ERROR"
                status["reason"] = "cycle_error"
                self._state["status"] = status
                return copy.deepcopy(self._state)
        finally:
            with self._lock:
                if self._cycle_token is token:
                    self._cycle_inflight = False

    def _run_cycle_impl(self) -> Dict[str, Any]:
        started_perf = time.perf_counter()
        self.config_manager.reload_if_changed()
        cfg = self._get_config()
        now = _utc_now()
        now_iso = _iso(now)

        with self._lock:
            status = dict(self._state.get("status", {}) or {})
            status["last_run_time"] = now_iso
            self._state["status"] = status
            forced = bool(getattr(self, "_force_full_cycle", False))
            self._force_full_cycle = False

        if not self._is_enabled():
            with self._lock:
                self._state["disabled"] = True
                self._state["refresh_seconds"] = int(cfg["refresh_seconds"])
                self._state.setdefault("flags", {})["refreshing"] = False
                self._state["status"]["state"] = "PAUSED"
                self._state["status"]["reason"] = "agent_disabled"
                self._state["status"]["next_run_in"] = None
                self._persist_locked()
                duration_ms = (time.perf_counter() - started_perf) * 1000.0
                update_agent_status(
                    "premarket_scanner",
                    enabled=False,
                    last_run=now_iso,
                    next_run_in=None,
                    avg_duration_ms=round(duration_ms, 2),
                )
                return copy.deepcopy(self._state)

        if self._is_us_market_open():
            with self._lock:
                status = dict(self._state.get("status", {}) or {})
                status["last_run_time"] = now_iso
                status["last_success_time"] = now_iso
                status["last_error"] = None
                status["next_run_in"] = int(cfg["refresh_seconds"])
                status["market_state"] = "OPEN"
                status["state"] = "PAUSED"
                status["reason"] = "market_open_paused"
                flags = dict(self._state.get("flags", {}) or {})
                flags["market_open"] = True
                flags["scope"] = "US_ONLY"
                flags["refreshing"] = False
                self._state["flags"] = flags
                snapshot_candidates = self._state.get("snapshot_candidates")
                if not isinstance(snapshot_candidates, list) or not snapshot_candidates:
                    snapshot_candidates = list(self._state.get("candidates", []) or [])
                self._state["snapshot_candidates"] = snapshot_candidates
                # The frozen snapshot keeps the time it was BUILT. Re-stamping
                # as_of_utc on every paused tick made a 09:25 pre-market
                # snapshot read "as of 15:40" all session long.
                if not snapshot_candidates or not self._state.get("as_of_utc"):
                    self._state["as_of_utc"] = now_iso
                open_session = self._session_info(now)
                prev_session = self._state.get("session")
                if open_session or isinstance(prev_session, dict):
                    self._state["session"] = self._refresh_session_clock(
                        prev_session if isinstance(prev_session, dict) else {}, open_session
                    )
                # Scanning resumes once the regular session has closed.
                try:
                    resume_at = us_equity_market_status(now).close_time_et
                    if resume_at > now:
                        status["next_refresh_at_utc"] = _iso(resume_at)
                        status["next_run_in"] = int((resume_at - now).total_seconds())
                    else:
                        status["next_refresh_at_utc"] = None
                except Exception:
                    status["next_refresh_at_utc"] = None
                self._state["candidates"] = []
                self._state["long_candidates"] = []
                self._state["short_candidates"] = []
                self._state["watch_only"] = []
                # Clear the pulse too: a pulse whose top_long/top_short reference
                # symbols that are no longer in the (emptied) candidate list is
                # structurally inconsistent — never serve it during market hours.
                self._state["pulse"] = None
                self.invalidate_pulse_cache()
                self._state["status"] = status
                self._state["disabled"] = True
                self._state["refresh_seconds"] = int(cfg["refresh_seconds"])
                self._persist_locked()
                duration_ms = (time.perf_counter() - started_perf) * 1000.0
                update_agent_status(
                    "premarket_scanner",
                    enabled=True,
                    last_run=now_iso,
                    last_success=now_iso,
                    last_error="market_open_paused",
                    avg_duration_ms=round(duration_ms, 2),
                    next_run_in=int(cfg["refresh_seconds"]),
                )
                return copy.deepcopy(self._state)

        # Session awareness: outside 04:00-09:30 ET there is no pre-market.
        # The payload says which session the prices belong to, and while the
        # market is fully closed the (unchangeable) last-session recap is not
        # rebuilt on every tick.
        session = self._session_info(now)
        if (not forced) and self._closed_session_recap_is_current(session, cfg, now):
            return self._idle_closed_cycle(session, cfg, now, started_perf)

        # Source failures are non-fatal, but they must stay visible: a cycle
        # that ran on empty movers/news/earnings is DEGRADED, not OK.
        source_errors: List[str] = []
        try:
            try:
                movers, movers_age = self._load_movers(cfg)
            except Exception as exc:
                logger.warning("Premarket movers fetch failed; continuing with empty movers: %s", exc)
                source_errors.append(f"movers: {exc}")
                movers, movers_age = [], None
            movers = [_normalize_mover_volume(m) for m in movers]
            watchlist_symbols = self._get_watchlist_symbols()
            symbols = [str(m.get("symbol") or "").upper() for m in movers if str(m.get("symbol") or "").strip()]
            symbols = list(dict.fromkeys([s for s in symbols if s]))

            try:
                news_items, news_age, news_available = self._load_news_items(cfg, symbols, session=session)
            except Exception as exc:
                logger.warning("Premarket news fetch failed; continuing without news: %s", exc)
                source_errors.append(f"news: {exc}")
                news_items, news_age, news_available = [], None, False
            try:
                earnings_items, earnings_age, earnings_available = self._load_earnings(cfg, symbols)
            except Exception as exc:
                logger.warning("Premarket earnings fetch failed; continuing without earnings: %s", exc)
                source_errors.append(f"earnings: {exc}")
                earnings_items, earnings_age, earnings_available = [], None, False

            payload = self._build_payload(
                cfg=cfg,
                movers=movers,
                news_items=news_items,
                earnings_items=earnings_items,
                movers_age=movers_age,
                news_age=news_age,
                earnings_age=earnings_age,
                news_available=news_available,
                earnings_available=earnings_available,
                watchlist_symbols=watchlist_symbols,
                session=session,
            )
            payload["flags"]["market_open"] = False
            payload["flags"]["scope"] = "US_ONLY"
            payload["flags"]["refreshing"] = False
            payload["status"]["market_state"] = "CLOSED"
            payload["status"]["state"] = "DEGRADED" if source_errors else "OK"
            payload["status"]["reason"] = "source_error" if source_errors else None
            payload["as_of_utc"] = now_iso
            payload["snapshot_candidates"] = list(payload.get("candidates", []) or [])

            data_session = payload.get("session") if isinstance(payload.get("session"), dict) else None
            ollama_used = self._apply_ollama(payload.get("candidates", []), cfg, session=data_session)
            payload["flags"]["ollama_used"] = bool(ollama_used)
            post_ollama_groups = self._build_direction_groups(payload.get("candidates", []))
            payload["long_candidates"] = post_ollama_groups["LONG"]
            payload["short_candidates"] = post_ollama_groups["SHORT"]
            payload["watch_only"] = post_ollama_groups["WATCH"]

            # Aggregate Pre-Market Pulse AI (Task #14 — B2): one cached cloud
            # LLM call over the session's top candidates. Non-fatal; None when
            # AI is unavailable / disabled / local. Computed after per-ticker
            # Ollama so the pulse can lean on the enriched view of the book.
            pulse = self.get_premarket_pulse(
                cfg, payload.get("candidates", []) or [], session=data_session
            )
            payload["pulse"] = pulse
            payload["flags"]["pulse_used"] = pulse is not None

            source_error_text = "; ".join(source_errors) or None
            next_refresh_at = self._next_refresh_at(session, cfg, now, now)
            next_run_in = int(max(0.0, (next_refresh_at - now).total_seconds()))
            with self._lock:
                self._state = payload
                self._state["status"]["last_success_time"] = now_iso
                self._state["status"]["last_full_cycle_time"] = now_iso
                self._state["status"]["last_error"] = source_error_text
                self._state["status"]["next_run_in"] = next_run_in
                self._state["status"]["next_refresh_at_utc"] = _iso(next_refresh_at)
                self._persist_locked()
                duration_ms = (time.perf_counter() - started_perf) * 1000.0
                update_agent_status(
                    "premarket_scanner",
                    enabled=True,
                    last_run=now_iso,
                    last_success=now_iso,
                    last_error=source_error_text,
                    avg_duration_ms=round(duration_ms, 2),
                    next_run_in=next_run_in,
                )
                return copy.deepcopy(self._state)
        except Exception as e:
            logger.error(f"PremarketScannerAgent cycle error: {e}")
            with self._lock:
                status = dict(self._state.get("status", {}) or {})
                status["last_run_time"] = now_iso
                status["last_error"] = str(e)
                status["next_run_in"] = int(cfg["refresh_seconds"])
                status["next_refresh_at_utc"] = _iso(now + timedelta(seconds=int(cfg["refresh_seconds"])))
                status["state"] = "ERROR"
                status["reason"] = "cycle_error"
                self._state["status"] = status
                self._state.setdefault("flags", {})["refreshing"] = False
                self._state["disabled"] = False
                self._persist_locked()
                duration_ms = (time.perf_counter() - started_perf) * 1000.0
                update_agent_status(
                    "premarket_scanner",
                    enabled=True,
                    last_run=now_iso,
                    last_error=str(e),
                    avg_duration_ms=round(duration_ms, 2),
                    next_run_in=int(cfg["refresh_seconds"]),
                )
                return copy.deepcopy(self._state)

    def _loop(self) -> None:
        startup_cfg = self._get_config()
        startup_delay = max(0, int(startup_cfg.get("startup_delay_seconds", 8)))
        if startup_delay > 0:
            self._wake_event.wait(timeout=startup_delay)
            self._wake_event.clear()
        while not self._stop_event.is_set():
            started = time.time()
            self.config_manager.reload_if_changed()
            cfg = self._get_config()

            if self._is_enabled():
                with self._lock:
                    self._pending_refresh = False
                with request_context(reason="AGENT_REFRESH", path="/agent/premarket", cache_only=False):
                    with track_agent_run(
                        "premarket_scanner",
                        external_call_budget=int(cfg["external_call_budget"]),
                    ):
                        self.run_cycle()
            else:
                with self._lock:
                    status = dict(self._state.get("status", {}) or {})
                    status["next_run_in"] = None
                    status["state"] = "PAUSED"
                    status["reason"] = "agent_disabled"
                    self._state["status"] = status
                    self._state.setdefault("flags", {})["refreshing"] = False
                    self._state["disabled"] = True

            interval = max(15, int(cfg["refresh_seconds"]))
            elapsed = time.time() - started
            wait_sec = max(1.0, interval - elapsed)
            # A closed-session recap or the 04:00 ET pre-market open can fall
            # between two ticks: wake for it instead of up to one interval late.
            try:
                with self._lock:
                    due = _parse_iso((self._state.get("status") or {}).get("next_refresh_at_utc"))
                if due is not None:
                    until_due = (due - _utc_now()).total_seconds()
                    if 0.0 < until_due < wait_sec:
                        wait_sec = max(1.0, until_due + 1.0)
            except Exception:
                pass
            self._wake_event.wait(timeout=wait_sec)
            self._wake_event.clear()

    # ------------------------------------------------------------------
    # Session awareness
    # ------------------------------------------------------------------
    @staticmethod
    def _session_info(now_utc: Optional[datetime] = None) -> Dict[str, Any]:
        """US-equity session snapshot (core.market_hours). {} if unavailable."""
        try:
            return dict(us_equity_session_snapshot(now_utc or _utc_now()))
        except Exception as exc:
            logger.warning("Premarket session snapshot unavailable: %s", exc)
            return {}

    @staticmethod
    def _data_kind_for(session: Optional[Dict[str, Any]]) -> Optional[str]:
        state = str((session or {}).get("state") or "").upper()
        if state == "PRE_MARKET":
            return DATA_KIND_PREMARKET
        if state == "AFTER_HOURS":
            return DATA_KIND_AFTER_HOURS
        if state == "CLOSED":
            return DATA_KIND_LAST_SESSION
        if state == "REGULAR":
            return DATA_KIND_REGULAR
        return None

    @classmethod
    def _describe_data_session(
        cls,
        session: Optional[Dict[str, Any]],
        movers_age: Optional[int],
        now: datetime,
    ) -> Optional[Dict[str, Any]]:
        """Session block for the payload: the clock snapshot plus what the
        candidate prices ARE (data_kind), which trading day they belong to and
        how old they really are.

        The movers feed is re-fetched every few minutes even when nothing has
        traded for two days, so "fetched 40s ago" is not "quoted 40s ago":
        while the market is closed the quote time is capped at the end of the
        last extended session."""
        if not isinstance(session, dict) or not session:
            return None
        out = dict(session)
        kind = cls._data_kind_for(session)
        fetched_at = None
        if movers_age is not None:
            try:
                fetched_at = now - timedelta(seconds=max(0, int(movers_age)))
            except Exception:
                fetched_at = None
        quotes_as_of = fetched_at
        if kind == DATA_KIND_LAST_SESSION:
            last_trade = _parse_iso(session.get("last_extended_close_utc"))
            if last_trade is not None and (quotes_as_of is None or last_trade < quotes_as_of):
                quotes_as_of = last_trade
        session_date = session.get("quote_session_date")
        if kind == DATA_KIND_PREMARKET:
            data_label = "Live pre-market quotes vs previous close"
        elif kind == DATA_KIND_AFTER_HOURS:
            data_label = "After-hours quotes vs today's regular close (not a pre-market gap)"
        elif kind == DATA_KIND_LAST_SESSION:
            data_label = (
                f"Market closed: last prints of the {session_date} session "
                "(not a live pre-market gap)"
            )
        else:
            data_label = "Regular-session quotes"
        out["data_kind"] = kind
        out["data_session_date"] = session_date
        out["data_label"] = data_label
        out["movers_fetched_at_utc"] = _iso(fetched_at)
        out["quotes_as_of_utc"] = _iso(quotes_as_of)
        out["quotes_age_seconds"] = (
            int(max(0.0, (now - quotes_as_of).total_seconds())) if quotes_as_of is not None else None
        )
        return out

    @staticmethod
    def _refresh_session_clock(stored: Dict[str, Any], fresh: Dict[str, Any]) -> Dict[str, Any]:
        """Overlay a fresh clock snapshot on a stored session block, keeping
        the data_* fields recorded when the candidates were built."""
        out = dict(stored or {})
        if isinstance(fresh, dict):
            out.update(fresh)
        quotes_as_of = _parse_iso(out.get("quotes_as_of_utc"))
        if quotes_as_of is not None:
            out["quotes_age_seconds"] = int(max(0.0, (_utc_now() - quotes_as_of).total_seconds()))
        return out

    @staticmethod
    def _next_refresh_at(
        session: Optional[Dict[str, Any]],
        cfg: Dict[str, Any],
        now: datetime,
        last_full_cycle: Optional[datetime],
    ) -> datetime:
        """When the candidate data will next be rebuilt."""
        refresh = int(cfg.get("refresh_seconds", 300) or 300)
        if str((session or {}).get("state") or "").upper() != "CLOSED":
            return now + timedelta(seconds=refresh)
        closed_refresh = int(cfg.get("closed_session_refresh_seconds", 21600) or 21600)
        due = (last_full_cycle or now) + timedelta(seconds=closed_refresh)
        premarket_open = _parse_iso((session or {}).get("next_premarket_open_utc"))
        if premarket_open is not None and now < premarket_open < due:
            due = premarket_open
        if due <= now:
            due = now + timedelta(seconds=refresh)
        return due

    def _closed_session_recap_is_current(
        self,
        session: Optional[Dict[str, Any]],
        cfg: Dict[str, Any],
        now: datetime,
    ) -> bool:
        """True when the market is fully closed and the stored payload already
        is the recap of that same last session, built recently enough."""
        if str((session or {}).get("state") or "").upper() != "CLOSED":
            return False
        with self._lock:
            state = self._state if isinstance(self._state, dict) else {}
            prev = state.get("session")
            status = dict(state.get("status") or {})
            has_candidates = bool(state.get("candidates"))
        if not isinstance(prev, dict):
            return False
        if prev.get("data_kind") != DATA_KIND_LAST_SESSION:
            return False
        if prev.get("data_session_date") != (session or {}).get("quote_session_date"):
            return False
        last_full = _parse_iso(status.get("last_full_cycle_time"))
        if last_full is None:
            return False
        healthy = has_candidates and str(status.get("state") or "").upper() == "OK"
        if healthy:
            max_age = int(cfg.get("closed_session_refresh_seconds", 21600) or 21600)
        else:
            # Empty / degraded recap: retry, but not on every tick.
            max_age = max(900, 3 * int(cfg.get("refresh_seconds", 300) or 300))
        return (now - last_full).total_seconds() < max_age

    def _idle_closed_cycle(
        self,
        session: Dict[str, Any],
        cfg: Dict[str, Any],
        now: datetime,
        started_perf: float,
    ) -> Dict[str, Any]:
        """Market closed and the last-session recap is current: spend nothing.
        No movers/news/earnings fetch, no LLM call, candidates and as_of_utc
        untouched; only the clock fields and the real next-refresh time move."""
        now_iso = _iso(now)
        with self._lock:
            status = dict(self._state.get("status", {}) or {})
            last_full = _parse_iso(status.get("last_full_cycle_time"))
            next_refresh_at = self._next_refresh_at(session, cfg, now, last_full)
            next_run_in = int(max(0.0, (next_refresh_at - now).total_seconds()))
            status["last_run_time"] = now_iso
            status["market_state"] = "CLOSED"
            status["next_run_in"] = next_run_in
            status["next_refresh_at_utc"] = _iso(next_refresh_at)
            self._state["status"] = status
            prev_session = self._state.get("session")
            self._state["session"] = self._refresh_session_clock(
                prev_session if isinstance(prev_session, dict) else {}, session
            )
            flags = dict(self._state.get("flags", {}) or {})
            flags["market_open"] = False
            flags["scope"] = "US_ONLY"
            flags["refreshing"] = False
            flags["idle_market_closed"] = True
            self._state["flags"] = flags
            self._state["disabled"] = False
            self._state["refresh_seconds"] = int(cfg["refresh_seconds"])
            recap_candidates = list(self._state.get("candidates") or [])
            recap_session = dict(self._state.get("session") or {})
            pulse_missing = bool(cfg.get("pulse_enabled", True)) and recap_candidates and not self._state.get("pulse")
        if pulse_missing and (time.monotonic() - float(getattr(self, "_last_pulse_retry_ts", 0.0) or 0.0)) >= 900.0:
            # The recap was built while the AI was unavailable: retry the one
            # aggregate call (not the whole cycle) at most every 15 minutes.
            self._last_pulse_retry_ts = time.monotonic()
            try:
                retried = self.get_premarket_pulse(cfg, recap_candidates, session=recap_session)
            except Exception:
                retried = None
            if retried is not None:
                with self._lock:
                    self._state["pulse"] = retried
                    self._state.setdefault("flags", {})["pulse_used"] = True
        with self._lock:
            self._persist_locked()
            duration_ms = (time.perf_counter() - started_perf) * 1000.0
            update_agent_status(
                "premarket_scanner",
                enabled=True,
                last_run=now_iso,
                last_error=status.get("last_error"),
                avg_duration_ms=round(duration_ms, 2),
                next_run_in=next_run_in,
            )
            return copy.deepcopy(self._state)

    def _load_movers(self, cfg: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        cache_ttl = int(cfg["cache_ttl_seconds"])
        now_ts = time.time()
        movers, age = self._scanner_cached_movers(now_ts=now_ts)

        if not movers:
            file_movers, file_age = self._hotlist_cached_movers(now_ts=now_ts)
            if file_movers:
                movers, age = file_movers, file_age

        stale = (age is None) or (age > cache_ttl)
        if (not movers or stale) and self._is_enabled():
            refreshed, refresh_age = self._refresh_movers_from_scanner(now_ts=now_ts)
            if refreshed:
                movers, age = refreshed, refresh_age

        filtered = [m for m in movers if not self._is_excluded_symbol(str(m.get("symbol") or ""))]
        watchlist_symbols = self._get_watchlist_symbols()
        if watchlist_symbols:
            filtered.extend(self._watchlist_cached_movers(watchlist_symbols))

        deduped = self._deduplicate_movers(filtered)
        return deduped, age



    def _scanner_cached_movers(self, now_ts: Optional[float] = None) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        now_ts = now_ts if now_ts is not None else time.time()
        try:
            from agents.scanner_agent import get_scanner_agent

            scanner = get_scanner_agent(self.config_manager)
            # Scanner normally runs inside MarketEngine while the pre-market
            # agent runs inside the API process.  Refresh the API-side scanner
            # facade from the atomically published snapshot before reading it;
            # otherwise this process keeps its empty startup cache forever.
            load_shared_snapshot = getattr(scanner, "_load_shared_snapshot_if_newer", None)
            if callable(load_shared_snapshot):
                load_shared_snapshot()
            lock = getattr(scanner, "_state_lock", None)
            if lock is not None:
                with lock:
                    raw = list(getattr(scanner, "cached_hot_list", []) or [])
                    raw.extend(list(getattr(scanner, "cached_watchlist_stock_list", []) or []))
                    last_scan = _safe_float(getattr(scanner, "last_scan_time", 0), 0.0) or 0.0
            else:
                raw = list(getattr(scanner, "cached_hot_list", []) or [])
                raw.extend(list(getattr(scanner, "cached_watchlist_stock_list", []) or []))
                last_scan = _safe_float(getattr(scanner, "last_scan_time", 0), 0.0) or 0.0
            movers = [self._normalize_mover(item) for item in raw if isinstance(item, dict)]
            movers = [m for m in movers if m]
            movers = self._deduplicate_movers(movers)
            age = int(max(0.0, now_ts - last_scan)) if last_scan > 0 else None
            return movers, age
        except Exception:
            return [], None

    def _refresh_movers_from_scanner(self, now_ts: Optional[float] = None) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        now_ts = now_ts if now_ts is not None else time.time()
        try:
            from agents.scanner_agent import get_scanner_agent

            scanner = get_scanner_agent(self.config_manager)
            # run_scan() makes HTTP calls that can take minutes.
            # Do NOT hold _state_lock during the scan - it blocks the scanner API.
            # run_scan() internally acquires the lock briefly to update cached data.
            scanner.run_scan()
            return self._scanner_cached_movers(now_ts=now_ts)
        except Exception as e:
            logger.warning(f"Premarket movers refresh skipped: {e}")
            return [], None

    def _hotlist_cached_movers(self, now_ts: Optional[float] = None) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        now_ts = now_ts if now_ts is not None else time.time()
        try:
            if not HOTLIST_CACHE_PATH.exists():
                return [], None
            with HOTLIST_CACHE_PATH.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            ts = _safe_float(payload.get("ts"), 0.0) or 0.0
            rows = payload.get("data", []) if isinstance(payload, dict) else []
            movers = []
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                normalized = self._normalize_mover(
                    {
                        "symbol": row.get("ticker") or row.get("symbol"),
                        "change_pct": row.get("change_pct"),
                        "price": row.get("price"),
                        "source": "hotlist_cache",
                    }
                )
                if normalized:
                    movers.append(normalized)
            age = int(max(0.0, now_ts - ts)) if ts > 0 else None
            return movers, age
        except Exception:
            return [], None

    def _get_watchlist_symbols(self) -> List[str]:
        symbols: List[str] = []
        try:
            watchlist = self.config_manager.watchlist or {}
            if isinstance(watchlist, dict):
                for key in ("order", "yahoo", "stocks", "binance", "crypto", "cryptos"):
                    symbols.extend(watchlist.get(key, []) or [])
        except Exception:
            pass

        out: List[str] = []
        seen = set()
        for raw in symbols:
            sym = str(raw or "").upper().strip()
            if not sym or sym in seen:
                continue
            if self._is_excluded_symbol(sym):
                continue
            seen.add(sym)
            out.append(sym)
        return out

    def _watchlist_cached_movers(self, symbols: List[str]) -> List[Dict[str, Any]]:
        if not symbols:
            return []
        try:
            if not STATE_PATH.exists():
                return []
            with STATE_PATH.open("r", encoding="utf-8") as f:
                state_payload = json.load(f) or {}
        except Exception:
            return []

        tickers_block = state_payload.get("tickers", {})
        rows_by_symbol: Dict[str, Dict[str, Any]] = {}
        if isinstance(tickers_block, dict):
            for sym, row in tickers_block.items():
                key = str(sym or "").upper().strip()
                if key and isinstance(row, dict):
                    rows_by_symbol[key] = row
        elif isinstance(tickers_block, list):
            for row in tickers_block:
                if not isinstance(row, dict):
                    continue
                key = str(row.get("ticker") or row.get("symbol") or "").upper().strip()
                if key:
                    rows_by_symbol[key] = row

        out: List[Dict[str, Any]] = []
        for symbol in symbols:
            row = rows_by_symbol.get(symbol, {})
            normalized = self._normalize_mover(
                {
                    "symbol": symbol,
                    "gap_pct": row.get("gap_pct"),
                    "change_pct": row.get("change_pct"),
                    "premarket_price": row.get("premarket_price"),
                    "price": row.get("price"),
                    "prev_close": row.get("prev_close", row.get("previous_close", row.get("previousClose"))),
                    "premarket_volume": row.get("premarket_volume", row.get("preMarketVolume")),
                    "avg_volume": row.get("avg_volume", row.get("avgVolume", row.get("averageVolume"))),
                    "source": "watchlist_state",
                }
            )
            if normalized:
                out.append(normalized)
        return out

    def _load_news_items(
        self,
        cfg: Dict[str, Any],
        symbols: List[str],
        session: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[int], bool]:
        cache_ttl = int(cfg["cache_ttl_seconds"])
        lookback_hours = max(1, int(cfg["lookback_hours"]))
        cutoff = self._news_cutoff(_utc_now(), lookback_hours, session)
        agent = get_news_tab_agent_if_ready()
        if agent is None:
            return [], None, False

        def _read_cached_news() -> Tuple[List[Dict[str, Any]], Optional[int]]:
            try:
                state = agent.get_news_tab_state(active_tickers=symbols[:80] if symbols else None) or {}
                last_update = _parse_iso(state.get("last_update"))
                age_local = int(max(0.0, (_utc_now() - last_update).total_seconds())) if last_update else None
            except Exception:
                age_local = None

            try:
                rows = agent.get_all_articles_flat(active_tickers=symbols[:80] if symbols else None) or []
            except Exception:
                rows = []

            filtered: List[Dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                published = _parse_iso(row.get("published_at"))
                if published is None or published < cutoff:
                    continue
                filtered.append(row)
            return filtered, age_local

        out, age = _read_cached_news()
        stale = (age is None) or (age > cache_ttl)
        coverage = self._build_news_index(out, symbols)
        missing_symbols = [s for s in symbols if not coverage.get(s)]
        refresh_batch_size = max(1, min(100, _safe_int(cfg.get("news_refresh_batch_size"), 50)))
        refresh_cooldown = max(15, _safe_int(cfg.get("news_refresh_cooldown_seconds"), 120))
        now_ts = time.time()
        recent_refresh = (now_ts - self._last_news_refresh_ts) < refresh_cooldown
        should_refresh = (not recent_refresh) and ((not out) or stale or bool(missing_symbols))

        # New rule: if cache lookup yields no/insufficient ticker news, trigger one fetch pass
        # through the existing NewsTabAgent and persist there (no duplicate fetch logic here).
        if self._is_enabled() and should_refresh:
            refresh_symbols = (missing_symbols or symbols)[:refresh_batch_size]
            if not refresh_symbols:
                return out, age, bool(out)
            timeout_sec = max(1, _safe_int(cfg.get("timeout_seconds"), 8))
            refresh_errors: List[str] = []
            done = threading.Event()
            with self._news_refresh_lock:
                if self._news_refresh_inflight:
                    return out, age, bool(out)
                self._news_refresh_inflight = True

            def _refresh_worker() -> None:
                try:
                    refreshed_any = False
                    ensure_cached = getattr(agent, "ensure_ticker_news_cached", None)
                    if callable(ensure_cached):
                        for symbol in refresh_symbols:
                            try:
                                result = ensure_cached(symbol, force_refresh=False, allow_remote=True) or {}
                            except TypeError:
                                result = ensure_cached(symbol) or {}
                            status = str(result.get("status") or "").lower()
                            fetched = bool(result.get("fetched", False))
                            if status in {"refreshed", "ok"} and fetched:
                                refreshed_any = True
                    else:
                        try:
                            agent.refresh_all_news(refresh_symbols, include_market=False)
                        except TypeError:
                            agent.refresh_all_news(refresh_symbols)
                        refreshed_any = True
                    if refreshed_any:
                        self._last_news_refresh_ts = time.time()
                except Exception as ex:
                    refresh_errors.append(str(ex))
                finally:
                    with self._news_refresh_lock:
                        self._news_refresh_inflight = False
                    done.set()

            inherited_context = copy_context()
            t = threading.Thread(
                target=lambda: inherited_context.run(_refresh_worker),
                daemon=True,
                name="PremarketNewsRefresh",
            )
            t.start()
            finished = done.wait(timeout=float(timeout_sec))
            if not finished:
                logger.warning(
                    f"Premarket news refresh timed out after {timeout_sec}s; using cached news for now."
                )
            elif refresh_errors:
                logger.warning(f"Premarket news refresh skipped: {refresh_errors[0]}")
            out, age = _read_cached_news()

        return out, age, bool(out)

    @staticmethod
    def _news_cutoff(
        now: datetime,
        lookback_hours: int,
        session: Optional[Dict[str, Any]] = None,
    ) -> datetime:
        """Oldest headline that can still explain the move on screen.

        `lookback_hours` (24h) is measured from the wall clock, but the move
        is measured from the last regular close. Across a weekend or holiday
        the two drift apart: on a Sunday morning, or in Monday's pre-market,
        "last 24h" excludes everything published around Friday's close, i.e.
        the very headlines that caused the move, and the ticker was shown (and
        described by the AI) as moving for no reason. The window therefore
        never starts later than the morning of the last regular session."""
        cutoff = now - timedelta(hours=max(1, int(lookback_hours)))
        last_close = _parse_iso((session or {}).get("last_regular_close_utc")) if isinstance(session, dict) else None
        if last_close is not None:
            session_start = last_close - timedelta(hours=8)
            if session_start < cutoff:
                cutoff = session_start
        return cutoff

    def _load_earnings(
        self,
        cfg: Dict[str, Any],
        symbols: List[str],
    ) -> Tuple[List[Dict[str, Any]], Optional[int], bool]:
        cache_ttl = int(cfg["cache_ttl_seconds"])
        calendar = get_economic_calendar_agent(self.config_manager.config)
        today = _utc_now().date()
        start = datetime.combine(today - timedelta(days=1), datetime.min.time())
        end = datetime.combine(today + timedelta(days=1), datetime.max.time())

        cached = calendar.get_calendar_events_range(
            start,
            end,
            watchlist_tickers=symbols[:120] if symbols else None,
            allow_remote=False,
        ) or {}
        earnings = [e for e in (cached.get("earnings", []) or []) if isinstance(e, dict)]
        age = self._earnings_cache_age_seconds(calendar, symbols)
        stale = (age is None) or (age > cache_ttl)

        if (not earnings or stale) and self._is_enabled():
            try:
                refreshed = calendar.get_calendar_events_range(
                    start,
                    end,
                    watchlist_tickers=symbols[:120] if symbols else None,
                    allow_remote=True,
                ) or {}
                earnings = [e for e in (refreshed.get("earnings", []) or []) if isinstance(e, dict)]
                age = self._earnings_cache_age_seconds(calendar, symbols)
            except Exception as e:
                logger.warning(f"Premarket earnings refresh skipped: {e}")

        return earnings, age, bool(earnings)

    def _earnings_cache_age_seconds(self, calendar: Any, symbols: List[str]) -> Optional[int]:
        now_ts = time.time()
        try:
            lock = getattr(calendar, "_cache_lock", None)
            if lock is not None:
                with lock:
                    events_cache = getattr(calendar, "_events_cache", {}) or {}
                    meta = dict(events_cache.get("earnings_meta", {}) or {})
                    cache_path = Path(getattr(calendar, "_cache_path", "calendar_events_cache.json"))
            else:
                events_cache = getattr(calendar, "_events_cache", {}) or {}
                meta = dict(events_cache.get("earnings_meta", {}) or {})
                cache_path = Path(getattr(calendar, "_cache_path", "calendar_events_cache.json"))

            vals = []
            for sym in symbols:
                ts = _safe_float(meta.get(sym.upper()), None)
                if ts:
                    vals.append(ts)
            if vals:
                return int(max(0.0, now_ts - max(vals)))
            if cache_path.exists():
                return int(max(0.0, now_ts - cache_path.stat().st_mtime))
        except Exception:
            pass
        return None

    def _build_payload(
        self,
        cfg: Dict[str, Any],
        movers: List[Dict[str, Any]],
        news_items: List[Dict[str, Any]],
        earnings_items: List[Dict[str, Any]],
        movers_age: Optional[int],
        news_age: Optional[int],
        earnings_age: Optional[int],
        news_available: bool,
        earnings_available: bool,
        watchlist_symbols: Optional[List[str]] = None,
        session: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # What these prices are (live pre-market / after-hours / last session)
        # and how old they really are. None when the caller gave no session.
        session_block = self._describe_data_session(session, movers_age, _utc_now())
        price_session = (session_block or {}).get("data_kind")
        quote_time_utc = (session_block or {}).get("quotes_as_of_utc")
        news_index = self._build_news_index(news_items, [m.get("symbol") for m in movers])
        earnings_index = self._build_earnings_index(earnings_items)
        min_gap = _safe_float(cfg.get("min_gap_pct"), 4.0) or 4.0
        max_results = max(1, _safe_int(cfg.get("max_results"), 30))
        watch_set = {
            str(s or "").upper().strip()
            for s in (watchlist_symbols or [])
            if str(s or "").strip() and not self._is_excluded_symbol(str(s or ""))
        }

        inputs_age = {
            "news_cache_age_seconds": news_age,
            "earnings_cache_age_seconds": earnings_age,
            "movers_cache_age_seconds": movers_age,
        }
        age_values = [int(v) for v in inputs_age.values() if v is not None]
        max_age = max(age_values) if age_values else None
        freshness = "fresh"
        if max_age is not None and max_age > 600:
            freshness = "stale"
        elif max_age is not None and max_age > 180:
            freshness = "warm"

        # Max-age guard: don't serve stale earnings as current. If the earnings
        # cache age exceeds the hard cap, mark earnings unavailable + stale so
        # the frontend can show "earnings data stale, refresh needed" rather than
        # treating an empty/stale set as "no earnings today". Win-rate-neutral:
        # earnings are a catalyst input; this only surfaces real staleness.
        earnings_max_age = int(cfg.get("earnings_max_age_seconds", 86400) or 86400)
        earnings_stale = (earnings_age is not None) and (int(earnings_age) > earnings_max_age)
        if earnings_stale:
            earnings_available = False
            # Stale earnings (cache > max age) are likely from a prior session
            # and must NOT drive scoring or earnings-elimination: a past-date
            # row from a stale cache would wrongly drop a ticker that already
            # reported last quarter. Drop the index so every ticker sees no
            # earnings (neutral), while the flag still surfaces staleness to UI.
            earnings_index = {}
        # Symmetric news-staleness flag (audit #14): surface when the news cache
        # age exceeds the cache TTL so the frontend can show "news stale" rather
        # than silently serving old headlines. Win-rate-neutral (display-only).
        news_stale = (news_age is not None) and (int(news_age) > int(cfg.get("cache_ttl_seconds", 300)))

        eliminate_reported = bool(cfg.get("eliminate_reported", True))
        reported_today: List[Dict[str, Any]] = []

        candidates: List[Dict[str, Any]] = []
        for mover in movers:
            symbol = str(mover.get("symbol") or "").upper()
            if not symbol:
                continue
            if self._is_excluded_symbol(symbol):
                continue
            gap_pct = _safe_float(mover.get("gap_pct"), 0.0) or 0.0
            is_watch_symbol = symbol in watch_set
            if abs(gap_pct) < min_gap and not is_watch_symbol:
                continue

            ticker_news = list(news_index.get(symbol, []) or [])[:5]
            earnings = dict(earnings_index.get(symbol, {}) or {})

            earnings_enriched = self._normalize_earnings_payload(earnings)
            eps_surprise = _safe_float(earnings_enriched.get("eps_surprise_pct"), None)
            rev_surprise = _safe_float(earnings_enriched.get("revenue_surprise_pct"), None)

            # Premium: drop tickers whose earnings release has already fired — a
            # spent catalyst (the gap is the realized move, not an anticipated
            # one). Surfaced transparently in `reported_today` so the user can
            # see what was filtered and why. Session-aware (BMO/AMC); undated /
            # ambiguous same-day rows are kept. See Task #14 — B1.
            if eliminate_reported and self._earnings_already_reported(earnings_enriched, _utc_now()):
                reported_today.append({
                    "symbol": symbol,
                    "reported_date": earnings_enriched.get("date") or earnings_enriched.get("date_formatted"),
                    "session": (str(earnings_enriched.get("when") or earnings_enriched.get("session") or "").lower() or None),
                    "eps_surprise_pct": earnings_enriched.get("eps_surprise_pct"),
                    "revenue_surprise_pct": earnings_enriched.get("revenue_surprise_pct"),
                    "is_watchlist": is_watch_symbol,
                })
                continue

            gap_score = score_gap_pct(gap_pct)
            earnings_score = score_earnings_surprise(eps_surprise, rev_surprise)
            text_blob = " ".join(
                [
                    str(n.get("title") or "")
                    + " "
                    + str(n.get("summary") or n.get("text") or "")
                    for n in ticker_news
                ]
            )
            news_score = score_news_lexicon(text_blob)

            # Directional scoring:
            # - Gap contributes with sign (up-gap helps LONG, down-gap helps SHORT)
            # - Earnings/news contributions are mirrored for opposite direction
            gap_long = gap_score if gap_pct > 0 else (-gap_score if gap_pct < 0 else 0)
            gap_short = -gap_long
            score_long = int(gap_long + earnings_score + news_score)
            score_short = int(gap_short - earnings_score - news_score)
            catalysts = self._derive_catalysts(ticker_news, earnings_enriched)

            premarket_price = _safe_float(mover.get("premarket_price"), None)
            prev_close = _safe_float(mover.get("prev_close"), None)
            if premarket_price not in (None, 0.0) and prev_close not in (None, 0.0) and prev_close > 0:
                gap_pct = ((premarket_price - prev_close) / prev_close) * 100.0
            elif prev_close in (None, 0.0) and premarket_price not in (None, 0.0):
                denom = 1.0 + (gap_pct / 100.0)
                if abs(denom) > 1e-9:
                    prev_close = premarket_price / denom

            premarket_volume = _safe_float(mover.get("premarket_volume"), None)
            avg_volume = _safe_float(mover.get("avg_volume"), None)
            premarket_vol_rel = None
            if premarket_volume not in (None, 0.0) and avg_volume not in (None, 0.0):
                premarket_vol_rel = float(max(0.0, premarket_volume) / max(1.0, avg_volume))

            catalyst_score = _score_catalyst_strength(
                earnings_score=earnings_score,
                news_score=news_score,
                catalysts=catalysts,
                # `earnings_enriched` always carries the six normalized keys
                # (all None when the ticker has no calendar row), so its truth
                # value gave EVERY candidate the "has an earnings event" bonus.
                # The bonus belongs only to tickers with a real earnings row.
                has_earnings=bool(earnings),
            )
            liquidity_score = _score_liquidity(avg_volume)
            vol_activity_score = _score_volume_activity(premarket_vol_rel)

            gap_long_score = _clamp((max(gap_pct, 0.0) / 12.0) * 100.0, 0.0, 100.0)
            gap_short_score = _clamp((max(-gap_pct, 0.0) / 12.0) * 100.0, 0.0, 100.0)
            premarket_score_long = float(
                _clamp((0.45 * gap_long_score) + (0.30 * catalyst_score) + (0.15 * vol_activity_score) + (0.10 * liquidity_score), 0.0, 100.0)
            )
            premarket_score_short = float(
                _clamp((0.45 * gap_short_score) + (0.30 * catalyst_score) + (0.15 * vol_activity_score) + (0.10 * liquidity_score), 0.0, 100.0)
            )
            legacy_direction = classify_direction(gap_pct, score_long, score_short)
            direction = _derive_premarket_direction(
                premarket_score_long=premarket_score_long,
                premarket_score_short=premarket_score_short,
                legacy_direction=legacy_direction,
            )
            why = (
                f"Gap {gap_pct:+.2f}% | catalyst {int(round(catalyst_score))} | "
                f"vol_rel {premarket_vol_rel:.2f} | liq {int(round(liquidity_score))}"
                if premarket_vol_rel is not None
                else f"Gap {gap_pct:+.2f}% | catalyst {int(round(catalyst_score))} | liq {int(round(liquidity_score))}"
            )

            candidate = {
                "symbol": symbol,
                "direction": direction,
                "gap_pct": round(gap_pct, 4),
                "premarket_price": round(premarket_price, 4) if premarket_price is not None else None,
                "prev_close": round(prev_close, 4) if prev_close is not None else None,
                "premarket_volume": round(premarket_volume, 2) if premarket_volume is not None else None,
                "avg_volume": round(avg_volume, 2) if avg_volume is not None else None,
                "premarket_vol_rel": round(premarket_vol_rel, 4) if premarket_vol_rel is not None else None,
                # Explicit unavailable flags so the frontend can surface
                # "volume data unavailable" instead of a silent null. Win-rate-
                # neutral: scoring already handles None deterministically
                # (_score_liquidity / _score_volume_activity return a neutral 45.0);
                # these flags are display-only.
                "avg_volume_unavailable": avg_volume is None,
                "premarket_volume_unavailable": premarket_volume is None,
                "premarket_vol_rel_unavailable": premarket_vol_rel is None,
                "catalyst_score": int(round(catalyst_score)),
                "liquidity_score": int(round(liquidity_score)),
                "premarket_score_long": int(round(premarket_score_long)),
                "premarket_score_short": int(round(premarket_score_short)),
                "score": int(score_long),
                "score_long": int(score_long),
                "score_short": int(score_short),
                "catalysts": catalysts,
                "ollama": None,
                "earnings": earnings_enriched,
                "news": ticker_news,
                "is_watchlist": is_watch_symbol,
                "why": why,
                "freshness": freshness,
                "inputs_age": dict(inputs_age),
                # Which session `premarket_price` is from and when it was last
                # a live quote (not when the cache was last re-read).
                "price_session": price_session,
                "quote_time_utc": quote_time_utc,
            }
            candidates.append(candidate)

        candidates.sort(
            key=lambda c: (
                1 if bool(c.get("is_watchlist")) else 0,
                max(_safe_int(c.get("premarket_score_long"), 0), _safe_int(c.get("premarket_score_short"), 0)),
                abs(_safe_float(c.get("gap_pct"), 0.0) or 0.0),
                abs((_safe_int(c.get("score_long"), 0) - _safe_int(c.get("score_short"), 0))),
            ),
            reverse=True,
        )
        candidates = self._deduplicate_candidates(candidates)
        if watch_set:
            watch_candidates = [c for c in candidates if bool(c.get("is_watchlist"))]
            non_watch_candidates = [c for c in candidates if not bool(c.get("is_watchlist"))]
            final_limit = min(300, len(watch_candidates) + max_results)
            candidates = (watch_candidates + non_watch_candidates)[:final_limit]
        else:
            candidates = candidates[:max_results]

        groups = self._build_direction_groups(candidates)

        status = {
            "last_run_time": _iso(_utc_now()),
            "last_success_time": None,
            "last_error": None,
            "state": "OK",
            "reason": None,
            "last_inputs_used": {
                "news_cache_age_seconds": news_age,
                "earnings_cache_age_seconds": earnings_age,
                "movers_cache_age_seconds": movers_age,
            },
            "next_run_in": int(cfg["refresh_seconds"]),
        }

        return {
            "as_of_utc": _iso(_utc_now()),
            "refresh_seconds": int(cfg["refresh_seconds"]),
            "inputs_used": dict(inputs_age),
            "flags": {
                "news_available": bool(news_available),
                "news_stale": bool(news_stale),
                "earnings_available": bool(earnings_available),
                "earnings_stale": bool(earnings_stale),
                "ollama_used": False,
                "refreshing": False,
                "reported_today_count": len(reported_today),
                "session_state": (session_block or {}).get("state"),
                "data_kind": price_session,
            },
            "session": session_block,
            "candidates": candidates,
            "long_candidates": groups["LONG"],
            "short_candidates": groups["SHORT"],
            "watch_only": groups["WATCH"],
            "reported_today": reported_today,
            "snapshot_candidates": list(candidates),
            "status": status,
            "disabled": False,
        }

    def _build_news_index(self, news_items: List[Dict[str, Any]], symbols: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        symbols_norm = list(
            dict.fromkeys(
                [
                    str(s or "").upper()
                    for s in symbols
                    if str(s or "").strip() and not self._is_excluded_symbol(str(s or ""))
                ]
            )
        )
        by_symbol: Dict[str, List[Dict[str, Any]]] = {s: [] for s in symbols_norm}
        if not symbols_norm:
            return by_symbol

        for row in news_items:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "").upper().strip()
            if ticker and ticker in by_symbol:
                by_symbol[ticker].append(row)
                continue

            text = f"{row.get('title', '')} {row.get('summary', '')} {row.get('text', '')}".upper()
            for symbol in symbols_norm:
                if self._mentions_symbol(text, symbol):
                    by_symbol[symbol].append(row)

        for symbol, rows in by_symbol.items():
            rows.sort(
                key=lambda r: _parse_iso(r.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            by_symbol[symbol] = rows
        return by_symbol

    @staticmethod
    def _mentions_symbol(text_upper: str, symbol: str) -> bool:
        try:
            return bool(re.search(rf"\b{re.escape(symbol)}\b", text_upper))
        except Exception:
            return False

    def _build_earnings_index(self, earnings_items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for row in earnings_items:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper().strip()
            if not symbol:
                continue
            if self._is_excluded_symbol(symbol):
                continue
            prev = out.get(symbol)
            if prev is None:
                out[symbol] = row
                continue
            prev_dt = _parse_iso(prev.get("date") or prev.get("date_formatted"))
            curr_dt = _parse_iso(row.get("date") or row.get("date_formatted"))
            if curr_dt and (not prev_dt or curr_dt > prev_dt):
                out[symbol] = row
                continue
            if curr_dt and prev_dt and curr_dt == prev_dt:
                # Same event date can appear multiple times in cache with different completeness.
                # Prefer the richer row (actual EPS/revenue/surprises if available).
                if self._earnings_row_quality(row) > self._earnings_row_quality(prev):
                    out[symbol] = row
        return out

    @staticmethod
    def _earnings_row_quality(row: Dict[str, Any]) -> int:
        if not isinstance(row, dict):
            return 0
        score = 0
        if _safe_float(row.get("eps_actual", row.get("epsActual", row.get("eps"))), None) is not None:
            score += 4
        if _safe_float(row.get("revenue_actual", row.get("revenueActual", row.get("revenue"))), None) is not None:
            score += 4
        if _safe_float(row.get("eps_surprise_pct"), None) is not None:
            score += 2
        if _safe_float(row.get("revenue_surprise_pct"), None) is not None:
            score += 2
        if _safe_float(row.get("eps_estimated", row.get("epsEstimated", row.get("eps_estimate"))), None) is not None:
            score += 1
        if _safe_float(
            row.get("revenue_estimated", row.get("revenueEstimated", row.get("estimatedRevenue"))),
            None,
        ) is not None:
            score += 1
        return score

    def _normalize_earnings_payload(self, row: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(row, dict):
            return {}
        payload = dict(row)
        eps_est = _safe_float(
            payload.get("eps_estimated", payload.get("epsEstimated", payload.get("eps_estimate"))),
            None,
        )
        eps_act = _safe_float(
            payload.get("eps_actual", payload.get("eps", payload.get("epsActual"))),
            None,
        )
        rev_est = _safe_float(
            payload.get("revenue_estimated", payload.get("revenueEstimated", payload.get("estimatedRevenue"))),
            None,
        )
        rev_act = _safe_float(
            payload.get("revenue_actual", payload.get("revenue", payload.get("actualRevenue"))),
            None,
        )

        eps_surprise = None
        if eps_est not in (None, 0.0) and eps_act is not None:
            eps_surprise = ((eps_act - eps_est) / abs(eps_est)) * 100.0
        rev_surprise = None
        if rev_est not in (None, 0.0) and rev_act is not None:
            rev_surprise = ((rev_act - rev_est) / abs(rev_est)) * 100.0

        payload["eps_estimated"] = eps_est
        payload["eps_actual"] = eps_act
        payload["revenue_estimated"] = rev_est
        payload["revenue_actual"] = rev_act
        payload["eps_surprise_pct"] = round(eps_surprise, 4) if eps_surprise is not None else None
        payload["revenue_surprise_pct"] = round(rev_surprise, 4) if rev_surprise is not None else None
        return payload

    @staticmethod
    def _earnings_already_reported(row: Dict[str, Any], now_utc: datetime) -> bool:
        """True if the earnings release in `row` has already occurred by `now_utc`.

        Session-aware: FinnHub's `date` is date-only (midnight UTC), so for a
        same-day event we use the BMO/AMC session hint to decide whether the
        print has happened yet. A BMO report is treated as reported once the US
        pre-market window has closed (>= 14:30 UTC — the latest US market open
        across DST, so we never drop an upcoming BMO prematurely); an AMC report
        is reported after the after-hours bulk print (>= 22:00 UTC). Past dates
        are always reported; future dates and undated / malformed rows are kept
        (an undated row is a missing-data flag, not a stale event — mirrors
        PreEarningsAgent._filter_stale_items). An unknown same-day session is
        kept (we do not silently drop an ambiguous catalyst).
        """
        if not isinstance(row, dict):
            return False
        dt = _parse_iso(row.get("date") or row.get("date_formatted"))
        if dt is None:
            return False
        # "Today" is the exchange's day (US/Eastern), not UTC's. With UTC
        # dates, every evening from 00:00 UTC (19:00/20:00 ET) until ET
        # midnight TOMORROW's reports looked "same day" and, the ET clock
        # being past both session cutoffs, were dropped as already reported —
        # exactly the catalysts the next pre-market is about. Date-only
        # calendar rows (midnight UTC) are already exchange-local dates.
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        ev_date = us_eastern_date(dt)
        today = us_eastern_date(None, now_utc)
        if ev_date is None or today is None:
            return False
        if ev_date > today:
            return False
        if ev_date < today:
            return True
        # Same calendar day -> resolve by session hint (delegated to market_hours, §28).
        session = str(row.get("when") or row.get("session") or "")
        return earnings_session_reported(session, now_utc)

    def _derive_catalysts(self, news_rows: List[Dict[str, Any]], earnings: Dict[str, Any]) -> List[str]:
        tags: List[str] = []
        for row in news_rows:
            text = f"{row.get('title', '')} {row.get('summary', '')} {row.get('text', '')}"
            for tag, pattern in CATALYST_PATTERNS.items():
                if pattern.search(text):
                    tags.append(tag)
        eps = _safe_float(earnings.get("eps_surprise_pct"), None)
        rev = _safe_float(earnings.get("revenue_surprise_pct"), None)
        if eps is not None:
            if eps >= 10:
                tags.append("EARNINGS_BEAT")
            elif eps <= -10:
                tags.append("EARNINGS_MISS")
        if rev is not None:
            if rev >= 5:
                tags.append("REVENUE_BEAT")
            elif rev <= -5:
                tags.append("REVENUE_MISS")
        return sorted(set(tags))

    def _apply_ollama(
        self,
        candidates: List[Dict[str, Any]],
        cfg: Dict[str, Any],
        session: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not bool(cfg.get("use_ollama", True)):
            return False
        if not candidates:
            return False
        max_tickers = max(1, _safe_int(cfg.get("ollama_max_tickers"), 10))
        timeout_sec = max(1, _safe_int(cfg.get("ollama_timeout_seconds"), 6))
        workers = max(1, _safe_int(cfg.get("ollama_workers"), 4))

        # Select the top tickers by the PREMIUM composite score — the same metric
        # the UI sorts by (premarket_score_long/short) and the aggregate Pulse
        # summarizes (max of the two). The previous key was the vestigial legacy
        # int divergence `abs(score_long - score_short)`, which could pick a
        # low-premium ticker for AI enrichment while skipping a high-premium one
        # the user sees ranked #1 (audit O2).
        selected = sorted(
            candidates,
            key=lambda c: max(
                _safe_int(c.get("premarket_score_long"), 0),
                _safe_int(c.get("premarket_score_short"), 0),
            ),
            reverse=True,
        )[:max_tickers]

        try:
            model = self._analyst_model()
        except Exception:
            model = None
        if not model:
            return False
        timeout_sec = call_timeout_for(model, timeout_sec)

        # An answer is kept while the question is the same. Asking again for the top
        # tickers on every 5-minute cycle cost about 100 model calls an hour in
        # pre-market hours, most of them for tickers without a single new headline.
        reuse_minutes = max(0, _safe_int(cfg.get("ollama_reuse_minutes"), 30))
        earlier_answers: Dict[str, Dict[str, Any]] = {}
        if reuse_minutes:
            for saved in list((getattr(self, "_state", None) or {}).get("candidates") or []):
                if isinstance(saved, dict) and isinstance(saved.get("ollama"), dict):
                    earlier_answers[str(saved.get("symbol") or "").upper()] = saved["ollama"]

        def _attach(cand: Dict[str, Any], enriched: Dict[str, Any]) -> None:
            cand["ollama"] = enriched
            catalyst = str(enriched.get("catalyst") or "").strip()
            tag = re.sub(r"[\s\-]+", "_", catalyst.upper())
            if tag and tag not in PLACEHOLDER_CATALYSTS:
                tags = list(cand.get("catalysts", []) or [])
                tags.append(tag)
                cand["catalysts"] = sorted(set(tags))

        used = False
        to_ask: List[Tuple[Dict[str, Any], str]] = []
        for cand in selected:
            fingerprint = self._enrichment_fingerprint(cand, model, session)
            kept = self._reusable_enrichment(
                earlier_answers.get(str(cand.get("symbol") or "").upper()), fingerprint, reuse_minutes,
            )
            if kept is not None:
                _attach(cand, kept)
                used = True
            else:
                to_ask.append((cand, fingerprint))
        if not to_ask:
            return used

        # Enrich the selected tickers concurrently instead of sequentially.
        # A sequential loop of up to `ollama_max_tickers` (default 10) tickers at
        # `ollama_timeout_seconds` (default 6s) each blocked the agent cycle for
        # ~60s. Each candidate dict is independent — a worker only writes to its
        # own `ollama`/`catalysts` keys — so there is no shared mutable state
        # across workers. The ollama client's own circuit breaker handles a dead
        # provider (returns CIRCUIT_OPEN immediately), so a bounded pool does not
        # hammer a downed endpoint; the per-result error codes are still surfaced
        # and logged here.
        def _enrich_one(
            cand: Dict[str, Any], fingerprint: str,
        ) -> Tuple[Dict[str, Any], str, Optional[Dict[str, Any]]]:
            return cand, fingerprint, self._ollama_enrich_candidate(cand, model, timeout_sec, session=session)

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_enrich_one, cand, fingerprint) for cand, fingerprint in to_ask]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    cand, fingerprint, enriched = fut.result()
                except Exception:
                    continue
                if not isinstance(enriched, dict):
                    continue

                err = str(enriched.get("_error") or "").strip().upper()
                if err:
                    if err == "CIRCUIT_OPEN":
                        logger.info("Premarket Ollama skipped: provider circuit is open.")
                    elif err == "TIMEOUT":
                        logger.info("Premarket Ollama skipped after timeout (timeout=%ss).", timeout_sec)
                    continue

                enriched["inputs_fingerprint"] = fingerprint
                enriched["answered_at"] = _iso(_utc_now())
                _attach(cand, enriched)
                used = True
        return used

    def _enrichment_fingerprint(
        self, candidate: Dict[str, Any], model: Any, session: Optional[Dict[str, Any]],
    ) -> str:
        """Identifies the catalyst question asked about a candidate.

        Covers what the prompt is built from and what changes its meaning: the
        headlines, the earnings surprise, the session kind, the model, and the
        gap in whole percent. The exact gap and the clock are left out: they
        differ on every cycle without changing what moved the stock.
        """
        earnings = dict(candidate.get("earnings", {}) or {})
        titles = [
            self._sanitize_prompt_text(row.get("title"), max_len=160)
            for row in list(candidate.get("news", []) or [])[:3]
            if isinstance(row, dict) and str(row.get("title") or "").strip()
        ]
        kind = ""
        if isinstance(session, dict) and session:
            kind = str(session.get("data_kind") or self._data_kind_for(session) or "")
        question = [
            str(candidate.get("symbol") or "").upper(), titles,
            earnings.get("eps_surprise_pct"), earnings.get("revenue_surprise_pct"),
            round(_safe_float(candidate.get("gap_pct"), 0.0) or 0.0), kind, str(model or ""),
        ]
        return hashlib.sha1(json.dumps(question, default=str).encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _reusable_enrichment(
        earlier: Any, fingerprint: str, reuse_minutes: int,
    ) -> Optional[Dict[str, Any]]:
        """The earlier answer, when it answered this very question recently enough."""
        if reuse_minutes <= 0 or not fingerprint or not isinstance(earlier, dict):
            return None
        if earlier.get("inputs_fingerprint") != fingerprint:
            return None
        answered_at = _parse_iso(earlier.get("answered_at"))
        if answered_at is None:
            return None
        age_seconds = (_utc_now() - answered_at).total_seconds()
        if age_seconds < 0 or age_seconds > reuse_minutes * 60:
            return None
        return copy.deepcopy(earlier)

    def _ollama_enrich_candidate(
        self,
        candidate: Dict[str, Any],
        model: str,
        timeout_sec: int,
        session: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        symbol = str(candidate.get("symbol") or "").upper()
        news_rows = list(candidate.get("news", []) or [])
        earnings = dict(candidate.get("earnings", {}) or {})
        titles = [
            self._sanitize_prompt_text(n.get("title"), max_len=160)
            for n in news_rows[:3]
            if str(n.get("title") or "").strip()
        ]
        gap = _safe_float(candidate.get("gap_pct"), 0.0) or 0.0
        eps_surprise = earnings.get("eps_surprise_pct")
        rev_surprise = earnings.get("revenue_surprise_pct")

        prompt = (
            "You are a market catalyst classifier. "
            "Return strict JSON only with keys catalyst, summary, confidence.\n"
            f"{self._session_prompt_line(session)}"
            f"Symbol: {symbol}\n"
            f"Gap%: {gap:.2f}\n"
            f"EPS surprise%: {eps_surprise}\n"
            f"Revenue surprise%: {rev_surprise}\n"
            f"Headlines: {' | '.join(titles) if titles else 'none'}\n"
            "Rules: summary max 2 short sentences; confidence between 0 and 1. "
            "'Headlines: none' means the scanner found no headline, not that none "
            "exists: then answer catalyst \"none\" and do not guess a cause."
        )
        try:
            # Route through `ollama.generate` (uses /api/chat, the endpoint
            # ollama.com's CLOUD API actually exposes). The previous direct POST
            # to `{OLLAMA_BASE_URL}/api/generate` returned HTTP 404 on the cloud
            # host because cloud does NOT expose /api/generate — only /api/chat.
            # `ollama.generate` normalizes chat `message.content` -> `response`.
            result = ollama.generate(
                prompt,
                model=model,
                timeout_override=timeout_sec,
            )
            if not isinstance(result, dict) or result.get("error"):
                err = str(result.get("error") or "").lower() if isinstance(result, dict) else ""
                if "circuit" in err:
                    return {"_error": "CIRCUIT_OPEN"}
                if "timed out" in err or "timeout" in err:
                    return {"_error": "TIMEOUT"}
                return {"_error": "ERROR"}
            raw = str(result.get("response") or "").strip()
            parsed = self._extract_json(raw)
            if not isinstance(parsed, dict):
                return {"_error": "PARSE"}
            catalyst = str(parsed.get("catalyst") or "").strip()
            summary = str(parsed.get("summary") or "").strip()
            confidence = _safe_float(parsed.get("confidence"), 0.0) or 0.0
            confidence = max(0.0, min(1.0, confidence))
            if not summary and not catalyst:
                return {"_error": "EMPTY"}
            return {
                "catalyst": catalyst,
                "summary": summary[:240],
                "confidence": round(confidence, 4),
            }
        except Exception as e:
            msg = str(e).lower()
            if "timed out" in msg or "timeout" in msg:
                return {"_error": "TIMEOUT"}
            return {"_error": "ERROR"}

    @staticmethod
    def _format_age(seconds: Any) -> str:
        sec = _safe_float(seconds, None)
        if sec is None:
            return "unknown age"
        sec = max(0.0, sec)
        if sec < 90:
            return f"{int(sec)} seconds"
        if sec < 5400:
            return f"{int(round(sec / 60.0))} minutes"
        if sec < 172800:
            return f"{sec / 3600.0:.0f} hours"
        return f"{sec / 86400.0:.1f} days"

    @staticmethod
    def _et_label(iso_value: Any) -> str:
        dt = _parse_iso(iso_value)
        if dt is None:
            return "unknown"
        return dt.astimezone(EASTERN_TZ).strftime("%a %Y-%m-%d %H:%M ET")

    @classmethod
    def _session_prompt_line(cls, session: Optional[Dict[str, Any]]) -> str:
        """One prompt line with the TRUE session state and data age, so the
        model is never asked to analyse a pre-market that does not exist.
        Empty when the caller supplied no session (legacy callers/tests)."""
        if not isinstance(session, dict) or not session:
            return ""
        kind = session.get("data_kind") or cls._data_kind_for(session)
        now_et = cls._et_label(session.get("now_utc"))
        if kind == DATA_KIND_LAST_SESSION:
            closed_kind = str(session.get("closed_kind") or "closed")
            return (
                f"Session: the US stock market is CLOSED ({closed_kind}); now {now_et}. "
                "There is NO pre-market session right now. "
                f"Gap% values are the last prints of the {session.get('data_session_date') or session.get('quote_session_date')} "
                f"session vs its reference close, about {cls._format_age(session.get('quotes_age_seconds'))} old. "
                f"Next regular open: {cls._et_label(session.get('next_regular_open_utc'))}.\n"
            )
        if kind == DATA_KIND_AFTER_HOURS:
            return (
                f"Session: US after-hours ({now_et}). Gap% is the after-hours price vs today's "
                "regular close, not a pre-market gap. "
                f"Next regular open: {cls._et_label(session.get('next_regular_open_utc'))}.\n"
            )
        if kind == DATA_KIND_PREMARKET:
            return (
                f"Session: US pre-market ({now_et}); the regular session opens at 09:30 ET. "
                "Gap% is the pre-market price vs the previous close.\n"
            )
        return f"Session: {session.get('state') or 'unknown'} ({now_et}).\n"

    def _compute_premarket_pulse(
        self,
        cfg: Dict[str, Any],
        candidates: List[Dict[str, Any]],
        session: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Aggregate Pre-Market Pulse: one LLM call over the top candidates
        producing {summary, sentiment, top_long, top_short, risks, model,
        generated_at}. Cloud-only (local models too slow for an aggregate
        call), non-fatal (None on any error / circuit / parse failure /
        disabled). See Task #14 — B2."""
        if not bool(cfg.get("pulse_enabled", True)):
            return None
        if not candidates:
            return None
        try:
            if not (ollama.is_cloud_host() or ollama.check_availability()):
                return None
            model = self._analyst_model()
        except Exception:
            return None
        if not model:
            return None

        top = sorted(
            candidates,
            key=lambda c: max(
                _safe_int(c.get("premarket_score_long"), 0),
                _safe_int(c.get("premarket_score_short"), 0),
            ),
            reverse=True,
        )[:12]
        lines: List[str] = []
        for c in top:
            sym = str(c.get("symbol") or "")
            direction = str(c.get("direction") or "WATCH")
            gap = _safe_float(c.get("gap_pct"), 0.0) or 0.0
            sL = _safe_int(c.get("premarket_score_long"), 0)
            sS = _safe_int(c.get("premarket_score_short"), 0)
            cats = ",".join(list(c.get("catalysts") or [])[:3]) or "none"
            news_list = list(c.get("news") or [])
            top_news = self._sanitize_prompt_text((news_list[0] or {}).get("title"), max_len=80) if news_list else ""
            lines.append(f"- {sym} {direction} gap{gap:+.1f}% L{sL}/S{sS} cat[{cats}] \"{top_news}\"")

        data_kind = (session or {}).get("data_kind") if isinstance(session, dict) else None
        if isinstance(session, dict) and session and not data_kind:
            data_kind = self._data_kind_for(session)
        json_keys = (
            "sentiment (one of bullish|bearish|neutral), top_long (array of "
            "{symbol,reason} for the 1-3 best long setups), top_short (array of "
            "{symbol,reason} for the 1-3 best short setups), risks (one short "
            "sentence on the main risk to the read). Be honest and terse; do not "
            "invent symbols or data not in the list."
        )
        if data_kind == DATA_KIND_LAST_SESSION:
            pulse_kind = "last_session_recap"
            system = (
                "You are a senior equity analyst. The US stock market is CLOSED and "
                "there is NO pre-market session right now. Produce a CONCISE recap of "
                "the LAST session as STRICT JSON only with keys: summary (one or two "
                "sentences on how these names ended the last session and what to watch "
                "at the next open; never call it a live pre-market, never say prices "
                "are moving now or refer to the open as today's unless it is), "
                + json_keys
            )
            header = (
                f"Last-session movers ({session.get('data_session_date')} session; market closed, "
                f"data about {self._format_age(session.get('quotes_age_seconds'))} old) "
                "- top movers + watchlist:\n"
            )
        elif data_kind == DATA_KIND_AFTER_HOURS:
            pulse_kind = "after_hours"
            system = (
                "You are a senior equity analyst covering the US after-hours session. "
                "Produce a CONCISE after-hours pulse as STRICT JSON only with keys: "
                "summary (one or two sentences on the after-hours tone: breadth, "
                "sentiment, dominant catalysts; these are after-hours moves vs today's "
                "regular close, not pre-market gaps), "
                + json_keys
            )
            header = "After-hours movers vs today's regular close (top movers + watchlist):\n"
        else:
            pulse_kind = "premarket"
            system = (
                "You are a senior pre-market analyst. Produce a CONCISE pre-market "
                "pulse as STRICT JSON only with keys: summary (one or two sentences "
                "on the session tone: breadth, sentiment, dominant catalysts), "
                + json_keys
            )
            header = "Today's pre-market candidates (top movers + watchlist):\n"
        prompt = (
            self._session_prompt_line(session)
            + header
            + "\n".join(lines)
            + "\nLine format: SYMBOL DIRECTION gap L<long score>/S<short score> "
            "cat[catalyst tags] \"top headline\". cat[none] and an empty headline mean "
            "the scanner found NO catalyst or news for that ticker: say the driver is "
            "unknown, do not infer one."
            + "\nReturn STRICT JSON only: summary, sentiment, top_long, top_short, risks."
        )
        timeout_sec = call_timeout_for(model, max(5, min(30, _safe_int(cfg.get("pulse_timeout_seconds"), 12))))
        try:
            result = ollama.generate(prompt, model=model, system=system, timeout_override=timeout_sec)
        except Exception:
            return None
        if not isinstance(result, dict) or result.get("error"):
            return None
        raw = str(result.get("response") or "").strip()
        parsed = self._extract_json(raw)
        if not isinstance(parsed, dict):
            return None
        pulse = {
            "summary": str(parsed.get("summary") or "").strip(),
            "sentiment": str(parsed.get("sentiment") or "").strip().lower(),
            "top_long": self._normalize_pulse_picks(parsed.get("top_long")),
            "top_short": self._normalize_pulse_picks(parsed.get("top_short")),
            "risks": str(parsed.get("risks") or "").strip(),
            "model": str(result.get("_meta", {}).get("model") or model or "").strip(),
            "generated_at": _iso(_utc_now()),
            # What this text is a read OF: a live pre-market, the after-hours
            # tape, or a recap of a finished session (market closed).
            "kind": pulse_kind,
            "session_state": (session or {}).get("state") if isinstance(session, dict) else None,
            "data_session_date": (session or {}).get("data_session_date") if isinstance(session, dict) else None,
            "data_age_seconds": (session or {}).get("quotes_age_seconds") if isinstance(session, dict) else None,
        }
        if not (pulse["summary"] or pulse["top_long"] or pulse["top_short"]):
            return None
        # Defense-in-depth (audit #5): the LLM is told not to invent symbols,
        # but enforce it programmatically — drop any pick whose symbol is not
        # in the candidate batch so a hallucinated ticker never surfaces as a
        # "best long/short setup".
        valid_symbols = {str(c.get("symbol") or "").upper() for c in candidates}
        pulse["top_long"] = [p for p in pulse["top_long"] if p.get("symbol") and p["symbol"].upper() in valid_symbols]
        pulse["top_short"] = [p for p in pulse["top_short"] if p.get("symbol") and p["symbol"].upper() in valid_symbols]
        return pulse

    @staticmethod
    def _normalize_pulse_picks(value: Any) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        if isinstance(value, list):
            for item in value[:3]:
                if isinstance(item, dict):
                    out.append({
                        "symbol": str(item.get("symbol") or "").strip(),
                        "reason": str(item.get("reason") or "").strip(),
                    })
                elif isinstance(item, str):
                    s = str(item).strip()
                    if s:
                        out.append({"symbol": s, "reason": ""})
        return out

    def get_premarket_pulse(
        self,
        cfg: Dict[str, Any],
        candidates: List[Dict[str, Any]],
        ttl_seconds: Optional[float] = None,
        session: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Cached (TTL `pulse_ttl_seconds`, default 300s) aggregate pulse. A
        successful pulse is served from cache until TTL AND while the candidate
        set is unchanged; a None result is NOT cached so a transient failure
        retries on the next cycle. Non-fatal."""
        if not bool(cfg.get("pulse_enabled", True)):
            return None
        if ttl_seconds is None:
            ttl_seconds = max(30.0, float(_safe_int(cfg.get("pulse_ttl_seconds"), 300)))
        now = time.monotonic()
        # The session is part of the identity: a pulse written about a live
        # pre-market must not be re-served as the recap of a closed market.
        session_key = None
        if isinstance(session, dict) and session:
            session_key = (
                session.get("data_kind") or self._data_kind_for(session),
                session.get("data_session_date") or session.get("quote_session_date"),
            )
        fingerprint = (self._pulse_fingerprint(candidates), session_key)
        cached = getattr(self, "_premarket_pulse_cache", None)
        if (
            cached is not None
            and (now - float(cached[0])) < ttl_seconds
            and cached[1] is not None
            and cached[2] == fingerprint
        ):
            return cached[1]
        if isinstance(session, dict) and session:
            pulse = self._compute_premarket_pulse(cfg, candidates, session=session)
        else:
            pulse = self._compute_premarket_pulse(cfg, candidates)
        self._premarket_pulse_cache = (now, pulse, fingerprint)
        return pulse

    @staticmethod
    def _pulse_fingerprint(candidates: List[Dict[str, Any]]) -> tuple:
        """Stable identity of the candidate batch used to key the pulse cache.
        Two batches with the same symbols/directions are treated as identical
        so an unchanged book reuses the cached pulse; a changed book forces a
        recompute even within the TTL window."""
        items = []
        for c in candidates or []:
            sym = str(c.get("symbol") or "").upper()
            if not sym:
                continue
            items.append((sym, str(c.get("direction") or "WATCH").upper()))
        return tuple(sorted(items))

    def invalidate_pulse_cache(self) -> None:
        """Clear the cached pulse so the next call recomputes. Called on manual
        refresh and on market-open pause so a stale pulse is never re-served
        over a new (or emptied) candidate set."""
        self._premarket_pulse_cache = None

    @staticmethod
    def _sanitize_prompt_text(text: Any, max_len: int = 200) -> str:
        """Normalize untrusted text (news headlines) before interpolating it
        into an LLM prompt (audit #8). Collapses whitespace and strips
        newlines/tabs/control chars so an injected "ignore prior instructions"
        can't sit on its own line masquerading as a system directive, and
        bounds the length. The text itself is preserved (only whitespace is
        normalized); LLM output is JSON-parsed regardless, so impact is
        bounded — this is defense-in-depth for a premium product."""
        s = str(text or "")
        s = re.sub(r"[\x00-\x1f\x7f]", " ", s)  # drop control chars / newlines / tabs
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) > max_len:
            s = s[:max_len].rstrip() + "…"
        return s

    @staticmethod
    def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        snippet = text[start : end + 1]
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return None
        return None

    def _normalize_mover(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        symbol = str(row.get("symbol") or row.get("ticker") or "").upper().strip()
        if not symbol:
            return None
        price = _safe_float(
            row.get("premarket_price")
            or row.get("postMarketPrice")
            or row.get("preMarketPrice")
            or row.get("after_hours_price")
            or (row.get("price") if str(row.get("session") or row.get("source") or "").lower() in {"premarket", "pre_market", "after_hours", "postmarket"} else None),
            None,
        )
        if price is None or price <= 0:
            return None
        prev_close = _safe_float(
            row.get("prev_close")
            or row.get("previousClose")
            or row.get("regularMarketPreviousClose")
            or row.get("previous_close"),
            None,
        )
        gap = None
        if price is not None and prev_close is not None and prev_close > 0:
            gap = ((price - prev_close) / prev_close) * 100.0
        if gap is None:
            gap = _safe_float(
                row.get("gap_pct")
                or row.get("postMarketChangePercent")
                or row.get("preMarketChangePercent")
                or row.get("change_pct", row.get("changesPercentage")),
                0.0,
            ) or 0.0
        premarket_volume = _safe_float(
            row.get("premarket_volume")
            or row.get("preMarketVolume")
            or row.get("extended_hours_volume")
            or row.get("extendedHoursVolume"),
            None,
        )
        avg_volume = _safe_float(
            row.get("avg_volume")
            or row.get("average_volume")
            or row.get("avgVolume")
            or row.get("averageVolume")
            or row.get("averageDailyVolume")
            or row.get("average_daily_volume")
            or row.get("averageDailyVolume10Day")
            or row.get("averageDailyVolume3Month")
            or row.get("avg_vol"),
            None,
        )
        return {
            "symbol": symbol,
            "gap_pct": float(gap),
            "premarket_price": price,
            "prev_close": prev_close,
            "premarket_volume": premarket_volume,
            "avg_volume": avg_volume,
            "source": str(row.get("source") or "scanner"),
        }

    def _deduplicate_movers(self, movers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        pos: Dict[str, int] = {}
        for row in movers:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").upper().strip()
            if not sym:
                continue
            idx = pos.get(sym)
            if idx is None:
                pos[sym] = len(out)
                out.append(row)
                continue
            prev = out[idx]
            out[idx] = self._select_better_mover(prev, row)
        return out

    @staticmethod
    def _mover_quality(row: Dict[str, Any]) -> int:
        if not isinstance(row, dict):
            return 0
        score = 0
        if _safe_float(row.get("premarket_price"), None) is not None:
            score += 2
        if _safe_float(row.get("prev_close"), None) is not None:
            score += 1
        if _safe_float(row.get("premarket_volume"), None) is not None:
            score += 1
        if _safe_float(row.get("avg_volume"), None) is not None:
            score += 1
        if str(row.get("source") or "").lower() == "scanner":
            score += 1
        return score

    @classmethod
    def _select_better_mover(cls, left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
        left_gap = abs(_safe_float(left.get("gap_pct"), 0.0) or 0.0)
        right_gap = abs(_safe_float(right.get("gap_pct"), 0.0) or 0.0)
        if right_gap > left_gap:
            preferred, fallback = right, left
        elif right_gap < left_gap:
            preferred, fallback = left, right
        elif cls._mover_quality(right) > cls._mover_quality(left):
            preferred, fallback = right, left
        else:
            preferred, fallback = left, right

        # Different scanner feeds can contribute complementary observations
        # for the same symbol (for example gap/price in the hot list and
        # average volume in the watchlist snapshot).  Keep the preferred row's
        # price signal while filling only genuinely missing metrics.
        merged = dict(preferred)
        for field in ("premarket_price", "prev_close", "premarket_volume", "avg_volume"):
            if _safe_float(merged.get(field), None) is None and _safe_float(fallback.get(field), None) is not None:
                merged[field] = fallback.get(field)
        return merged

    @staticmethod
    def _is_crypto_symbol(symbol: str) -> bool:
        sym = str(symbol or "").upper()
        return (
            sym.endswith("-USD")
            or sym.endswith("USDT")
            or sym.endswith("-USDT")
            or sym.endswith("-USDC")
            or sym.endswith("USD=X")
        )

    @staticmethod
    def _is_us_equity_symbol(symbol: str) -> bool:
        sym = str(symbol or "").upper().strip()
        if not sym:
            return False
        if any(token in sym for token in ("=X", "/", " ", "^")):
            return False
        if sym.endswith(("-USD", "USDT", "-USDT", "-USDC", "USD=X")):
            return False
        if "." in sym:
            parts = sym.split(".")
            if len(parts) != 2:
                return False
            base, suffix = parts
            return bool(base.isalpha() and suffix.isalpha() and 1 <= len(base) <= 5 and len(suffix) == 1)
        return bool(sym.isalpha() and 1 <= len(sym) <= 5)

    @staticmethod
    def _is_commodity_symbol(symbol: str) -> bool:
        return str(symbol or "").upper().strip() in COMMODITY_EXCLUDED_SYMBOLS

    @classmethod
    def _is_excluded_symbol(cls, symbol: str) -> bool:
        sym = str(symbol or "").upper().strip()
        if cls._is_crypto_symbol(sym) or cls._is_commodity_symbol(sym):
            return True
        return not cls._is_us_equity_symbol(sym)

    @staticmethod
    def _is_us_market_open(now_utc: Optional[datetime] = None) -> bool:
        # Folded into core.market_hours (§28). Delegating to us_equity_market_status
        # also makes this check honor MS_MARKET_HOURS_OVERRIDE consistently with the
        # rest of the system (previously the agent ignored the override).
        return us_equity_market_status(now_utc or _utc_now()).is_open

    def is_market_open(self) -> bool:
        return self._is_us_market_open()

    def _is_enabled(self) -> bool:
        return is_agent_enabled("premarket_scanner", config=self.config_manager.config, default=False)

    def _analyst_model(self) -> Optional[str]:
        """The model the owner assigned to market analysis; the global model when the role has none."""
        from core.ai.role_resolution import assigned_model

        return assigned_model("market_analyst", config=getattr(self, "config_manager", None),
                              fallback=ollama.get_best_model())

    def _get_config(self) -> Dict[str, Any]:
        cfg = dict(self.DEFAULT_CONFIG)
        merged = (self.config_manager.config or {}).get("premarket", {}) or {}
        if isinstance(merged, dict):
            cfg.update(merged)

        for env_key, (cfg_key, caster) in self.ENV_MAP.items():
            raw = os.getenv(env_key)
            if raw is None or str(raw).strip() == "":
                continue
            try:
                cfg[cfg_key] = caster(raw)
            except Exception:
                continue

        cfg["refresh_seconds"] = max(30, _safe_int(cfg.get("refresh_seconds"), 300))
        cfg["startup_delay_seconds"] = max(
            0,
            min(300, _safe_int(cfg.get("startup_delay_seconds"), 8)),
        )
        cfg["external_call_budget"] = max(
            1,
            min(200, _safe_int(cfg.get("external_call_budget"), 24)),
        )
        cfg["lookback_hours"] = max(1, _safe_int(cfg.get("lookback_hours"), 24))
        cfg["min_gap_pct"] = max(0.1, _safe_float(cfg.get("min_gap_pct"), 4.0) or 4.0)
        cfg["max_results"] = max(1, min(200, _safe_int(cfg.get("max_results"), 30)))
        cfg["timeout_seconds"] = max(2, _safe_int(cfg.get("timeout_seconds"), 8))
        cfg["cache_ttl_seconds"] = max(30, _safe_int(cfg.get("cache_ttl_seconds"), 300))
        cfg["use_ollama"] = bool(cfg.get("use_ollama", True))
        cfg["ollama_max_tickers"] = max(1, min(50, _safe_int(cfg.get("ollama_max_tickers"), 10)))
        cfg["ollama_timeout_seconds"] = max(1, min(30, _safe_int(cfg.get("ollama_timeout_seconds"), 6)))
        cfg["ollama_workers"] = max(1, min(8, _safe_int(cfg.get("ollama_workers"), 4)))
        cfg["aftermarket_max_symbols"] = max(0, min(2000, _safe_int(cfg.get("aftermarket_max_symbols"), 0)))
        cfg["aftermarket_workers"] = max(1, min(16, _safe_int(cfg.get("aftermarket_workers"), 6)))
        cfg["aftermarket_cache_ttl_seconds"] = max(
            10, min(3600, _safe_int(cfg.get("aftermarket_cache_ttl_seconds"), 60))
        )
        cfg["news_refresh_batch_size"] = max(1, min(100, _safe_int(cfg.get("news_refresh_batch_size"), 50)))
        cfg["news_refresh_cooldown_seconds"] = max(
            15, min(3600, _safe_int(cfg.get("news_refresh_cooldown_seconds"), 120))
        )
        cfg["closed_session_refresh_seconds"] = max(
            int(cfg["refresh_seconds"]),
            min(86400, _safe_int(cfg.get("closed_session_refresh_seconds"), 21600)),
        )
        return cfg

    def _load_state(self) -> Dict[str, Any]:
        cfg = self._get_config()
        template = {
            "as_of_utc": None,
            "refresh_seconds": int(cfg["refresh_seconds"]),
            "inputs_used": {
                "news_cache_age_seconds": None,
                "earnings_cache_age_seconds": None,
                "movers_cache_age_seconds": None,
            },
            "flags": {
                "news_available": False,
                "earnings_available": False,
                "ollama_used": False,
                "refreshing": False,
                "market_open": False,
                "scope": "US_ONLY",
            },
            "session": None,
            "candidates": [],
            "long_candidates": [],
            "short_candidates": [],
            "watch_only": [],
            "snapshot_candidates": [],
            "status": {
                "last_run_time": None,
                "last_success_time": None,
                "last_error": None,
                "state": None,
                "reason": None,
                "market_state": None,
                "last_inputs_used": {
                    "news_cache_age_seconds": None,
                    "earnings_cache_age_seconds": None,
                    "movers_cache_age_seconds": None,
                },
                "next_run_in": int(cfg["refresh_seconds"]),
            },
            "disabled": not self._is_enabled(),
        }

        try:
            if PREMARKET_OUTPUT_PATH.exists():
                with PREMARKET_OUTPUT_PATH.open("r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    merged = copy.deepcopy(template)
                    merged.update(loaded)
                    merged["status"] = {**template["status"], **(loaded.get("status", {}) or {})}
                    return self._sanitize_state_payload(merged, cfg)
        except Exception as e:
            logger.warning(f"Premarket cache load failed: {e}")
        return template

    def _apply_market_open_state(self, payload: Dict[str, Any], market_open: Optional[bool] = None) -> Dict[str, Any]:
        """Serve the frozen premarket snapshot while the US session is open.

        When the market opens the scanner pauses and its live candidate list is
        empty; the frontend sections read long_candidates/short_candidates/
        watch_only first, so without this restore the grouped view renders a
        blank screen at 09:30 ET even though snapshot_candidates has valid data.

        Applied on BOTH output paths — the in-memory read path (get_state) and
        the disk persistence path (_persist_locked) — because production serves
        /api/premarket from the persisted data/premarket.json file, never from
        get_state directly."""
        if market_open is None:
            market_open = self._is_us_market_open()
        flags = payload.get("flags", {}) if isinstance(payload, dict) else {}
        if not isinstance(flags, dict):
            flags = {}
        flags["market_open"] = bool(market_open)
        flags["scope"] = "US_ONLY"
        flags["refreshing"] = bool(flags.get("refreshing", False))
        payload["flags"] = flags
        if market_open and not list(payload.get("candidates") or []):
            snapshot = payload.get("snapshot_candidates")
            if isinstance(snapshot, list) and snapshot:
                payload["candidates"] = snapshot
                if not list(payload.get("long_candidates") or []):
                    long_snap = [c for c in snapshot if str(c.get("direction") or "").upper() == "LONG"]
                    short_snap = [c for c in snapshot if str(c.get("direction") or "").upper() == "SHORT"]
                    watch_snap = [c for c in snapshot if str(c.get("direction") or "").upper() not in ("LONG", "SHORT")]
                    payload["long_candidates"] = long_snap
                    payload["short_candidates"] = short_snap
                    payload["watch_only"] = watch_snap
                # Signal to the UI that this is a frozen snapshot (session is open).
                flags["frozen_snapshot"] = True
                payload["flags"] = flags
        return payload

    def _persist_locked(self) -> None:
        PREMARKET_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = PREMARKET_OUTPUT_PATH.with_suffix(".json.tmp")
        try:
            snapshot = self._sanitize_state_payload(
                self._apply_market_open_state(copy.deepcopy(self._state)), self._get_config()
            )
            with temp_path.open("w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=2, ensure_ascii=False)
            os.replace(str(temp_path), str(PREMARKET_OUTPUT_PATH))
        except Exception as e:
            logger.error(f"Premarket cache persist failed: {e}")
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass

    def _sanitize_state_payload(self, payload: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return payload
        out = dict(payload)
        cfg_local = cfg or self._get_config()
        candidates = out.get("candidates")
        if isinstance(candidates, list):
            max_results = max(1, _safe_int(cfg_local.get("max_results"), 30))
            deduped = self._deduplicate_candidates(candidates)
            # Same cap as _build_payload: watchlist rows ride on top of
            # max_results. A flat [:max_results] here cut the list that
            # _build_payload had just built (watchlist first), so with a busy
            # watchlist the served/persisted payload lost the non-watchlist
            # movers the cycle had selected.
            watch_count = sum(1 for c in deduped if bool(c.get("is_watchlist")))
            out["candidates"] = deduped[: min(300, watch_count + max_results)]
            groups = self._build_direction_groups(out["candidates"])
            out["long_candidates"] = groups["LONG"]
            out["short_candidates"] = groups["SHORT"]
            out["watch_only"] = groups["WATCH"]
        else:
            out["long_candidates"] = []
            out["short_candidates"] = []
            out["watch_only"] = []
        return out

    @staticmethod
    def _build_direction_groups(candidates: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        groups = {"LONG": [], "SHORT": [], "WATCH": []}
        for row in candidates if isinstance(candidates, list) else []:
            if not isinstance(row, dict):
                continue
            direction = str(row.get("direction") or "").upper()
            if direction == "LONG":
                groups["LONG"].append(row)
            elif direction == "SHORT":
                groups["SHORT"].append(row)
            else:
                groups["WATCH"].append(row)
        return groups

    @staticmethod
    def _deduplicate_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen = set()
        for candidate in candidates if isinstance(candidates, list) else []:
            if not isinstance(candidate, dict):
                continue
            sym = str(candidate.get("symbol") or "").upper().strip()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            out.append(candidate)
        return out


_premarket_scanner_agent: Optional[PremarketScannerAgent] = None
_premarket_scanner_lock = threading.Lock()


def get_premarket_scanner_agent(config_manager: Optional[ConfigManager] = None) -> PremarketScannerAgent:
    global _premarket_scanner_agent
    with _premarket_scanner_lock:
        if _premarket_scanner_agent is None:
            _premarket_scanner_agent = PremarketScannerAgent(config_manager=config_manager)
    return _premarket_scanner_agent


def get_premarket_scanner_agent_if_ready() -> Optional[PremarketScannerAgent]:
    return _premarket_scanner_agent
