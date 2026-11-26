import copy
import logging
import math
import os
import re
import threading
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from api import actions_service
from api.state_service import get_state as get_ui_state
from api.state_service import load_traderadar_state, save_traderadar_state
from core.agent_toggles import is_agent_enabled
from core.config import ConfigManager, data_path, is_crypto_ticker, normalize_ticker
from core.quick_backtester import QuickBacktester
from core.agent_metrics import track_agent_run
from core.http_utils import http_get
from core.observability import update_agent_status
from core.request_context import get_request_context, request_context

logger = logging.getLogger("TradeRadarAgent")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    try:
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


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
            text = value.strip().replace("%", "")
            if not text:
                return default
            return float(text)
        return float(value)
    except Exception:
        return default


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _extract_symbol(value: Any) -> str:
    return str(value or "").upper().strip()


def _finite(value: Any) -> Optional[float]:
    """float(value) when it is a real finite number, else None (NaN/inf/None
    must never reach the payload as a number: NaN is not valid JSON and a NaN
    indicator would pass ``isinstance(x, float)`` checks as if it were data)."""
    try:
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"


def _to_binance_pair(symbol: str) -> str:
    """Scanner/watchlist crypto symbol -> Binance spot pair.
    KAVAUSDT -> KAVAUSDT, BTC-USD -> BTCUSDT, ETH-USDT -> ETHUSDT."""
    token = _extract_symbol(symbol).replace("-", "").replace("/", "")
    if token.endswith("USD") and not token.endswith(("USDT", "USDC")):
        token = token[:-3] + "USDT"
    return token


def filter_suppressed_candidates(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Drop candidates whose symbol is under an ACTIVE suppression and prune
    expired entries from the ``suppressed`` map. Used at serve time so the
    Suppress button really hides the card (the candidate itself is kept in
    the stored state, so un-suppressing restores it immediately)."""
    if not isinstance(payload, dict):
        return payload
    now = _now_utc()
    active: Dict[str, str] = {}
    for sym, until_iso in (payload.get("suppressed") or {}).items():
        until = _parse_iso(until_iso)
        key = _extract_symbol(sym)
        if key and until and until > now:
            active[key] = _iso(until)
    payload["suppressed"] = active
    if active:
        kept = [
            c for c in (payload.get("candidates") or [])
            if not (isinstance(c, dict) and _extract_symbol(c.get("symbol")) in active)
        ]
        removed = len(payload.get("candidates") or []) - len(kept)
        payload["candidates"] = kept
        if removed and isinstance(payload.get("total_candidates"), int):
            payload["total_candidates"] = max(0, payload["total_candidates"] - removed)
    return payload


class TradeRadarAgent:
    # Red flags that block entry readiness outright (see _build_readiness).
    HARD_BLOCKERS = frozenset({
        "missing_price",
        "missing_ohlc",
        "missing_indicators",
        "analysis_local_insufficient",
        "analysis_strength_estimated",
        "analysis_remote_failed",
        "enrichment_failed",
        "backtest_missing_ohlc",
        "backtest_no_trades",
        # The candidate's own backtest lost money over a usable sample
        # (>= backtest_negative_min_trades trades, profit factor < 1.0).
        "backtest_negative",
        "ohlc_local_cache_insufficient",
    })

    # Evidence notes carried in readiness["blockers"] for the card, which do
    # NOT block: a losing backtest over too few trades is not a verdict.
    NON_BLOCKING_READINESS_NOTES = frozenset({"backtest_thin_sample"})

    DEFAULT_CONFIG: Dict[str, Any] = {
        "threshold_pct": 10.0,
        # Asset-class-adaptive move threshold (display-only, win-rate-neutral).
        # None/0 -> use the adaptive default (crypto max(base,15%), stock
        # min(base,5%)). Set both equal to threshold_pct to restore flat behaviour.
        "threshold_pct_crypto": None,
        "threshold_pct_stock": None,
        "radar_interval_sec": 300,
        "cooldown_sec": 600,
        "max_candidates": 50,
        "max_per_cycle": 10,
        # Automatic cycles deliberately enrich fewer symbols so the provider
        # budget is not exhausted. Manual refreshes retain max_per_cycle.
        "auto_max_per_cycle": 4,
        # Ghost-setup expiry (TR-COR-04): candidates with last_seen older than
        # this are dropped at the start of each cycle. Default 24h; 0 = keep
        # forever (legacy behaviour, not recommended).
        "candidate_max_age_hours": 24,
        "ohlc_ttl_sec": 300,
        "news_ttl_sec": 300,
        "calendar_ttl_sec": 1800,
        "calendar_window_days": 3,
        "backtest_ttl_sec": 1800,
        "backtest_days": 30,
        "enable_backtest": True,
        # A backtest with at least this many closed trades and profit factor
        # below 1.0 blocks readiness (`backtest_negative`). Below it the losing
        # sample is only flagged (`backtest_thin_sample`, non-blocking).
        "backtest_negative_min_trades": 20,
        "suppression_default_sec": 3600,
        "analysis_include_heavy": False,
        "analysis_disable_ollama": True,
        # Policy: JSON/cache-first, remote only as fallback when local data is missing/stale.
        "local_only_mode": False,
        "allow_remote_on_miss": True,
        "allow_analysis_remote": True,
        "allow_ohlc_remote": True,
        "allow_news_refresh": True,
        "allow_calendar_refresh": True,
        "news_refresh_cooldown_sec": 120,
    }

    def __init__(self, config_manager: Optional[ConfigManager] = None):
        self.config_manager = config_manager or ConfigManager()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._pending_force = False
        self._pending_symbols: Set[str] = set()
        self._last_external_trigger_ts = 0.0

        # On-demand one-shot runner. When the continuous loop is not running in this
        # process (agents.trade_radar.api_autostart=false), a manual refresh must still
        # produce fresh data. We run a single cycle in a throwaway thread, guarded so
        # concurrent refresh clicks collapse into one in-flight run.
        self._oneshot_thread: Optional[threading.Thread] = None
        self._last_cycle_done_ts = time.time()
        self._last_watchdog_kick_ts = 0.0
        try:
            self._startup_delay_seconds = max(
                0.0,
                min(
                    300.0,
                    float(os.getenv("TRADE_RADAR_STARTUP_DELAY_SECONDS", "45") or 45),
                ),
            )
        except (TypeError, ValueError):
            self._startup_delay_seconds = 45.0
        self._auto_cache_only = str(
            os.getenv("MS_TRADE_RADAR_AUTO_CACHE_ONLY", "1") or "1"
        ).strip().lower() not in {"0", "false", "no", "off"}

        # Price history is the one input the radar cannot score without. Auto
        # cycles stay cache-only for news/calendar/analysis, but the bounded
        # OHLC fetch (<= auto_max_per_cycle symbols per cycle, gated by the
        # ``allow_ohlc_remote`` config) is allowed through — stocks already
        # reached Yahoo in auto cycles (yfinance is not routed through the
        # cache-only HTTP guard) while crypto klines were silently blocked, so
        # every crypto card read "No OHLC data". MS_TRADE_RADAR_AUTO_OHLC_REMOTE=0
        # restores the strict behaviour.
        self._auto_ohlc_remote = str(
            os.getenv("MS_TRADE_RADAR_AUTO_OHLC_REMOTE", "1") or "1"
        ).strip().lower() not in {"0", "false", "no", "off"}

        self._ohlc_cache: Dict[str, Dict[str, Any]] = {}
        self._last_news_refresh_ts = 0.0
        self._last_calendar_refresh_ts = 0.0

        self._quick_backtester = QuickBacktester(self.config_manager)
        self._state = self._load_state()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="TradeRadarAgent")
            self._thread.start()
            logger.info("TradeRadarAgent started")

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3.0)
        logger.info("TradeRadarAgent stopped")

    def _loop_alive(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    @property
    def is_running(self) -> bool:
        """Whether the scheduled worker thread is alive (including while sleeping)."""
        return self._loop_alive()

    def get_runtime_status(self) -> Dict[str, Any]:
        with self._lock:
            interval = int((self._state.get("config", {}) or {}).get("radar_interval_sec", 300))
            running = bool(self._thread and self._thread.is_alive())
        return {
            "running": running,
            "next_run_in": max(10, interval) if running else None,
        }

    def run_once_async(self, force: bool = False, symbols: Optional[List[str]] = None) -> bool:
        """Run a single refresh cycle in a background thread.

        Used when the continuous loop is not running in this process, so a manual
        Force/Refresh still fetches fresh data. Returns True if a run was started,
        False if one was already in flight (the click collapses into it).
        """
        symbols_norm = [_extract_symbol(s) for s in (symbols or []) if _extract_symbol(s)]
        with self._lock:
            if self._oneshot_thread and self._oneshot_thread.is_alive():
                # A run is already underway; fold this request's intent into it.
                if force:
                    self._pending_force = True
                if symbols_norm:
                    self._pending_symbols.update(symbols_norm)
                return False

            def _once() -> None:
                try:
                    with request_context(reason="AGENT_REFRESH_ONESHOT", path="/agent/trade-radar", cache_only=False):
                        with track_agent_run("trade_radar"):
                            started = time.time()
                            self.run_cycle(force_refresh=bool(force), forced_symbols=symbols_norm)
                            self._mark_cycle_done(started)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.error(f"TradeRadarAgent one-shot refresh error: {exc}")
                    update_agent_status(
                        "trade_radar",
                        enabled=is_agent_enabled("trade_radar", config=self.config_manager.config, default=True),
                        last_error=str(exc),
                        last_run=_iso(_now_utc()),
                    )

            self._oneshot_thread = threading.Thread(
                target=_once, daemon=True, name="TradeRadarAgentOneShot"
            )
            self._oneshot_thread.start()
            logger.info("TradeRadarAgent one-shot refresh started (loop not running)")
            return True

    def request_refresh(self, force: bool = False, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        if not is_agent_enabled("trade_radar", config=self.config_manager.config, default=True):
            return {"status": "disabled", "force": bool(force), "symbols": []}
        symbols_norm = [_extract_symbol(s) for s in (symbols or []) if _extract_symbol(s)]
        accepted = False
        with self._lock:
            if force:
                self._pending_force = True
                accepted = True
            if symbols_norm:
                self._pending_symbols.update(symbols_norm)
                accepted = True
            if not force and not symbols_norm:
                now_ts = time.time()
                if now_ts - self._last_external_trigger_ts >= 60:
                    self._last_external_trigger_ts = now_ts
                    accepted = True

        ran_oneshot = False
        if accepted:
            self._wake_event.set()
            # If the continuous loop isn't running in this process, the wake event has
            # no consumer — run the cycle on demand so the refresh actually happens.
            if not self._loop_alive():
                ran_oneshot = self.run_once_async(force=force, symbols=symbols_norm)

        return {
            "status": "accepted" if accepted else "noop",
            "force": bool(force),
            "symbols": symbols_norm,
            "mode": "oneshot" if ran_oneshot else ("loop" if accepted else "noop"),
        }

    def update_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            cfg = dict(self._state.get("config", {}))
            cfg = self._sanitize_config(cfg, updates or {})
            self._state["config"] = cfg
            self._state["as_of"] = _iso(_now_utc())
            self._persist_locked()
        self._wake_event.set()
        return copy.deepcopy(cfg)

    def suppress_symbols(self, symbols: List[str], duration_sec: Optional[int] = None) -> Dict[str, Any]:
        symbols_norm = [_extract_symbol(s) for s in (symbols or []) if _extract_symbol(s)]
        if not symbols_norm:
            return {"suppressed": {}, "count": 0}

        with self._lock:
            cfg = self._state.get("config", {})
            sec = duration_sec if duration_sec is not None else int(cfg.get("suppression_default_sec", 3600))
            try:
                sec = max(60, int(sec))
            except Exception:
                sec = 3600
            until = _now_utc() + timedelta(seconds=sec)
            suppressed = dict(self._state.get("suppressed", {}) or {})
            for sym in symbols_norm:
                suppressed[sym] = _iso(until)
            self._state["suppressed"] = suppressed
            self._state["as_of"] = _iso(_now_utc())
            self._persist_locked()

        return {"suppressed": {s: _iso(until) for s in symbols_norm}, "count": len(symbols_norm)}

    def unsuppress_symbols(self, symbols: List[str]) -> Dict[str, Any]:
        """Lift a suppression early (the Undo path of the Suppress button).
        Pre-fix a mis-click hid nothing and could not be undone for an hour."""
        symbols_norm = [_extract_symbol(s) for s in (symbols or []) if _extract_symbol(s)]
        removed: List[str] = []
        with self._lock:
            suppressed = dict(self._state.get("suppressed", {}) or {})
            for sym in symbols_norm:
                if sym in suppressed:
                    suppressed.pop(sym, None)
                    removed.append(sym)
            if removed:
                self._state["suppressed"] = suppressed
                self._state["as_of"] = _iso(_now_utc())
                self._persist_locked()
            remaining = self._cleanup_suppressed_map(suppressed)
        return {"removed": removed, "count": len(removed), "suppressed": remaining}

    def _mark_cycle_done(self, started: float) -> None:
        """Record loop heartbeat and publish as_of for a cycle that completed."""
        with self._lock:
            self._last_cycle_done_ts = time.time()
            as_of = _parse_iso(self._state.get("as_of"))
            if as_of is None or as_of.timestamp() < started:
                self._state["as_of"] = _iso(_now_utc())
                self._persist_locked()

    def _watchdog_kick(self) -> None:
        """Run a one-shot cycle when state is stale and the loop is dead or wedged."""
        with self._lock:
            cfg = self._state.get("config", {}) or {}
            interval = max(10, int(_safe_float(cfg.get("radar_interval_sec"), 300.0) or 300))
            as_of = _parse_iso(self._state.get("as_of"))
        now = time.time()
        budget = max(900.0, 3.0 * interval)
        if as_of is not None and (now - as_of.timestamp()) < budget:
            return
        if now - self._last_watchdog_kick_ts < budget:
            return
        wedged = (now - self._last_cycle_done_ts) > budget
        if self._loop_alive() and not wedged:
            return
        if not is_agent_enabled("trade_radar", config=self.config_manager.config, default=True):
            return
        self._last_watchdog_kick_ts = now
        logger.warning(f"TradeRadarAgent stale state (loop_alive={self._loop_alive()}, wedged={wedged}); kicking one-shot")
        self.run_once_async(force=False)

    def _adopt_published_state(self) -> None:
        """Adopt state published by the scheduled radar loop (possibly another process) when newer."""
        if self._loop_alive():
            return
        disk = load_traderadar_state()
        if not isinstance(disk, dict) or not isinstance(disk.get("candidates"), list):
            return
        disk_ts = _parse_iso(disk.get("as_of"))
        with self._lock:
            mem_ts = _parse_iso(self._state.get("as_of"))
            if disk_ts is None or (mem_ts is not None and disk_ts <= mem_ts):
                return
            for key in ("as_of", "candidates", "meta", "suppressed"):
                if key in disk:
                    self._state[key] = copy.deepcopy(disk[key])
            self._last_cycle_done_ts = max(self._last_cycle_done_ts, disk_ts.timestamp())

    def get_state(self, limit: int = 50, include_suppressed: bool = True) -> Dict[str, Any]:
        try:
            self._adopt_published_state()
            self._watchdog_kick()
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(f"TradeRadarAgent watchdog error: {exc}")
        with self._lock:
            data = copy.deepcopy(self._state)
        if not include_suppressed:
            # UI serve path: a suppressed symbol must actually leave the list.
            filter_suppressed_candidates(data)
        candidates = data.get("candidates", []) or []
        try:
            lim = max(1, min(200, int(limit or 50)))
        except Exception:
            lim = 50

        def _rank(item: Dict[str, Any]) -> Tuple[float, float]:
            analysis = item.get("analysis", {}) if isinstance(item, dict) else {}
            conf = _safe_float((analysis or {}).get("confidence", 0.0), 0.0) or 0.0
            change_abs = abs(_safe_float((item or {}).get("change_pct", 0.0), 0.0) or 0.0)
            return conf, change_abs

        ordered = sorted([c for c in candidates if isinstance(c, dict)], key=_rank, reverse=True)
        # Serve-time honesty flag: when the scanner re-saw the symbol AFTER its
        # last analysis (move fresher than the verdict), the card mixes a new
        # % with an old read — mark it instead of letting it pass as current.
        cfg_now = data.get("config", {}) if isinstance(data.get("config"), dict) else {}
        base_threshold = _safe_float(cfg_now.get("threshold_pct"), 10.0) or 10.0
        stale_after_sec = (
            (_safe_float(cfg_now.get("cooldown_sec"), 600.0) or 600.0)
            + (_safe_float(cfg_now.get("radar_interval_sec"), 300.0) or 300.0)
        )
        for item in ordered:
            last_proc = _parse_iso(item.get("last_processed"))
            last_seen = _parse_iso(item.get("last_seen"))
            # A candidate the radar has not scored yet carries only the
            # placeholder analysis (NEUTRAL / 0 / default setup). Say so, so
            # the UI can show "analysis pending" instead of a scored-0 card.
            item["analysis_pending"] = last_proc is None
            # A candidate analysed before the negative-backtest rule existed
            # (or under another threshold) keeps its old READY until its next
            # enrichment - which never comes for a mover that left the scanner
            # list. Apply the current rule to what is served.
            if last_proc is not None:
                self._apply_backtest_verdict_on_read(item, cfg_now)
            # The scanner re-sights a mover every cycle while re-analysis waits
            # for the per-symbol cooldown, so an analysis up to one
            # cooldown + one cycle older than the last sighting is the normal
            # steady state, not a warning (live: 4 of 6 cards carried this
            # flag five minutes after being analysed). Flag only real lag.
            lag_ok = timedelta(seconds=stale_after_sec)
            if last_proc is not None and last_seen is not None and (last_seen - last_proc) <= lag_ok:
                pass
            elif last_seen is not None and (last_proc is None or last_seen > last_proc):
                flags = list(item.get("red_flags") or [])
                if "analysis_older_than_move" not in flags:
                    flags.append("analysis_older_than_move")
                    item["red_flags"] = flags
            # The move that put the symbol on the radar has since faded below
            # the intake threshold (price/% are kept current at intake, the
            # analysis is not re-run for a non-qualifying move).
            try:
                eff = self._effective_threshold(_extract_symbol(item.get("symbol")), cfg_now, base_threshold)
                move_now = abs(_safe_float(item.get("change_pct"), 0.0) or 0.0)
                if last_proc is not None and move_now < float(eff):
                    flags = list(item.get("red_flags") or [])
                    if "move_faded_below_threshold" not in flags:
                        flags.append("move_faded_below_threshold")
                        item["red_flags"] = flags
            except Exception:
                pass
        return {
            "as_of": data.get("as_of"),
            "config": data.get("config", {}),
            "suppressed": data.get("suppressed", {}),
            # 2026-09-03 audit: the UI reads meta.processed/queued ("Last
            # cycle: N enriched · N queued") — without this it always showed 0/0.
            "meta": data.get("meta", {}),
            "total_candidates": len(ordered),
            "candidates": ordered[:lim],
        }

    def _loop(self) -> None:
        if self._startup_delay_seconds > 0:
            self._wake_event.wait(timeout=self._startup_delay_seconds)
            self._wake_event.clear()
            if self._stop_event.is_set():
                return
        while not self._stop_event.is_set():
            started = time.time()
            force = False
            forced_symbols: List[str] = []
            with self._lock:
                force = bool(self._pending_force)
                forced_symbols = list(self._pending_symbols)
                self._pending_force = False
                self._pending_symbols.clear()

            # A cycle the user asked for (Force, or Refresh on a selected
            # symbol) must be able to reach the network: pre-fix it inherited
            # the automatic loop's cache-only context, so "Force" re-ran the
            # same cache-only pass and no price history/news was ever fetched.
            user_triggered = bool(force or forced_symbols)
            try:
                with request_context(
                    reason="AGENT_REFRESH_MANUAL" if user_triggered else "AGENT_REFRESH",
                    path="/agent/trade-radar",
                    cache_only=bool(self._auto_cache_only) and not user_triggered,
                ):
                    with track_agent_run("trade_radar"):
                        self.run_cycle(force_refresh=force, forced_symbols=forced_symbols)
                        self._mark_cycle_done(started)
            except Exception as e:
                logger.error(f"TradeRadarAgent cycle error: {e}")
                update_agent_status(
                    "trade_radar",
                    enabled=is_agent_enabled("trade_radar", config=self.config_manager.config, default=True),
                    last_error=str(e),
                    last_run=_iso(_now_utc()),
                )

            with self._lock:
                interval = int((self._state.get("config", {}) or {}).get("radar_interval_sec", 300))
            interval = max(10, interval)
            elapsed = time.time() - started
            wait_sec = max(1.0, interval - elapsed)

            self._wake_event.wait(timeout=wait_sec)
            self._wake_event.clear()

    def run_cycle(self, force_refresh: bool = False, forced_symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        started_perf = time.perf_counter()
        self.config_manager.reload_if_changed()
        if not is_agent_enabled("trade_radar", config=self.config_manager.config, default=True):
            duration_ms = (time.perf_counter() - started_perf) * 1000.0
            update_agent_status(
                "trade_radar",
                enabled=False,
                last_run=_iso(_now_utc()),
                next_run_in=None,
                avg_duration_ms=round(duration_ms, 2),
            )
            return {
                "status": "disabled",
                "as_of": _iso(_now_utc()),
                "config": copy.deepcopy(self._state.get("config", {})),
                "suppressed": copy.deepcopy(self._state.get("suppressed", {})),
                "candidates": copy.deepcopy(self._state.get("candidates", [])),
                "meta": {"processed": 0, "queued": 0, "scanner_candidates": 0},
            }

        forced = {_extract_symbol(s) for s in (forced_symbols or []) if _extract_symbol(s)}
        with self._lock:
            cfg = copy.deepcopy(self._state.get("config", {}))
            current_candidates = copy.deepcopy(self._state.get("candidates", []) or [])
            suppressed = dict(self._state.get("suppressed", {}) or {})

        cfg = self._sanitize_config(cfg, {})
        suppressed = self._cleanup_suppressed_map(suppressed)
        watch_map = self._load_watchlist_map()
        candidate_map = self._candidate_map(current_candidates)
        # Trade Radar audit TR-COR-04 (2026-07): expire ghost setups. A
        # candidate whose ``last_seen`` is older than the configured max age is
        # dropped before re-processing so a stalled refresh cycle (the
        # @staticmethod regression crashed it for months) cannot leave stale
        # candidates that look READY for moves long since passed. Default 24h;
        # set ``candidate_max_age_hours=0`` to disable (backward-compat).
        max_age_hours = _safe_float(cfg.get("candidate_max_age_hours"), 24.0) or 0.0
        if max_age_hours > 0:
            cutoff = _now_utc() - timedelta(hours=float(max_age_hours))
            expired = 0
            for sym in list(candidate_map.keys()):
                cand = candidate_map.get(sym) or {}
                ls = _parse_iso(cand.get("last_seen"))
                if ls is None or ls < cutoff:
                    candidate_map.pop(sym, None)
                    expired += 1
            if expired:
                logger.info(f"TradeRadar expired {expired} candidate(s) older than {max_age_hours}h")
        # Purge untradeable index symbols that predate the intake filter —
        # without this a persisted "^TNX SHORT" row survives until the 24h
        # ghost expiry even though new intake skips it.
        purged_indices = [sym for sym in candidate_map if sym.startswith("^")]
        for sym in purged_indices:
            candidate_map.pop(sym, None)
        if purged_indices:
            logger.info(f"TradeRadar purged {len(purged_indices)} untradeable index candidate(s): {purged_indices}")

        try:
            scanner_candidates = self._collect_scanner_candidates()
        except Exception as exc:
            logger.error(f"TradeRadar scanner intake failed; continuing with existing candidates: {exc}")
            scanner_candidates = []
        threshold = _safe_float(cfg.get("threshold_pct"), 10.0) or 10.0
        now_iso = _iso(_now_utc())
        seen_this_cycle: Set[str] = set()

        for item in scanner_candidates:
            symbol = _extract_symbol(item.get("symbol") or item.get("ticker"))
            if not symbol:
                continue
            # Market-data indices (^TNX, ^VIX, ^DJI…) are scanner context, not
            # tradeable candidates — live feed showed a "^TNX SHORT -49.7%"
            # radar row nobody could act on. Skip at intake.
            if symbol.startswith("^"):
                continue
            if self._is_suppressed(symbol, suppressed):
                continue
            change_pct = _safe_float(item.get("change_pct"), 0.0) or 0.0
            eff_threshold = self._effective_threshold(symbol, cfg, threshold)
            if abs(change_pct) < eff_threshold and symbol not in forced:
                # Still on the scanner but no longer a qualifying move. Keep
                # the card's price and % truthful (pre-fix they froze at the
                # last qualifying snapshot and the frozen % kept the symbol
                # "due", re-analysing stale numbers every cooldown). last_seen
                # is NOT advanced, so the card ages out on the normal expiry.
                existing = candidate_map.get(symbol)
                if existing is not None:
                    existing["change_pct"] = round(change_pct, 4)
                    live_price = _safe_float(item.get("price"), None)
                    if live_price is not None and live_price > 0:
                        existing["price"] = live_price
                continue
            seen_this_cycle.add(symbol)
            candidate = candidate_map.get(symbol) or self._new_candidate(symbol)
            candidate["source"] = str(item.get("source") or "scanner")
            candidate["change_pct"] = round(change_pct, 4)
            candidate["price"] = _safe_float(item.get("price"), _safe_float(candidate.get("price"), 0.0)) or 0.0
            candidate["volume"] = _safe_float(item.get("volume"), _safe_float(candidate.get("volume"), 0.0)) or 0.0
            candidate["last_seen"] = now_iso
            candidate_map[symbol] = candidate

        for symbol in forced:
            if symbol not in candidate_map and not self._is_suppressed(symbol, suppressed):
                base = self._new_candidate(symbol)
                wl = watch_map.get(symbol, {})
                base["source"] = "manual"
                base["change_pct"] = _safe_float(wl.get("change_pct"), 0.0) or 0.0
                base["price"] = _safe_float(wl.get("price"), 0.0) or 0.0
                base["volume"] = _safe_float(wl.get("volume"), 0.0) or 0.0
                base["last_seen"] = now_iso
                candidate_map[symbol] = base

        now = _now_utc()
        max_per_cycle = self._resolve_cycle_limit(
            cfg,
            manual=bool(force_refresh or forced),
        )
        due_symbols: List[str] = []
        for symbol, candidate in candidate_map.items():
            if self._is_suppressed(symbol, suppressed):
                continue
            # Automatic cycles only re-score symbols the scanner confirmed in
            # THIS cycle. A candidate that dropped off the scanner keeps its
            # last snapshot (and ages out); re-enriching it would stamp a fresh
            # "last_processed" on stale price/% data and burn the per-cycle
            # budget. Force / an explicitly requested symbol still re-scores.
            if not force_refresh and symbol not in forced and symbol not in seen_this_cycle:
                continue
            if self._is_due_for_processing(
                symbol=symbol,
                candidate=candidate,
                threshold=self._effective_threshold(symbol, cfg, threshold),
                now=now,
                forced=forced,
                force_refresh=force_refresh,
            ):
                due_symbols.append(symbol)

        # Processing fairness (2026-08 audit): rank due symbols by how long
        # they have waited, then by move size. The old |change|-only ranking
        # let the same big (often stale) movers monopolize the 4-per-cycle
        # auto budget every cooldown window, so smaller names went unprocessed
        # for a full day (live: QNRX analysis 24h old contradicting its own
        # current -6.9% move on the card).
        due_symbols = sorted(
            due_symbols,
            key=lambda s: self._due_rank(s, candidate_map, forced, now),
            reverse=True,
        )
        due_before_limit = len(due_symbols)
        due_symbols = due_symbols[:max_per_cycle]

        processed = 0
        for symbol in due_symbols:
            candidate = candidate_map.get(symbol)
            if not candidate:
                continue
            try:
                enriched = self._enrich_candidate(
                    symbol=symbol,
                    candidate=candidate,
                    watchlist_item=watch_map.get(symbol, {}),
                    cfg=cfg,
                    now=now,
                    force_refresh=force_refresh or symbol in forced,
                )
                candidate_map[symbol] = enriched
                processed += 1
            except Exception as e:
                logger.error(f"TradeRadar enrich failed for {symbol}: {e}")
                candidate = dict(candidate)
                red_flags = list(candidate.get("red_flags", []) or [])
                red_flags.append("enrichment_failed")
                candidate["red_flags"] = sorted(set(red_flags))
                # The previous readiness (possibly READY) described the last
                # successful analysis; with this pass failed it must not stay
                # actionable. Rebuild it with the failure as a hard blocker.
                prev_analysis = candidate.get("analysis") if isinstance(candidate.get("analysis"), dict) else {}
                try:
                    candidate["readiness"] = self._build_readiness(
                        verdict=str(prev_analysis.get("verdict") or "NEUTRAL").upper(),
                        confidence=_safe_float(prev_analysis.get("confidence"), 0.0) or 0.0,
                        entry=_safe_float(prev_analysis.get("entry"), 0.0) or 0.0,
                        tp=_safe_float(prev_analysis.get("tp"), 0.0) or 0.0,
                        sl=_safe_float(prev_analysis.get("sl"), 0.0) or 0.0,
                        red_flags=candidate["red_flags"],
                    )
                except Exception:
                    candidate["readiness"] = {
                        "status": "BLOCKED", "can_open": False, "close_to_open": False,
                        "blockers": ["enrichment_failed"],
                        "note": "Insufficient validated data for entry.",
                    }
                candidate["last_processed"] = _iso(now)
                candidate["cooldown_until"] = _iso(now + timedelta(seconds=int(cfg.get("cooldown_sec", 600))))
                candidate_map[symbol] = candidate

        max_candidates = max(5, int(_safe_float(cfg.get("max_candidates"), 50) or 50))
        candidates = list(candidate_map.values())
        candidates = sorted(
            candidates,
            key=lambda c: (
                1 if _parse_iso(c.get("last_processed")) else 0,
                abs(_safe_float(c.get("change_pct"), 0.0) or 0.0),
                _parse_iso(c.get("last_seen")) or datetime.min.replace(tzinfo=timezone.utc),
            ),
            reverse=True,
        )[:max_candidates]

        payload = {
            "as_of": _iso(_now_utc()),
            "config": cfg,
            "suppressed": suppressed,
            "candidates": candidates,
            "meta": {
                "processed": processed,
                # The backlog is what was due but not processed this cycle —
                # due_symbols is already truncated to the cycle limit, so the
                # un-truncated due count is what "queued" must report.
                "queued": max(0, due_before_limit - processed),
                "scanner_candidates": len(scanner_candidates),
            },
        }

        duration_ms = (time.perf_counter() - started_perf) * 1000.0
        with self._lock:
            self._state["as_of"] = payload["as_of"]
            self._state["config"] = payload["config"]
            self._state["suppressed"] = payload["suppressed"]
            self._state["candidates"] = payload["candidates"]
            meta = dict(self._state.get("meta", {}) or {})
            prev_avg = _safe_float(meta.get("avg_duration_ms"), None)
            avg = duration_ms if prev_avg is None else ((float(prev_avg) * 0.7) + (duration_ms * 0.3))
            meta.update(payload.get("meta", {}))
            meta["avg_duration_ms"] = round(avg, 2)
            self._state["meta"] = meta
            self._persist_locked()
        update_agent_status(
            "trade_radar",
            enabled=True,
            last_run=payload.get("as_of"),
            last_success=payload.get("as_of"),
            last_error=None,
            avg_duration_ms=round(avg, 2),
            next_run_in=int(cfg.get("radar_interval_sec", 300)),
        )
        return payload

    def _load_state(self) -> Dict[str, Any]:
        template = {
            "as_of": None,
            "config": dict(self.DEFAULT_CONFIG),
            "suppressed": {},
            "candidates": [],
            "meta": {},
        }
        stored = load_traderadar_state(default=template)
        if not isinstance(stored, dict):
            stored = {}

        merged = dict(template)
        merged["config"] = self._sanitize_config(dict(template["config"]), stored.get("config", {}))
        merged["suppressed"] = self._cleanup_suppressed_map(dict(stored.get("suppressed", {}) or {}))
        merged["candidates"] = [c for c in (stored.get("candidates", []) or []) if isinstance(c, dict)]
        merged["as_of"] = stored.get("as_of")
        merged["meta"] = stored.get("meta", {}) if isinstance(stored.get("meta"), dict) else {}
        return merged

    def _persist_locked(self) -> None:
        try:
            save_traderadar_state(self._state)
        except Exception as e:
            logger.error(f"TradeRadar state persist failed: {e}")

    @classmethod
    def _sanitize_config(cls, current: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
        cfg = dict(cls.DEFAULT_CONFIG)
        cfg.update(current or {})
        for key, value in (updates or {}).items():
            if key not in cls.DEFAULT_CONFIG:
                continue
            cfg[key] = value

        cfg["threshold_pct"] = _clamp(_safe_float(cfg.get("threshold_pct"), 10.0) or 10.0, 1.0, 80.0)
        cfg["radar_interval_sec"] = int(_clamp(_safe_float(cfg.get("radar_interval_sec"), 300) or 300, 30, 3600))
        cfg["cooldown_sec"] = int(_clamp(_safe_float(cfg.get("cooldown_sec"), 600) or 600, 30, 86400))
        cfg["max_candidates"] = int(_clamp(_safe_float(cfg.get("max_candidates"), 50) or 50, 5, 500))
        cfg["max_per_cycle"] = int(_clamp(_safe_float(cfg.get("max_per_cycle"), 10) or 10, 1, 50))
        cfg["auto_max_per_cycle"] = int(
            _clamp(_safe_float(cfg.get("auto_max_per_cycle"), 4) or 4, 1, 50)
        )
        cfg["ohlc_ttl_sec"] = int(_clamp(_safe_float(cfg.get("ohlc_ttl_sec"), 300) or 300, 30, 3600))
        cfg["news_ttl_sec"] = int(_clamp(_safe_float(cfg.get("news_ttl_sec"), 300) or 300, 30, 3600))
        cfg["calendar_ttl_sec"] = int(_clamp(_safe_float(cfg.get("calendar_ttl_sec"), 1800) or 1800, 60, 7200))
        cfg["calendar_window_days"] = int(_clamp(_safe_float(cfg.get("calendar_window_days"), 3) or 3, 1, 14))
        cfg["backtest_ttl_sec"] = int(_clamp(_safe_float(cfg.get("backtest_ttl_sec"), 1800) or 1800, 60, 86400))
        cfg["backtest_days"] = int(_clamp(_safe_float(cfg.get("backtest_days"), 30) or 30, 5, 120))
        cfg["backtest_negative_min_trades"] = int(
            _clamp(_safe_float(cfg.get("backtest_negative_min_trades"), 20) or 20, 1, 1000)
        )
        cfg["suppression_default_sec"] = int(
            _clamp(_safe_float(cfg.get("suppression_default_sec"), 3600) or 3600, 60, 604800)
        )
        cfg["enable_backtest"] = bool(cfg.get("enable_backtest", True))
        cfg["analysis_include_heavy"] = bool(cfg.get("analysis_include_heavy", False))
        cfg["analysis_disable_ollama"] = bool(cfg.get("analysis_disable_ollama", True))
        cfg["local_only_mode"] = bool(cfg.get("local_only_mode", False))
        cfg["allow_remote_on_miss"] = bool(cfg.get("allow_remote_on_miss", True))
        cfg["allow_analysis_remote"] = bool(cfg.get("allow_analysis_remote", True))
        cfg["allow_ohlc_remote"] = bool(cfg.get("allow_ohlc_remote", True))
        cfg["allow_news_refresh"] = bool(cfg.get("allow_news_refresh", True))
        cfg["allow_calendar_refresh"] = bool(cfg.get("allow_calendar_refresh", True))
        cfg["news_refresh_cooldown_sec"] = int(
            _clamp(_safe_float(cfg.get("news_refresh_cooldown_sec"), 120) or 120, 15, 3600)
        )

        if cfg["local_only_mode"]:
            cfg["allow_remote_on_miss"] = False
            cfg["allow_analysis_remote"] = False
            cfg["allow_ohlc_remote"] = False
            cfg["allow_news_refresh"] = False
            cfg["allow_calendar_refresh"] = False
        elif not cfg["allow_remote_on_miss"]:
            cfg["allow_analysis_remote"] = False
            cfg["allow_ohlc_remote"] = False
            cfg["allow_news_refresh"] = False
            cfg["allow_calendar_refresh"] = False
        return cfg

    @staticmethod
    def _resolve_cycle_limit(cfg: Dict[str, Any], *, manual: bool) -> int:
        configured = max(1, int(_safe_float(cfg.get("max_per_cycle"), 10) or 10))
        if manual:
            return configured
        automatic = max(
            1,
            int(_safe_float(cfg.get("auto_max_per_cycle"), 4) or 4),
        )
        try:
            runtime_cap = max(
                1,
                min(
                    50,
                    int(os.getenv("MS_TRADE_RADAR_AUTO_MAX_PER_CYCLE", "4") or 4),
                ),
            )
        except (TypeError, ValueError):
            runtime_cap = 4
        return min(configured, automatic, runtime_cap)

    @staticmethod
    def _analysis_needs_remote(analysis_raw: Dict[str, Any]) -> bool:
        if not isinstance(analysis_raw, dict):
            return True
        best = analysis_raw.get("best_signal", {})
        if not isinstance(best, dict):
            return True
        direction = str(best.get("direction") or "").upper()
        mode = str(best.get("mode") or "").upper()
        score = _safe_float(best.get("score"), None)
        if direction not in {"BUY", "SELL", "LONG", "SHORT", "BULLISH", "BEARISH"}:
            return True
        if mode and mode not in {"PULLBACK", "BREAKOUT", "CONTRA", "ORB_FVG"}:
            return True
        if score is None:
            return True
        return False

    def _collect_scanner_candidates(self) -> List[Dict[str, Any]]:
        try:
            from agents.scanner_agent import get_scanner_agent

            scanner = get_scanner_agent(self.config_manager)
            # Important: do NOT trigger scanner refresh here.
            # Trade Radar must consume scanner's cached output only.
            hot: List[Dict[str, Any]] = []
            lock = getattr(scanner, "_state_lock", None)
            if lock is not None:
                with lock:
                    raw = getattr(scanner, "cached_hot_list", []) or []
                    hot = [x for x in raw if isinstance(x, dict)]
            else:
                raw = getattr(scanner, "cached_hot_list", []) or []
                hot = [x for x in raw if isinstance(x, dict)]
            return hot
        except Exception as e:
            logger.error(f"TradeRadar scanner source failed: {e}")
            return []

    def _load_watchlist_map(self) -> Dict[str, Dict[str, Any]]:
        try:
            state = get_ui_state(include_news=False, include_hotlist=False, lean=True) or {}
            rows = state.get("tickers", []) if isinstance(state, dict) else []
            out: Dict[str, Dict[str, Any]] = {}
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                symbol = _extract_symbol(row.get("ticker"))
                if not symbol:
                    continue
                out[symbol] = row
            return out
        except Exception as e:
            logger.warning(f"TradeRadar watchlist snapshot unavailable: {e}")
            return {}

    @staticmethod
    def _candidate_map(candidates: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for item in candidates or []:
            if not isinstance(item, dict):
                continue
            symbol = _extract_symbol(item.get("symbol"))
            if not symbol:
                continue
            out[symbol] = item
        return out

    @staticmethod
    def _new_candidate(symbol: str) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "source": "scanner",
            "change_pct": 0.0,
            "price": 0.0,
            "volume": 0.0,
            "last_seen": None,
            "last_processed": None,
            "cooldown_until": None,
            "analysis": {
                "verdict": "NEUTRAL",
                "confidence": 0,
                "time_horizon": "intraday",
                "strategy": {"risk_mode": "MEDIUM", "trade_mode": "PULLBACK"},
                "entry": 0.0,
                "tp": 0.0,
                "sl": 0.0,
                "strength": 0,
                "key_indicators": {"rsi": None, "adx": None, "macd_hist": None, "atr": None},
            },
            "news": {
                "available": False,
                "bullish_count": 0,
                "bearish_count": 0,
                "top": [],
                "last_update": None,
            },
            "calendar": {
                "available": False,
                "earnings": {},
                "macro_next_72h": [],
                "last_update": None,
            },
            "backtest": {
                "available": False,
                "days": 0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "max_drawdown": 0.0,
                "closed_trades": 0,
                "signals_traded": 0,
                "open_trades": 0,
                "variant_used": "RADAR_QUICK",
                "fallback_used": False,
                "last_update": None,
            },
            "reasons": [],
            "red_flags": [],
            "readiness": {
                # Not analysed yet — distinct from BLOCKED (analysed and
                # failed a check). can_open stays False either way.
                "status": "PENDING",
                "can_open": False,
                "close_to_open": False,
                "distance_to_open": 60.0,
                "entry_confidence_threshold": 60,
                "checks": {
                    "has_direction": False,
                    "has_trade_levels": False,
                    "no_hard_blockers": False,
                    "confidence_ready": False,
                },
                "blockers": [],
                "note": "Queued for analysis — not scored yet.",
            },
            "data_freshness": {
                "scanner_ts": None,
                "ohlc_ts": None,
                "news_ts": None,
                "calendar_ts": None,
                "backtest_ts": None,
            },
        }

    @staticmethod
    def _effective_threshold(symbol: str, cfg: Dict[str, Any], base: float) -> float:
        """Asset-class-adaptive move threshold (display-only, win-rate-neutral:
        Trade Radar never opens trades -- Q3 contract tested in
        test_trade_radar_audit_fixes.py). A flat 10% threshold over-reports
        routine crypto pumps (crypto naturally moves more) and under-reports
        meaningful stock moves. Per-class config overrides
        ``threshold_pct_crypto`` / ``threshold_pct_stock`` take precedence;
        absent an override, crypto defaults to max(base, 15%) and stocks to
        min(base, 5%) -- so the user's ``threshold_pct`` still bounds the
        adaptive value (floor for crypto, ceiling for stocks) and setting
        both overrides to ``base`` restores the legacy flat behaviour."""
        try:
            if is_crypto_ticker(symbol):
                ov = _safe_float(cfg.get("threshold_pct_crypto"), None)
                if ov is not None and float(ov) > 0:
                    return float(ov)
                return max(float(base), 15.0)
            ov = _safe_float(cfg.get("threshold_pct_stock"), None)
            if ov is not None and float(ov) > 0:
                return float(ov)
            return min(float(base), 5.0)
        except Exception:
            return float(base)

    @staticmethod
    def _due_rank(
        symbol: str,
        candidate_map: Dict[str, Dict[str, Any]],
        forced: Set[str],
        now: datetime,
    ) -> Tuple[int, float, float]:
        """Rank key for due symbols: forced first, then longest-waiting, then
        biggest move. See run_cycle for the starvation this rotation fixes."""
        cand = candidate_map.get(symbol) or {}
        last_proc = _parse_iso(cand.get("last_processed"))
        waited = (now - last_proc).total_seconds() if last_proc else float("inf")
        move = abs(_safe_float(cand.get("change_pct"), 0.0) or 0.0)
        return (1 if symbol in forced else 0, waited, move)

    @staticmethod
    def _is_due_for_processing(
        symbol: str,
        candidate: Dict[str, Any],
        threshold: float,
        now: datetime,
        forced: Set[str],
        force_refresh: bool,
    ) -> bool:
        if symbol in forced or force_refresh:
            return True
        change_pct = abs(_safe_float(candidate.get("change_pct"), 0.0) or 0.0)
        if change_pct < threshold:
            return False
        last_processed = _parse_iso(candidate.get("last_processed"))
        if not last_processed:
            return True
        cooldown_until = _parse_iso(candidate.get("cooldown_until"))
        if cooldown_until and now < cooldown_until:
            return False
        return True

    def _enrich_candidate(
        self,
        symbol: str,
        candidate: Dict[str, Any],
        watchlist_item: Dict[str, Any],
        cfg: Dict[str, Any],
        now: datetime,
        force_refresh: bool,
    ) -> Dict[str, Any]:
        out = copy.deepcopy(candidate)
        watchlist_item = watchlist_item if isinstance(watchlist_item, dict) else {}
        red_flags: List[str] = []
        local_only = bool(cfg.get("local_only_mode", True))
        allow_remote_global = bool(cfg.get("allow_remote_on_miss", False)) and not local_only
        allow_analysis_remote = allow_remote_global and bool(cfg.get("allow_analysis_remote", False))
        allow_ohlc_remote = allow_remote_global and bool(cfg.get("allow_ohlc_remote", False))
        allow_news_refresh = allow_remote_global and bool(cfg.get("allow_news_refresh", False))
        allow_calendar_refresh = allow_remote_global and bool(cfg.get("allow_calendar_refresh", False))
        if local_only:
            red_flags.append("local_cache_only_mode")

        scanner_ts = out.get("last_seen")
        price = _safe_float(out.get("price"), _safe_float(watchlist_item.get("price"), 0.0)) or 0.0
        change_pct = _safe_float(out.get("change_pct"), _safe_float(watchlist_item.get("change_pct"), 0.0)) or 0.0
        volume = _safe_float(out.get("volume"), _safe_float(watchlist_item.get("volume"), 0.0)) or 0.0
        out["price"] = price
        out["change_pct"] = round(change_pct, 4)
        out["volume"] = volume

        rsi = _finite(_safe_float(watchlist_item.get("rsi"), None))
        macd_hist = _finite(_safe_float(watchlist_item.get("macd_hist"), None))
        atr = _finite(_safe_float(watchlist_item.get("atr"), None))
        adx = _finite(self._extract_adx(watchlist_item.get("adx")))
        indicator_timeframe: Optional[str] = None

        need_ohlc = (
            rsi is None
            or macd_hist is None
            or atr is None
            or adx is None
            or bool(cfg.get("enable_backtest", True))
        )
        ohlc_ts = None
        ohlc_df = None
        if need_ohlc:
            ohlc_df, ohlc_ts, ohlc_warn = self._get_ohlc_cached(
                symbol=symbol,
                is_crypto=is_crypto_ticker(symbol),
                days=int(cfg.get("backtest_days", 30)),
                ttl_sec=int(cfg.get("ohlc_ttl_sec", 300)),
                allow_remote=allow_ohlc_remote,
                force_refresh=bool(force_refresh),
            )
            if ohlc_warn:
                red_flags.extend(self._normalize_ohlc_warnings(ohlc_warn))
            if ohlc_df is None:
                red_flags.append("missing_ohlc")
            else:
                # A frame that already carries indicator columns (precomputed
                # by the caller) is read as-is; otherwise the indicators are
                # computed here from the bars. Pre-fix neither OHLC source
                # produced these columns (the remote frame is raw OHLC and the
                # local-cache path called a method that does not exist), so
                # every off-watchlist candidate read "indicators unavailable"
                # even with a full month of bars in hand.
                last = ohlc_df.iloc[-1] if len(ohlc_df.index) > 0 else None
                if last is not None:
                    if rsi is None:
                        rsi = _finite(last.get("rsi"))
                    if macd_hist is None:
                        macd_hist = _finite(last.get("macd_hist"))
                    if atr is None:
                        atr = _finite(last.get("atr"))
                    if adx is None:
                        adx = _finite(last.get("adx"))
                if rsi is None or macd_hist is None or atr is None or adx is None:
                    snap = self._indicator_snapshot(ohlc_df)
                    if rsi is None:
                        rsi = snap.get("rsi")
                    if macd_hist is None:
                        macd_hist = snap.get("macd_hist")
                    if atr is None:
                        atr = snap.get("atr")
                    if adx is None:
                        adx = snap.get("adx")
                    indicator_timeframe = snap.get("timeframe")

        analysis_raw = self._build_local_analysis_stub(
            symbol=symbol,
            candidate=out,
            watchlist_item=watchlist_item,
            price=price,
            change_pct=change_pct,
            atr=atr,
        )
        used_remote_analysis = False
        if self._analysis_needs_remote(analysis_raw):
            if allow_analysis_remote:
                try:
                    remote_analysis = actions_service.analyze_ticker(
                        symbol,
                        include_heavy=bool(cfg.get("analysis_include_heavy", False)),
                        disable_ollama=bool(cfg.get("analysis_disable_ollama", True)),
                        force_refresh=force_refresh,
                    )
                    if isinstance(remote_analysis, dict):
                        analysis_raw = remote_analysis
                        used_remote_analysis = True
                    else:
                        red_flags.append("analysis_remote_empty")
                except Exception as analysis_error:
                    red_flags.append("analysis_remote_failed")
                    logger.warning(f"TradeRadar remote analysis failed for {symbol}: {analysis_error}")
            else:
                red_flags.append("analysis_local_insufficient")
        elif local_only:
            red_flags.append("analysis_local_cache_only")
        if used_remote_analysis:
            red_flags.append("analysis_remote_fallback")

        verdict, strength, trade_mode = self._derive_signal_direction(analysis_raw, change_pct)
        strength_estimated = bool((analysis_raw or {}).get("estimated"))
        if strength_estimated:
            red_flags.append("analysis_strength_estimated")
        mode_source = "analysis" if used_remote_analysis else str(
            (analysis_raw or {}).get("mode_source") or "default"
        )
        setup_label = self._setup_label(verdict, trade_mode, mode_source, change_pct)
        has_real_atr = isinstance(atr, (int, float)) and atr > 0
        levels_basis = "analysis" if used_remote_analysis else ("atr" if has_real_atr else "default_pct")
        entry, tp, sl = self._derive_trade_levels(
            analysis_raw=analysis_raw,
            verdict=verdict,
            price=price,
            atr=atr,
        )

        news_pack, news_meta = self._build_news_pack(
            symbol=symbol,
            cfg=cfg,
            force_refresh=force_refresh,
            allow_remote_refresh=allow_news_refresh,
        )
        calendar_pack, calendar_meta = self._build_calendar_pack(
            symbol=symbol,
            cfg=cfg,
            force_refresh=force_refresh,
            allow_remote_refresh=allow_calendar_refresh,
        )

        backtest_pack = out.get("backtest", {}) if isinstance(out.get("backtest"), dict) else {}
        should_run_backtest = self._should_run_backtest(backtest_pack, cfg, force_refresh=force_refresh)
        # An "unavailable" result is not worth keeping for the full TTL once
        # the bars are in hand (it used to pin "backtest unavailable" on the
        # card for 30 minutes after price history came back).
        if ohlc_df is not None and not backtest_pack.get("available"):
            should_run_backtest = True
        if cfg.get("enable_backtest", True):
            if should_run_backtest and ohlc_df is not None:
                backtest_pack = self._run_backtest_lite(
                    symbol=symbol,
                    df=ohlc_df,
                    trade_mode=trade_mode,
                    days=int(cfg.get("backtest_days", 30)),
                )
            elif should_run_backtest and ohlc_df is None:
                backtest_pack = {
                    "available": False,
                    "unavailable_reason": "no_price_history",
                    "days": int(cfg.get("backtest_days", 30)),
                    "win_rate": 0.0,
                    "profit_factor": 0.0,
                    "max_drawdown": 0.0,
                    "closed_trades": 0,
                    "signals_traded": 0,
                    "open_trades": 0,
                    "variant_used": "RADAR_QUICK",
                    "fallback_used": False,
                    "last_update": _iso(now),
                }
                red_flags.append("backtest_missing_ohlc")
        else:
            backtest_pack = {
                "available": False,
                "unavailable_reason": "disabled",
                "days": int(cfg.get("backtest_days", 30)),
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "max_drawdown": 0.0,
                "closed_trades": 0,
                "signals_traded": 0,
                "open_trades": 0,
                "variant_used": "RADAR_QUICK",
                "fallback_used": False,
                "last_update": _iso(now),
            }

        # Negative-backtest verdict, recomputed on every pass (a pack reused
        # from the TTL cache predates these fields).
        backtest_pack = self._annotate_backtest_verdict(backtest_pack, cfg)

        confidence_info = self._compute_confidence(
            verdict=verdict,
            strength=strength,
            news_pack=news_pack,
            calendar_pack=calendar_pack,
            backtest_pack=backtest_pack,
        )
        confidence = float(confidence_info.get("final_score", 0.0) or 0.0)
        # Evidence cap (2026-08 audit): a verdict whose technical evidence is
        # absent (all key indicators null, or the strength itself was estimated
        # from the move) must not display high-band confidence — live rows read
        # conf 80 with every indicator missing. Capped below the 60 entry gate;
        # the breakdown keeps the raw formula math and records the applied cap.
        indicators_missing = rsi is None and atr is None and adx is None
        hard_evidence_missing = indicators_missing or "analysis_strength_estimated" in red_flags
        if hard_evidence_missing and confidence > 59.0:
            confidence = 59.0
            confidence_info["evidence_cap"] = 59.0
            # Name the real cause: with price history flowing, the usual
            # reason is an estimated strength, not missing indicators.
            cap_causes: List[str] = []
            if indicators_missing:
                cap_causes.append("key indicators missing")
            if "analysis_strength_estimated" in red_flags:
                cap_causes.append("strength estimated")
            confidence_info["evidence_cap_cause"] = ", ".join(cap_causes)
        if backtest_pack.get("available") and int(_safe_float(backtest_pack.get("closed_trades"), 0) or 0) == 0:
            red_flags.append("backtest_no_trades")
        time_horizon = self._derive_time_horizon(change_pct=change_pct, adx=adx)

        reasons = self._build_reasons(
            symbol=symbol,
            change_pct=change_pct,
            threshold=self._effective_threshold(symbol, cfg, float(cfg.get("threshold_pct", 10.0))),
            verdict=verdict,
            trade_mode=trade_mode,
            strength=strength,
            rsi=rsi,
            adx=adx,
            news_pack=news_pack,
            backtest_pack=backtest_pack,
            confidence_info=confidence_info,
            strength_estimated=strength_estimated,
            indicator_timeframe=indicator_timeframe,
        )
        red_flags.extend(self._build_red_flags(verdict, news_pack, calendar_pack, backtest_pack, price, change_pct))
        # Trade Radar audit TR-COR-03 (2026-07): a candidate whose key
        # indicators are ALL null (rsi/adx/atr None -- OHLC present but too short
        # to compute, or stale cache) must not reach READY. Pre-fix such
        # candidates passed has_trade_levels via the synthetic atr=price*0.02
        # fallback in _build_local_analysis_stub and had no hard blocker, so 29/50
        # live candidates showed READY with every key_indicator null. Flag them
        # so _build_readiness hard-blocks. Display-only (no signal opens here).
        if rsi is None and atr is None and adx is None:
            red_flags.append("missing_indicators")
        # Keep hard blockers ahead of informational flags before truncating:
        # the old alphabetical cut at 10 could drop a late-sorting blocker
        # (ohlc_local_cache_insufficient, missing_price...) so readiness never
        # saw it.
        unique_flags = sorted(set([rf for rf in red_flags if rf]))
        clean_red_flags = (
            [rf for rf in unique_flags if rf in self.HARD_BLOCKERS]
            + [rf for rf in unique_flags if rf not in self.HARD_BLOCKERS]
        )[:14]

        analysis_payload = {
            "verdict": verdict,
            "confidence": int(round(confidence)),
            "time_horizon": time_horizon,
            # trade_mode is the mode the backtest-lite runs with. When no
            # strategy supplied it (trade_mode_source == "default") it is only
            # a placeholder, so setup_label carries what the card may truthfully
            # call the setup (a short that follows a -58% move is MOMENTUM, not
            # a PULLBACK).
            "strategy": {
                "risk_mode": "MEDIUM",
                "trade_mode": trade_mode,
                "trade_mode_source": mode_source,
                "setup_label": setup_label,
            },
            # Preserve analytical precision for low-price assets. Rounding to
            # four decimals can collapse a stop/target into entry (or zero).
            "entry": float(entry) if entry is not None else 0.0,
            "tp": float(tp) if tp is not None else 0.0,
            "sl": float(sl) if sl is not None else 0.0,
            # "atr": levels are entry -/+ ATR multiples from real bars;
            # "default_pct": no ATR was available, levels use a flat 2%
            # volatility assumption (not a measurement); "analysis": levels
            # came from the full ticker analysis.
            "levels_basis": levels_basis,
            "strength": int(round(_clamp(strength, 0.0, 100.0))),
            "strength_estimated": strength_estimated,
            "indicator_timeframe": indicator_timeframe,
            "key_indicators": {
                "rsi": round(rsi, 3) if isinstance(rsi, (int, float)) else None,
                "adx": round(adx, 3) if isinstance(adx, (int, float)) else None,
                "macd_hist": round(macd_hist, 6) if isinstance(macd_hist, (int, float)) else None,
                "atr": round(atr, 6) if isinstance(atr, (int, float)) else None,
            },
            "confidence_breakdown": {
                "base_strength": round(float(confidence_info.get("base", 0.0) or 0.0), 2),
                "news_alignment_bonus": round(float(confidence_info.get("news_bonus", 0.0) or 0.0), 2),
                "calendar_risk_penalty": round(float(confidence_info.get("calendar_penalty", 0.0) or 0.0), 2),
                "backtest_bonus": round(float(confidence_info.get("backtest_bonus", 0.0) or 0.0), 2),
                "final_confidence": int(round(confidence)),
                "evidence_cap": confidence_info.get("evidence_cap"),
                "formula": "base + news - calendar + backtest"
                + (
                    f" (capped: {confidence_info.get('evidence_cap_cause') or 'key indicators missing'})"
                    if confidence_info.get("evidence_cap") is not None
                    else ""
                ),
                # Which inputs were actually measured — a 0.0 component with
                # its input unavailable is "not assessed", not "neutral".
                "base_estimated": strength_estimated,
                "news_assessed": bool(news_pack.get("available")),
                # Macro events are the part of the calendar every symbol is
                # exposed to; a populated earnings feed alone does not make a
                # crypto pair's (or a stock's macro) event risk "assessed".
                "calendar_assessed": bool(
                    calendar_pack.get("available")
                    or calendar_pack.get("macro_checked", calendar_pack.get("checked"))
                ),
                "backtest_assessed": bool(backtest_pack.get("available"))
                and int(_safe_float(backtest_pack.get("closed_trades"), 0) or 0) > 0,
            },
        }
        readiness = self._build_readiness(
            verdict=verdict,
            confidence=confidence,
            entry=entry,
            tp=tp,
            sl=sl,
            red_flags=clean_red_flags,
        )

        out["analysis"] = analysis_payload
        out["news"] = news_pack
        out["calendar"] = calendar_pack
        out["backtest"] = backtest_pack
        out["readiness"] = readiness
        out["reasons"] = reasons[:7]
        out["red_flags"] = clean_red_flags

        out["data_freshness"] = {
            "scanner_ts": scanner_ts,
            "ohlc_ts": ohlc_ts,
            "news_ts": news_meta.get("news_ts"),
            "calendar_ts": calendar_meta.get("calendar_ts"),
            "backtest_ts": backtest_pack.get("last_update"),
        }

        out["last_processed"] = _iso(now)
        out["cooldown_until"] = _iso(now + timedelta(seconds=int(cfg.get("cooldown_sec", 600))))
        return out

    @staticmethod
    def _extract_adx(raw: Any) -> Optional[float]:
        if isinstance(raw, dict):
            if "adx" in raw:
                return _safe_float(raw.get("adx"), None)
            if "value" in raw:
                return _safe_float(raw.get("value"), None)
        return _safe_float(raw, None)

    def _get_ohlc_cached(
        self,
        symbol: str,
        is_crypto: bool,
        days: int,
        ttl_sec: int,
        allow_remote: bool,
        force_refresh: bool = False,
    ) -> Tuple[Any, Optional[str], List[str]]:
        now_ts = time.time()
        key = f"{symbol}|{days}"
        entry = self._ohlc_cache.get(key)
        if entry:
            age = now_ts - float(entry.get("ts", 0.0))
            has_df = entry.get("df") is not None
            # A cached MISS is honoured only briefly and never against an
            # explicit refresh — pre-fix a cache-only pass stored "no data" for
            # the full TTL and the user's Force a minute later was served that
            # miss without ever trying the network.
            ttl = max(30, ttl_sec) if has_df else 60
            if age < ttl and not (force_refresh and (not has_df or age >= 60)):
                return entry.get("df"), entry.get("ts_iso"), list(entry.get("warnings", []) or [])

        warnings: List[str] = []
        local_df, local_warnings = self._load_ohlc_from_local_cache(symbol=symbol, days=days)
        warnings.extend(local_warnings)
        if local_df is not None:
            ts_iso = _iso(_now_utc())
            self._ohlc_cache[key] = {"ts": now_ts, "ts_iso": ts_iso, "df": local_df, "warnings": warnings}
            return local_df, ts_iso, warnings

        if not allow_remote:
            ts_iso = _iso(_now_utc())
            self._ohlc_cache[key] = {"ts": now_ts, "ts_iso": ts_iso, "df": None, "warnings": warnings}
            return None, ts_iso, warnings

        try:
            with self._ohlc_fetch_context():
                if is_crypto:
                    # Crypto pairs live on the exchange, never on Yahoo: 5-minute
                    # klines straight from Binance (KuCoin as fallback), the
                    # same bar size the stock path uses.
                    df, w = self._fetch_crypto_ohlc(symbol=symbol, days=max(5, int(days)))
                else:
                    df, w = self._quick_backtester.fetch_backtest_data(
                        ticker=symbol,
                        is_crypto=False,
                        days=max(5, int(days)),
                        ui_interval="5min",
                    )
            warnings.extend([str(x) for x in (w or []) if str(x).strip()])
            if df is not None:
                # The bars arrived; "local cache miss" is no longer a caveat
                # about this card's data and was shown to the user as a red flag.
                warnings = [x for x in warnings if "ohlc_local_cache_miss" not in str(x)]
            ts_iso = _iso(_now_utc())
            self._ohlc_cache[key] = {"ts": now_ts, "ts_iso": ts_iso, "df": df, "warnings": warnings}
            return df, ts_iso, warnings
        except Exception as e:
            warnings.append(f"ohlc_fetch_error:{e}")
            self._ohlc_cache[key] = {"ts": now_ts, "ts_iso": _iso(_now_utc()), "df": None, "warnings": warnings}
            return None, self._ohlc_cache[key]["ts_iso"], warnings

    def _ohlc_fetch_context(self):
        """Request context for the price-history fetch.

        Inside the automatic loop's cache-only context the kline call would be
        refused by the HTTP guard; the bounded OHLC fetch is deliberately let
        through (see ``_auto_ohlc_remote``). Outside a cache-only context this
        is a no-op."""
        ctx = get_request_context()
        if (
            ctx is not None
            and bool(getattr(ctx, "cache_only", False))
            and bool(getattr(self, "_auto_ohlc_remote", True))
        ):
            return request_context(
                reason="AGENT_REFRESH_OHLC",
                path="/agent/trade-radar/ohlc",
                cache_only=False,
                agent_name=getattr(ctx, "agent_name", None),
                external_budget=getattr(ctx, "external_budget", None),
            )
        return nullcontext()

    @staticmethod
    def _fetch_crypto_ohlc(symbol: str, days: int) -> Tuple[Any, List[str]]:
        """5-minute OHLC for a crypto pair from Binance spot klines (KuCoin as
        fallback). Returns (DataFrame|None, warnings). The frame has a UTC
        DatetimeIndex plus open/high/low/close/volume/timestamp columns."""
        import pandas as pd

        warnings: List[str] = []
        pair = _to_binance_pair(symbol)
        bars_needed = max(300, min(int(days) * 288, 12000))
        rows: List[Any] = []
        end_time: Optional[int] = None
        for _ in range(12):
            params: Dict[str, Any] = {"symbol": pair, "interval": "5m", "limit": 1000}
            if end_time is not None:
                params["endTime"] = end_time
            resp = http_get(
                BINANCE_KLINES_URL,
                provider="binance",
                params=params,
                timeout=(2, 6),
                max_retries=1,
            )
            if resp is None:
                warnings.append("ohlc_remote_blocked:binance")
                break
            if resp.status_code != 200:
                warnings.append(f"ohlc_remote_http_{resp.status_code}:binance")
                break
            data = resp.json()
            if not isinstance(data, list) or not data:
                break
            rows = data + rows
            end_time = int(data[0][0]) - 1
            if len(rows) >= bars_needed or len(data) < 1000:
                break

        df = None
        if rows:
            df = pd.DataFrame(
                [
                    {
                        "open_time": int(k[0]),
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                    }
                    for k in rows
                ]
            )
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            df = df.drop_duplicates(subset=["open_time"], keep="last").set_index("open_time").sort_index()
            df.attrs["radar_ohlc_source"] = "binance"
        else:
            try:
                from api.market_data_service import KuCoinClient

                kdf = KuCoinClient.fetch_klines(symbol, interval="5min", limit=1500)
            except Exception as exc:  # pragma: no cover - defensive
                kdf = None
                warnings.append(f"ohlc_fetch_error:kucoin:{exc}")
            if kdf is not None and not getattr(kdf, "empty", True):
                df = kdf.copy()
                df.columns = [str(c).lower() for c in df.columns]
                df = df.sort_index()
                df.attrs["radar_ohlc_source"] = "kucoin"

        if df is None or df.empty:
            warnings.append(f"No historical data for {symbol}")
            return None, warnings
        df["timestamp"] = df.index
        df.attrs["radar_ohlc_interval"] = "5m"
        return df, warnings

    @staticmethod
    def _indicator_snapshot(df: Any) -> Dict[str, Any]:
        """Latest RSI(14) / MACD histogram (12,26,9) / ATR(14) / ADX(14) from an
        OHLC frame, on 5-minute bars (finer bars are resampled). Every value is
        a finite float or None — never NaN, never a default."""
        out: Dict[str, Any] = {"rsi": None, "macd_hist": None, "atr": None, "adx": None, "timeframe": None, "bars": 0}
        try:
            import pandas as pd

            from core.scoring_system import calculate_atr
            from core.ta import TechnicalAnalysis

            if df is None or getattr(df, "empty", True):
                return out
            cols = {str(c).lower(): c for c in df.columns}
            need = ["open", "high", "low", "close"]
            if not all(c in cols for c in need):
                return out
            if isinstance(df.index, pd.DatetimeIndex):
                idx: Any = df.index
            elif "timestamp" in cols:
                idx = pd.DatetimeIndex(pd.to_datetime(df[cols["timestamp"]], errors="coerce", utc=True))
            else:
                idx = None
            frame = pd.DataFrame(
                {c: pd.to_numeric(df[cols[c]], errors="coerce").to_numpy() for c in need},
                index=idx if idx is not None else pd.RangeIndex(len(df)),
            ).dropna()
            timeframe: Optional[str] = None
            if isinstance(frame.index, pd.DatetimeIndex) and len(frame) > 3:
                frame = frame[~frame.index.isna()].sort_index()
                step = frame.index.to_series().diff().dt.total_seconds().median()
                if step == step and step > 0:
                    if step < 299:
                        frame = frame.resample("5min").agg(
                            {"open": "first", "high": "max", "low": "min", "close": "last"}
                        ).dropna()
                        step = 300.0
                    if step >= 86400:
                        timeframe = f"{int(round(step / 86400))}d"
                    elif step >= 3600:
                        timeframe = f"{int(round(step / 3600))}h"
                    else:
                        timeframe = f"{int(round(step / 60))}m"
            out["bars"] = int(len(frame))
            out["timeframe"] = timeframe
            if len(frame) < 35:
                return out
            rsi_s = TechnicalAnalysis.calc_rsi(frame["close"], 14)
            _macd, _sig, hist_s = TechnicalAnalysis.calc_macd(frame["close"], 12, 26, 9)
            atr_s = calculate_atr(frame, period=14)
            adx_s = TechnicalAnalysis.calc_adx(frame["high"], frame["low"], frame["close"], 14)
            out["rsi"] = _finite(rsi_s.iloc[-1]) if len(rsi_s) else None
            out["macd_hist"] = _finite(hist_s.iloc[-1]) if len(hist_s) else None
            atr_v = _finite(atr_s.iloc[-1]) if len(atr_s) else None
            out["atr"] = atr_v if (atr_v is not None and atr_v > 0) else None
            out["adx"] = _finite(adx_s.iloc[-1]) if len(adx_s) else None
        except Exception as exc:
            logger.warning(f"TradeRadar indicator snapshot failed: {exc}")
        return out

    @staticmethod
    def _setup_label(verdict: str, trade_mode: str, mode_source: str, change_pct: float) -> str:
        """What the card may truthfully call the setup. A mode supplied by a
        strategy/analysis is shown as-is; the placeholder mode is replaced by a
        description of what the verdict actually is relative to the move."""
        if mode_source in {"watchlist_strategy", "analysis"}:
            return str(trade_mode or "").upper() or "UNCLASSIFIED"
        if verdict not in {"LONG", "SHORT"}:
            return "UNCLASSIFIED"
        with_move = (verdict == "LONG" and change_pct > 0) or (verdict == "SHORT" and change_pct < 0)
        return "MOMENTUM" if with_move else "PULLBACK"

    @staticmethod
    def _frame_span_days(df: Any) -> Optional[float]:
        """Calendar span the bars actually cover (None when it cannot be read)."""
        try:
            import pandas as pd

            if df is None or getattr(df, "empty", True):
                return None
            if isinstance(df.index, pd.DatetimeIndex):
                idx = df.index
            else:
                cols = {str(c).lower(): c for c in df.columns}
                if "timestamp" not in cols:
                    return None
                idx = pd.DatetimeIndex(pd.to_datetime(df[cols["timestamp"]], errors="coerce", utc=True))
            idx = idx[~idx.isna()]
            if len(idx) < 2:
                return None
            span = (idx.max() - idx.min()).total_seconds() / 86400.0
            return float(span) if span > 0 else None
        except Exception:
            return None

    def _load_ohlc_from_local_cache(self, symbol: str, days: int) -> Tuple[Any, List[str]]:
        warnings: List[str] = []
        try:
            import pandas as pd
        except Exception as e:
            return None, [f"ohlc_local_reader_missing:{e}"]

        try:
            y_ticker = normalize_ticker(symbol, provider="yahoo")
            # configured data folder (MS_DATA_DIR), not a hard-coded <repo>/data
            cache_file = data_path("cache_backtest", f"{y_ticker}_5min.csv")
            if not cache_file.exists():
                return None, ["ohlc_local_cache_miss"]

            df = pd.read_csv(cache_file)
            if df is None or df.empty:
                return None, ["ohlc_local_cache_empty"]

            df.columns = [str(c).lower() for c in df.columns]
            if "timestamp" not in df.columns:
                if "date" in df.columns:
                    df = df.rename(columns={"date": "timestamp"})
                elif "datetime" in df.columns:
                    df = df.rename(columns={"datetime": "timestamp"})
            required = {"timestamp", "open", "high", "low", "close", "volume"}
            if not required.issubset(set(df.columns)):
                return None, ["ohlc_local_cache_invalid_cols"]

            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
            df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
            df = df.drop_duplicates(subset=["timestamp"], keep="last")
            now = pd.Timestamp.utcnow().tz_localize(None)
            slice_start = now - pd.Timedelta(days=max(5, int(days)))
            df = df[df["timestamp"] >= slice_start].copy()
            if len(df.index) < 50:
                return None, ["ohlc_local_cache_insufficient"]

            # Indicators are computed from these bars by _indicator_snapshot in
            # _enrich_candidate. (This used to call
            # QuickBacktester._calculate_indicators, which does not exist — the
            # AttributeError was swallowed and every local-cache hit was
            # reported as an OHLC error.)
            return df, warnings
        except Exception as e:
            return None, [f"ohlc_local_cache_error:{e}"]

    @staticmethod
    def _build_local_analysis_stub(
        symbol: str,
        candidate: Dict[str, Any],
        watchlist_item: Dict[str, Any],
        price: float,
        change_pct: float,
        atr: Optional[float],
    ) -> Dict[str, Any]:
        prev_analysis = (candidate or {}).get("analysis", {}) if isinstance(candidate, dict) else {}
        prev_strategy = (prev_analysis or {}).get("strategy", {}) if isinstance(prev_analysis, dict) else {}
        best = watchlist_item.get("best_strategy", {}) if isinstance(watchlist_item, dict) else {}

        trade_mode = str(
            (best or {}).get("trade_mode")
            or (prev_strategy or {}).get("trade_mode")
            or "PULLBACK"
        ).upper()
        if trade_mode not in {"PULLBACK", "BREAKOUT", "CONTRA", "ORB_FVG"}:
            trade_mode = "PULLBACK"
        # Where the mode came from. Only a mode a strategy actually produced
        # may be shown as the setup; the "PULLBACK" default is a placeholder.
        valid_modes = {"PULLBACK", "BREAKOUT", "CONTRA", "ORB_FVG"}
        best_mode = str((best or {}).get("trade_mode") or "").upper()
        prev_source = str((prev_strategy or {}).get("trade_mode_source") or "")
        if best_mode in valid_modes:
            mode_source = "watchlist_strategy"
        elif (
            not best_mode
            and str((prev_strategy or {}).get("trade_mode") or "").upper() == trade_mode
            and prev_source in {"watchlist_strategy", "analysis"}
        ):
            mode_source = prev_source
        else:
            mode_source = "default"

        trend_hint = str(watchlist_item.get("trend_1h") or watchlist_item.get("col_1h") or "").upper()
        prev_verdict = str((prev_analysis or {}).get("verdict") or "").upper()
        verdict = prev_verdict if prev_verdict in {"LONG", "SHORT"} else ""
        # Signal-direction audit (2026-08): the current move is the freshest
        # directional fact this stub has. Pre-fix the carried previous verdict
        # had top priority forever, so a name that pumped yesterday and dumped
        # -16% today still rendered "LONG" (live: BICOUSDT/ENAUSDT/QNRX) — the
        # row contradicted its own % badge. A significant counter-move (>=5%,
        # the stock intake floor) now overrides the stale verdict; smaller
        # moves keep the carried verdict / trend hint (pullback tolerance).
        if abs(change_pct) >= 5.0:
            verdict = "LONG" if change_pct > 0 else "SHORT"
        if not verdict:
            if "BULL" in trend_hint:
                verdict = "LONG"
            elif "BEAR" in trend_hint:
                verdict = "SHORT"
            else:
                verdict = "LONG" if change_pct >= 0 else "SHORT"

        signal_score = _safe_float(
            watchlist_item.get("signal_score"),
            _safe_float((prev_analysis or {}).get("strength"), None),
        )
        estimated_strength = False
        if signal_score is None or float(signal_score) <= 0:
            # Conservative fallback: price move is informative, but not equivalent to full model strength.
            signal_score = min(70.0, 30.0 + abs(change_pct) * 1.5)
            estimated_strength = True
        signal_score = _clamp(float(signal_score), 0.0, 100.0)

        entry = price if isinstance(price, (int, float)) else 0.0
        # ATR fallback is RELATIVE to price. The old absolute floor of $0.01
        # was 44% of a $0.0227 token, producing absurd levels (live: BICOUSDT
        # entry 0.0227, target 0.0427 = +88%). Sub-cent assets get a 2%
        # synthetic ATR like everything else; a zero/absent price yields no
        # levels and the missing_price blocker handles it.
        atr_v = atr if isinstance(atr, (int, float)) and atr > 0 else abs(entry) * 0.02
        if verdict == "SHORT":
            tp = entry - (2.0 * atr_v)
            sl = entry + (1.5 * atr_v)
            direction = "SELL"
        else:
            tp = entry + (2.0 * atr_v)
            sl = entry - (1.5 * atr_v)
            direction = "BUY"

        return {
            "best_signal": {
                "direction": direction,
                "mode": trade_mode,
                "score": signal_score,
            },
            "last_signal": {
                "entry": entry,
                "tp": tp,
                "sl": sl,
            },
            "trend_1h": watchlist_item.get("trend_1h") or watchlist_item.get("col_1h") or "N/A",
            "estimated": estimated_strength,
            "mode_source": mode_source,
        }

    @staticmethod
    def _derive_signal_direction(analysis_raw: Dict[str, Any], change_pct: float) -> Tuple[str, float, str]:
        payload = analysis_raw if isinstance(analysis_raw, dict) else {}
        best = payload.get("best_signal", {}) if isinstance(payload, dict) else {}
        direction_raw = str((best or {}).get("direction") or "").upper().strip()
        mode_raw = str((best or {}).get("mode") or "PULLBACK").upper().strip()
        strength = _safe_float((best or {}).get("score"), None)
        if strength is None:
            strength = _safe_float(payload.get("signal_score"), None)
        if strength is None:
            strength = 55.0 if abs(change_pct) >= 10 else 45.0

        verdict = "NEUTRAL"
        if direction_raw in {"BUY", "LONG", "BULLISH"}:
            verdict = "LONG"
        elif direction_raw in {"SELL", "SHORT", "BEARISH"}:
            verdict = "SHORT"
        else:
            trend_h = str((analysis_raw or {}).get("trend_1h") or "").upper()
            if "BULL" in trend_h:
                verdict = "LONG"
            elif "BEAR" in trend_h:
                verdict = "SHORT"
            elif change_pct >= 10:
                verdict = "LONG"
            elif change_pct <= -10:
                verdict = "SHORT"

        trade_mode = mode_raw if mode_raw in {"PULLBACK", "BREAKOUT", "CONTRA", "ORB_FVG"} else "PULLBACK"
        return verdict, _clamp(float(strength or 0.0), 0.0, 100.0), trade_mode

    @staticmethod
    def _derive_trade_levels(
        analysis_raw: Dict[str, Any],
        verdict: str,
        price: float,
        atr: Optional[float],
    ) -> Tuple[float, float, float]:
        payload = analysis_raw if isinstance(analysis_raw, dict) else {}
        last_signal = payload.get("last_signal", {}) if isinstance(payload, dict) else {}
        entry = _safe_float(last_signal.get("entry"), None)
        tp = _safe_float(last_signal.get("tp"), None)
        sl = _safe_float(last_signal.get("sl"), None)

        fallback_entry = price if isinstance(price, (int, float)) else 0.0
        # Relative-only synthetic ATR — see _build_local_analysis_stub: the old
        # absolute $0.01 floor wrecked levels for sub-cent assets.
        atr_v = atr if isinstance(atr, (int, float)) and atr > 0 else abs(fallback_entry) * 0.02
        if entry is None or entry <= 0:
            entry = fallback_entry

        if tp is None or tp <= 0 or sl is None or sl <= 0:
            if verdict == "SHORT":
                tp = entry - (2.0 * atr_v)
                sl = entry + (1.5 * atr_v)
            else:
                tp = entry + (2.0 * atr_v)
                sl = entry - (1.5 * atr_v)

        return float(entry or 0.0), float(tp or 0.0), float(sl or 0.0)

    def _build_news_pack(
        self,
        symbol: str,
        cfg: Dict[str, Any],
        force_refresh: bool,
        allow_remote_refresh: bool,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if not is_agent_enabled("news_tab", config=self.config_manager.config, default=True):
            return {
                "available": False,
                "disabled": True,
                "top": [],
                "bullish_count": 0,
                "bearish_count": 0,
                "last_update": None,
            }, {"news_ts": None}
        news_ttl = int(cfg.get("news_ttl_sec", 300))
        state = None
        last_update_dt = None
        news_ts = None

        try:
            from agents.news_tab_agent import (
                get_news_tab_agent_if_ready,
                get_news_tab_agent_status,
                get_news_tab_agent,
                kickoff_news_tab_agent_init,
            )

            kickoff_news_tab_agent_init(self.config_manager.config)
            agent = get_news_tab_agent_if_ready()
            if agent is not None:
                state = agent.get_news_tab_state(active_tickers=[symbol]) or {}
            else:
                status = get_news_tab_agent_status()
                if status.get("ready"):
                    agent = get_news_tab_agent(self.config_manager.config)
                    state = agent.get_news_tab_state(active_tickers=[symbol]) or {}

            if isinstance(state, dict):
                news_ts = state.get("last_update")
                last_update_dt = _parse_iso(news_ts)

            ticker_items = []
            if isinstance(state, dict):
                ticker_news = state.get("ticker_news", {}) or {}
                if not is_crypto_ticker(symbol):
                    ticker_items = list((ticker_news.get(symbol) or []))
                    ticker_items = [a for a in ticker_items if self._article_matches_symbol(a, symbol)]
                if not ticker_items:
                    market_pool = list(state.get("market_news", []) or []) + list(state.get("breaking_news", []) or [])
                    ticker_items = [a for a in market_pool if self._article_matches_symbol(a, symbol)]

            stale = True
            if last_update_dt:
                stale = (_now_utc() - last_update_dt).total_seconds() > news_ttl

            refresh_cooldown = int(cfg.get("news_refresh_cooldown_sec", 120))
            now_ts = time.time()
            can_refresh_now = force_refresh or ((now_ts - self._last_news_refresh_ts) >= refresh_cooldown)
            if allow_remote_refresh and can_refresh_now and (force_refresh or (not ticker_items and stale)) and state is not None:
                try:
                    agent = get_news_tab_agent(self.config_manager.config)
                    ensured = None
                    ensure_cached = getattr(agent, "ensure_ticker_news_cached", None)
                    if callable(ensure_cached):
                        try:
                            ensured = ensure_cached(symbol, force_refresh=bool(force_refresh), allow_remote=True) or {}
                        except TypeError:
                            ensured = ensure_cached(symbol) or {}
                    else:
                        # Fallback for older NewsTabAgent implementations
                        try:
                            agent.refresh_all_news([symbol], include_market=False)
                        except TypeError:
                            agent.refresh_all_news([symbol])
                        ensured = {"status": "refreshed", "fetched": True}

                    ensured_status = str((ensured or {}).get("status") or "").lower()
                    if ensured_status in {"refreshed", "ok", "shared_wait", "miss_throttled", "hit"}:
                        self._last_news_refresh_ts = time.time()
                    state = agent.get_news_tab_state(active_tickers=[symbol]) or state
                    news_ts = state.get("last_update")
                    ticker_news = state.get("ticker_news", {}) or {}
                    if not is_crypto_ticker(symbol):
                        ticker_items = list((ticker_news.get(symbol) or []))
                        ticker_items = [a for a in ticker_items if self._article_matches_symbol(a, symbol)]
                    else:
                        ticker_items = []
                    if not ticker_items:
                        market_pool = list(state.get("market_news", []) or []) + list(state.get("breaking_news", []) or [])
                        ticker_items = [a for a in market_pool if self._article_matches_symbol(a, symbol)]
                except Exception as refresh_error:
                    logger.warning(f"TradeRadar news refresh skipped for {symbol}: {refresh_error}")

            ticker_items = [a for a in ticker_items if isinstance(a, dict)]
            top = sorted(
                ticker_items,
                key=lambda x: (
                    int(_safe_float(x.get("impact_score"), 0) or 0),
                    _parse_iso(x.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc),
                ),
                reverse=True,
            )[:3]

            bullish = 0
            bearish = 0
            for item in ticker_items:
                sentiment = str(item.get("sentiment") or "").lower()
                if "bull" in sentiment:
                    bullish += 1
                elif "bear" in sentiment:
                    bearish += 1

            result = {
                "available": bool(ticker_items),
                # The news store was read; zero matching articles is then a
                # finding ("no news for this symbol"), not an outage.
                "checked": isinstance(state, dict),
                "bullish_count": bullish,
                "bearish_count": bearish,
                "top": [
                    {
                        "title": str(i.get("title") or "")[:280],
                        "impact_score": int(_safe_float(i.get("impact_score"), 0) or 0),
                        "sentiment": str(i.get("sentiment") or "neutral").lower(),
                        "published_at": i.get("published_at"),
                    }
                    for i in top
                ],
                "last_update": news_ts,
            }
            if not result["available"]:
                result["available"] = False
            return result, {"news_ts": news_ts}
        except Exception as e:
            logger.warning(f"TradeRadar news component failed for {symbol}: {e}")
            return {
                "available": False,
                "bullish_count": 0,
                "bearish_count": 0,
                "top": [],
                "last_update": news_ts,
            }, {"news_ts": news_ts}

    @staticmethod
    def _article_matches_symbol(article: Dict[str, Any], symbol: str) -> bool:
        if not isinstance(article, dict):
            return False
        sym = _extract_symbol(symbol)
        if not sym:
            return False
        title = str(article.get("title") or "").upper()
        ticker = _extract_symbol(article.get("ticker"))
        related_raw = article.get("related_tickers") or article.get("symbols") or article.get("tickers") or []
        if isinstance(related_raw, str):
            related = [x.strip().upper() for x in related_raw.split(",") if x and x.strip()]
        elif isinstance(related_raw, list):
            related = [str(t or "").upper() for t in related_raw]
        else:
            related = []
        base = sym.replace("-USD", "").replace("-USDT", "").replace("-USDC", "")

        # For crypto symbols, avoid title-based matching to prevent collisions with equity tickers (e.g., AXS).
        if is_crypto_ticker(sym):
            aliases = {
                sym,
                f"{base}-USD",
                f"{base}-USDT",
                f"{base}USDT",
                f"{base}USD",
            }
            if ticker in aliases or any(r in aliases for r in related):
                stock_markers = ["(NYSE:", "(NASDAQ:", "(LON:", "SHARE PRICE", "HOLDINGS", "INVESTMENT", "INVESTOR", "DIVIDEND", "EARNINGS"]
                crypto_markers = ["CRYPTO", "TOKEN", "COIN", "BLOCKCHAIN", "BINANCE", "DEFI", "WEB3", "NFT", "USDT", "USDC", "-USD", f"${base}"]
                has_stock_markers = any(m in title for m in stock_markers)
                has_crypto_markers = any(m in title for m in crypto_markers)
                if has_stock_markers and not has_crypto_markers:
                    return False
                return True
            return False

        if ticker == sym or sym in related:
            return True
        if ticker and ticker != sym and sym not in related:
            return False
        if not title:
            return False
        if not base or len(base) < 3:
            return False
        if re.search(rf"\b{re.escape(base)}\b", title):
            return True
        return False

    def _build_calendar_pack(
        self,
        symbol: str,
        cfg: Dict[str, Any],
        force_refresh: bool,
        allow_remote_refresh: bool,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if not is_agent_enabled("economic_calendar", config=self.config_manager.config, default=True):
            return {
                "has_upcoming_earnings": False,
                "earnings_date": None,
                "earnings_eps_est": None,
                "earnings_eps_actual": None,
                "macro_events": [],
                "disabled": True,
            }, {"calendar_ts": None}

        window_days = int(cfg.get("calendar_window_days", 3))
        cal_ttl = int(cfg.get("calendar_ttl_sec", 1800))
        now = _now_utc()
        start = now
        end = now + timedelta(days=window_days)
        calendar_ts = None

        try:
            from agents.economic_calendar_agent import get_economic_calendar_agent

            calendar = get_economic_calendar_agent(self.config_manager.config)
            state = calendar.get_state(days=max(7, window_days)) or {}
            calendar_ts = state.get("as_of")

            earnings_all = list(state.get("earnings", []) or [])
            economic_all = list(state.get("economic", []) or [])
            # What the calendar could actually tell us. An EMPTY feed cannot
            # vouch for "nothing scheduled" (live: the macro list was empty
            # while the radar still reported a clean 72h window).
            macro_checked = bool(economic_all)
            earnings_checked = bool(earnings_all)

            earnings_for_symbol = [e for e in earnings_all if _extract_symbol(e.get("symbol")) == symbol]
            earnings_for_symbol = sorted(
                earnings_for_symbol,
                key=lambda e: _parse_iso(e.get("date")) or datetime.max.replace(tzinfo=timezone.utc),
            )
            earnings_pick = earnings_for_symbol[0] if earnings_for_symbol else {}

            macro_events = []
            for event in economic_all:
                if not isinstance(event, dict):
                    continue
                dt = _parse_iso(event.get("date"))
                if not dt:
                    continue
                if start <= dt <= (now + timedelta(hours=72)):
                    macro_events.append(
                        {
                            "title": event.get("title") or "",
                            "date": _iso(dt),
                            "impact": str(event.get("impact") or "").lower(),
                        }
                    )
            macro_events = sorted(macro_events, key=lambda x: _parse_iso(x.get("date")) or now)[:5]

            is_cache_old = True
            ts_dt = _parse_iso(calendar_ts)
            if ts_dt:
                is_cache_old = (now - ts_dt).total_seconds() > cal_ttl
            should_refresh = allow_remote_refresh and (force_refresh or ((not earnings_pick and not macro_events) and is_cache_old))

            if should_refresh:
                try:
                    refreshed = calendar.get_calendar_events_range(
                        start_date=start,
                        end_date=end,
                        watchlist_tickers=[symbol],
                        allow_remote=True,
                    ) or {}
                    self._last_calendar_refresh_ts = time.time()
                    earnings_ref = list(refreshed.get("earnings", []) or [])
                    earnings_checked = earnings_checked or bool(earnings_ref)
                    macro_checked = macro_checked or bool(refreshed.get("economic"))
                    earnings_for_symbol = [e for e in earnings_ref if _extract_symbol(e.get("symbol")) == symbol]
                    earnings_for_symbol = sorted(
                        earnings_for_symbol,
                        key=lambda e: _parse_iso(e.get("date")) or datetime.max.replace(tzinfo=timezone.utc),
                    )
                    earnings_pick = earnings_for_symbol[0] if earnings_for_symbol else earnings_pick

                    macro_events = []
                    for event in (refreshed.get("economic", []) or []):
                        if not isinstance(event, dict):
                            continue
                        dt = _parse_iso(event.get("date"))
                        if not dt:
                            continue
                        if start <= dt <= (now + timedelta(hours=72)):
                            macro_events.append(
                                {
                                    "title": event.get("title") or "",
                                    "date": _iso(dt),
                                    "impact": str(event.get("impact") or "").lower(),
                                }
                            )
                    macro_events = sorted(macro_events, key=lambda x: _parse_iso(x.get("date")) or now)[:5]
                    calendar_ts = _iso(_now_utc())
                except Exception as refresh_error:
                    logger.warning(f"TradeRadar calendar refresh skipped for {symbol}: {refresh_error}")

            earnings_payload = {}
            if earnings_pick:
                date_val = str(earnings_pick.get("date_formatted") or earnings_pick.get("date") or "")[:10]
                earnings_payload = {
                    "date": date_val,
                    "when": str(earnings_pick.get("when") or "").lower(),
                }

            result = {
                "available": bool(earnings_payload or macro_events),
                # Which feeds were readable. With a populated feed an empty
                # window means "nothing scheduled"; with an empty feed it
                # means "unknown".
                "macro_checked": macro_checked,
                "earnings_checked": earnings_checked,
                "checked": bool(macro_checked or earnings_checked),
                "earnings": earnings_payload,
                "macro_next_72h": macro_events,
                "last_update": calendar_ts,
            }
            return result, {"calendar_ts": calendar_ts}
        except Exception as e:
            logger.warning(f"TradeRadar calendar component failed for {symbol}: {e}")
            return {
                "available": False,
                "earnings": {},
                "macro_next_72h": [],
                "last_update": calendar_ts,
            }, {"calendar_ts": calendar_ts}

    @staticmethod
    def _should_run_backtest(backtest_pack: Dict[str, Any], cfg: Dict[str, Any], force_refresh: bool) -> bool:
        if force_refresh:
            return True
        if not isinstance(backtest_pack, dict) or not backtest_pack.get("last_update"):
            return True
        ttl = int(cfg.get("backtest_ttl_sec", 1800))
        last_dt = _parse_iso(backtest_pack.get("last_update"))
        if not last_dt:
            return True
        return (_now_utc() - last_dt).total_seconds() > ttl

    def _run_backtest_lite(self, symbol: str, df: Any, trade_mode: str, days: int) -> Dict[str, Any]:
        now_iso = _iso(_now_utc())
        try:
            def _run_variant(variant_mode: str) -> Dict[str, Any]:
                return self._quick_backtester.quick_backtest(
                    ticker=symbol,
                    risk_mode="MEDIUM",
                    is_crypto=is_crypto_ticker(symbol),
                    days=max(5, int(days)),
                    trade_mode=(trade_mode or "PULLBACK"),
                    df=df,
                    variant_mode=variant_mode,
                    ui_interval="5min",
                ) or {}

            result = _run_variant("RADAR_QUICK")
            used_variant = "RADAR_QUICK"
            fallback_used = False

            closed_primary = int(_safe_float(result.get("closed_trades"), 0) or 0)
            signals_primary = int(_safe_float(result.get("signals_traded"), 0) or 0)
            if not result.get("error") and closed_primary <= 0 and signals_primary <= 0:
                fallback = _run_variant("CUSTOM")
                closed_fb = int(_safe_float(fallback.get("closed_trades"), 0) or 0)
                signals_fb = int(_safe_float(fallback.get("signals_traded"), 0) or 0)
                if not fallback.get("error") and (closed_fb > 0 or signals_fb > 0):
                    result = fallback
                    used_variant = "CUSTOM"
                    fallback_used = True

            win_rate = _safe_float(result.get("win_rate_trades"), None)
            if win_rate is None:
                win_rate = _safe_float(result.get("win_rate"), 0.0) or 0.0
            # QuickBacktester returns total_signals/trade_count (and wins+losses),
            # NOT closed_trades/signals_traded/open_trades. Pre-fix every backtest
            # pack reported closed_trades=0, which fired a false "backtest_no_trades"
            # red_flag on every candidate even when trades existed. Map the real
            # fields; open_trades stays 0 (quick_backtester is all-closed).
            total_signals = int(_safe_float(result.get("total_signals"), 0) or 0)
            if not total_signals:
                total_signals = int(_safe_float(result.get("trade_count"), 0) or 0)
            if not total_signals:
                total_signals = int(_safe_float(result.get("wins"), 0) or 0) + int(_safe_float(result.get("losses"), 0) or 0)
            closed_trades = int(_safe_float(result.get("closed_trades"), 0) or 0) or total_signals
            signals_traded = int(_safe_float(result.get("signals_traded"), 0) or 0) or total_signals
            open_trades = int(_safe_float(result.get("open_trades"), 0) or 0)
            # The window label must be what the bars cover, not what was asked
            # for (a new listing or a short provider history is not "30d").
            span_days = self._frame_span_days(df)
            days_covered = max(1, int(round(span_days))) if span_days else max(5, int(days))
            days_covered = min(days_covered, max(5, int(days)))
            # None = not measurable (no trades, or no losing trade yet). The
            # confidence bonus already treats a missing ratio as "no bonus".
            pf_measured = _finite(result.get("profit_factor"))
            # "Too few bars to test" is not "tested and found no trades": the
            # latter is a hard blocker, the former is simply unavailable.
            insufficient = bool(result.get("insufficient_data"))
            return {
                "available": bool(result and not result.get("error")) and not insufficient,
                **({"unavailable_reason": "insufficient_price_history"} if insufficient else {}),
                "days": days_covered,
                "days_requested": max(5, int(days)),
                "bars": int(len(df)) if df is not None else 0,
                # What was simulated: the generic RSI/MACD entry rules in both
                # directions on these bars — context for how the symbol trades,
                # not a test of this card's direction or levels.
                "scope": "generic_rsi_macd_both_directions",
                "win_rate": round(float(win_rate or 0.0), 2),
                "profit_factor": round(pf_measured, 4) if (pf_measured is not None and closed_trades > 0) else None,
                "max_drawdown": round(float(_safe_float(result.get("max_drawdown_pct"), 0.0) or 0.0), 4),
                "closed_trades": closed_trades,
                "signals_traded": signals_traded,
                "open_trades": open_trades,
                "variant_used": used_variant,
                "fallback_used": fallback_used,
                "last_update": now_iso,
            }
        except Exception as e:
            logger.warning(f"TradeRadar backtest failed for {symbol}: {e}")
            return {
                "available": False,
                "days": max(5, int(days)),
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "max_drawdown": 0.0,
                "closed_trades": 0,
                "signals_traded": 0,
                "open_trades": 0,
                "variant_used": "RADAR_QUICK",
                "fallback_used": False,
                "last_update": now_iso,
            }

    @classmethod
    def _apply_backtest_verdict_on_read(cls, item: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
        """Bring a served candidate's backtest verdict / readiness up to date.

        Returns True when the readiness was rebuilt. Works on the copy that
        `get_state` serves; the stored state is updated by the next enrichment.
        """
        pack = item.get("backtest")
        if not isinstance(pack, dict):
            return False
        verdict = cls._annotate_backtest_verdict(pack, cfg)
        flags = [str(f) for f in (item.get("red_flags") or []) if f]
        wanted = (
            "backtest_negative" if verdict["negative"]
            else ("backtest_thin_sample" if verdict["thin_sample"] else "")
        )
        current = [f for f in flags if f in ("backtest_negative", "backtest_thin_sample")]
        item["backtest"] = verdict
        if current == ([wanted] if wanted else []):
            return False
        flags = [f for f in flags if f not in ("backtest_negative", "backtest_thin_sample")]
        if wanted:
            flags.append(wanted)
        item["red_flags"] = (
            [f for f in flags if f in cls.HARD_BLOCKERS] + [f for f in flags if f not in cls.HARD_BLOCKERS]
        )
        analysis = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
        try:
            item["readiness"] = cls._build_readiness(
                verdict=str(analysis.get("verdict") or "NEUTRAL").upper(),
                confidence=_safe_float(analysis.get("confidence"), 0.0) or 0.0,
                entry=_safe_float(analysis.get("entry"), 0.0) or 0.0,
                tp=_safe_float(analysis.get("tp"), 0.0) or 0.0,
                sl=_safe_float(analysis.get("sl"), 0.0) or 0.0,
                red_flags=item["red_flags"],
            )
        except Exception:
            if wanted == "backtest_negative":
                item["readiness"] = {
                    "status": "BLOCKED", "can_open": False, "close_to_open": False,
                    "blockers": ["backtest_negative"],
                    "note": "Blocked: this symbol's own backtest lost money (profit factor below 1.00).",
                }
        return True

    @staticmethod
    def _annotate_backtest_verdict(backtest_pack: Any, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Add ``negative`` / ``thin_sample`` to a backtest pack (returns a copy).

        ``negative``: the backtest is available, has at least
        ``backtest_negative_min_trades`` closed trades and a measured profit
        factor below 1.0 -> readiness blocker ``backtest_negative``.
        ``thin_sample``: the same losing result over fewer trades -> the
        non-blocking flag ``backtest_thin_sample``. An unmeasurable profit
        factor (None) is neither.
        """
        pack = dict(backtest_pack) if isinstance(backtest_pack, dict) else {}
        min_trades = int(
            _clamp(_safe_float((cfg or {}).get("backtest_negative_min_trades"), 20) or 20, 1, 1000)
        )
        closed_trades = int(_safe_float(pack.get("closed_trades"), 0) or 0)
        profit_factor = _finite(pack.get("profit_factor"))
        losing = bool(pack.get("available")) and closed_trades > 0 and profit_factor is not None and profit_factor < 1.0
        pack["negative"] = bool(losing and closed_trades >= min_trades)
        pack["thin_sample"] = bool(losing and closed_trades < min_trades)
        pack["negative_min_trades"] = min_trades
        return pack

    @staticmethod
    def _compute_confidence(
        verdict: str,
        strength: float,
        news_pack: Dict[str, Any],
        calendar_pack: Dict[str, Any],
        backtest_pack: Dict[str, Any],
    ) -> Dict[str, float]:
        base = _clamp(float(strength or 0.0), 0.0, 100.0)
        bonus_news = 0.0
        bullish = int(news_pack.get("bullish_count", 0) or 0)
        bearish = int(news_pack.get("bearish_count", 0) or 0)
        if verdict == "LONG" and bullish > bearish:
            bonus_news = min(10.0, (bullish - bearish) * 3.0)
        elif verdict == "SHORT" and bearish > bullish:
            bonus_news = min(10.0, (bearish - bullish) * 3.0)

        penalty_cal = 0.0
        now = _now_utc()
        earnings = calendar_pack.get("earnings", {}) if isinstance(calendar_pack, dict) else {}
        earnings_date = str((earnings or {}).get("date") or "").strip()
        if earnings_date:
            dt = _parse_iso(f"{earnings_date}T00:00:00+00:00")
            if dt and 0 <= (dt - now).total_seconds() <= 86400:
                penalty_cal += 10.0
        for event in (calendar_pack.get("macro_next_72h", []) or []):
            impact = str((event or {}).get("impact") or "").lower()
            dt = _parse_iso((event or {}).get("date"))
            if dt and 0 <= (dt - now).total_seconds() <= 86400:
                if impact == "high":
                    penalty_cal += 5.0
                elif impact == "medium":
                    penalty_cal += 2.0
        penalty_cal = min(15.0, penalty_cal)

        bonus_bt = 0.0
        if backtest_pack.get("available"):
            pf = float(_safe_float(backtest_pack.get("profit_factor"), 0.0) or 0.0)
            closed_trades = int(_safe_float(backtest_pack.get("closed_trades"), 0) or 0)
            # An unavailable profit factor is converted to no bonus below. The
            # quick backtester reports it as null until it observes a loss, so
            # an unmeasurable ratio can never increase Trade Radar confidence.
            # TR-COR-01 (same audit): max_drawdown is a POSITIVE peak-to-trough
            # magnitude (quick_backtester.py:145), so the guard is ``dd <= 15.0``
            # (not ``dd >= -15.0`` which was always true).
            dd = float(_safe_float(backtest_pack.get("max_drawdown"), 0.0) or 0.0)
            if closed_trades <= 0:
                bonus_bt = 0.0
            else:
                if pf > 1.3 and dd <= 15.0:
                    bonus_bt = 10.0
                elif pf > 1.1 and dd <= 20.0:
                    bonus_bt = 5.0
                # Sample-size honesty (2026-08 audit): PF from a handful of
                # trades is noise — live: FTFT earned the full +10 off a
                # 4-trade PF 4.39 and read conf 80 with every indicator
                # missing. Under 5 closed trades the bonus is halved.
                if 0 < closed_trades < 5:
                    bonus_bt = round(bonus_bt / 2.0, 1)

        score = _clamp(base + bonus_news - penalty_cal + bonus_bt, 0.0, 100.0)
        return {
            "base": round(base, 4),
            "news_bonus": round(bonus_news, 4),
            "calendar_penalty": round(penalty_cal, 4),
            "backtest_bonus": round(bonus_bt, 4),
            "final_score": round(score, 4),
        }

    @staticmethod
    def _derive_time_horizon(change_pct: float, adx: Optional[float]) -> str:
        if abs(change_pct) >= 15.0 and (adx is not None and adx >= 25.0):
            return "1-2 days"
        return "intraday"

    @staticmethod
    def _build_reasons(
        symbol: str,
        change_pct: float,
        threshold: float,
        verdict: str,
        trade_mode: str,
        strength: float,
        rsi: Optional[float],
        adx: Optional[float],
        news_pack: Dict[str, Any],
        backtest_pack: Dict[str, Any],
        confidence_info: Dict[str, Any],
        strength_estimated: bool = False,
        indicator_timeframe: Optional[str] = None,
    ) -> List[str]:
        reasons: List[str] = []
        reasons.append(f"{symbol} moved {change_pct:+.2f}% (threshold {threshold:.2f}%).")
        if strength_estimated:
            # No strategy scored this symbol: the direction is just the sign of
            # the move and the strength a function of its size. Calling that a
            # "technical verdict via PULLBACK" was false on both counts.
            reasons.append(
                f"Direction {verdict} follows the move - no strategy signal for this symbol; "
                f"strength {int(round(strength))}/100 is estimated from the move size."
            )
        else:
            reasons.append(f"Technical verdict {verdict} via {trade_mode}, strength {int(round(strength))}/100.")

        ind_bits: List[str] = []
        if isinstance(rsi, (int, float)):
            ind_bits.append(f"RSI {rsi:.1f}")
        if isinstance(adx, (int, float)):
            ind_bits.append(f"ADX {adx:.1f}")
        if ind_bits:
            tf_note = f" ({indicator_timeframe} bars)" if indicator_timeframe else ""
            reasons.append("Indicators" + tf_note + ": " + ", ".join(ind_bits) + ".")

        if news_pack.get("available"):
            reasons.append(
                f"News flow: bullish {int(news_pack.get('bullish_count', 0) or 0)}, bearish {int(news_pack.get('bearish_count', 0) or 0)}."
            )
        if backtest_pack.get("available"):
            closed_trades = int(_safe_float(backtest_pack.get("closed_trades"), 0) or 0)
            open_trades = int(_safe_float(backtest_pack.get("open_trades"), 0) or 0)
            variant_used = str(backtest_pack.get("variant_used") or "RADAR_QUICK").upper()
            variant_note = f" [{variant_used}]"
            if bool(backtest_pack.get("fallback_used")):
                variant_note += " (fallback)"
            if closed_trades > 0:
                # Profit factor is unmeasurable until the sample has a losing
                # trade; it was printed as "PF 0.00", i.e. as the worst
                # possible result, for an all-winning sample.
                pf_value = _finite(backtest_pack.get("profit_factor"))
                pf_text = f"PF {pf_value:.2f}" if pf_value is not None else "PF n/a (no losing trades)"
                reasons.append(
                    f"Backtest-lite {int(backtest_pack.get('days', 0) or 0)}d: WR {float(backtest_pack.get('win_rate', 0.0)):.1f}%, "
                    f"{pf_text} ({closed_trades} trades){variant_note}."
                )
            else:
                if open_trades > 0:
                    reasons.append(
                        f"Backtest-lite {int(backtest_pack.get('days', 0) or 0)}d: {open_trades} open trade(s), 0 closed{variant_note}; "
                        "performance metrics are provisional."
                    )
                else:
                    reasons.append(
                        f"Backtest-lite {int(backtest_pack.get('days', 0) or 0)}d: no closed trades{variant_note}, metrics are non-informative."
                    )
        reasons.append(
            "Confidence math: "
            f"base {float(_safe_float(confidence_info.get('base'), 0.0) or 0.0):.1f}"
            f" + news {float(_safe_float(confidence_info.get('news_bonus'), 0.0) or 0.0):.1f}"
            f" - calendar {float(_safe_float(confidence_info.get('calendar_penalty'), 0.0) or 0.0):.1f}"
            f" + backtest {float(_safe_float(confidence_info.get('backtest_bonus'), 0.0) or 0.0):.1f}"
            f" = {float(_safe_float(confidence_info.get('final_score'), 0.0) or 0.0):.1f}."
        )
        if confidence_info.get("evidence_cap") is not None:
            cause = str(confidence_info.get("evidence_cap_cause") or "key indicators missing")
            if "key indicators missing" in cause:
                tail = "confidence capped below the entry gate until technical data returns."
            else:
                tail = "confidence capped below the entry gate until a strategy signal confirms it."
            reasons.append(f"Evidence cap {int(confidence_info['evidence_cap'])}: {cause} — {tail}")
        return reasons

    @staticmethod
    def _build_red_flags(
        verdict: str,
        news_pack: Dict[str, Any],
        calendar_pack: Dict[str, Any],
        backtest_pack: Dict[str, Any],
        price: float,
        change_pct: float,
    ) -> List[str]:
        flags: List[str] = []
        if price <= 0:
            flags.append("missing_price")
        # "Unavailable" is reserved for a source that could not be read. A
        # source that was read and had nothing for the symbol is reported as
        # such (news) or not flagged at all (an empty calendar window is the
        # normal, risk-free case — it used to raise "calendar unavailable").
        if not news_pack.get("available"):
            flags.append("no_symbol_news" if news_pack.get("checked") else "news_unavailable")
        if not calendar_pack.get("available") and not calendar_pack.get("checked"):
            flags.append("calendar_unavailable")
        elif "macro_checked" in calendar_pack and not calendar_pack.get("macro_checked"):
            flags.append("macro_calendar_unavailable")
        if backtest_pack and not backtest_pack.get("available"):
            flags.append("backtest_unavailable")
        if backtest_pack and backtest_pack.get("available"):
            if int(_safe_float(backtest_pack.get("closed_trades"), 0) or 0) == 0:
                flags.append("backtest_no_trades")
            elif backtest_pack.get("negative"):
                flags.append("backtest_negative")
            elif backtest_pack.get("thin_sample"):
                flags.append("backtest_thin_sample")

        bullish = int(news_pack.get("bullish_count", 0) or 0)
        bearish = int(news_pack.get("bearish_count", 0) or 0)
        if verdict == "LONG" and bearish > bullish + 1:
            flags.append("news_contradicts_long")
        if verdict == "SHORT" and bullish > bearish + 1:
            flags.append("news_contradicts_short")

        earnings = calendar_pack.get("earnings", {}) if isinstance(calendar_pack, dict) else {}
        earnings_date = str((earnings or {}).get("date") or "").strip()
        if earnings_date:
            dt = _parse_iso(f"{earnings_date}T00:00:00+00:00")
            if dt and 0 <= (dt - _now_utc()).total_seconds() <= 86400:
                flags.append("earnings_within_24h")

        if abs(change_pct) >= 20.0:
            flags.append("extreme_volatility")
        return flags

    @staticmethod
    def _build_readiness(
        verdict: str,
        confidence: float,
        entry: float,
        tp: float,
        sl: float,
        red_flags: List[str],
    ) -> Dict[str, Any]:
        verdict_ok = verdict in {"LONG", "SHORT"}
        levels_ok = (0 < sl < entry < tp) if verdict == "LONG" else (0 < tp < entry < sl) if verdict == "SHORT" else False
        hard_blocker_set = TradeRadarAgent.HARD_BLOCKERS
        blockers = [f for f in (red_flags or []) if f in hard_blocker_set]
        confidence_ready = confidence >= 60.0
        near_ready = confidence >= 50.0
        can_open = verdict_ok and levels_ok and (not blockers) and confidence_ready
        close_to_open = verdict_ok and levels_ok and (not blockers) and near_ready and not can_open

        if can_open:
            status = "READY"
            note = "Signal can be opened under current rules."
        elif close_to_open:
            status = "NEAR"
            note = "Setup is close; one stronger confirmation can unlock entry."
        elif blockers:
            # A hard blocker (missing data / 0-trade backtest / null indicators)
            # is a real block, not a soft "watch and wait" state. Surfacing it as
            # BLOCKED is honest for the buyer; WATCH is reserved for setups that
            # have valid data but need more confidence.
            status = "BLOCKED"
            note = "Insufficient validated data for entry."
            if "backtest_negative" in blockers:
                note = "Blocked: this symbol's own backtest lost money (profit factor below 1.00)."
        elif verdict_ok and levels_ok:
            status = "WATCH"
            note = "Directional setup exists, but confidence/risk gates are not met."
        else:
            status = "BLOCKED"
            note = "Insufficient validated data for entry."

        distance_to_open = max(0.0, 60.0 - float(confidence or 0.0))
        # Evidence notes ride at the end of the list for the card; they never
        # count as blockers (status / can_open / no_hard_blockers ignore them).
        notes = [
            f for f in (red_flags or [])
            if f in TradeRadarAgent.NON_BLOCKING_READINESS_NOTES
        ]
        # backtest_negative must survive the display cut below.
        blockers = (
            [b for b in blockers if b == "backtest_negative"]
            + [b for b in blockers if b != "backtest_negative"]
        )
        return {
            "status": status,
            "can_open": bool(can_open),
            "close_to_open": bool(close_to_open),
            "distance_to_open": round(distance_to_open, 2),
            "entry_confidence_threshold": 60,
            "checks": {
                "has_direction": verdict_ok,
                "has_trade_levels": levels_ok,
                "no_hard_blockers": len(blockers) == 0,
                "confidence_ready": confidence_ready,
            },
            "blockers": blockers[:6] + notes,
            "non_blocking_notes": notes,
            "note": note,
        }

    @staticmethod
    def _normalize_ohlc_warnings(warnings: List[Any]) -> List[str]:
        out: List[str] = []
        for raw in (warnings or []):
            text = str(raw or "").strip()
            if not text:
                continue
            s = text.lower()
            if "ohlc_local_cache_miss" in s:
                out.append("ohlc_local_cache_miss")
            elif "ohlc_local_cache_empty" in s:
                out.append("ohlc_local_cache_empty")
            elif "ohlc_local_cache_invalid_cols" in s:
                out.append("ohlc_local_cache_invalid")
            elif "ohlc_local_cache_insufficient" in s:
                out.append("ohlc_local_cache_insufficient")
            elif "ohlc_local_reader_missing" in s:
                out.append("ohlc_local_reader_missing")
            elif "ohlc_remote_blocked" in s:
                out.append("ohlc_remote_blocked")
            elif "ohlc_remote_http_" in s:
                out.append("ohlc_remote_fetch_error")
            elif "connection" in s or "timeout" in s or "forbidden" in s or "fetch error" in s or "circuit open" in s:
                out.append("ohlc_remote_fetch_error")
            elif "no data" in s or "no historical data" in s or "nu s-au g" in s:
                out.append("ohlc_remote_no_data")
            elif "ohlc_fetch_error" in s:
                out.append("ohlc_fetch_error")
            else:
                out.append("ohlc_warning")
        return sorted(set(out))

    @staticmethod
    def _is_suppressed(symbol: str, suppressed_map: Dict[str, str]) -> bool:
        until = _parse_iso((suppressed_map or {}).get(symbol))
        if not until:
            return False
        return until > _now_utc()

    @staticmethod
    def _cleanup_suppressed_map(suppressed_map: Dict[str, str]) -> Dict[str, str]:
        now = _now_utc()
        cleaned: Dict[str, str] = {}
        for symbol, until_iso in (suppressed_map or {}).items():
            sym = _extract_symbol(symbol)
            until = _parse_iso(until_iso)
            if not sym or not until:
                continue
            if until > now:
                cleaned[sym] = _iso(until)
        return cleaned


_trade_radar_agent: Optional[TradeRadarAgent] = None
_trade_radar_lock = threading.Lock()


def get_trade_radar_agent(config_manager: Optional[ConfigManager] = None) -> TradeRadarAgent:
    global _trade_radar_agent
    with _trade_radar_lock:
        if _trade_radar_agent is None:
            _trade_radar_agent = TradeRadarAgent(config_manager=config_manager)
    return _trade_radar_agent


def get_trade_radar_agent_if_ready() -> Optional[TradeRadarAgent]:
    return _trade_radar_agent
