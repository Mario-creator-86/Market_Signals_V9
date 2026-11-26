import json
import math
import logging
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, List, Dict, Optional, Tuple
from core.sqlite_store import get_sqlite_store
from core.market_hours import MarketHoursError, assert_trade_allowed, is_equity_market_hours_enforced
from core.config import RUNTIME_DIR

# Constants
CAPITAL_STATE_PATH = Path(
    os.getenv("MS_CAPITAL_STATE_PATH") or (RUNTIME_DIR / "capital.json")
)
CAPITAL_STORE_KEY = "capital_state"
_CAPITAL_JSON_WRITE_LOCK = threading.Lock()

logger = logging.getLogger("CapitalManager")

# Returned by _read_state_from_storage to a read-only sync when the stored row
# is byte-identical to the one the in-memory state was parsed from.
_STATE_UNCHANGED: Dict[str, Any] = {}

# A failed save is logged at ERROR the first time and then at most once per
# this many seconds while it keeps failing (the tracker saves every few seconds).
_SAVE_ERROR_LOG_INTERVAL_SECONDS = 60.0
# The per-process save-health file is refreshed at most this often while saves succeed.
_SAVE_HEALTH_FILE_INTERVAL_SECONDS = 60.0
SAVE_HEALTH_FILE_PREFIX = "capital_save_health"


class CapitalPersistenceError(RuntimeError):
    """A Capital mutation could not be written to the authoritative store."""


class _CapitalStateLock:
    """Re-entrant lock that knows how deeply its owner holds it.

    The parsed-state cache may only trust the in-memory state when a read is
    the outermost holder: a read nested inside a mutating method (which holds
    the lock from its sync to its save) must not mark memory as clean, because
    the mutation that follows could fail before it is saved.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._depth = 0

    def acquire(self, *args: Any, **kwargs: Any) -> bool:
        acquired = self._lock.acquire(*args, **kwargs)
        if acquired:
            self._depth += 1
        return acquired

    def release(self) -> None:
        self._depth -= 1
        self._lock.release()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_exc: Any) -> None:
        self.release()

    @property
    def depth(self) -> int:
        """Hold depth; only meaningful to the thread that holds the lock."""
        return self._depth

DEFAULT_MANUAL_STOP_LOSS_PCT = 0.02
DEFAULT_MANUAL_TAKE_PROFIT_PCT = 0.04


def resolve_deployable_balance(capital_manager: Any) -> float:
    """Read segregated Capital cash with compatibility for test/legacy managers."""
    method = getattr(capital_manager, "get_deployable_balance", None)
    if callable(method):
        try:
            value = method()
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                return float(value)
        except Exception:
            pass
    return float(capital_manager.get_balance() or 0.0)


def is_research_position(position: Dict[str, Any]) -> bool:
    payload = position or {}
    return bool(
        payload.get("research_only") is True
        or str(payload.get("position_type") or "").lower().strip() == "research_cohort"
    )


_EXIT_LOG_INTERVAL_SECONDS = 900.0
_exit_log_last: Dict[Any, float] = {}


def _exit_log_due(key: Any) -> bool:
    """Rate-limit repeated exit log lines (log only; the exit itself is untouched).

    With the US market closed every stock position past its target was logged
    as "closing" and then "skipped" on each tracker pass: two lines per
    position every few seconds, all weekend.
    """
    now = time.monotonic()
    last = _exit_log_last.get(key)
    if last is not None and (now - last) < _EXIT_LOG_INTERVAL_SECONDS:
        return False
    _exit_log_last[key] = now
    return True


def resolve_capital_equity(
    capital_manager: Any,
    positions: Optional[List[Dict[str, Any]]] = None,
) -> float:
    """Return Capital cash plus reserved margin and mark-to-market PnL.

    Risk percentages must use account equity, not the smaller cash balance left
    after open positions reserve margin. Research-only cohorts stay segregated.
    """
    cash = resolve_deployable_balance(capital_manager)
    if positions is None:
        try:
            positions = list(capital_manager.get_positions() or [])
        except Exception:
            positions = []
    equity = cash
    for position in positions or []:
        if (
            not isinstance(position, dict)
            or str(position.get("status", "")).upper() != "OPEN"
            or is_research_position(position)
        ):
            continue
        try:
            margin = float(position.get("margin_used", 0.0) or 0.0)
            # Cash already includes paid entry commissions. Account equity must
            # therefore add mark-to-market P&L, not per-trade net P&L (which also
            # subtracts the entry commission and an estimated future exit fee).
            unrealized = position.get("unrealized_pnl_gross")
            if unrealized is None:
                unrealized = position.get("current_pnl")
            if unrealized is None:
                entry = float(position.get("entry_price", 0.0) or 0.0)
                current = float(position.get("current_price", 0.0) or 0.0)
                quantity = float(position.get("qty", 0.0) or 0.0)
                if entry > 0 and current > 0 and quantity > 0:
                    is_long = str(position.get("direction", "LONG")).upper() == "LONG"
                    unrealized = (
                        (current - entry) * quantity
                        if is_long
                        else (entry - current) * quantity
                    )
                else:
                    unrealized = position.get("pnl", 0.0)
            equity += margin + float(unrealized or 0.0)
        except (TypeError, ValueError):
            continue
    return float(equity)


def _now_local_iso() -> str:
    return datetime.now().astimezone().isoformat()


# An unchanged exit-recheck skip (same position, reason and cause) is written to
# the Capital state and the decision audit at most once per this many seconds.
_EXIT_SKIP_RECORD_INTERVAL_SECONDS = 900.0


class CapitalManager:
    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._instance_lock:
            if not cls._instance:
                cls._instance = super(CapitalManager, cls).__new__(cls, *args, **kwargs)
        return cls._instance

    def __init__(self):
        if not hasattr(self, "initialized"):
            self.balance: float = 1000.0
            self.history: List[Dict] = []
            self.positions: List[Dict] = []
            self._lock = _CapitalStateLock()
            try:
                self._store = get_sqlite_store()
            except Exception as e:
                logger.error(f"SQLite store unavailable for capital: {e}")
                self._store = None
            self.initialized = True
            self.load()
            self.exit_manager = None  # Initialized lazily to avoid circular imports

            self._price_cache: Dict[str, Tuple[float, float]] = {}
            self._price_cache_lock = threading.Lock()
            self._quote_warning_at: Dict[str, float] = {}
            self._atr_cache: Dict[str, Tuple[float, float]] = {}
            self._atr_cache_lock = threading.Lock()
            self._pnl_thread = None
            self._snapshot_thread = None
            process_role = os.getenv("MS_PROCESS_ROLE", "").strip().lower()
            disable_threads = (
                os.getenv("MS_DISABLE_CAPITAL_THREADS") == "1"
                or process_role == "api"
                or "PYTEST_CURRENT_TEST" in os.environ
            )
            if not disable_threads:
                self._pnl_thread = threading.Thread(target=self.start_pnl_tracker, daemon=True, name="PnLTracker")
                self._pnl_thread.start()

                self._snapshot_thread = threading.Thread(target=self._daily_snapshot_loop, daemon=True, name="DailySnapshot")
                self._snapshot_thread.start()

    def _log_quote_warning(self, key: str, message: str, interval_seconds: float = 300.0) -> None:
        """Rate-limit repeated provider warnings without hiding the first failure."""
        now = time.monotonic()
        with self._price_cache_lock:
            warning_times = getattr(self, "_quote_warning_at", None)
            if not isinstance(warning_times, dict):
                warning_times = {}
                self._quote_warning_at = warning_times
            previous = float(warning_times.get(key, 0.0) or 0.0)
            if previous and (now - previous) < max(1.0, interval_seconds):
                return
            warning_times[key] = now
        logger.warning(message)

    def _parse_state_text(self, source: str, raw: str) -> Optional[Dict]:
        """Parse one stored state document, or reuse the in-memory state.

        The capital blob is ~12 MB; parsing it for every getter was the main
        cost of each read. A read-only sync whose stored text is identical to
        the text the in-memory state was parsed from returns
        ``_STATE_UNCHANGED`` and nothing is parsed. The key is the stored text
        itself (not a timestamp), so a write by any process is always seen.
        """
        clean = getattr(self, "_clean_state_text", None)
        if (
            getattr(self, "_read_only_sync", False)
            and clean is not None
            and clean[0] == source
            and clean[1] == raw
        ):
            self._state_cache_hits = int(getattr(self, "_state_cache_hits", 0) or 0) + 1
            return _STATE_UNCHANGED
        data = json.loads(raw)
        self._state_parse_count = int(getattr(self, "_state_parse_count", 0) or 0) + 1
        if isinstance(data, dict):
            self._last_read_state_text = (source, raw)
        return data

    def _read_state_from_storage(self) -> Optional[Dict]:
        """Read capital state from SQLite first, then JSON fallback."""
        data = None
        self._last_read_state_text = None

        if self._store is not None:
            try:
                raw = None
                get_text = getattr(self._store, "get_text", None)
                if callable(get_text):
                    raw = get_text(CAPITAL_STORE_KEY, None)
                if isinstance(raw, str):
                    try:
                        data = self._parse_state_text("sqlite", raw)
                    except ValueError as e:
                        logger.error(f"Invalid JSON in SQLite key '{CAPITAL_STORE_KEY}': {e}")
                        data = None
                else:
                    # Test doubles and legacy stores without a text accessor.
                    data = self._store.get_json(CAPITAL_STORE_KEY, None)
                if isinstance(data, dict):
                    logger.debug("Loaded capital state from SQLite")
            except Exception as e:
                logger.error(f"Error loading capital state from SQLite: {e}")
                data = None

        if not isinstance(data, dict) and CAPITAL_STATE_PATH.exists():
            try:
                with open(CAPITAL_STATE_PATH, "r", encoding="utf-8") as f:
                    data = self._parse_state_text("json", f.read())
                logger.debug("Loaded capital state from JSON")
            except Exception as e:
                logger.error(f"Error loading capital state: {e}")
                data = None

        return data if isinstance(data, dict) else None

    def _apply_loaded_state(self, data: Dict) -> None:
        try:
            raw_balance = data.get("balance", 1000.0)
            self.balance = 1000.0 if raw_balance is None else float(raw_balance)
        except Exception:
            self.balance = 1000.0
        history = data.get("history", [])
        self.history = history if isinstance(history, list) else []
        positions = data.get("positions", [])
        self._last_duplicate_merge_count = 0
        if isinstance(positions, list):
            self.positions, self._last_duplicate_merge_count = self._merge_duplicate_open_positions(positions)
        else:
            self.positions = []

    def _sync_from_storage(self, initialize_if_missing: bool = False) -> bool:
        read_only = bool(getattr(self, "_read_only_sync", False))
        if not read_only:
            # A mutation may follow this sync: the in-memory state can no
            # longer be assumed equal to the stored text, so the next read
            # parses the stored row again.
            self._clean_state_text = None
        data = self._read_state_from_storage()
        if data is _STATE_UNCHANGED:
            return True
        if isinstance(data, dict):
            self._apply_loaded_state(data)
            if int(getattr(self, "_last_duplicate_merge_count", 0) or 0) > 0:
                self._save_unlocked()
            elif read_only and getattr(getattr(self, "_lock", None), "depth", None) == 1:
                # Outermost holder only: a read nested inside a mutating
                # method must not vouch for memory that is about to change.
                self._clean_state_text = getattr(self, "_last_read_state_text", None)
            self._last_read_state_text = None
            return True
        self._clean_state_text = None
        if initialize_if_missing:
            self.balance = 1000.0
            self.history = []
            self.positions = []
            self._save_unlocked()
        return False

    def _sync_for_read(self) -> bool:
        """Sync for a caller that only reads; call with ``self._lock`` held.

        Unchanged stored state is not parsed again (see ``_parse_state_text``).
        Every path that mutates and saves keeps using ``_sync_from_storage``,
        which always re-parses, so a mutation never starts from reused objects.
        """
        previous = bool(getattr(self, "_read_only_sync", False))
        self._read_only_sync = True
        try:
            return self._sync_from_storage(initialize_if_missing=False)
        finally:
            self._read_only_sync = previous

    def load(self):
        """Load capital state from persistent storage."""
        with self._lock:
            self._sync_from_storage(initialize_if_missing=True)

    # ── save health ───────────────────────────────────────────────────────

    def _save_health_state(self) -> Dict[str, Any]:
        state = getattr(self, "_save_health", None)
        if not isinstance(state, dict):
            state = {
                "last_save_ok_at": None,
                "last_save_error": None,
                "last_save_error_at": None,
                "consecutive_save_failures": 0,
                "total_save_failures": 0,
                "last_mirror_error": None,
                "last_mirror_error_at": None,
                "consecutive_mirror_failures": 0,
            }
            self._save_health = state
        return state

    @staticmethod
    def _save_health_role() -> str:
        role = os.getenv("MS_PROCESS_ROLE", "").strip().lower()
        return "".join(ch for ch in role if ch.isalnum() or ch in "-_") or "engine"

    def _write_save_health_file(self, state: Dict[str, Any]) -> None:
        """Publish this process's save health for the other process (best effort).

        The engine process opens and closes positions while the API process
        builds the read model, so the engine's failures must be readable from
        outside it. A small file per process role next to the JSON mirror; it
        is not part of the Capital state.
        """
        try:
            role = self._save_health_role()
            path = CAPITAL_STATE_PATH.parent / f"{SAVE_HEALTH_FILE_PREFIX}.{role}.json"
            payload = dict(state)
            payload.update({
                "process_role": role,
                "pid": os.getpid(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp_path.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(str(tmp_path), str(path))
            self._save_health_file_at = time.monotonic()
        except Exception as exc:
            logger.debug(f"capital save-health file not written: {exc}")

    def _record_save_result(
        self,
        primary_error: Optional[BaseException],
        mirror_error: Optional[BaseException],
    ) -> None:
        """Make a failed save impossible to miss; never raises.

        ``primary`` is the store reads come from: SQLite when it is available,
        otherwise the JSON file. ``mirror`` is the JSON file kept next to SQLite.
        """
        try:
            state = self._save_health_state()
            now_iso = datetime.now(timezone.utc).isoformat()
            now_mono = time.monotonic()
            was_failing = int(state.get("consecutive_save_failures") or 0) > 0
            self._last_save_persisted = primary_error is None
            if primary_error is None:
                state["last_save_ok_at"] = now_iso
                if was_failing:
                    logger.warning(
                        "Capital state save recovered after %s consecutive failure(s); last error: %s",
                        state.get("consecutive_save_failures"),
                        state.get("last_save_error"),
                    )
                state["consecutive_save_failures"] = 0
            else:
                state["consecutive_save_failures"] = int(state.get("consecutive_save_failures") or 0) + 1
                state["total_save_failures"] = int(state.get("total_save_failures") or 0) + 1
                state["last_save_error"] = f"{type(primary_error).__name__}: {primary_error}"[:400]
                state["last_save_error_at"] = now_iso
                last_logged = getattr(self, "_save_error_logged_at", None)
                if last_logged is None or (now_mono - last_logged) >= _SAVE_ERROR_LOG_INTERVAL_SECONDS:
                    self._save_error_logged_at = now_mono
                    logger.error(
                        "CAPITAL STATE NOT SAVED (%s consecutive failure(s), last good save %s): %s. "
                        "Positions and cash changed in memory are not persisted.",
                        state["consecutive_save_failures"],
                        state.get("last_save_ok_at") or "never in this process",
                        state["last_save_error"],
                    )
            if mirror_error is None:
                state["consecutive_mirror_failures"] = 0
            else:
                state["consecutive_mirror_failures"] = int(state.get("consecutive_mirror_failures") or 0) + 1
                state["last_mirror_error"] = f"{type(mirror_error).__name__}: {mirror_error}"[:400]
                state["last_mirror_error_at"] = now_iso
                if primary_error is None:
                    last_logged = getattr(self, "_mirror_error_logged_at", None)
                    if last_logged is None or (now_mono - last_logged) >= _SAVE_ERROR_LOG_INTERVAL_SECONDS:
                        self._mirror_error_logged_at = now_mono
                        logger.error(
                            "Capital JSON mirror not written (%s consecutive failure(s)); "
                            "SQLite holds the state: %s",
                            state["consecutive_mirror_failures"],
                            state["last_mirror_error"],
                        )
            file_at = getattr(self, "_save_health_file_at", None)
            if (
                primary_error is not None
                or mirror_error is not None
                or was_failing
                or file_at is None
                or (now_mono - file_at) >= _SAVE_HEALTH_FILE_INTERVAL_SECONDS
            ):
                self._write_save_health_file(state)
        except Exception as exc:  # health bookkeeping must never break a save
            logger.debug(f"capital save-health bookkeeping failed: {exc}")

    def get_save_health(self) -> Dict[str, Any]:
        """Save health of THIS process's manager (see ``read_capital_save_health``)."""
        with self._lock:
            state = dict(self._save_health_state())
        state["process_role"] = self._save_health_role()
        state["status"] = "failing" if int(state.get("consecutive_save_failures") or 0) > 0 else "ok"
        return state

    def _require_last_save_persisted(self, what: str) -> None:
        """Refuse to report ``what`` as done when its save did not persist.

        Uses the rollback that already exists: the in-memory state is rebuilt
        from storage (every operation starts that way), which discards the
        unsaved position and cash debit, and the error makes the paper broker
        record the order as FAILED instead of FILLED.
        """
        if getattr(self, "_last_save_persisted", True):
            return
        error = self._save_health_state().get("last_save_error") or "unknown error"
        try:
            self._sync_from_storage(initialize_if_missing=False)
        except Exception as exc:
            logger.error(f"Capital state reload after failed save also failed: {exc}")
        raise CapitalPersistenceError(f"{what} was not persisted and is not open: {error}")

    def _save_unlocked(self) -> None:
        # Whatever happens below, the in-memory state is no longer known to be
        # the parse of the stored row.
        self._clean_state_text = None
        primary_error: Optional[BaseException] = None
        mirror_error: Optional[BaseException] = None
        store_attempted = False
        try:
            # Capital-integrity: never let the tail-trim drop an OPEN position.
            # Pre-fix `self.positions[-300:]` trimmed by insertion order, and
            # close_position mutates status in place (never removes the entry),
            # so a long-held OPEN position sitting near the head was the first to
            # be dropped on save -> its reserved margin was debited at open but
            # never released (release only fires in close_position) -> permanent
            # margin leak + balance understated + _sync_closed_signal_capital
            # can no longer find the position to settle. Partition so OPEN
            # positions are always retained; only the CLOSED tail is trimmed.
            if len(self.positions) <= 300:
                saved_positions = list(self.positions)
            else:
                open_pos = [p for p in self.positions
                            if str((p or {}).get("status", "")).upper() == "OPEN"]
                closed_pos = [p for p in self.positions
                              if str((p or {}).get("status", "")).upper() != "OPEN"]
                saved_positions = closed_pos[-300:] + open_pos
            data = {
                "balance": self.balance,
                "history": self.history[-100:],  # Keep last 100 transactions
                "positions": saved_positions,
                "updated_at": _now_local_iso()
            }

            if self._store is not None:
                store_attempted = True
                try:
                    self._store.set_json(CAPITAL_STORE_KEY, data)
                except Exception as e:
                    # Logged (rate-limited, at ERROR) by _record_save_result.
                    primary_error = e

            # Atomic JSON mirror. Use a unique temp file because the API,
            # engine and PnL tracker can save the mirror concurrently.
            with _CAPITAL_JSON_WRITE_LOCK:
                CAPITAL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
                temp_path = None
                try:
                    fd, tmp_name = tempfile.mkstemp(
                        prefix=f"{CAPITAL_STATE_PATH.name}.",
                        suffix=".tmp",
                        dir=str(CAPITAL_STATE_PATH.parent),
                    )
                    temp_path = Path(tmp_name)
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2)
                    for attempt in range(20):
                        try:
                            os.replace(str(temp_path), str(CAPITAL_STATE_PATH))
                            temp_path = None
                            break
                        except PermissionError:
                            if attempt == 19:
                                raise
                            time.sleep(0.05 * (attempt + 1))
                finally:
                    if temp_path is not None:
                        try:
                            temp_path.unlink(missing_ok=True)
                        except Exception:
                            pass
        except Exception as e:
            mirror_error = e
            if not store_attempted:
                # No SQLite write was made: either the state could not be
                # built, or the JSON file is the only store. Nothing persisted.
                primary_error = e
        self._record_save_result(primary_error, mirror_error)

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _normalize_position_ticker(value: Any) -> str:
        return str(value or "").upper().strip()

    @classmethod
    def _normalize_position_direction(cls, value: Any) -> str:
        side = str(value or "").upper().strip()
        if side in {"BUY", "LONG"}:
            return "LONG"
        if side in {"SELL", "SHORT"}:
            return "SHORT"
        return side

    @staticmethod
    def _position_type(position: Dict[str, Any]) -> str:
        explicit = str((position or {}).get("position_type") or "").lower().strip()
        if explicit:
            return explicit
        if (
            (position or {}).get("signal_id")
            or (position or {}).get("source_signal_id")
            or "commission_usd" in (position or {})
        ):
            return "paper_signal"
        return "manual"

    @classmethod
    def _is_research_position(cls, position: Dict[str, Any]) -> bool:
        return bool(is_research_position(position) or cls._position_type(position) == "research_cohort")

    @classmethod
    def _ledger_position_metadata(
        cls,
        position: Dict[str, Any],
        **extra: Any,
    ) -> Dict[str, Any]:
        """Return durable transaction provenance for Capital/research segregation."""
        payload = position or {}
        research_only = cls._is_research_position(payload)
        metadata = {
            "position_id": str(payload.get("position_id") or ""),
            "ticker": str(payload.get("ticker") or ""),
            "position_type": cls._position_type(payload),
            "research_only": research_only,
            "account_scope": "research" if research_only else "capital",
        }
        metadata.update({key: value for key, value in extra.items() if value is not None})
        return metadata

    @staticmethod
    def _timestamp_utc(value: Any) -> Optional[datetime]:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _closed_position_snapshot(cls, position: Dict[str, Any]) -> Dict[str, Any]:
        """Compact, user-facing audit data for one closed Capital position."""
        payload = position or {}
        decision = payload.get("decision_context") if isinstance(payload.get("decision_context"), dict) else {}
        scoring = payload.get("score_snapshot") if isinstance(payload.get("score_snapshot"), dict) else {}
        if not scoring:
            scoring = decision.get("scoring") if isinstance(decision.get("scoring"), dict) else {}
        sizing = payload.get("sizing_context") if isinstance(payload.get("sizing_context"), dict) else {}
        if not sizing:
            sizing = decision.get("sizing") if isinstance(decision.get("sizing"), dict) else {}
        risk = payload.get("risk_context") if isinstance(payload.get("risk_context"), dict) else {}
        if not risk:
            risk = decision.get("risk") if isinstance(decision.get("risk"), dict) else {}
        review = decision.get("ollama") if isinstance(decision.get("ollama"), dict) else {}
        critic = decision.get("ai_critic") if isinstance(decision.get("ai_critic"), dict) else {}

        opened_at = payload.get("opened_at") or decision.get("timestamp")
        closed_at = payload.get("closed_at")
        opened_dt = cls._timestamp_utc(opened_at)
        closed_dt = cls._timestamp_utc(closed_at)
        holding_seconds = None
        if opened_dt is not None and closed_dt is not None:
            holding_seconds = max(0, int((closed_dt - opened_dt).total_seconds()))

        compact_scoring = {
            "signal_score": scoring.get("signal_score"),
            "signal_threshold": scoring.get("signal_threshold"),
            "probability_score": scoring.get("probability_score"),
            "quality_score": scoring.get("quality_score"),
        }
        # A score that was never measured is null + "unavailable", never a
        # number that reads as a measurement (reporting only; no gate reads it).
        from core.portfolio.evidence import validation_fields

        compact_scoring.update(validation_fields(payload))
        compact_sizing = {
            "base_risk_pct": sizing.get("base_risk_pct"),
            "risk_pct_used": sizing.get("risk_pct_used"),
            "position_size_pct": sizing.get("position_size_pct"),
            "size_factor": sizing.get("size_factor"),
            "confidence_size_cap": sizing.get("confidence_size_cap"),
            "ai_critic_size_cap": sizing.get("ai_critic_size_cap"),
            "effective_size_factor": sizing.get("effective_size_factor"),
            "prevented_compound_size_factor": sizing.get("prevented_compound_size_factor"),
            "sizing_policy": sizing.get("sizing_policy"),
        }
        compact_risk = {
            "risk_reward_ratio": risk.get("risk_reward_ratio"),
            "stop_distance_pct": risk.get("stop_distance_pct"),
            "target_distance_pct": risk.get("target_distance_pct"),
            "stop": risk.get("stop") or payload.get("stop_loss_price"),
            "target": risk.get("target") or payload.get("take_profit_price"),
        }
        compact_review = {
            "available": review.get("available"),
            "recommendation": review.get("recommendation"),
            "confidence": review.get("confidence"),
            "size_factor": review.get("size_factor"),
            "provider": review.get("provider"),
            "model": review.get("model"),
            "reason": str(review.get("reason") or "")[:800],
        }
        compact_critic = {
            "available": critic.get("available", bool(critic)),
            "veto": critic.get("veto"),
            "model_veto": critic.get("model_veto"),
            "severity": critic.get("severity"),
            "size_factor": critic.get("size_factor"),
            "quality_score": critic.get("quality_score"),
            "policy_reason": critic.get("policy_reason"),
            "categories": list(critic.get("categories") or [])[:10],
            "rationale": str(critic.get("rationale") or "")[:800],
        }
        return {
            "position_id": str(payload.get("position_id") or ""),
            "signal_id": str(payload.get("signal_id") or payload.get("source_signal_id") or ""),
            "ticker": str(payload.get("ticker") or ""),
            "direction": str(payload.get("direction") or "").upper(),
            "strategy_type": str(payload.get("strategy_type") or decision.get("strategy_type") or ""),
            "opened_at": opened_at,
            "closed_at": closed_at,
            "holding_seconds": holding_seconds,
            "entry_price": payload.get("entry_price"),
            "exit_price": payload.get("exit_price"),
            "trigger_price": payload.get("trigger_price"),
            "stop_loss_price": payload.get("stop_loss_price"),
            "take_profit_price": payload.get("take_profit_price"),
            "qty": payload.get("qty"),
            "usd_size": payload.get("usd_size"),
            "margin_used": payload.get("margin_used"),
            "leverage": payload.get("leverage"),
            "gross_pnl": payload.get("gross_pnl"),
            "net_pnl": payload.get("net_pnl", payload.get("pnl")),
            "cash_pnl": payload.get("cash_pnl"),
            "pnl_pct_notional": payload.get("pnl_pct_notional"),
            "pnl_pct_margin": payload.get("pnl_pct_margin"),
            "entry_commission": payload.get("commission_usd", payload.get("fees_estimate")),
            "exit_commission": payload.get("exit_commission"),
            "close_reason": payload.get("close_reason"),
            "requested_close_reason": payload.get("requested_close_reason"),
            "close_reason_warning": payload.get("close_reason_warning"),
            "entry_reason": str(payload.get("entry_reason") or decision.get("reason") or "")[:800],
            "entry_thesis": str(payload.get("entry_thesis") or "")[:800],
            "entry_price_source": payload.get("entry_price_source"),
            "exit_price_source": payload.get("exit_price_source"),
            "exit_condition_since": payload.get("exit_condition_since"),
            "exit_condition": payload.get("exit_condition"),
            "evidence_excluded": payload.get("evidence_excluded") is True,
            "evidence_exclusion_reason": payload.get("evidence_exclusion_reason"),
            "evidence_exclusion_basis": payload.get("evidence_exclusion_basis"),
            "evidence_exclusion_delay_hours": payload.get("evidence_exclusion_delay_hours"),
            "evidence_exclusion_note": payload.get("evidence_exclusion_note"),
            "scoring": compact_scoring,
            "sizing": compact_sizing,
            "risk": compact_risk,
            "ai_review": compact_review,
            "critic": compact_critic,
        }

    def _capital_accounting_unlocked(self) -> Dict[str, Any]:
        """Reconcile deployable Capital separately from research cohorts.

        Research positions share the durable paper ledger for execution and exit
        testing, but their reserves, fees and realized PnL must not alter the
        deployable account used by Capital sizing or circuit breakers.
        """
        last_reset_at: Optional[datetime] = None
        for transaction in self.history or []:
            if not isinstance(transaction, dict) or str(transaction.get("type") or "").upper() != "RESET":
                continue
            timestamp = self._timestamp_utc(transaction.get("timestamp"))
            if timestamp and (last_reset_at is None or timestamp > last_reset_at):
                last_reset_at = timestamp

        open_margin = 0.0
        open_entry_fees_since_reset = 0.0
        realized_pnl_since_reset = 0.0
        open_count = 0
        closed_count = 0
        for position in self.positions or []:
            if not isinstance(position, dict) or not self._is_research_position(position):
                continue
            status = str(position.get("status") or "").upper()
            if status == "OPEN":
                open_count += 1
                open_margin += self._safe_float(position.get("margin_used"), 0.0)
                opened_at = self._timestamp_utc(position.get("opened_at"))
                if last_reset_at is None or (opened_at is not None and opened_at >= last_reset_at):
                    open_entry_fees_since_reset += self._safe_float(
                        position.get("commission_usd", position.get("fees_estimate")),
                        0.0,
                    )
                continue
            if status != "CLOSED":
                continue
            closed_at = self._timestamp_utc(position.get("closed_at"))
            if last_reset_at is not None and (closed_at is None or closed_at < last_reset_at):
                continue
            closed_count += 1
            realized_pnl_since_reset += self._safe_float(
                position.get("net_pnl", position.get("pnl")),
                0.0,
            )

        raw_cash = float(self.balance or 0.0)
        deployable_cash = (
            raw_cash
            + open_margin
            + open_entry_fees_since_reset
            - realized_pnl_since_reset
        )
        return {
            "raw_cash_balance": raw_cash,
            "deployable_cash_balance": deployable_cash,
            "research_open_margin": open_margin,
            "research_open_entry_fees": open_entry_fees_since_reset,
            "research_realized_pnl": realized_pnl_since_reset,
            "research_open_positions": open_count,
            "research_closed_positions": closed_count,
            "last_reset_at": last_reset_at.isoformat() if last_reset_at else "",
        }

    @classmethod
    def _is_manual_position(cls, position: Dict[str, Any]) -> bool:
        """True if the position was opened manually (user-controlled).

        Manual positions bypass the regular market-hours guard on open/close
        (the user may trade them 24/7) and are excluded from AI/trailing exit
        management. Mirrors the predicate used by `_ai_exit_management_enabled`.
        """
        if cls._position_type(position) == "manual":
            return True
        strategy_type = str((position or {}).get("strategy_type") or "").upper().strip()
        return strategy_type.startswith("MANUAL")

    @classmethod
    def _ai_exit_management_enabled(cls, position: Dict[str, Any]) -> bool:
        explicit = (position or {}).get("ai_exit_management_enabled")
        if explicit is not None:
            return bool(explicit)
        strategy_type = str((position or {}).get("strategy_type") or "").upper().strip()
        # Every automated position, rule baskets and learning trades included: the owner asked on
        # 2026-11-24 that Opus may close a position when it sees the market change ("Da, pornește la
        # toate, dar la 1h"); each position is reviewed at most once an hour
        # (ollama.exit_decision_cooldown_seconds) and a close still needs the confidence bar and the
        # evidence guard. Manual positions stay opt-in.
        return cls._position_type(position) != "manual" and not strategy_type.startswith("MANUAL")

    def _maybe_apply_ai_tighten_stop(
        self,
        position: Dict[str, Any],
        ai_exit: Dict[str, Any],
        curr_price: float,
        ollama_cfg: Dict[str, Any],
    ) -> bool:
        """Apply an AI ``tighten_stop`` recommendation if enabled and valid.

        Gated by ``ollama.exit_tighten_stop_enabled`` (default False): when
        disabled the AI exit layer is restricted to close/hold only and never
        moves the stop. Returns True if the trailing stop was updated.
        """
        if not bool(ollama_cfg.get("exit_tighten_stop_enabled", False)):
            return False
        if not ai_exit.get("available"):
            return False
        if str(ai_exit.get("action") or "hold").lower() != "tighten_stop":
            return False
        ai_exit_min_confidence = float(ollama_cfg.get("exit_decision_min_confidence", 0.8) or 0.8)
        min_conf = float(
            ollama_cfg.get("exit_tighten_stop_min_confidence", ai_exit_min_confidence)
            or ai_exit_min_confidence
        )
        min_conf = max(0.0, min(1.0, min_conf))
        if float(ai_exit.get("confidence", 0.0) or 0.0) < min_conf:
            return False
        pid = position.get("position_id")
        direction = str(position.get("direction", "LONG")).upper()
        new_stop = float(ai_exit.get("new_stop") or 0.0)
        current_stop = float(position.get("trailing_sl") or position.get("stop_loss_price") or 0.0)
        valid_long_stop = direction == "LONG" and current_stop < new_stop < curr_price
        valid_short_stop = direction == "SHORT" and curr_price < new_stop < current_stop
        if new_stop <= 0 or not (valid_long_stop or valid_short_stop):
            return False
        with self._lock:
            self._sync_from_storage(initialize_if_missing=False)
            for i, p in enumerate(self.positions):
                if p.get("position_id") == pid and p.get("status") == "OPEN":
                    self.positions[i]["trailing_sl"] = round(new_stop, 8)
                    break
            self._save_unlocked()
        return True

    @staticmethod
    def _unique_text_list(*values: Any) -> List[str]:
        out: List[str] = []
        for value in values:
            items = value if isinstance(value, list) else [value]
            for item in items:
                text = str(item or "").strip()
                if text and text not in out:
                    out.append(text)
        return out

    def _find_open_position_index(
        self,
        *,
        ticker: str,
        direction: str,
        position_type: str,
        leverage: Optional[int] = None,
    ) -> Optional[int]:
        symbol = self._normalize_position_ticker(ticker)
        side = self._normalize_position_direction(direction)
        wanted_type = str(position_type or "").lower().strip()
        for idx, pos in enumerate(self.positions):
            if not isinstance(pos, dict):
                continue
            if str(pos.get("status", "")).upper() != "OPEN":
                continue
            if self._normalize_position_ticker(pos.get("ticker")) != symbol:
                continue
            if self._normalize_position_direction(pos.get("direction")) != side:
                continue
            if self._position_type(pos) != wanted_type:
                continue
            if leverage is not None:
                try:
                    if int(pos.get("leverage", 1) or 1) != int(leverage):
                        continue
                except Exception:
                    continue
            return idx
        return None

    def _merge_position_addon(
        self,
        base_position: Dict[str, Any],
        addon_position: Dict[str, Any],
        *,
        reason: str,
    ) -> Dict[str, Any]:
        base = dict(base_position or {})
        addon = dict(addon_position or {})

        old_qty = self._safe_float(base.get("qty"), 0.0)
        add_qty = self._safe_float(addon.get("qty"), 0.0)
        total_qty = old_qty + add_qty

        old_usd = self._safe_float(base.get("usd_size"), 0.0)
        add_usd = self._safe_float(addon.get("usd_size"), 0.0)
        old_margin = self._safe_float(base.get("margin_used"), old_usd)
        add_margin = self._safe_float(addon.get("margin_used"), add_usd)
        old_entry = self._safe_float(base.get("entry_price"), 0.0)
        add_entry = self._safe_float(addon.get("entry_price"), 0.0)
        old_sl = self._safe_float(base.get("stop_loss_price"), 0.0)
        add_sl = self._safe_float(addon.get("stop_loss_price"), 0.0)
        old_tp = self._safe_float(base.get("take_profit_price"), 0.0)
        add_tp = self._safe_float(addon.get("take_profit_price"), 0.0)

        if total_qty > 0:
            base["entry_price"] = round(((old_entry * old_qty) + (add_entry * add_qty)) / total_qty, 8)
            base["qty"] = round(total_qty, 8)

            # Normalize direction early for conservative SL/TP logic
            merge_direction = self._normalize_position_direction(base.get("direction") or addon.get("direction"))

            # Conservative SL: use the least risky stop for the combined position.
            # LONG: lowest SL (furthest from entry = widest stop). SHORT: highest SL.
            if old_sl > 0 and add_sl > 0:
                if merge_direction == "LONG":
                    base["stop_loss_price"] = round(min(old_sl, add_sl), 8)
                else:
                    base["stop_loss_price"] = round(max(old_sl, add_sl), 8)
            elif add_sl > 0:
                base["stop_loss_price"] = round(add_sl, 8)

            # Conservative TP: use the most ambitious target for the combined position.
            # LONG: highest TP. SHORT: lowest TP.
            if old_tp > 0 and add_tp > 0:
                if merge_direction == "LONG":
                    base["take_profit_price"] = round(max(old_tp, add_tp), 8)
                else:
                    base["take_profit_price"] = round(min(old_tp, add_tp), 8)
            elif add_tp > 0:
                base["take_profit_price"] = round(add_tp, 8)

            # Reset trailing state since risk profile changed after merge
            base["trailing_sl"] = 0.0
            base["max_favorable_excursion"] = 0.0

        base["usd_size"] = round(old_usd + add_usd, 6)
        base["margin_used"] = round(old_margin + add_margin, 6)
        base["ticker"] = self._normalize_position_ticker(base.get("ticker") or addon.get("ticker"))
        base["direction"] = self._normalize_position_direction(base.get("direction") or addon.get("direction"))
        base["position_type"] = self._position_type(base)
        base["status"] = "OPEN"
        base["last_added_at"] = addon.get("opened_at") or _now_local_iso()
        base["add_count"] = int(base.get("add_count", 1) or 1) + 1

        if "commission_usd" in base or "commission_usd" in addon:
            base["commission_usd"] = round(
                self._safe_float(base.get("commission_usd"), 0.0)
                + self._safe_float(addon.get("commission_usd"), 0.0),
                6,
            )
        if "fees_estimate" in base or "fees_estimate" in addon:
            base["fees_estimate"] = round(
                self._safe_float(base.get("fees_estimate"), 0.0)
                + self._safe_float(addon.get("fees_estimate"), 0.0),
                6,
            )

        base_signal = base.get("signal_id") or base.get("source_signal_id")
        addon_signal = addon.get("signal_id") or addon.get("source_signal_id")
        signal_ids = self._unique_text_list(base.get("source_signal_ids"), base_signal, addon_signal)
        if signal_ids:
            base["source_signal_ids"] = signal_ids
            if not base.get("signal_id"):
                base["signal_id"] = signal_ids[0]
            base["source_signal_id"] = base.get("source_signal_id") or signal_ids[0]

        merged_ids = self._unique_text_list(
            base.get("merged_position_ids"),
            base.get("position_id"),
            addon.get("position_id"),
        )
        if merged_ids:
            base["merged_position_ids"] = merged_ids

        event = {
                    "timestamp": addon.get("opened_at") or _now_local_iso(),
            "reason": reason,
            "source_position_id": addon.get("position_id"),
            "signal_id": addon_signal,
            "entry_price": round(add_entry, 8),
            "qty": round(add_qty, 8),
            "usd_size": round(add_usd, 6),
            "margin_used": round(add_margin, 6),
        }
        if "commission_usd" in addon:
            event["commission_usd"] = round(self._safe_float(addon.get("commission_usd"), 0.0), 6)
        if "fees_estimate" in addon:
            event["fees_estimate"] = round(self._safe_float(addon.get("fees_estimate"), 0.0), 6)
        events = list(base.get("scale_in_events") or [])
        events.append(event)
        base["scale_in_events"] = events[-25:]
        base["scale_in_summary"] = (
            f"{base['ticker']} {base['direction']} aggregated into one open position "
            f"({base['add_count']} entries, ${base['usd_size']:.2f} notional)."
        )

        decision_context = dict(base.get("decision_context") or {})
        addon_context = addon.get("decision_context")
        if isinstance(addon_context, dict):
            scale_contexts = list(decision_context.get("scale_in_contexts") or [])
            scale_contexts.append(addon_context)
            decision_context["scale_in_contexts"] = scale_contexts[-10:]
        decision_context["position_aggregation"] = {
            "mode": "scale_in",
            "add_count": base["add_count"],
            "last_added_at": base["last_added_at"],
            "source_signal_ids": base.get("source_signal_ids", []),
        }
        if decision_context:
            base["decision_context"] = decision_context

        return base

    def _position_signal_ids(self, position: Dict[str, Any]) -> List[str]:
        return self._unique_text_list(
            (position or {}).get("source_signal_ids"),
            (position or {}).get("signal_id"),
            (position or {}).get("source_signal_id"),
        )

    def _is_duplicate_signal_position(self, base_position: Dict[str, Any], addon_position: Dict[str, Any]) -> bool:
        base_ids = set(self._position_signal_ids(base_position))
        addon_ids = set(self._position_signal_ids(addon_position))
        return bool(base_ids and addon_ids and base_ids.intersection(addon_ids))

    def _duplicate_position_refund_amount(self, position: Dict[str, Any]) -> float:
        ptype = self._position_type(position)
        margin = self._safe_float((position or {}).get("margin_used"), self._safe_float((position or {}).get("usd_size"), 0.0))
        if ptype in {"paper_signal", "research_cohort"}:
            return margin + self._safe_float((position or {}).get("commission_usd"), 0.0)
        return margin

    def _mark_duplicate_position_ignored(
        self,
        base_position: Dict[str, Any],
        duplicate_position: Dict[str, Any],
        *,
        refund_debit: bool,
    ) -> Dict[str, Any]:
        base = dict(base_position or {})
        duplicate = dict(duplicate_position or {})
        duplicate_id = duplicate.get("position_id")
        signal_ids = self._position_signal_ids(duplicate)

        ignored_ids = self._unique_text_list(base.get("ignored_duplicate_position_ids"), duplicate_id)
        if ignored_ids:
            base["ignored_duplicate_position_ids"] = ignored_ids
        base["duplicate_signal_guard"] = {
            "last_duplicate_position_id": duplicate_id,
            "last_duplicate_signal_ids": signal_ids,
            "last_seen_at": duplicate.get("opened_at") or _now_local_iso(),
            "refund_applied": bool(refund_debit),
        }

        if refund_debit:
            refund = self._duplicate_position_refund_amount(duplicate)
            if refund > 0:
                self.balance = float(self.balance or 0.0) + refund
                self._add_transaction(
                    "DUPLICATE_POSITION_REFUND",
                    refund,
                    self.balance,
                    f"{base.get('ticker')} duplicate signal ignored ({', '.join(signal_ids) or duplicate_id})",
                    metadata=self._ledger_position_metadata(
                        duplicate,
                        duplicate_of_position_id=str(base.get("position_id") or ""),
                    ),
                )

        return base

    def _merge_duplicate_open_positions(self, positions: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
        merged: List[Dict[str, Any]] = []
        open_key_to_idx: Dict[Tuple[str, str, str, int], int] = {}
        duplicate_count = 0

        for raw_pos in positions:
            pos = dict(raw_pos or {}) if isinstance(raw_pos, dict) else raw_pos
            if not isinstance(pos, dict) or str(pos.get("status", "")).upper() != "OPEN":
                merged.append(pos)
                continue

            symbol = self._normalize_position_ticker(pos.get("ticker"))
            side = self._normalize_position_direction(pos.get("direction"))
            ptype = self._position_type(pos)
            try:
                leverage = int(pos.get("leverage", 1) or 1)
            except Exception:
                leverage = 1

            if not symbol or side not in {"LONG", "SHORT"}:
                merged.append(pos)
                continue

            key = (symbol, side, ptype, leverage if ptype == "manual" else 1)
            if key not in open_key_to_idx:
                open_key_to_idx[key] = len(merged)
                pos["position_type"] = ptype
                if ptype == "paper_signal":
                    pos["direction"] = side
                merged.append(pos)
                continue

            base_idx = open_key_to_idx[key]
            if self._is_duplicate_signal_position(merged[base_idx], pos):
                merged[base_idx] = self._mark_duplicate_position_ignored(
                    merged[base_idx],
                    pos,
                    refund_debit=True,
                )
            else:
                merged[base_idx] = self._merge_position_addon(
                    merged[base_idx],
                    pos,
                    reason="state_duplicate_migration",
                )
            duplicate_count += 1

        if duplicate_count:
            logger.warning(f"Merged {duplicate_count} duplicate open capital position(s) from persistent state")
        return merged, duplicate_count

    def _build_fallback_decision_context(
        self,
        *,
        ticker: str,
        direction: str,
        entry_price: float,
        take_profit_price: float,
        stop_loss_price: float,
        usd_size: float,
        leverage: int = 1,
        reason: str = "",
        strategy_type: str = "",
        decision_source: str = "system_fallback",
    ) -> Dict[str, Any]:
        from core.trade_decision import build_manual_decision_context

        context = build_manual_decision_context(
            ticker=ticker,
            direction=direction,
            entry_price=entry_price,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            usd_size=usd_size,
            leverage=leverage,
            reason=reason or "Recovered position context",
            strategy_type=strategy_type or "SYSTEM",
        )
        context["decision_source"] = decision_source
        context["decision_status"] = "accepted"
        return context

    def _apply_position_explainability(
        self,
        position: Dict[str, Any],
        *,
        decision_context: Optional[Dict[str, Any]] = None,
        strategy_type: str = "",
        entry_reason: str = "",
    ) -> Dict[str, Any]:
        from core.portfolio.position import normalize_position_evidence
        from core.trade_decision import summarize_position_explainability

        pos = dict(position or {})
        if not isinstance(decision_context, dict):
            decision_context = self._build_fallback_decision_context(
                ticker=str(pos.get("ticker") or ""),
                direction=str(pos.get("direction") or "LONG"),
                entry_price=self._safe_float(pos.get("entry_price"), 0.0),
                take_profit_price=self._safe_float(pos.get("take_profit_price"), 0.0),
                stop_loss_price=self._safe_float(pos.get("stop_loss_price"), 0.0),
                usd_size=self._safe_float(pos.get("usd_size"), 0.0),
                leverage=int(self._safe_float(pos.get("leverage"), 1.0)),
                reason=entry_reason or str(pos.get("note") or ""),
                strategy_type=strategy_type or str(pos.get("strategy_type") or ""),
            )
        else:
            # Capital state is persisted as JSON in both SQLite and the mirror
            # file.  Keep a defensive boundary here so an upstream provider or
            # dataclass can never turn a successful paper fill into an orphaned
            # execution order during the subsequent save.
            decision_context = json.loads(
                json.dumps(decision_context, ensure_ascii=False, default=str)
            )

        summary = summarize_position_explainability({"decision_context": decision_context})
        pos["decision_context"] = decision_context
        pos["entry_reason"] = entry_reason or summary.get("reason") or str(pos.get("note") or "")
        pos["strategy_type"] = strategy_type or summary.get("strategy_type") or "SYSTEM"
        pos["decision_timestamp"] = summary.get("timestamp") or pos.get("opened_at")
        pos["decision_status"] = summary.get("decision_status") or "accepted"
        pos["score_snapshot"] = dict(summary.get("scoring") or {})
        pos["sizing_context"] = dict(summary.get("sizing") or {})
        pos["risk_context"] = dict(summary.get("risk") or {})
        pos.update(
            normalize_position_evidence(
                pos,
                decision_context=decision_context,
                strategy_type=pos["strategy_type"],
                entry_reason=pos["entry_reason"],
            )
        )
        return pos

    def save(self):
        """Save capital state to SQLite + JSON mirror."""
        with self._lock:
            self._save_unlocked()

    def get_balance(self) -> float:
        with self._lock:
            self._sync_for_read()
            return self.balance

    def get_capital_accounting(self) -> Dict[str, Any]:
        with self._lock:
            self._sync_for_read()
            return dict(self._capital_accounting_unlocked())

    def get_deployable_balance(self) -> float:
        return float(self.get_capital_accounting()["deployable_cash_balance"])

    def get_history(self) -> List[Dict]:
        with self._lock:
            self._sync_for_read()
            # Copies: unchanged stored state is no longer re-parsed for every
            # read, so a caller editing a returned row must not reach the
            # objects the next reader gets.
            return [dict(entry) if isinstance(entry, dict) else entry for entry in self.history]

    def get_positions(self) -> List[Dict]:
        with self._lock:
            self._sync_for_read()
            return [dict(pos) if isinstance(pos, dict) else pos for pos in self.positions]

    def get_equity_curve(self, limit: int = 30) -> List[Dict]:
        """Return the newest ``limit`` points of the tiered equity history.

        Oldest first: daily points, then hourly (last 7 days), then every
        recorded point (last 2 hours); each carries its ``resolution``. A
        stored list from before the tiers existed is migrated in place here.
        """
        from core.portfolio.equity_history import compact_equity_history, needs_migration

        history: List[Dict] = []
        if self._store:
            try:
                history = self._store.get_json("equity_history", [])
                if not isinstance(history, list):
                    history = []
            except Exception:
                history = []
        if not history:
            return []
        try:
            compacted = compact_equity_history(history)
        except Exception as exc:
            logger.error(f"Equity history compaction failed; serving the stored list: {exc}")
            return list(history[-limit:])
        if needs_migration(history):
            try:
                self._store.set_json("equity_history", compacted)
                logger.info(
                    "Equity history migrated to tiered resolution: %s stored points -> %s",
                    len(history),
                    len(compacted),
                )
            except Exception as exc:
                logger.error(f"Equity history migration not saved: {exc}")
        return list(compacted[-limit:])

    def get_ledger(self, limit: int = 20) -> List[Dict]:
        """Return reconciled transactions with explicit Capital/research scope."""
        with self._lock:
            self._sync_for_read()
            return [
                self._enrich_ledger_entry_unlocked(entry)
                for entry in self.history[-limit:]
                if isinstance(entry, dict)
            ]

    def _position_for_ledger_entry_unlocked(
        self,
        entry: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Resolve a legacy ledger row to its position without mutating history."""
        metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
        position_id = str(metadata.get("position_id") or entry.get("position_id") or "").strip()
        if position_id:
            for position in self.positions or []:
                if str((position or {}).get("position_id") or "").strip() == position_id:
                    return position

        details = str(entry.get("details") or "").upper().strip()
        ticker = str(metadata.get("ticker") or "").upper().strip()
        if not ticker and details:
            ticker = details.split()[0].strip()
        event_time = self._timestamp_utc(entry.get("timestamp"))
        if not ticker or event_time is None:
            return None

        event_type = str(entry.get("type") or "").upper().strip()
        timestamp_fields = {
            "OPEN_POSITION": ("opened_at",),
            "SCALE_POSITION": ("last_added_at", "opened_at"),
            "RELEASE_MARGIN": ("closed_at",),
            "CLOSE_POSITION": ("closed_at",),
            "UPDATE_RISK_LEVELS": ("risk_levels_updated_at",),
            "DUPLICATE_POSITION_REFUND": ("opened_at",),
        }.get(event_type, ())
        if not timestamp_fields:
            return None

        best: Optional[Dict[str, Any]] = None
        best_delta = float("inf")
        for position in self.positions or []:
            if not isinstance(position, dict):
                continue
            if self._normalize_position_ticker(position.get("ticker")) != self._normalize_position_ticker(ticker):
                continue
            for field in timestamp_fields:
                position_time = self._timestamp_utc(position.get(field))
                if position_time is None:
                    continue
                delta = abs((position_time - event_time).total_seconds())
                if delta <= 15.0 and delta < best_delta:
                    best = position
                    best_delta = delta
        return best

    def _enrich_ledger_entry_unlocked(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        enriched = dict(entry or {})
        metadata = (
            dict(enriched.get("metadata") or {})
            if isinstance(enriched.get("metadata"), dict)
            else {}
        )
        scope = str(enriched.get("account_scope") or metadata.get("account_scope") or "").lower().strip()
        position = self._position_for_ledger_entry_unlocked(enriched)
        if scope not in {"capital", "research"} and position is not None:
            position_metadata = self._ledger_position_metadata(position)
            for key, value in position_metadata.items():
                metadata.setdefault(key, value)
            scope = str(position_metadata["account_scope"])
        if scope not in {"capital", "research"} and str(enriched.get("type") or "").upper() in {
            "RESET",
            "DEPOSIT",
            "WITHDRAW",
            "WIN",
            "LOSS",
        }:
            scope = "capital"
        if scope not in {"capital", "research"}:
            scope = "unclassified"

        if (
            str(enriched.get("type") or "").upper() == "CLOSE_POSITION"
            and isinstance(position, dict)
        ):
            metadata["position_snapshot"] = self._closed_position_snapshot(position)

        raw_balance = self._safe_float(enriched.get("raw_balance", enriched.get("balance")), 0.0)
        enriched["raw_balance"] = raw_balance
        enriched["balance"] = raw_balance
        enriched["balance_kind"] = "raw_shared_cash"
        enriched["account_scope"] = scope
        enriched["affects_deployable_capital"] = (
            scope == "capital" if scope in {"capital", "research"} else None
        )
        if metadata:
            enriched["metadata"] = metadata
        return enriched

    def get_decisions(self, limit: int = 10) -> List[Dict]:
        """Return last N trade decision/audit events."""
        try:
            from core.trade_audit import get_trade_audit
            return get_trade_audit(limit=limit)
        except Exception as e:
            logger.error(f"Error fetching trade decisions: {e}")
            return []

    def get_position(self, position_id: str) -> Optional[Dict]:
        pid = str(position_id or "").strip()
        if not pid:
            return None
        with self._lock:
            self._sync_for_read()
            for pos in self.positions:
                if str((pos or {}).get("position_id", "")).strip() == pid:
                    return dict(pos)
        return None

    def update_position_levels(
        self,
        *,
        position_id: str,
        stop_loss_price: float,
        take_profit_price: float,
        current_price: float,
        reason: str = "Manual SL/TP update",
    ) -> Dict[str, Any]:
        """Atomically replace an open position's protection levels.

        A manual level update becomes authoritative for that position: stale
        trailing/AI overrides are cleared so they cannot silently supersede the
        levels the operator just selected.
        """
        pid = str(position_id or "").strip()
        stop_price = self._safe_float(stop_loss_price, 0.0)
        target_price = self._safe_float(take_profit_price, 0.0)
        market_price = self._safe_float(current_price, 0.0)
        update_reason = str(reason or "Manual SL/TP update").strip()[:240]

        if not pid:
            raise ValueError("position_id is required")
        if not all(math.isfinite(value) and value > 0 for value in (stop_price, target_price, market_price)):
            raise ValueError("stop_loss_price, take_profit_price and current_price must be finite and > 0")

        # Keep a small live-price buffer so a save cannot act as an accidental
        # market close while the PnL tracker is evaluating the same position.
        minimum_gap = market_price * 0.0005
        updated: Optional[Dict[str, Any]] = None
        old_levels: Dict[str, Any] = {}

        with self._lock:
            self._sync_from_storage(initialize_if_missing=False)
            for index, current in enumerate(self.positions):
                if str((current or {}).get("position_id") or "").strip() != pid:
                    continue
                position = dict(current or {})
                if str(position.get("status") or "").upper() != "OPEN":
                    raise ValueError("position already closed")

                direction = self._normalize_position_direction(position.get("direction"))
                if direction == "LONG":
                    if stop_price >= market_price - minimum_gap:
                        raise ValueError("LONG stop loss must remain at least 0.05% below the live price")
                    if target_price <= market_price + minimum_gap:
                        raise ValueError("LONG take profit must remain at least 0.05% above the live price")
                elif direction == "SHORT":
                    if stop_price <= market_price + minimum_gap:
                        raise ValueError("SHORT stop loss must remain at least 0.05% above the live price")
                    if target_price >= market_price - minimum_gap:
                        raise ValueError("SHORT take profit must remain at least 0.05% below the live price")
                else:
                    raise ValueError("position direction must be LONG or SHORT")

                old_levels = {
                    "stop_loss_price": self._safe_float(position.get("stop_loss_price"), 0.0),
                    "take_profit_price": self._safe_float(position.get("take_profit_price"), 0.0),
                    "trailing_sl": self._safe_float(position.get("trailing_sl"), 0.0),
                }
                changed_at = _now_local_iso()
                position["stop_loss_price"] = round(stop_price, 8)
                position["take_profit_price"] = round(target_price, 8)
                position["trailing_sl"] = 0.0
                position["automatic_trailing_enabled"] = False
                position["ai_exit_management_enabled"] = False
                position["exit_management_mode"] = "manual_levels_only"
                position["manual_risk_override"] = True
                position["risk_levels_updated_at"] = changed_at
                position["risk_levels_update_reason"] = update_reason

                from core.trade_decision import _compute_risk_profile

                current_risk = _compute_risk_profile(
                    self._safe_float(position.get("entry_price"), 0.0),
                    target_price,
                    stop_price,
                    direction,
                )
                current_risk["manual_override"] = True
                current_risk["updated_at"] = changed_at
                position["risk_context"] = current_risk

                history = list(position.get("risk_level_history") or [])
                history.append(
                    {
                        "timestamp": changed_at,
                        "reason": update_reason,
                        "live_price": round(market_price, 8),
                        "old_stop_loss_price": round(old_levels["stop_loss_price"], 8),
                        "old_take_profit_price": round(old_levels["take_profit_price"], 8),
                        "old_trailing_sl": round(old_levels["trailing_sl"], 8),
                        "stop_loss_price": round(stop_price, 8),
                        "take_profit_price": round(target_price, 8),
                    }
                )
                position["risk_level_history"] = history[-20:]
                self.positions[index] = position
                self._add_transaction(
                    "UPDATE_RISK_LEVELS",
                    0.0,
                    self.balance,
                    (
                        f"{position.get('ticker', '')} {direction} manual SL/TP "
                        f"{old_levels['stop_loss_price']:.8g}/{old_levels['take_profit_price']:.8g} "
                        f"-> {stop_price:.8g}/{target_price:.8g}"
                    ),
                    metadata=self._ledger_position_metadata(
                        position,
                        live_price=round(market_price, 8),
                        reason=update_reason,
                    ),
                )
                self._save_unlocked()
                updated = dict(position)
                break

        if updated is None:
            raise KeyError("position not found")

        try:
            from core.trade_audit import record_trade_event

            record_trade_event(
                event_type="position_risk_levels_updated",
                status="open",
                ticker=updated.get("ticker", ""),
                signal_id=updated.get("signal_id", ""),
                position_id=pid,
                strategy_type=updated.get("strategy_type", ""),
                summary=update_reason,
                payload={
                    "old_levels": old_levels,
                    "stop_loss_price": updated["stop_loss_price"],
                    "take_profit_price": updated["take_profit_price"],
                    "live_price": market_price,
                    "automatic_trailing_enabled": False,
                    "ai_exit_management_enabled": False,
                },
            )
        except Exception as exc:
            logger.error(f"Failed to audit position risk update {pid}: {exc}")

        return updated

    def attach_entry_chart_snapshot(self, position_id: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """Atomically attach immutable entry-chart evidence to a paper position."""
        pid = str(position_id or "").strip()
        if not pid:
            raise ValueError("position_id is required")
        if not isinstance(snapshot, dict) or not snapshot:
            raise ValueError("snapshot metadata is required")
        with self._lock:
            self._sync_from_storage(initialize_if_missing=False)
            for index, current in enumerate(self.positions):
                if str((current or {}).get("position_id") or "").strip() != pid:
                    continue
                position = dict(current or {})
                decision_context = dict(position.get("decision_context") or {})
                # Never overwrite a successful frozen capture with a later
                # retry/failure. The first entry image is the audit artifact.
                existing = decision_context.get("entry_chart_snapshot")
                if isinstance(existing, dict) and existing.get("available") is True:
                    return dict(position)
                snapshot_metadata = dict(snapshot)
                decision_context["entry_chart_snapshot"] = snapshot_metadata
                position["entry_chart_snapshot"] = snapshot_metadata
                position = self._apply_position_explainability(
                    position,
                    decision_context=decision_context,
                    strategy_type=str(position.get("strategy_type") or ""),
                    entry_reason=str(position.get("entry_reason") or ""),
                )
                self.positions[index] = position
                self._save_unlocked()
                try:
                    from core.trade_audit import record_trade_event

                    record_trade_event(
                        event_type="entry_chart_captured",
                        status="captured" if snapshot_metadata.get("available") else "unavailable",
                        ticker=position.get("ticker", ""),
                        signal_id=position.get("signal_id", "") or "",
                        position_id=pid,
                        strategy_type=position.get("strategy_type", ""),
                        summary=(
                            f"Frozen {snapshot_metadata.get('timeframe', '30m')} entry chart attached"
                            if snapshot_metadata.get("available")
                            else str(snapshot_metadata.get("reason") or "Entry chart unavailable")
                        ),
                        payload={"position": dict(position), "entry_chart_snapshot": snapshot_metadata},
                    )
                except Exception as exc:
                    logger.error("Failed to audit entry chart for %s: %s", pid, exc)
                return dict(position)
        raise ValueError(f"position not found: {pid}")

    def set_balance(self, amount: float, reason: str = "Manual Reset", clear_positions: bool = True):
        """Force set the balance (e.g., reset)."""
        removed_positions: List[Dict[str, Any]] = []
        reset_at = _now_local_iso()
        reset_equity = float(amount)
        with self._lock:
            self._sync_from_storage(initialize_if_missing=False)
            old_balance = self.balance
            self.balance = float(amount)
            if clear_positions:
                removed_positions = [
                    dict(position)
                    for position in self.positions
                    if isinstance(position, dict)
                    and str(position.get("status") or "").upper() == "OPEN"
                ]
                self.positions = []
            else:
                # Capital-integrity: kept OPEN positions still have their margin
                # reserved (debited from the OLD balance at open). close_position
                # will credit that margin back when each position closes. If we
                # leave the new balance at `amount`, that future credit becomes a
                # DOUBLE credit (margin was never deducted from the new balance).
                # Subtract the total reserved open margin so the cash balance
                # stays consistent with the live reservation. Pre-fix this path
                # inflated the balance by sum(open margin_used) on every
                # reset-with-positions-kept.
                open_margin = 0.0
                for p in self.positions:
                    p = p or {}
                    if str(p.get("status", "")).upper() == "OPEN":
                        try:
                            open_margin += float(p.get("margin_used", 0.0) or 0.0)
                        except Exception:
                            pass
                self.balance = float(amount) - open_margin
            self._add_transaction(
                "RESET",
                amount,
                self.balance,
                reason,
                metadata={
                    "account_scope": "capital",
                    "clear_positions": bool(clear_positions),
                    "removed_position_ids": [
                        str(position.get("position_id") or "")
                        for position in removed_positions
                        if str(position.get("position_id") or "")
                    ],
                },
            )
            if self.history:
                reset_at = str(self.history[-1].get("timestamp") or reset_at)
            self._save_unlocked()
            # Reset the equity curve too so it never spans a balance reset. A
            # pre-reset near-zero equity next to a post-reset 100k produced absurd
            # % swings on the cockpit chart (e.g. +10,000%). Start fresh: drop the
            # old history and seed a single baseline point at the reset equity
            # (cash + any margin still reserved on kept positions).
            if self._store:
                try:
                    self._store.set_json("equity_history", [])
                except Exception:
                    pass
            open_margin_now = 0.0
            open_count_now = 0
            for p in self.positions:
                if (
                    str((p or {}).get("status", "")).upper() == "OPEN"
                    and not self._is_research_position(p)
                ):
                    open_count_now += 1
                    try:
                        open_margin_now += float(p.get("margin_used", 0.0) or 0.0)
                    except Exception:
                        pass
            deployable_cash = self._capital_accounting_unlocked()["deployable_cash_balance"]
            reset_equity = deployable_cash + open_margin_now
            self._append_equity_snapshot(
                reset_equity,
                open_count_now,
                deployable_cash=deployable_cash,
                open_margin=open_margin_now,
                mark_to_market_pnl=0.0,
            )
            logger.info(f"Capital reset from ${old_balance:.2f} to ${self.balance:.2f}")

        # Never acquire the signal lock while holding the Capital lock. The
        # signal reconciler calls back into Capital and uses the reverse order.
        if removed_positions:
            try:
                from core.signals import get_signal_manager

                get_signal_manager().archive_positions_removed_by_reset(
                    removed_positions,
                    reason=reason,
                    reset_at=reset_at,
                )
            except Exception:
                logger.exception("Failed to archive signals cleared by Capital reset")

        try:
            from core.bot_controller import BotController

            BotController.notify_capital_reset(reset_equity)
        except Exception:
            logger.exception("Failed to rebase circuit breaker after Capital reset")

    def deposit(self, amount: float, reason: str = "Deposit"):
        """Add funds."""
        if amount <= 0:
            return
        with self._lock:
            self._sync_from_storage(initialize_if_missing=False)
            self.balance += float(amount)
            self._add_transaction(
                "DEPOSIT",
                amount,
                self.balance,
                reason,
                metadata={"account_scope": "capital"},
            )
            self._save_unlocked()
            logger.info(f"Deposited ${amount:.2f}. New Balance: ${self.balance:.2f}")

    def add_trade_pnl(self, pnl_amount: float, ticker: str, signal_id: str):
        """Update balance based on trade PnL (can be negative)."""
        with self._lock:
            # Pull latest persisted state before applying pnl so one process
            # cannot overwrite a recent reset/write from another process.
            self._sync_from_storage(initialize_if_missing=False)
            self.balance += float(pnl_amount)
            action = "WIN" if pnl_amount >= 0 else "LOSS"
            self._add_transaction(
                action,
                pnl_amount,
                self.balance,
                f"Trade {ticker} ({signal_id})",
                metadata={
                    "account_scope": "capital",
                    "ticker": str(ticker or ""),
                    "signal_id": str(signal_id or ""),
                },
            )
            self._save_unlocked()

    def _get_cached_price(self, ticker: str) -> Optional[Tuple[float, str]]:
        """
        Get price from state cache first, avoiding external API calls.
        Returns (price, source) if cache hit, None if cache miss or stale.

        Cache freshness: stocks < 5 seconds, crypto < 3 seconds.
        """
        import time
        from datetime import datetime, timezone

        symbol = str(ticker).upper().strip()
        is_crypto = (
            symbol.endswith("USDT")
            or symbol.endswith("USD")
            or symbol in ["BTC", "ETH", "SOL"]
        )
        max_age = 3.0 if is_crypto else 5.0  # seconds

        now = time.time()

        # First: check in-memory price cache
        with self._price_cache_lock:
            cached = self._price_cache.get(symbol)
            if cached:
                price, timestamp = cached
                if (now - timestamp) < max_age:
                    return price, "in_memory_cache"

        # Second: try to read from state cache (from state_store or state_persistence)
        try:
            # Try state_store first (fastest - in-memory)
            from core.state_store import get_state_store

            store = get_state_store()
            state_snap = store.get_snapshot("state")
            if state_snap and isinstance(state_snap, dict):
                tickers = state_snap.get("tickers", {})
                ticker_data = tickers.get(symbol)
                if isinstance(ticker_data, dict):
                    price = ticker_data.get("price")
                    last_update = ticker_data.get("last_update")
                    if price and last_update:
                        try:
                            last_dt = datetime.fromisoformat(last_update)
                            if last_dt.tzinfo is None:
                                last_dt = last_dt.replace(tzinfo=timezone.utc)
                            now_dt = datetime.now(timezone.utc)
                            age_sec = (now_dt - last_dt).total_seconds()
                            if age_sec < max_age:
                                return float(price), "state_store"
                        except Exception:
                            pass
        except Exception:
            pass

        # Third: try state_persistence as fallback (SQLite/JSON)
        try:
            from core.state_persistence import load_state_payload

            state = load_state_payload({})
            if isinstance(state, dict):
                tickers = state.get("tickers", {})
                ticker_data = tickers.get(symbol)
                if isinstance(ticker_data, dict):
                    price = ticker_data.get("price")
                    last_update = ticker_data.get("last_update")
                    if price and last_update:
                        try:
                            last_dt = datetime.fromisoformat(last_update)
                            if last_dt.tzinfo is None:
                                last_dt = last_dt.replace(tzinfo=timezone.utc)
                            now_dt = datetime.now(timezone.utc)
                            age_sec = (now_dt - last_dt).total_seconds()
                            if age_sec < max_age:
                                return float(price), "state_persistence"
                        except Exception:
                            pass
        except Exception:
            pass

        return None

    def _update_price_cache(
        self,
        ticker: str,
        price: float,
        source: str = "capital_price_cache",
    ) -> None:
        """Update both the local and cross-process verified-price caches."""
        import time

        with self._price_cache_lock:
            self._price_cache[ticker.upper().strip()] = (price, time.time())
        try:
            from core.live_price_cache import store_live_price

            store_live_price(ticker, price, source=source)
        except Exception as exc:
            logger.debug(f"Cross-process live price cache skipped for {ticker}: {exc}")

    def _get_atr_for_symbol(self, symbol: str, last_price: float) -> float:
        """Fetch ATR from cached state or compute via yfinance. Returns ATR value."""
        import time
        sym = symbol.upper().strip()
        now = time.time()
        with self._atr_cache_lock:
            cached = self._atr_cache.get(sym)
            if cached:
                atr_val, timestamp = cached
                if (now - timestamp) < 300:  # 5 minute cache
                    return float(atr_val)
        try:
            import yfinance as yf
            t = yf.Ticker(sym)
            hist = t.history(period="1mo", interval="1d")
            if hist is not None and not hist.empty and len(hist) >= 15:
                high_low = hist["High"] - hist["Low"]
                high_close = (hist["High"] - hist["Close"].shift()).abs()
                low_close = (hist["Low"] - hist["Close"].shift()).abs()
                tr = high_low.combine(high_close, max).combine(low_close, max)
                atr_val = float(tr.rolling(window=14).mean().iloc[-1])
                if atr_val > 0 and last_price > 0:
                    with self._atr_cache_lock:
                        self._atr_cache[sym] = (atr_val, now)
                    return atr_val
        except Exception:
            pass
        return 0.0

    def _get_execution_price(self, ticker: str, direction: str, *, use_cache: bool = True) -> dict:
        """
        direction: "BUY" or "SELL"
        Cache-first by default; set use_cache=False for order execution so entry/exit
        prices use the current bid/ask or orderbook side.
        """
        from core.http_utils import http_get
        from datetime import datetime
        import yfinance as yf
        from core.config import ConfigManager
        from core.trading_costs import resolve_trading_cost_model

        symbol = str(ticker).upper().strip()
        cost_model = resolve_trading_cost_model(
            symbol,
            strategy_v2_config=ConfigManager().strategy_v2,
        )
        side = str(direction).upper().strip()
        # "LONG" / "SHORT" map to "BUY"/"SELL"
        if side == "LONG":
            side = "BUY"
        elif side == "SHORT":
            side = "SELL"

        is_crypto = (
            symbol.endswith("USDT")
            or symbol.endswith("USD")
            or symbol in ["BTC", "ETH", "SOL"]
        )
        binance_sym = symbol.replace("-", "")
        if is_crypto:
            if binance_sym.endswith("USD") and not binance_sym.endswith("USDT"):
                binance_sym = binance_sym[:-3] + "USDT"
            if not (
                binance_sym.endswith("USDT")
                or binance_sym.endswith("BUSD")
                or binance_sym.endswith("USDC")
            ):
                binance_sym += "USDT"

        execution_price = 0.0
        spread_pct = 0.0
        slippage_pct = 0.0
        source = "Unknown"
        etoro_indicative_price = 0.0

        if use_cache:
            cached = self._get_cached_price(symbol)
            if cached:
                price, cache_source = cached
                # Add small spread adjustment for BUY/SELL side
                spread_factor = 0.0005  # 5 bps default
                if symbol in ["AAPL", "MSFT", "NVDA", "SPY", "QQQ"]:
                    spread_factor = 0.0001
                execution_price = price * (
                    1 + spread_factor if side == "BUY" else 1 - spread_factor
                )
                spread_pct = spread_factor * 2 * 100
                source = f"{cache_source}"

        def _apply_book_top(best_bid: float, best_ask: float, source_name: str) -> None:
            nonlocal execution_price, spread_pct, source
            execution_price = best_ask if side == "BUY" else best_bid
            mid = (best_bid + best_ask) / 2
            if mid > 0:
                spread_pct = (best_ask - best_bid) / mid * 100
            source = source_name
            # Update cache with mid price
            self._update_price_cache(symbol, mid, source=f"{source_name}_mid")

        def _parse_binance_depth(response, source_name: str) -> None:
            if not (response and response.status_code == 200):
                return
            data = response.json()
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            if bids and asks:
                _apply_book_top(float(bids[0][0]), float(asks[0][0]), source_name)

        # Crypto: Binance orderbook if not in cache
        if execution_price <= 0 and is_crypto:
            try:
                r = http_get(
                    "https://api.binance.com/api/v3/depth",
                    provider="binance",
                    params={"symbol": binance_sym, "limit": 5},
                    timeout=(1, 3),
                    max_retries=0,
                    priority=True,
                )
                _parse_binance_depth(r, "Binance_orderbook")
            except Exception as e:
                logger.error(f"Error fetching depth for {binance_sym}: {e}")

        # Binance spot lists fewer tokens than Binance futures; new listings
        # often trade as perps only (e.g. BULLAUSDT), so fall back to the fapi
        # orderbook — the same venue the TradingView chart feed shows.
        if execution_price <= 0 and is_crypto:
            try:
                r = http_get(
                    "https://fapi.binance.com/fapi/v1/depth",
                    provider="binance",
                    params={"symbol": binance_sym, "limit": 5},
                    timeout=(1, 3),
                    max_retries=0,
                    priority=True,
                )
                _parse_binance_depth(r, "Binance_futures_orderbook")
            except Exception as e:
                logger.error(f"Error fetching futures depth for {binance_sym}: {e}")

        # KuCoin lists tokens absent from Binance spot and futures alike; the
        # public level1 endpoint returns the live top of book.
        if execution_price <= 0 and is_crypto:
            kucoin_sym = binance_sym[:-4] + "-USDT"
            try:
                r = http_get(
                    "https://api.kucoin.com/api/v1/market/orderbook/level1",
                    provider="kucoin",
                    params={"symbol": kucoin_sym},
                    timeout=(1, 3),
                    max_retries=0,
                    priority=True,
                )
                if r and r.status_code == 200:
                    data = (r.json() or {}).get("data") or {}
                    best_bid = float(data.get("bestBid") or 0.0)
                    best_ask = float(data.get("bestAsk") or 0.0)
                    if best_bid > 0 and best_ask > 0:
                        _apply_book_top(best_bid, best_ask, "KuCoin_orderbook")
                    else:
                        last_price_kc = float(data.get("price") or 0.0)
                        if last_price_kc > 0:
                            execution_price = last_price_kc
                            source = "KuCoin_orderbook"
                            self._update_price_cache(symbol, last_price_kc, source="KuCoin_orderbook_last")
            except Exception as e:
                logger.error(f"Error fetching KuCoin book top for {kucoin_sym}: {e}")

        # eToro live quote (fast, low-latency, real bid/ask/spread for equities and crypto)
        if execution_price <= 0:
            try:
                from core.etoro_client import get_etoro_client
                et = get_etoro_client()
                if et.is_available():
                    etoro_sym = symbol
                    if is_crypto:
                        for suf in ("-USD", "-USDT", "USDT", "USD"):
                            if etoro_sym.endswith(suf) and len(etoro_sym) > len(suf):
                                etoro_sym = etoro_sym[:-len(suf)]
                                break
                    quote = et.get_quote(etoro_sym)
                    if quote and quote.get("price") and float(quote.get("price") or 0.0) > 0:
                        bid = quote.get("bid")
                        ask = quote.get("ask")
                        last_price = float(quote["price"])
                        has_book = bid is not None and ask is not None and bid > 0 and ask > 0
                        mid = (bid + ask) / 2 if has_book else 0.0
                        # Same staleness rule as the Yahoo book below: a wide
                        # top of book is an indicative quote, not a fillable one.
                        book_is_tight = has_book and mid > 0 and (ask - bid) / mid <= 0.015
                        if book_is_tight:
                            execution_price = ask if side == "BUY" else bid
                            spread_pct = (ask - bid) / mid * 100
                            source = "eToro_bid_ask"
                            self._update_price_cache(symbol, mid, source="eToro_bid_ask_mid")
                        elif is_crypto:
                            execution_price = last_price
                            source = "eToro_quote"
                            self._update_price_cache(symbol, last_price, source="eToro_quote_last")
                        else:
                            # Equities: an indicative eToro quote must not
                            # pre-empt Yahoo. It did, and because it is never a
                            # verified fill price, stock exits stopped firing
                            # (11 positions stayed OPEN for two months).
                            etoro_indicative_price = last_price
            except Exception as e:
                logger.debug(f"eToro execution price error for {symbol}: {e}")

        # Fallback: Yahoo Finance if not in cache and not crypto
        if execution_price <= 0 and not is_crypto:
            try:
                import concurrent.futures

                def _fetch_yahoo(sym):
                    t = yf.Ticker(sym)
                    # Try fast_info last_price first, then bid/ask from info
                    last_price = float(t.fast_info.last_price or 0.0)
                    bid = None
                    ask = None
                    try:
                        info = t.info
                        if isinstance(info, dict):
                            bid_val = info.get("bid")
                            ask_val = info.get("ask")
                            if bid_val is not None and ask_val is not None:
                                bid = float(bid_val)
                                ask = float(ask_val)
                    except Exception:
                        pass
                    return {"last_price": last_price, "bid": bid, "ask": ask}

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(_fetch_yahoo, symbol)
                    yahoo_data = future.result(timeout=5)

                last_price = float(yahoo_data.get("last_price") or 0.0)
                bid = yahoo_data.get("bid")
                ask = yahoo_data.get("ask")

                if last_price <= 0:
                    raise ValueError("Yahoo returned zero price")

                # Prefer real bid/ask when available AND reasonable
                if (
                    bid is not None and ask is not None and bid > 0 and ask > 0
                    and last_price > 0
                ):
                    spread_abs = ask - bid
                    spread_rel = spread_abs / last_price if last_price > 0 else 1.0
                    # The book must also sit next to the last trade. A tight book far away from it is
                    # not a market: on 2026-11-24 SITM was stopped out at an ask of 539.09 while it
                    # traded 383.5-386.6, and MTSI at 224.29 while it traded 202-205.
                    mid_gap = abs((bid + ask) / 2.0 / last_price - 1.0) if last_price > 0 else 1.0
                    # Reject stale/wide spreads (more than 1.5% of last price)
                    if spread_rel <= 0.015 and mid_gap <= 0.02:
                        execution_price = ask if side == "BUY" else bid
                        mid = (bid + ask) / 2
                        if mid > 0:
                            spread_pct = (ask - bid) / mid * 100
                        source = "Yahoo_bid_ask"
                        self._update_price_cache(symbol, mid, source="Yahoo_bid_ask_mid")
                    else:
                        self._log_quote_warning(
                            f"yahoo_stale_spread:{symbol}",
                            f"Yahoo bid/ask for {symbol} rejected as stale "
                            f"(bid={bid:.2f}, ask={ask:.2f}, last={last_price:.2f}, "
                            f"spread={spread_rel*100:.1f}% (max 1.5%), "
                            f"mid {mid_gap*100:.1f}% from the last trade (max 2%))",
                        )
                        bid = None
                        ask = None
                if execution_price <= 0:
                    # ATR-based spread simulation
                    atr = self._get_atr_for_symbol(symbol, last_price)
                    if atr > 0 and last_price > 0:
                        spread_pct_raw = min((atr / last_price) * 100, 0.5)
                    else:
                        spread_pct_raw = 0.05  # 5 bps default
                    spread_factor = spread_pct_raw / 100.0
                    execution_price = last_price * (
                        1 + spread_factor if side == "BUY" else 1 - spread_factor
                    )
                    spread_pct = spread_pct_raw
                    source = "Yahoo_quote"
                    self._update_price_cache(symbol, last_price, source="Yahoo_quote")
            except concurrent.futures.TimeoutError:
                self._log_quote_warning(
                    f"yahoo_quote_timeout:{symbol}",
                    f"Yahoo quote timeout for {symbol} (5s)",
                )
            except Exception as e:
                logger.error(f"Error fetching Yahoo quote for {symbol}: {e}")

        if execution_price <= 0 and etoro_indicative_price > 0:
            # Display-only last resort; graded ESTIMATED below, never fillable.
            execution_price = etoro_indicative_price
            source = "eToro_quote"

        commission_rate = cost_model.commission_fraction
        from core.data_quality import QualifiedValue

        if execution_price <= 0:
            execution_price = 0.0
            source = "Unavailable"
            qval = QualifiedValue.unavailable(
                "execution_price",
                f"No verified execution price for {symbol}",
            )
        elif source in ["in_memory_cache", "state_store", "state_persistence"]:
            qval = QualifiedValue.verified(execution_price, source)
        elif source in [
            "Binance_orderbook",
            "Binance_futures_orderbook",
            "KuCoin_orderbook",
            "eToro_bid_ask",
            "Yahoo_bid_ask",
            "Yahoo_quote",
        ]:
            qval = QualifiedValue.verified(execution_price, source)
        else:
            qval = QualifiedValue.estimated(
                execution_price,
                source,
                "Execution price is estimated and cannot be used for paper fills",
            )

        return {
            "execution_price": execution_price,
            "execution_price_quality": qval.to_dict(),
            "spread_pct": spread_pct,
            "slippage_pct": slippage_pct,
            "commission_rate": commission_rate,
            "cost_model": cost_model.to_dict(),
            "total_cost_pct": spread_pct / 2 + slippage_pct + commission_rate * 100,
            "price_timestamp": _now_local_iso(),
            "source": source,
        }

    @staticmethod
    def _is_verified_execution_payload(exec_data: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(exec_data, dict):
            return False
        try:
            price = float(exec_data.get("execution_price", 0.0) or 0.0)
        except Exception:
            return False
        if price <= 0:
            return False

        source = str(exec_data.get("source") or "").strip().lower()
        if source in {"fallback", "unavailable", "unknown"}:
            return False

        quality_meta = exec_data.get("execution_price_quality") or {}
        if not isinstance(quality_meta, dict):
            return False
        level = str(quality_meta.get("level") or quality_meta.get("quality") or "").strip().upper()
        if quality_meta.get("is_valid") is False:
            return False
        return level == "VERIFIED"

    @staticmethod
    def _requested_hard_exit_reason(reason: Any) -> str:
        text = str(reason or "").strip().lower().replace("-", "_").replace(" ", "_")
        if not text or text.startswith("manual"):
            return ""
        if text.startswith("take_profit"):
            return "take_profit"
        if text.startswith("stop_loss"):
            return "stop_loss"
        if text.startswith("trailing_stop"):
            return "trailing_stop"
        return ""

    @classmethod
    def _hard_exit_reason_for_price(cls, position: Dict[str, Any], exit_price: float) -> str:
        try:
            price = float(exit_price or 0.0)
            direction = cls._normalize_position_direction((position or {}).get("direction"))
            sl_price = float((position or {}).get("stop_loss_price", 0.0) or 0.0)
            tp_price = float((position or {}).get("take_profit_price", 0.0) or 0.0)
            trailing_sl = float((position or {}).get("trailing_sl", 0.0) or 0.0)
        except Exception:
            return ""
        if price <= 0:
            return ""

        if direction == "LONG":
            if sl_price > 0 and price <= sl_price:
                return "stop_loss"
            if tp_price > 0 and price >= tp_price:
                return "take_profit"
            if trailing_sl > 0 and price <= trailing_sl:
                return "trailing_stop"
        elif direction == "SHORT":
            if sl_price > 0 and price >= sl_price:
                return "stop_loss"
            if tp_price > 0 and price <= tp_price:
                return "take_profit"
            if trailing_sl > 0 and price >= trailing_sl:
                return "trailing_stop"
        return ""

    def _resolve_close_reason_for_execution(
        self,
        position: Dict[str, Any],
        *,
        exit_price: float,
        requested_reason: str,
        gross_pnl: float,
        enforce_exit_reason: bool,
    ) -> Tuple[str, str]:
        requested = str(requested_reason or "Manual close")
        requested_hard = self._requested_hard_exit_reason(requested)
        if not requested_hard:
            return requested, ""

        actual_hard = self._hard_exit_reason_for_price(position, exit_price)
        if actual_hard == requested_hard:
            return requested, ""

        if requested_hard == "take_profit" and gross_pnl <= 0:
            warning = (
                f"Requested take_profit no longer matched final execution price "
                f"{float(exit_price):.8f}; gross_pnl={float(gross_pnl):.6f}"
            )
            if enforce_exit_reason and not actual_hard:
                raise ValueError(warning)
            return actual_hard or "market_exit_after_invalid_take_profit", warning

        # EX5: a trailing-stop request whose fill landed at/above TP (LONG) or
        # at/below TP (SHORT) captured TP-or-better -- the canonical
        # ``_hard_exit_reason_for_price`` returns "take_profit" for that fill, so
        # the recorded reason must follow it. Pre-fix the trailing_stop branch
        # fell through every guard and returned the requested "trailing_stop",
        # mislabeling a TP-capturing exit as a trailing stop in trade records /
        # exit_mix stats. Win-rate-neutral (win/loss is pnl-based, not
        # reason-based) -- this only corrects the label to match the realized
        # outcome. gross_pnl>0 here by construction (a fill at/above TP profits).
        if requested_hard == "trailing_stop" and actual_hard == "take_profit":
            warning = (
                f"Requested trailing_stop captured take_profit at fill "
                f"{float(exit_price):.8f}; recording take_profit"
            )
            return "take_profit", warning

        if requested_hard == "stop_loss" and gross_pnl >= 0 and not actual_hard:
            warning = (
                f"Requested stop_loss no longer matched final execution price "
                f"{float(exit_price):.8f}; gross_pnl={float(gross_pnl):.6f}"
            )
            if enforce_exit_reason:
                raise ValueError(warning)
            return "market_exit_after_invalid_stop_loss", warning

        if enforce_exit_reason and not actual_hard:
            warning = (
                f"Requested {requested_hard} no longer matched final execution price "
                f"{float(exit_price):.8f}"
            )
            raise ValueError(warning)

        return requested, ""

    def _strategy_is_profitable(self) -> bool:
        """True only when the recent paper record clears the profitability bar (matches the
        execution agent: >=30 trades, profit_factor >=1.2, expectancy >0). Cached ~120s so
        the open path doesn't recompute portfolio metrics on every position. Used to gate the
        leverage boost so we never amplify an unprofitable edge."""
        import time as _t
        cache = getattr(self, "_profit_gate_cache", None)
        now = _t.time()
        if cache and (now - cache[0]) < 120.0:
            return cache[1]
        ok = False
        try:
            from core.feedback_loop import compute_portfolio_metrics, get_recent_trades
            trades = get_recent_trades(50)
            # A profit booked because an exit was delayed by an outage was not
            # earned by the strategy and must never unlock the leverage boost.
            # The journal rows carry no marker, so match them to the excluded
            # closed positions; excluded losses keep counting (never less strict).
            with self._lock:
                unearned = {
                    (str(p.get("ticker") or "").upper(), str(p.get("closed_at") or ""))
                    for p in self.positions
                    if isinstance(p, dict) and p.get("evidence_excluded") is True
                }
            if unearned:
                trades = [
                    t for t in trades or []
                    if not (
                        isinstance(t, dict)
                        and float(t.get("net_pnl", 0) or 0) > 0
                        and (str(t.get("ticker") or "").upper(), str(t.get("closed_at") or "")) in unearned
                    )
                ]
            m = compute_portfolio_metrics(trades)
            ok = (
                int(m.get("total_trades", 0) or 0) >= 30
                and float(m.get("profit_factor", 0.0) or 0.0) >= 1.2
                and float(m.get("expectancy", 0.0) or 0.0) > 0.0
            )
        except Exception:
            ok = False
        self._profit_gate_cache = (now, ok)
        return ok

    def open_position(
        self,
        ticker: str,
        direction: str,
        size_usd: float,
        stop_loss_pct: float,
        take_profit_pct: float,
        signal_id: str = None,
        note: str = "",
        *,
        leverage: int = 1,
        decision_context: Optional[Dict[str, Any]] = None,
        strategy_type: str = "",
        entry_reason: str = "",
        position_type: str = "paper_signal",
        research_only: bool = False,
        expected_entry_price: Optional[float] = None,
        max_entry_deviation_pct: Optional[float] = None,
    ) -> dict:
        """
        Open a simulated paper trading position with real bid/ask and commissions.
        """
        assert_trade_allowed(ticker, "open")
        normalized_position_type = str(position_type or "paper_signal").lower().strip()
        if normalized_position_type not in {"paper_signal", "research_cohort"}:
            raise ValueError("position_type must be paper_signal or research_cohort")
        research_only = bool(research_only or normalized_position_type == "research_cohort")
        # Hoist the execution-price fetch OUTSIDE self._lock to avoid holding
        # the lock across a multi-second external HTTP call (mirrors
        # close_position at :1512, which already fetches its exit price before
        # reacquiring the lock). Pre-fix, open_position held self._lock from
        # :1147 through the _get_execution_price HTTP call at :1253, blocking
        # every other capital operation (close/equity/snapshot) for up to the
        # 5s request timeout -- a serialization bottleneck under concurrent
        # opens. The lock is reacquired below, where _sync_from_storage reloads
        # the latest persisted state and the existing margin_used/cash_debit
        # guards (re)validate available capital; a concurrent write that drops
        # the balance between the fetch and the lock is caught there and raises
        # "Insufficient capital" (benign; ExecutionAgent handles). Win-rate
        # neutral: entry price is the same regardless of where the fetch runs.
        _open_side = "BUY" if str(direction or "").upper().strip() in ("LONG", "BUY") else "SELL"
        exec_data = self._get_execution_price(ticker, _open_side, use_cache=False)
        expected_price = self._safe_float(expected_entry_price, 0.0)
        allowed_deviation_pct = self._safe_float(max_entry_deviation_pct, -1.0)
        entry_deviation_pct = None
        if expected_price > 0 and allowed_deviation_pct >= 0:
            execution_price = self._safe_float(exec_data.get("execution_price"), 0.0)
            if execution_price <= 0:
                raise ValueError("execution_price must be > 0")
            entry_deviation_pct = abs(execution_price - expected_price) / expected_price * 100.0
            if entry_deviation_pct > allowed_deviation_pct:
                raise ValueError(
                    f"live entry price deviates {entry_deviation_pct:.2f}% from signal reference "
                    f"(limit {allowed_deviation_pct:.2f}%)"
                )
        with self._lock:
            self._sync_from_storage(initialize_if_missing=False)
            available = (
                float(self.balance or 0.0)
                if research_only
                else float(self._capital_accounting_unlocked()["deployable_cash_balance"])
            )
            size_usd = float(size_usd or 0.0)
            stop_loss_pct = float(stop_loss_pct or 0.0)
            take_profit_pct = float(take_profit_pct or 0.0)

            if available <= 0:
                raise ValueError("Insufficient capital")
            if size_usd <= 0:
                raise ValueError("size_usd must be > 0")
            if stop_loss_pct <= 0 or take_profit_pct <= 0:
                raise ValueError("stop_loss_pct and take_profit_pct must be > 0")
            lev = int(leverage or 1)
            if lev < 1 or lev > 5:
                raise ValueError("leverage must be in [1,5]")

            # Surgical minimal: Capital decides/enforces leverage using score/prob from decision_context (high conf/regime/score -> allow up to 2x unless capped)
            # This makes Capital the final decider (verifiable, traceable); caller proposed lev respected but boosted for quality.
            _dec_ctx = decision_context or {}
            _paper_capital_experiment = bool(
                _dec_ctx.get("paper_capital_experiment")
                or (_dec_ctx.get("execution") or {}).get("paper_capital_experiment")
                or (_dec_ctx.get("execution_context") or {}).get("paper_capital_experiment")
            )
            _sc = _dec_ctx.get("scoring") or {}
            _score = float(_sc.get("signal_score") or _sc.get("probability_score", 0.0) or 0.0)
            # probability_score is a sigmoid in [0,1] (scoring_system.SignalScore); signal_score
            # is its 0..100 rescale (score = probability_score * 100). The previous code divided
            # probability_score by 100 UNCONDITIONALLY (because _score, the 0..100 signal_score,
            # is always > 1), so a 0.85 probability became 0.0085 and the prob-gated leverage
            # boost (_prob >= 0.75 / >= 0.85) could NEVER fire. Normalize by scale instead: a
            # value <= 1.0 is already a probability; a value > 1.0 is on the 0..100 score scale.
            _raw_prob = float(_sc.get("probability_score") or _sc.get("signal_score", 0.0) or 0.0)
            _prob = (_raw_prob / 100.0) if _raw_prob > 1.0 else _raw_prob
            _conf_tier = str((_dec_ctx.get("confidence") or {}).get("tier", "")).upper()
            # Gate the quality-based leverage boost on PROVEN profitability. Previously this
            # boosted to 2x purely on signal score, which silently overrode the execution
            # agent's force-1x-until-profitable guard — so an unprofitable edge (PF<1.2) still
            # ran 2x and amplified losses. Now the boost only applies once the strategy is
            # profitable; otherwise the caller-proposed leverage (1x under the guard) stands.
            # Prospective research positions remain fixed at their explicitly
            # bounded 1x proposal. Their outcomes are isolated from Capital's
            # realised-edge cache, so a portfolio-level profitability flag must
            # never enlarge the experiment's exposure.
            _allow_boost = (
                (not research_only)
                and (not _paper_capital_experiment)
                and self._strategy_is_profitable()
            )
            _suggested_lev = lev
            if _allow_boost and (_score >= 85 or _prob >= 0.75 or _conf_tier in ("A", "HIGH")):
                _suggested_lev = max(lev, 2)
            if _allow_boost and (_score >= 92 or _prob >= 0.85):
                _suggested_lev = max(_suggested_lev, 2)
            _suggested_lev = max(1, min(5, int(_suggested_lev)))
            if _suggested_lev != lev:
                logger.info(f"CAPITAL LEVERAGE DECISION: {ticker} score={_score:.1f} prob={_prob:.2f} tier={_conf_tier} proposed={lev} -> enforced={_suggested_lev} (profitable boost)")
            lev = _suggested_lev

            max_notional = available * 0.20 * float(lev)
            if size_usd > max_notional:
                logger.warning(
                    f"Size USD {size_usd} exceeds 20% margin cap for available capital {available} at {lev}x"
                )
                size_usd = max_notional

            if size_usd <= 0:
                raise ValueError("size_usd must be > 0")
            margin_used = size_usd / float(lev)
            if margin_used > available:
                raise ValueError("Insufficient capital")
            side = direction.upper().strip()
            if side not in ["LONG", "SHORT", "BUY", "SELL"]:
                raise ValueError("direction must be LONG/SHORT/BUY/SELL")
            is_long = side in ["LONG", "BUY"]
            normalized_direction = "LONG" if is_long else "SHORT"
            from core.position_conflicts import conflict_summary, opposing_open_positions

            opposing = opposing_open_positions(
                self.positions,
                ticker=ticker,
                proposed_direction=normalized_direction,
                comparable_position_type=normalized_position_type,
            )
            if opposing:
                message = conflict_summary(
                    opposing,
                    ticker=ticker,
                    proposed_direction=normalized_direction,
                )
                logger.warning("CAPITAL DIRECTION CONFLICT: %s", message)
                raise ValueError(message)
            logger.info(f"CAPITAL LEVERAGE/RESERVE DECISION: {ticker} {normalized_direction} lev={lev} score={_score:.1f} margin_reserved={margin_used:.2f} (only reserved used for future P&L; ctx={bool(decision_context)})")

            existing_idx = self._find_open_position_index(
                ticker=ticker,
                direction=normalized_direction,
                position_type=normalized_position_type,
            )
            if existing_idx is not None and signal_id:
                existing = dict(self.positions[existing_idx] or {})
                if signal_id in self._position_signal_ids(existing):
                    existing = self._mark_duplicate_position_ignored(
                        existing,
                        {
                            "position_id": f"duplicate_{uuid.uuid4().hex[:12]}",
                            "ticker": ticker.upper(),
                            "direction": normalized_direction,
                            "position_type": normalized_position_type,
                            "signal_id": signal_id,
                            "source_signal_id": signal_id,
                            "opened_at": _now_local_iso(),
                        },
                        refund_debit=False,
                    )
                    self.positions[existing_idx] = existing
                    self._save_unlocked()
                    try:
                        from core.trade_audit import record_trade_event

                        record_trade_event(
                            event_type="position_duplicate_ignored",
                            status="ignored",
                            ticker=existing.get("ticker", ""),
                            signal_id=signal_id or "",
                            position_id=existing.get("position_id", ""),
                            strategy_type=existing.get("strategy_type", ""),
                            summary="Duplicate signal already has an open paper position",
                            payload={"position": dict(existing), "signal_id": signal_id},
                        )
                    except Exception as exc:
                        logger.error(f"Failed to audit duplicate signal guard {existing.get('position_id')}: {exc}")
                    return dict(existing)
            
            entry_price = exec_data["execution_price"]
            commission_rate = exec_data["commission_rate"]
            if entry_price <= 0:
                raise ValueError("execution_price must be > 0")
            if not self._is_verified_execution_payload(exec_data):
                raise ValueError(f"verified live entry price unavailable for {ticker}")
            
            shares_qty = size_usd / entry_price
            commission_usd = size_usd * commission_rate
            cash_debit = margin_used + commission_usd
            if cash_debit > available:
                raise ValueError("Insufficient capital")
            
            stop_loss_price = entry_price * (1 - stop_loss_pct) if is_long else entry_price * (1 + stop_loss_pct)
            take_profit_price = entry_price * (1 + take_profit_pct) if is_long else entry_price * (1 - take_profit_pct)
            resolved_sector = ""
            try:
                from core.sectors import get_sector
                resolved_sector = get_sector(ticker)
            except Exception:
                resolved_sector = ""
            if not resolved_sector:
                t_up = str(ticker or "").upper().strip()
                if t_up.endswith("-USD") or t_up.endswith("USDT"):
                    resolved_sector = "Crypto"
                else:
                    resolved_sector = "Other"
            try:
                from core.strategy_v2.context_builder import infer_asset_class

                resolved_asset_class = infer_asset_class(str(ticker or ""))
            except Exception:
                resolved_asset_class = "US_STOCKS"

            pos = {
                "position_id": f"pos_{uuid.uuid4().hex[:12]}",
                "ticker": ticker.upper(),
                "sector": resolved_sector,
                "asset_class": resolved_asset_class,
                "direction": normalized_direction,
                "position_type": normalized_position_type,
                "research_only": research_only,
                "paper_capital_experiment": _paper_capital_experiment,
                "entry_price": round(entry_price, 8),
                "qty": round(shares_qty, 8),
                "usd_size": round(size_usd, 6),
                "margin_used": round(margin_used, 6),
                "leverage": lev,
                "commission_usd": round(commission_usd, 6),
                "stop_loss_price": round(stop_loss_price, 8),
                "take_profit_price": round(take_profit_price, 8),
                "opened_at": _now_local_iso(),
                "status": "OPEN",
                "signal_id": signal_id,
                "source_signal_id": signal_id,
                "note": note,
                "source": exec_data["source"],
                "entry_price_source": str(exec_data.get("source") or ""),
                "entry_price_timestamp": str(exec_data.get("price_timestamp") or ""),
                "entry_price_quality": dict(exec_data.get("execution_price_quality") or {}),
                "signal_reference_price": round(expected_price, 8) if expected_price > 0 else None,
                "entry_price_deviation_pct": (
                    round(entry_deviation_pct, 6) if entry_deviation_pct is not None else None
                ),
                "commission_rate": round(commission_rate, 8),
                "cost_model": exec_data.get("cost_model") or {},
                # Opened with first-touch tracking: the first time an exit
                # condition is met is recorded (exit_condition_since).
                "exit_condition_tracked": True,
            }
            pos = self._apply_position_explainability(
                pos,
                decision_context=decision_context,
                strategy_type=strategy_type,
                entry_reason=entry_reason or note,
            )

            if existing_idx is not None:
                self.balance -= cash_debit
                merged = self._merge_position_addon(
                    self.positions[existing_idx],
                    pos,
                    reason="signal_scale_in",
                )
                self.positions[existing_idx] = merged
                self._add_transaction(
                    "SCALE_POSITION",
                    -cash_debit,
                    self.balance,
                    f"{ticker.upper()} {'LONG' if is_long else 'SHORT'} add x{lev} notional=${size_usd:.2f} margin=${margin_used:.2f} (comm=${commission_usd:.2f})",
                    metadata=self._ledger_position_metadata(
                        merged,
                        signal_id=str(signal_id or ""),
                    ),
                )
                self._save_unlocked()
                self._require_last_save_persisted(f"Scale-in of {ticker.upper()}")
                try:
                    from core.trade_audit import record_trade_event

                    record_trade_event(
                        event_type="position_scaled",
                        status="open",
                        ticker=merged.get("ticker", ""),
                        signal_id=signal_id or "",
                        position_id=merged.get("position_id", ""),
                        strategy_type=merged.get("strategy_type", ""),
                        summary=merged.get("scale_in_summary", ""),
                        payload={"position": dict(merged), "addon": dict(pos)},
                    )
                except Exception as exc:
                    logger.error(f"Failed to audit scaled position {merged.get('position_id')}: {exc}")
                return dict(merged)

            self.balance -= cash_debit
            self.positions.append(pos)
            self._add_transaction(
                "OPEN_POSITION",
                -cash_debit,
                self.balance,
                f"{ticker.upper()} {'LONG' if is_long else 'SHORT'} x{lev} notional=${size_usd:.2f} margin=${margin_used:.2f} (comm=${commission_usd:.2f})",
                metadata=self._ledger_position_metadata(
                    pos,
                    signal_id=str(signal_id or ""),
                ),
            )
            self._save_unlocked()
            # A fill that was not written must not be reported as opened: the
            # order ledger would record FILLED for a position Capital does not
            # hold (the orphaned SOXLB fill of 2026-10-22).
            self._require_last_save_persisted(f"Position {pos.get('position_id')} ({ticker.upper()})")
            try:
                from core.trade_audit import record_trade_event

                record_trade_event(
                    event_type="position_opened",
                    status="open",
                    ticker=pos.get("ticker", ""),
                    signal_id=signal_id or "",
                    position_id=pos.get("position_id", ""),
                    strategy_type=pos.get("strategy_type", ""),
                    summary=pos.get("entry_reason", ""),
                    payload={"position": dict(pos)},
                )
            except Exception as exc:
                logger.error(f"Failed to audit opened position {pos.get('position_id')}: {exc}")
            return dict(pos)

    def open_manual_position(
        self,
        *,
        ticker: str,
        direction: str,
        leverage: int,
        usd_size: float,
        entry_price: float,
        max_size_pct: float = 0.4,
        fees_bps: Optional[float] = None,
        cost_model: Optional[Dict[str, Any]] = None,
        execution_data: Optional[Dict[str, Any]] = None,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        decision_context: Optional[Dict[str, Any]] = None,
        reason: str = "",
        strategy_type: str = "MANUAL",
    ) -> Dict:
        symbol = str(ticker or "").upper().strip()
        # Manual opens bypass the US regular market-hours guard by design —
        # the user may open manual positions 24/7 (equities at the last
        # available quote, crypto anytime). The automated open_position path
        # still enforces regular hours.
        side = str(direction or "").lower().strip()
        if side not in {"long", "short"}:
            raise ValueError("direction must be long|short")
        lev = int(leverage or 1)
        if lev < 1 or lev > 5:
            raise ValueError("leverage must be in [1,5]")
        notional = float(usd_size or 0.0)
        if notional <= 0:
            raise ValueError("usd_size must be > 0")
        px = float(entry_price or 0.0)
        if px <= 0:
            raise ValueError("entry_price must be > 0")
        from core.config import ConfigManager
        from core.trading_costs import resolve_trading_cost_model

        configured_cost_model = (
            dict(cost_model)
            if isinstance(cost_model, dict) and cost_model
            else resolve_trading_cost_model(symbol, strategy_v2_config=ConfigManager().strategy_v2).to_dict()
        )
        configured_fee_bps = float(configured_cost_model.get("commission_bps_per_side", 0.0) or 0.0)
        effective_fees_bps = configured_fee_bps if fees_bps is None else float(fees_bps)
        if not math.isfinite(effective_fees_bps) or effective_fees_bps < 0:
            raise ValueError("fees_bps must be a finite non-negative number")
        if fees_bps is not None and abs(effective_fees_bps - configured_fee_bps) > 1e-9:
            # Direct callers may run an explicit what-if/manual fee scenario.
            # Preserve that declaration in the ledger instead of presenting it
            # as an exchange-realized commission.
            configured_cost_model["commission_bps_per_side"] = effective_fees_bps
            configured_cost_model["provenance"] = "explicit_manual_fee_estimate"
        recorded_execution = dict(execution_data) if isinstance(execution_data, dict) else {}
        raw_price_quality = recorded_execution.get("execution_price_quality")
        recorded_price_quality = dict(raw_price_quality) if isinstance(raw_price_quality, dict) else {}
        sl_price = float(stop_loss_price or 0.0)
        tp_price = float(take_profit_price or 0.0)
        if sl_price <= 0:
            sl_price = px * (1 - DEFAULT_MANUAL_STOP_LOSS_PCT) if side == "long" else px * (1 + DEFAULT_MANUAL_STOP_LOSS_PCT)
        if tp_price <= 0:
            tp_price = px * (1 + DEFAULT_MANUAL_TAKE_PROFIT_PCT) if side == "long" else px * (1 - DEFAULT_MANUAL_TAKE_PROFIT_PCT)

        with self._lock:
            self._sync_from_storage(initialize_if_missing=False)
            available = float(self._capital_accounting_unlocked()["deployable_cash_balance"])
            if available <= 0:
                raise ValueError("insufficient capital")

            cap_pct = max(0.01, min(1.0, float(max_size_pct or 0.4)))
            max_notional = available * cap_pct * max(1.0, float(lev))
            if notional > max_notional:
                raise ValueError(f"usd_size exceeds max allowed ({max_notional:.2f})")

            margin_used = notional / float(lev)
            if margin_used > available:
                raise ValueError("insufficient free capital for required margin")

            qty = notional / px
            fees_est = abs(notional) * (effective_fees_bps / 10000.0)
            pos = {
                "position_id": f"pos_{uuid.uuid4().hex[:12]}",
                "ticker": symbol,
                "direction": side,
                "position_type": "manual",
                "leverage": lev,
                "usd_size": round(notional, 6),
                "margin_used": round(margin_used, 6),
                "entry_price": round(px, 8),
                "qty": round(qty, 8),
                "fees_estimate": round(fees_est, 6),
                "commission_usd": round(fees_est, 6),
                "fees_bps": effective_fees_bps,
                "commission_rate": round(effective_fees_bps / 10000.0, 8),
                "cost_model": configured_cost_model,
                "source": str(recorded_execution.get("source") or "manual_execution_price"),
                "execution_price_quality": recorded_price_quality,
                "price_timestamp": recorded_execution.get("price_timestamp") or _now_local_iso(),
                "stop_loss_price": round(sl_price, 8),
                "take_profit_price": round(tp_price, 8),
                "exit_management_mode": "manual_levels_only",
                "automatic_trailing_enabled": False,
                "ai_exit_management_enabled": False,
                "trailing_sl": 0.0,
                "max_favorable_excursion": 0.0,
                "opened_at": _now_local_iso(),
                "status": "OPEN",
                "exit_condition_tracked": True,
            }
            pos = self._apply_position_explainability(
                pos,
                decision_context=decision_context,
                strategy_type=strategy_type,
                entry_reason=reason or "Manual terminal order",
            )
            cash_debit = margin_used + fees_est
            # Capital-integrity guard mirroring open_position (line ~1228):
            # the margin-only check above (1369) does not account for fees, so a
            # manual position with margin_used just under `available` could push
            # the balance NEGATIVE once fees are debited (e.g. margin=999,
            # available=1000, fees=2 -> balance=-1). Block before mutating state.
            if cash_debit > available:
                raise ValueError(
                    "insufficient free capital for required margin + fees")
            self.balance = float(self.balance or 0.0) - cash_debit
            self.positions.append(pos)
            self._add_transaction(
                "OPEN_POSITION",
                -cash_debit,
                self.balance,
                f"{symbol} {side.upper()} x{lev} notional=${notional:.2f} margin=${margin_used:.2f} (fees=${fees_est:.2f})",
                metadata=self._ledger_position_metadata(pos),
            )
            self._save_unlocked()
            self._require_last_save_persisted(f"Manual position {pos.get('position_id')} ({symbol})")
            try:
                from core.trade_audit import record_trade_event

                record_trade_event(
                    event_type="position_opened",
                    status="open",
                    ticker=pos.get("ticker", ""),
                    signal_id="",
                    position_id=pos.get("position_id", ""),
                    strategy_type=pos.get("strategy_type", ""),
                    summary=pos.get("entry_reason", ""),
                    payload={"position": dict(pos)},
                )
            except Exception as exc:
                logger.error(f"Failed to audit manual position {pos.get('position_id')}: {exc}")
            return dict(pos)

    def reduce_position(
        self,
        *,
        position_id: str,
        fraction: float,
        exit_price: float = 0.0,
        reason: str = "Risk reduction",
        exit_execution_data: Optional[Dict[str, Any]] = None,
        trigger_price: Optional[float] = None,
    ) -> Dict:
        """Partially close a paper position with proportional exact accounting."""
        pid = str(position_id or "").strip()
        if not pid:
            raise ValueError("position_id is required")
        try:
            fraction = float(fraction)
        except (TypeError, ValueError) as exc:
            raise ValueError("fraction must be a number") from exc
        if not math.isfinite(fraction) or not (0.0 < fraction < 1.0):
            raise ValueError("fraction must be between 0 and 1")

        with self._lock:
            self._sync_from_storage(initialize_if_missing=False)
            guard_position = next(
                (
                    dict(pos or {})
                    for pos in self.positions
                    if str((pos or {}).get("position_id") or "").strip() == pid
                ),
                None,
            )
        if not guard_position:
            raise KeyError("position not found")
        if str(guard_position.get("status") or "").upper() != "OPEN":
            raise ValueError("position is not open")
        if not self._is_manual_position(guard_position):
            assert_trade_allowed(str(guard_position.get("ticker") or ""), "close")

        direction = str(guard_position.get("direction") or "LONG").upper()
        closing_side = "SELL" if direction == "LONG" else "BUY"
        px_out = float(exit_price or 0.0)
        if px_out <= 0:
            exit_execution_data = self._get_execution_price(
                str(guard_position.get("ticker") or ""), closing_side, use_cache=False
            )
            if not self._is_verified_execution_payload(exit_execution_data):
                raise ValueError("verified live exit price unavailable")
            px_out = float(exit_execution_data.get("execution_price") or 0.0)
        if px_out <= 0:
            raise ValueError("exit_price must be > 0")
        if exit_execution_data is not None and not self._is_verified_execution_payload(exit_execution_data):
            raise ValueError("verified live exit price unavailable")

        with self._lock:
            self._sync_from_storage(initialize_if_missing=False)
            target_idx = None
            target = None
            for idx, pos in enumerate(self.positions):
                if str((pos or {}).get("position_id") or "").strip() == pid:
                    target_idx = idx
                    target = dict(pos or {})
                    break
            if target_idx is None or target is None:
                raise KeyError("position not found")
            if str(target.get("status") or "").upper() != "OPEN":
                raise ValueError("position is not open")

            entry_price = float(target.get("entry_price") or 0.0)
            original_qty = float(target.get("qty") or 0.0)
            original_notional = float(target.get("usd_size") or 0.0)
            original_margin = float(target.get("margin_used") or 0.0)
            entry_commission_remaining = float(target.get("commission_usd") or 0.0)
            if entry_price <= 0 or original_qty <= 0 or original_notional <= 0:
                raise ValueError("invalid open position data")

            reduced_qty = original_qty * fraction
            reduced_notional = original_notional * fraction
            released_margin = original_margin * fraction
            allocated_entry_commission = entry_commission_remaining * fraction
            exit_commission_rate = 0.0
            if isinstance(exit_execution_data, dict):
                exit_commission_rate = float(exit_execution_data.get("commission_rate") or 0.0)
            if exit_commission_rate <= 0:
                exit_commission_rate = float(target.get("commission_rate") or 0.0)
            if exit_commission_rate <= 0:
                from core.config import ConfigManager
                from core.trading_costs import resolve_trading_cost_model

                cost_model = resolve_trading_cost_model(
                    str(target.get("ticker") or ""),
                    strategy_v2_config=ConfigManager().strategy_v2,
                ).to_dict()
                exit_commission_rate = float(cost_model["commission_bps_per_side"]) / 10000.0
                target.setdefault("cost_model", cost_model)
                target["pnl_cost_warning"] = (
                    "Reduction used the current configured commission estimate because the entry rate was absent."
                )

            exit_notional = px_out * reduced_qty
            exit_commission = exit_notional * exit_commission_rate
            gross_pnl = (
                (px_out - entry_price) * reduced_qty
                if direction == "LONG"
                else (entry_price - px_out) * reduced_qty
            )
            realized_net_pnl = gross_pnl - allocated_entry_commission - exit_commission
            cash_pnl = gross_pnl - exit_commission

            self.balance = float(self.balance or 0.0) + released_margin
            self._add_transaction(
                "RELEASE_MARGIN",
                released_margin,
                self.balance,
                f"{target.get('ticker')} proportional margin release",
                metadata=self._ledger_position_metadata(target),
            )
            self.balance += cash_pnl

            target["qty"] = round(original_qty - reduced_qty, 8)
            target["usd_size"] = round(original_notional - reduced_notional, 6)
            target["margin_used"] = round(original_margin - released_margin, 6)
            target["commission_usd"] = round(
                entry_commission_remaining - allocated_entry_commission, 6
            )
            reduction = {
                "reduced_at": _now_local_iso(),
                "fraction": round(fraction, 8),
                "exit_price": round(px_out, 8),
                "reduced_qty": round(reduced_qty, 8),
                "reduced_notional": round(reduced_notional, 6),
                "released_margin": round(released_margin, 6),
                "allocated_entry_commission": round(allocated_entry_commission, 6),
                "exit_commission": round(exit_commission, 6),
                "gross_pnl": round(gross_pnl, 6),
                "net_pnl": round(realized_net_pnl, 6),
                "cash_pnl": round(cash_pnl, 6),
                "reason": str(reason or "Risk reduction"),
            }
            if trigger_price is not None:
                reduction["trigger_price"] = round(float(trigger_price), 8)
            if isinstance(exit_execution_data, dict):
                reduction["exit_price_source"] = str(exit_execution_data.get("source") or "")
                reduction["exit_price_timestamp"] = str(
                    exit_execution_data.get("price_timestamp") or ""
                )
            reductions = list(target.get("realized_reductions") or [])
            reductions.append(reduction)
            target["realized_reductions"] = reductions
            target["realized_pnl"] = round(
                float(target.get("realized_pnl") or 0.0) + realized_net_pnl, 6
            )
            context = dict(target.get("decision_context") or {})
            management = list(context.get("position_management") or [])
            management.append(dict(reduction))
            context["position_management"] = management
            target["decision_context"] = context
            self.positions[target_idx] = target
            self._add_transaction(
                "REDUCE_POSITION",
                cash_pnl,
                self.balance,
                f"{target.get('ticker')} {direction} reduce {fraction * 100.0:.2f}% ({reason})",
                metadata=self._ledger_position_metadata(
                    target,
                    reduction=reduction,
                    realized_net_pnl=reduction["net_pnl"],
                ),
            )
            self._save_unlocked()

        try:
            from core.trade_audit import record_trade_event

            record_trade_event(
                event_type="position_reduced",
                status="open",
                ticker=target.get("ticker", ""),
                signal_id=target.get("signal_id", ""),
                position_id=pid,
                strategy_type=target.get("strategy_type", ""),
                summary=str(reason or "Risk reduction"),
                payload={"position": dict(target), "reduction": reduction},
            )
        except Exception as exc:
            logger.error("Failed to audit reduced position %s: %s", pid, exc)
        return dict(target)

    def _exit_delay_evidence_marker(self, position: Dict[str, Any], exit_price: float) -> Dict[str, Any]:
        """``evidence_excluded`` fields for a close delayed by an outage, else {}.

        Automated positions only: a manual position is closed by its owner and
        is not strategy evidence in the first place. Never raises; a failure
        here must not stop a close.
        """
        try:
            if self._is_manual_position(position):
                return {}
            from core.portfolio.evidence import assess_exit_delay, exit_delay_exclusion_hours

            threshold_hours = exit_delay_exclusion_hours(None)
            scanner_minutes = 0.0
            try:
                from core.config import ConfigManager

                manager = ConfigManager()
                threshold_hours = exit_delay_exclusion_hours(manager)
                scanner_minutes = float((manager.bot or {}).get("scanner_time_stop_minutes", 0) or 0)
            except Exception as exc:
                logger.debug(f"capital_evidence config unavailable; using defaults: {exc}")
            return assess_exit_delay(
                position,
                closed_at=position.get("closed_at"),
                exit_price=exit_price,
                threshold_hours=threshold_hours,
                scanner_time_stop_minutes=scanner_minutes,
            )
        except Exception as exc:
            logger.error(f"Exit-delay evidence check failed for {position.get('position_id')}: {exc}")
            return {}

    def close_position(
        self,
        *,
        position_id: str,
        exit_price: float = 0.0,
        reason: str = "Manual close",
        sync_signal: bool = True,
        enforce_exit_reason: bool = False,
        exit_execution_data: Optional[Dict[str, Any]] = None,
        trigger_price: Optional[float] = None,
    ) -> Dict:
        pid = str(position_id or "").strip()
        px_out = float(exit_price or 0.0)
        if not pid:
            raise ValueError("position_id is required")

        with self._lock:
            self._sync_from_storage(initialize_if_missing=False)
            guard_position = None
            for pos in self.positions:
                if str((pos or {}).get("position_id", "")).strip() == pid:
                    guard_position = pos
                    break
        if not guard_position:
            raise KeyError("position not found")
        if str(guard_position.get("status", "")).upper() != "OPEN":
            raise ValueError("position already closed")
        # Manual positions may be closed 24/7 (user-controlled); automated
        # positions still respect US regular market hours.
        if not self._is_manual_position(guard_position):
            assert_trade_allowed(str(guard_position.get("ticker") or ""), "close")

        # If px_out is not provided, fetch realistic execution price
        if px_out <= 0:
            # Find direction first
            with self._lock:
                target_pos = None
                for pos in self.positions:
                    if str((pos or {}).get("position_id", "")) == pid:
                        target_pos = pos
                        break
            if target_pos:
                direction = str(target_pos.get("direction", "long")).upper()
                closing_side = "SELL" if direction == "LONG" else "BUY"
                try:
                    exec_data = self._get_execution_price(target_pos.get("ticker"), closing_side, use_cache=False)
                    if not self._is_verified_execution_payload(exec_data):
                        raise ValueError("verified live exit price unavailable")
                    exit_execution_data = exec_data
                    px_out = exec_data["execution_price"]
                except Exception as e:
                    logger.error(f"Failed to get exit execution price: {e}")
                    if "verified live exit price unavailable" in str(e):
                        raise ValueError("verified live exit price unavailable")
                    raise ValueError("exit_price could not be fetched and was not provided")

        if px_out <= 0:
            raise ValueError("exit_price must be > 0")
        if exit_execution_data is not None and not self._is_verified_execution_payload(exit_execution_data):
            raise ValueError("verified live exit price unavailable")

        with self._lock:
            self._sync_from_storage(initialize_if_missing=False)
            target_idx = None
            target = None
            for idx, pos in enumerate(self.positions):
                if str((pos or {}).get("position_id", "")).strip() == pid:
                    target_idx = idx
                    target = dict(pos or {})
                    break
            if target_idx is None or target is None:
                raise KeyError("position not found")
            if str(target.get("status", "")).upper() != "OPEN":
                raise ValueError("position already closed")

            px_in = float(target.get("entry_price", 0.0) or 0.0)
            notional = float(target.get("usd_size", 0.0) or 0.0)
            margin = float(target.get("margin_used", 0.0) or 0.0)
            direction = str(target.get("direction", "long")).lower()
            fees_est = float(target.get("fees_estimate", 0.0) or 0.0)
            if px_in <= 0 or notional <= 0:
                raise ValueError("invalid open position data")

            move_pct = ((px_out - px_in) / px_in)
            if direction == "short":
                move_pct = -move_pct
                
            qty = target.get("qty", notional / px_in)
            
            gross_pnl = (px_out - px_in) * qty if direction == "long" else (px_in - px_out) * qty
            has_charged_entry_commission = "commission_usd" in target
            entry_commission = float(target.get("commission_usd", fees_est) or 0.0)

            # Exit commission — both position types pay commission on exit.
            exit_commission_rate = 0.0
            if isinstance(exit_execution_data, dict) and exit_execution_data.get("commission_rate"):
                exit_commission_rate = float(exit_execution_data["commission_rate"] or 0.0)
            elif float(target.get("commission_rate", 0.0) or 0.0) > 0:
                exit_commission_rate = float(target.get("commission_rate", 0.0))
            elif has_charged_entry_commission and notional > 0:
                # A legacy position without an explicit rate still contains the
                # exact paper debit. Reuse its recorded ratio instead of making
                # up a replacement commission assumption during close.
                exit_commission_rate = max(0.0, entry_commission / notional)
            else:
                # A legacy record without an entry fee can still be closed, but
                # uses the current explicit configured estimate and is marked
                # as requiring reconciliation.  Never substitute a hidden
                # fixed fee rate here.
                from core.config import ConfigManager
                from core.trading_costs import resolve_trading_cost_model

                fallback_cost_model = resolve_trading_cost_model(
                    str(target.get("ticker", "")),
                    strategy_v2_config=ConfigManager().strategy_v2,
                ).to_dict()
                exit_commission_rate = float(fallback_cost_model["commission_bps_per_side"]) / 10000.0
                target.setdefault("cost_model", fallback_cost_model)
                target["pnl_cost_warning"] = (
                    "Entry commission was not recorded; exit uses the current configured estimate and requires reconciliation."
                )
            exit_notional = px_out * qty
            exit_commission = exit_notional * exit_commission_rate

            close_leg_net_pnl = gross_pnl - entry_commission - exit_commission
            reductions = [
                item for item in (target.get("realized_reductions") or [])
                if isinstance(item, dict)
            ]
            prior_realized_pnl = sum(float(item.get("net_pnl") or 0.0) for item in reductions)
            prior_gross_pnl = sum(float(item.get("gross_pnl") or 0.0) for item in reductions)
            net_pnl = prior_realized_pnl + close_leg_net_pnl
            total_gross_pnl = prior_gross_pnl + gross_pnl
            cash_pnl = (
                gross_pnl - exit_commission
                if has_charged_entry_commission
                else close_leg_net_pnl
            )
            effective_reason, reason_warning = self._resolve_close_reason_for_execution(
                target,
                exit_price=px_out,
                requested_reason=str(reason or "Manual close"),
                gross_pnl=gross_pnl,
                enforce_exit_reason=bool(enforce_exit_reason),
            )

            self.balance = float(self.balance or 0.0) + margin
            self._add_transaction(
                "RELEASE_MARGIN",
                margin,
                self.balance,
                f"{target.get('ticker')} margin release",
                metadata=self._ledger_position_metadata(target),
            )
            self.balance = self.balance + cash_pnl
            target["exit_commission"] = round(exit_commission, 6)
            target["exit_commission_rate"] = round(exit_commission_rate, 8)
            target["status"] = "CLOSED"
            target["closed_at"] = _now_local_iso()
            target["exit_price"] = round(px_out, 8)
            target["gross_pnl"] = round(total_gross_pnl, 6)
            target["net_pnl"] = round(net_pnl, 6)
            target["pnl"] = round(net_pnl, 6)
            target["cash_pnl"] = round(cash_pnl, 6)
            target["close_leg_net_pnl"] = round(close_leg_net_pnl, 6)
            target["close_leg_gross_pnl"] = round(gross_pnl, 6)
            target["pnl_pct_notional"] = round(move_pct * 100.0, 4)
            target["pnl_pct_margin"] = round(move_pct * 100.0 * int(target.get("leverage", 1) or 1), 4)
            target["close_reason"] = effective_reason
            target["requested_close_reason"] = str(reason or "Manual close")
            if reason_warning:
                target["close_reason_warning"] = reason_warning
            if trigger_price is not None:
                try:
                    target["trigger_price"] = round(float(trigger_price), 8)
                except Exception:
                    pass
            if isinstance(exit_execution_data, dict):
                target["exit_price_source"] = str(exit_execution_data.get("source") or "")
                target["exit_price_timestamp"] = str(exit_execution_data.get("price_timestamp") or "")
                target["exit_price_quality"] = exit_execution_data.get("execution_price_quality") or {}
            decision_context = dict(target.get("decision_context") or {})
            decision_context["close_context"] = {
                "closed_at": target["closed_at"],
                "close_reason": target["close_reason"],
                "requested_close_reason": target["requested_close_reason"],
                "exit_price": target["exit_price"],
                "net_pnl": target["net_pnl"],
                "gross_pnl": target["gross_pnl"],
                "close_leg_net_pnl": target["close_leg_net_pnl"],
                "prior_realized_pnl": round(prior_realized_pnl, 6),
                "entry_commission": round(entry_commission, 6),
                "exit_commission": target["exit_commission"],
                "pnl_pct_notional": target["pnl_pct_notional"],
                "pnl_pct_margin": target["pnl_pct_margin"],
            }
            if reason_warning:
                decision_context["close_context"]["warning"] = reason_warning
            if trigger_price is not None:
                decision_context["close_context"]["trigger_price"] = target.get("trigger_price")
            if isinstance(exit_execution_data, dict):
                decision_context["close_context"]["exit_price_source"] = target.get("exit_price_source", "")
            # Evidence marker (reporting only; cash, P&L and the ledger above
            # are unchanged): an exit that happened days after it was due does
            # not measure the strategy.
            evidence_marker = self._exit_delay_evidence_marker(target, px_out)
            if evidence_marker:
                target.update(evidence_marker)
                decision_context["close_context"]["evidence_excluded"] = True
                decision_context["close_context"]["evidence_exclusion_reason"] = evidence_marker.get(
                    "evidence_exclusion_reason"
                )
                logger.warning(
                    "Closed %s %s is excluded from strategy evidence: %s",
                    target.get("ticker"),
                    target.get("position_id"),
                    evidence_marker.get("evidence_exclusion_note"),
                )
            target["decision_context"] = decision_context
            self.positions[target_idx] = target
            self._add_transaction(
                "CLOSE_POSITION",
                cash_pnl,
                self.balance,
                f"{target.get('ticker')} {direction.upper()} close ({effective_reason})",
                metadata=self._ledger_position_metadata(
                    target,
                    direction=direction.upper(),
                    requested_close_reason=target["requested_close_reason"],
                    close_reason=effective_reason,
                    close_reason_warning=reason_warning,
                    entry_price=target["entry_price"],
                    exit_price=target["exit_price"],
                    trigger_price=target.get("trigger_price"),
                    gross_pnl=target["gross_pnl"],
                    net_pnl=target["net_pnl"],
                    cash_pnl=target["cash_pnl"],
                    exit_price_source=target.get("exit_price_source", ""),
                    evidence_excluded=True if evidence_marker else None,
                    evidence_exclusion_reason=evidence_marker.get("evidence_exclusion_reason"),
                    evidence_exclusion_delay_hours=evidence_marker.get("evidence_exclusion_delay_hours"),
                ),
            )
            self._save_unlocked()
            try:
                from core.trade_audit import record_trade_event

                record_trade_event(
                    event_type="position_closed",
                    status="closed",
                    ticker=target.get("ticker", ""),
                    signal_id=target.get("signal_id", ""),
                    position_id=target.get("position_id", ""),
                    strategy_type=target.get("strategy_type", ""),
                    summary=target.get("close_reason", ""),
                    payload={"position": dict(target)},
                )
            except Exception as exc:
                logger.error(f"Failed to audit closed position {target.get('position_id')}: {exc}")

            # Feedback loop — update strategy performance from trade outcome
            try:
                from core.feedback_loop import process_closed_trade
                process_closed_trade(dict(target))
            except Exception as exc:
                logger.error(f"Feedback loop error for {target.get('position_id')}: {exc}")

            if sync_signal:
                try:
                    from core.signals import get_signal_manager

                    get_signal_manager().sync_closed_paper_position(dict(target))
                except Exception as exc:
                    logger.error(f"Failed to sync closed paper signal for {target.get('position_id')}: {exc}")

            return dict(target)

    def _get_fresh_price_cached(self, ticker: str, direction: str = "SELL") -> float:
        """
        Get price using cache-first strategy.
        Tries in-memory cache and state cache first before external API.
        Returns current price (adjusted for direction spread if fresh API call).
        """
        symbol = str(ticker).upper().strip()
        side = str(direction).upper().strip()
        if side == "LONG":
            side = "BUY"
        elif side == "SHORT":
            side = "SELL"

        is_crypto = (
            symbol.endswith("USDT")
            or symbol.endswith("USD")
            or symbol in ["BTC", "ETH", "SOL"]
        )
        max_age = 3.0 if is_crypto else 5.0  # seconds

        import time

        now = time.time()

        # Check in-memory class cache first
        with self._price_cache_lock:
            cached = self._price_cache.get(symbol)
            if cached:
                price, timestamp = cached
                if (now - timestamp) < max_age:
                    return price

        # Cache miss or stale - call _get_execution_price which will try state cache then external API
        exec_data = self._get_execution_price(symbol, side)
        if not self._is_verified_execution_payload(exec_data):
            raise ValueError(f"verified live price unavailable for {symbol}")
        return exec_data["execution_price"]

    @staticmethod
    def _mark_exit_condition_met(
        position: Dict[str, Any],
        recheck: Dict[str, Any],
        *,
        condition_confirmed: bool,
        blocked_in_session: bool,
    ) -> None:
        """Record since when a mechanical exit has been due but not executed.

        Written into the position inside the save the skip already makes (no
        extra write). ``exit_condition_since`` is the first recorded moment the
        stop / target / trailing stop / time stop was met; the Capital tab
        shows it and the evidence rule measures the exit delay from it.
        Discretionary (AI, manual) closes and a trigger that the final
        executable price did not confirm are not exit conditions.
        """
        from core.portfolio.evidence import (
            SINCE_BASIS_LEGACY,
            SINCE_BASIS_OBSERVED,
            exit_condition_kind,
        )

        kind = exit_condition_kind(recheck.get("requested_reason"))
        if not kind or not condition_confirmed:
            return
        timestamp = str(recheck.get("timestamp") or "")
        if not position.get("exit_condition_since"):
            since = timestamp
            basis = SINCE_BASIS_OBSERVED
            if position.get("exit_condition_tracked") is not True:
                # Opened before first-touch tracking existed: the condition may
                # have been met long before this record. Keep the earliest
                # recorded skip and say that the true first touch is unknown.
                basis = SINCE_BASIS_LEGACY
                prior = position.get("last_exit_recheck")
                prior_timestamp = str((prior or {}).get("timestamp") or "") if isinstance(prior, dict) else ""
                if prior_timestamp and exit_condition_kind((prior or {}).get("requested_reason")):
                    prior_dt = CapitalManager._timestamp_utc(prior_timestamp)
                    current_dt = CapitalManager._timestamp_utc(timestamp)
                    if prior_dt is not None and (current_dt is None or prior_dt < current_dt):
                        since = prior_timestamp
            position["exit_condition_since"] = since
            position["exit_condition_since_basis"] = basis
            position["exit_condition"] = kind
            position["exit_condition_reason"] = str(recheck.get("requested_reason") or "")
            position["exit_condition_trigger_price"] = recheck.get("trigger_price")
        if blocked_in_session and not position.get("exit_blocked_in_session_at"):
            # The exit was attempted while the instrument was tradable and
            # still failed: from here on the condition is never cleared.
            position["exit_blocked_in_session_at"] = timestamp

    def _clear_lapsed_exit_condition(self, position_id: str) -> None:
        """Forget an exit condition that was only ever met while the market was closed.

        A stock whose stop or target is crossed on an after-hours quote and is
        back inside its levels at the next tradable check never had an exit the
        strategy could take; keeping the old timestamp would later mark an
        on-time close as delayed. Once an in-session exit attempt has failed
        (``exit_blocked_in_session_at``) the condition is kept for good.
        """
        pid = str(position_id or "").strip()
        if not pid:
            return
        with self._lock:
            self._sync_from_storage(initialize_if_missing=False)
            for idx, pos in enumerate(self.positions):
                if (
                    isinstance(pos, dict)
                    and str(pos.get("position_id", "")).strip() == pid
                    and str(pos.get("status", "")).upper() == "OPEN"
                ):
                    if not pos.get("exit_condition_since") or pos.get("exit_blocked_in_session_at"):
                        return
                    updated = dict(pos)
                    updated["exit_condition_last_lapsed"] = {
                        "since": updated.get("exit_condition_since"),
                        "condition": updated.get("exit_condition"),
                        "trigger_price": updated.get("exit_condition_trigger_price"),
                        "lapsed_at": _now_local_iso(),
                    }
                    for field in (
                        "exit_condition_since",
                        "exit_condition_since_basis",
                        "exit_condition",
                        "exit_condition_reason",
                        "exit_condition_trigger_price",
                    ):
                        updated.pop(field, None)
                    # The position has now been seen without a due exit, so a
                    # later touch is an observed first touch.
                    updated["exit_condition_tracked"] = True
                    self.positions[idx] = updated
                    self._save_unlocked()
                    return

    def _record_exit_recheck_skip(
        self,
        position_id: str,
        warning: str,
        *,
        trigger_price: float,
        final_price: float,
        requested_reason: str,
        actual_reason: str = "",
        exit_source: str = "",
        condition_confirmed: bool = True,
    ) -> None:
        payload = {
            "warning": warning,
            "trigger_price": round(float(trigger_price or 0.0), 8),
            "final_price": round(float(final_price or 0.0), 8),
            "requested_reason": str(requested_reason or ""),
            "actual_reason": str(actual_reason or ""),
            "exit_source": str(exit_source or ""),
            "timestamp": _now_local_iso(),
        }
        # Reporting only (the exit decision was already made by the caller): the
        # same skip repeats on every tracker pass -- a stock position past its
        # target is refused once a minute for a whole weekend. Recording each
        # repeat rewrote the full Capital state every time and filled the
        # decision feed with identical rows, so an unchanged skip is recorded
        # at most once per window. The market-hours message embeds the current
        # time, hence the normalised key.
        skip_kind = "market_hours" if "outside US regular market hours" in str(warning) else str(warning)
        skip_key = (str(position_id or "").strip(), str(requested_reason or ""), skip_kind)
        now_mono = time.monotonic()
        with self._lock:
            recorded_at = getattr(self, "_exit_skip_recorded_at", None)
            if not isinstance(recorded_at, dict):
                recorded_at = {}
                self._exit_skip_recorded_at = recorded_at
            previous = recorded_at.get(skip_key)
            if previous is not None and (now_mono - previous) < _EXIT_SKIP_RECORD_INTERVAL_SECONDS:
                return
            recorded_at[skip_key] = now_mono
            if len(recorded_at) > 2000:
                for stale_key in sorted(recorded_at, key=recorded_at.get)[:1000]:
                    recorded_at.pop(stale_key, None)
        skip_ticker = ""
        with self._lock:
            # Re-read latest persisted state before mutating+saving so a
            # concurrent reset/deposit/withdrawal by another process isn't
            # clobbered by this thread's possibly-stale in-memory state.
            self._sync_from_storage(initialize_if_missing=False)
            for idx, pos in enumerate(self.positions):
                if (
                    str((pos or {}).get("position_id", "")).strip() == str(position_id or "").strip()
                    and str((pos or {}).get("status", "")).upper() == "OPEN"
                ):
                    skip_ticker = str((pos or {}).get("ticker") or "")
                    existing = dict(pos or {})
                    self._mark_exit_condition_met(
                        existing,
                        payload,
                        condition_confirmed=condition_confirmed,
                        blocked_in_session=skip_kind != "market_hours",
                    )
                    existing["last_exit_recheck_warning"] = warning
                    existing["last_exit_recheck"] = payload
                    self.positions[idx] = existing
                    self._save_unlocked()
                    break
        try:
            from core.trade_audit import record_trade_event

            record_trade_event(
                event_type="exit_recheck_skipped",
                status="skipped",
                ticker=skip_ticker,
                signal_id="",
                position_id=str(position_id or ""),
                strategy_type="",
                summary=warning,
                payload=payload,
            )
        except Exception:
            pass

    def _close_position_from_exit_decision(
        self,
        position: Dict[str, Any],
        *,
        reason: str,
        trigger_price: float,
    ) -> bool:
        if not isinstance(position, dict):
            return False
        pid = str(position.get("position_id") or "").strip()
        ticker = str(position.get("ticker") or "").strip()
        direction = self._normalize_position_direction(position.get("direction"))
        if not pid or not ticker or direction not in {"LONG", "SHORT"}:
            return False
        if is_equity_market_hours_enforced(ticker):
            try:
                assert_trade_allowed(ticker, "close")
            except MarketHoursError as exc:
                if _exit_log_due(("skipped", pid)):
                    logger.info(f"ExitManager skipped {ticker}: {exc}")
                self._record_exit_recheck_skip(
                    pid,
                    str(exc),
                    trigger_price=trigger_price,
                    final_price=0.0,
                    requested_reason=reason,
                )
                return False

        closing_side = "SELL" if direction == "LONG" else "BUY"
        try:
            exec_data = self._get_execution_price(ticker, closing_side, use_cache=False)
        except Exception as exc:
            warning = f"Exit recheck failed for {ticker}: {exc}"
            logger.warning(warning)
            self._record_exit_recheck_skip(
                pid,
                warning,
                trigger_price=trigger_price,
                final_price=0.0,
                requested_reason=reason,
            )
            return False

        if not self._is_verified_execution_payload(exec_data):
            warning = f"Exit recheck skipped for {ticker}: verified live exit price unavailable"
            logger.warning(warning)
            self._record_exit_recheck_skip(
                pid,
                warning,
                trigger_price=trigger_price,
                final_price=float(exec_data.get("execution_price", 0.0) or 0.0),
                requested_reason=reason,
                exit_source=str(exec_data.get("source") or ""),
            )
            return False

        final_price = float(exec_data.get("execution_price", 0.0) or 0.0)
        effective_reason = str(reason or "exit_manager")
        requested_hard = self._requested_hard_exit_reason(effective_reason)
        if requested_hard:
            actual_hard = self._hard_exit_reason_for_price(position, final_price)
            if actual_hard != requested_hard:
                if actual_hard in {"take_profit", "stop_loss", "trailing_stop"}:
                    effective_reason = actual_hard
                else:
                    warning = (
                        f"Exit recheck skipped {ticker} {requested_hard}: trigger price "
                        f"{float(trigger_price or 0.0):.8f}, live execution {final_price:.8f}"
                    )
                    logger.warning(warning)
                    self._record_exit_recheck_skip(
                        pid,
                        warning,
                        trigger_price=trigger_price,
                        final_price=final_price,
                        requested_reason=reason,
                        actual_reason=actual_hard,
                        exit_source=str(exec_data.get("source") or ""),
                        # The executable price did not reach the level: the
                        # trigger was a quote blip, not a met exit condition.
                        condition_confirmed=False,
                    )
                    return False

        from core.config import ConfigManager
        from core.execution.runtime import build_position_intent, get_paper_broker
        from core.execution.types import OrderAction

        runtime_config = ConfigManager()
        broker = get_paper_broker(config=runtime_config, capital_manager=self)
        decision_id = (
            f"exit:{pid}:{effective_reason}:"
            f"{float(trigger_price or 0.0):.8f}:{final_price:.8f}"
        )
        intent = build_position_intent(
            decision_id=decision_id,
            position=position,
            action=OrderAction.CLOSE,
            reason=effective_reason,
            metadata={
                "exit_price": final_price,
                "exit_execution_data": exec_data,
                "trigger_price": trigger_price,
                "enforce_exit_reason": bool(requested_hard),
            },
        )
        order = broker.submit(intent)
        if str(order.get("state") or "") == "FILLED":
            return True

        error = str(order.get("error") or "paper close rejected")
        # A trailing-stop update can race the final price recheck. The trigger
        # was valid, so create a distinct, idempotent market-close intent.
        if requested_hard and "no longer matched final execution price" in error:
            logger.warning(
                "Exit close for %s fell back to market after final reason mismatch: %s",
                ticker,
                error,
            )
            fallback = build_position_intent(
                decision_id=f"{decision_id}:market-fallback",
                position=position,
                action=OrderAction.CLOSE,
                reason=effective_reason,
                metadata={
                    "exit_price": final_price,
                    "exit_execution_data": exec_data,
                    "trigger_price": trigger_price,
                    "enforce_exit_reason": False,
                },
            )
            order = broker.submit(fallback)
            return str(order.get("state") or "") == "FILLED"

        raise ValueError(error)

    def _record_closed_position_afterward(self) -> None:
        """§89 afterward facts from real paper fills (P0 decision-scorecard loop).

        Every closed position carries `decision_context.decision_id`; writing the
        realized outcome back turns the Decision Ledger into a measurable
        feedback loop: acceptance performance feeds the AI decision scorecard
        next to the §90 rejection quality. Idempotent: one pass per process +
        a `position_close` basis check in the ledger (append-only history)."""
        if not hasattr(self, "_afterward_processed"):
            self._afterward_processed: set = set()
        store = None
        try:
            from core.decision_ledger.store import get_decision_ledger_store
            from core.decision_ledger.types import LedgerOutcome

            with self._lock:
                closed = [
                    dict(p) for p in self.positions
                    if str(p.get("status") or "").upper() == "CLOSED"
                ]
            for pos in closed:
                pid = str(pos.get("position_id") or "")
                if not pid or pid in self._afterward_processed:
                    continue
                self._afterward_processed.add(pid)
                decision_context = (
                    pos.get("decision_context") if isinstance(pos.get("decision_context"), dict) else {}
                )
                decision_id = str(decision_context.get("decision_id") or "").strip()
                entry = float(pos.get("entry_price") or 0.0)
                exit_price = float(pos.get("exit_price") or 0.0)
                if not decision_id or entry <= 0 or exit_price <= 0:
                    continue
                if store is None:
                    store = get_decision_ledger_store()
                try:
                    existing_bases = {
                        str(getattr(outcome, "basis") or "")
                        for outcome in (store.list_outcomes(decision_id) or [])
                    }
                except Exception:
                    existing_bases = set()
                if "position_close" in existing_bases:
                    continue
                direction = str(pos.get("direction") or "LONG").upper()
                raw_pct = (exit_price - entry) / entry * 100.0
                pnl_pct = raw_pct if direction in ("LONG", "BUY") else -raw_pct
                if pnl_pct > 0.05:
                    outcome = "FAVORABLE"
                elif pnl_pct < -0.05:
                    outcome = "ADVERSE"
                else:
                    outcome = "FLAT"
                store.append_outcome(LedgerOutcome(
                    decision_id=decision_id,
                    evaluated_at=datetime.now(timezone.utc).isoformat(),
                    outcome=outcome,
                    basis="position_close",
                    forward_return_pct=round(pnl_pct, 4),
                    bars_used=0,
                    detail={
                        "ticker": pos.get("ticker"),
                        "strategy_type": pos.get("strategy_type"),
                        "direction": direction,
                        "entry": entry,
                        "exit": exit_price,
                        "pnl_usd": pos.get("net_pnl") if pos.get("net_pnl") is not None else pos.get("pnl"),
                        "close_reason": pos.get("close_reason"),
                        "opened_at": pos.get("opened_at"),
                        "closed_at": pos.get("closed_at"),
                        # Lets the paper-performance evidence leave out trades
                        # whose exit was delayed by an outage.
                        "evidence_excluded": pos.get("evidence_excluded") is True,
                        "evidence_exclusion_reason": pos.get("evidence_exclusion_reason"),
                        "evidence_exclusion_delay_hours": pos.get("evidence_exclusion_delay_hours"),
                    },
                ))
                logger.info(
                    "Ledger afterward recorded for %s (%s %s): %+.2f%% (%s)",
                    decision_id, pos.get("ticker"), direction, pnl_pct,
                    pos.get("close_reason") or "closed",
                )
                # TradingAgents adoption: one bounded LLM reflection per closed
                # pre-earnings position — the lesson is stored and re-injected
                # into future §41 reviews (fail-open, dedup by decision_id).
                if str(pos.get("strategy_type") or "").upper().startswith("PRE_EARNINGS"):
                    try:
                        from core.config import ConfigManager as _CM
                        from core.ai.reflection import reflect_on_closed_position

                        ctx = decision_context
                        thesis = str(
                            ctx.get("reason")
                            or ((ctx.get("ai_decision") or {}).get("thesis")
                                if isinstance(ctx.get("ai_decision"), dict) else "")
                            or pos.get("entry_reason")
                            or ""
                        )
                        reflect_on_closed_position(
                            pos,
                            decision_summary=(
                                f"{pos.get('ticker')} {direction} PRE_EARNINGS entry "
                                f"{entry:.4g}; thesis: {thesis[:300]}"
                            ),
                            pnl_pct=pnl_pct,
                            close_reason=str(pos.get("close_reason") or "closed"),
                            config=_CM(),
                        )
                    except Exception as exc:
                        logger.debug(f"reflection hook skipped: {exc}")
        except Exception as exc:
            logger.debug(f"afterward recording skipped: {exc}")

    def start_pnl_tracker(self):
        import time
        from datetime import datetime, timezone

        # Lazy init of exit manager
        if self.exit_manager is None:
            try:
                from core.exit_manager import ExitManager
                from core.config import ConfigManager
                self.exit_manager = ExitManager(ConfigManager())
            except Exception as e:
                logger.error(f"Failed to initialize ExitManager: {e}")
                self.exit_manager = None

        while True:
            try:
                open_positions = []
                with self._lock:
                    # Read-only: the pass works on copies and every mutation
                    # below re-syncs before it saves.
                    self._sync_for_read()
                    open_positions = [dict(p) for p in self.positions if p.get("status") == "OPEN"]
                # P0 decision-scorecard loop: write realized outcomes of freshly
                # closed positions back into the Decision Ledger.
                self._record_closed_position_afterward()

                ollama_cfg = {}
                runtime_config = {}
                try:
                    from core.config import ConfigManager

                    runtime_config = ConfigManager().config
                    ollama_cfg = runtime_config.get("ollama", {}) if isinstance(runtime_config, dict) else {}
                except Exception:
                    ollama_cfg = {}
                    runtime_config = {}
                ai_exit_enabled = bool(ollama_cfg.get("exit_decision_enabled", False))
                ai_exit_min_confidence = float(ollama_cfg.get("exit_decision_min_confidence", 0.8) or 0.8)
                # Tightening a stop can only reduce the maximum loss.  It has a
                # dedicated (still conservative) threshold so a valid defensive
                # recommendation is not held to the more stringent close-order
                # standard.
                ai_tighten_stop_min_confidence = float(
                    ollama_cfg.get("exit_tighten_stop_min_confidence", ai_exit_min_confidence)
                    or ai_exit_min_confidence
                )
                ai_tighten_stop_min_confidence = max(0.0, min(1.0, ai_tighten_stop_min_confidence))
                ai_exit_cooldown = max(30.0, float(ollama_cfg.get("exit_decision_cooldown_seconds", 300) or 300))

                # Pass 1 is mechanical only (price, stop, target, trail, time
                # exit) for every position. AI exit reviews used to run inline,
                # so one position's stop check waited behind the others' model
                # calls (up to 45s each): a third of stop-losses filled more
                # than 1% beyond their level. Reviews now run after the pass.
                ai_review_queue = []
                for pos in open_positions:
                    ticker = pos.get("ticker")
                    pid = pos.get("position_id")
                    direction = pos.get("direction", "LONG").upper()
                    post_event_review = None

                    closing_side = "SELL" if direction == "LONG" else "BUY"
                    # Use cache-first price lookup
                    try:
                        curr_price = self._get_fresh_price_cached(ticker, closing_side)
                    except Exception as exc:
                        logger.debug(f"PnL tracker skipped {ticker}: {exc}")
                        continue

                    # Use ExitManager for intelligent exit decisions
                    if self.exit_manager is not None:
                        # The post-event AI review may only preempt the
                        # mechanical exit path (trail + time exit) while the
                        # AI layer can actually answer. With the AI layer
                        # disabled — or after repeated unavailable reviews —
                        # fall back to the mechanical path instead of leaving
                        # the position with a pending review forever.
                        allow_event_review = bool(ai_exit_enabled) and (
                            self._ai_exit_management_enabled(pos)
                        )
                        if allow_event_review and str(
                            pos.get("post_earnings_review_status") or ""
                        ) == "fallback_mechanical":
                            allow_event_review = False
                        exit_decision = self.exit_manager.evaluate_position(
                            pos,
                            curr_price,
                            current_time=datetime.now(timezone.utc),
                            allow_event_review=allow_event_review,
                        )

                        if exit_decision and exit_decision.get("action") == "close":
                            reason = exit_decision.get("reason", "exit_manager")
                            # Re-check position is still open (may have been closed in a prior iteration)
                            with self._lock:
                                still_open = any(
                                    p.get("position_id") == pid and p.get("status") == "OPEN"
                                    for p in self.positions
                                )
                            if still_open:
                                if _exit_log_due(("closing", pid, reason)):
                                    logger.info(f"ExitManager: closing {ticker} — {reason} at {curr_price}")
                                self._close_position_from_exit_decision(
                                    pos,
                                    reason=reason,
                                    trigger_price=curr_price,
                                )
                            continue

                        # No exit is due on this verified price. An exit
                        # condition recorded only while the market was closed
                        # has lapsed (bookkeeping; no exit decision changes).
                        if pos.get("exit_condition_since") and not pos.get("exit_blocked_in_session_at"):
                            try:
                                if is_equity_market_hours_enforced(ticker):
                                    assert_trade_allowed(ticker, "close")
                                self._clear_lapsed_exit_condition(pid)
                            except MarketHoursError:
                                pass
                            except Exception as exc:
                                logger.debug(f"Exit-condition lapse not recorded for {ticker}: {exc}")

                        if exit_decision and exit_decision.get("action") == "trail":
                            new_sl = exit_decision.get("new_sl", 0.0)
                            if new_sl > 0:
                                # Update trailing SL in position data
                                with self._lock:
                                    self._sync_from_storage(initialize_if_missing=False)
                                    for i, p in enumerate(self.positions):
                                        if p.get("position_id") == pid:
                                            self.positions[i]["trailing_sl"] = new_sl
                                            break
                                    self._save_unlocked()

                        if exit_decision and exit_decision.get("action") == "review":
                            post_event_review = dict(exit_decision)
                            event_key = str(post_event_review.get("earnings_time_utc") or "unknown")
                            if str(pos.get("post_earnings_review_event") or "") != event_key:
                                with self._lock:
                                    self._sync_from_storage(initialize_if_missing=False)
                                    for i, p in enumerate(self.positions):
                                        if p.get("position_id") == pid and p.get("status") == "OPEN":
                                            self.positions[i]["post_earnings_review"] = post_event_review
                                            self.positions[i]["post_earnings_review_event"] = event_key
                                            self.positions[i]["post_earnings_review_status"] = "pending"
                                            break
                                    self._save_unlocked()

                        # Update max favorable excursion
                        if exit_decision and exit_decision.get("max_favorable_excursion"):
                            mfe = exit_decision["max_favorable_excursion"]
                            with self._lock:
                                self._sync_from_storage(initialize_if_missing=False)
                                for i, p in enumerate(self.positions):
                                    if p.get("position_id") == pid:
                                        self.positions[i]["max_favorable_excursion"] = mfe
                                        break
                                self._save_unlocked()

                    else:
                        # Fallback: simple TP/SL check
                        sl = float(pos.get("stop_loss_price", 0.0))
                        tp = float(pos.get("take_profit_price", 0.0))
                        reason = None
                        if direction == "LONG":
                            if sl > 0 and curr_price <= sl:
                                reason = "stop_loss"
                            elif tp > 0 and curr_price >= tp:
                                reason = "take_profit"
                        else:
                            if sl > 0 and curr_price >= sl:
                                reason = "stop_loss"
                            elif tp > 0 and curr_price <= tp:
                                reason = "take_profit"
                        if reason:
                            with self._lock:
                                still_open = any(
                                    p.get("position_id") == pid and p.get("status") == "OPEN"
                                    for p in self.positions
                                )
                            if still_open:
                                logger.info(f"Triggering {reason} for {ticker} at {curr_price}")
                                self._close_position_from_exit_decision(
                                    pos,
                                    reason=reason,
                                    trigger_price=curr_price,
                                )
                                continue

                    if ai_exit_enabled:
                        ai_review_queue.append((pos, curr_price, post_event_review))

                # Pass 2: at most one model call per cycle, forced post-event
                # reviews first, then the position reviewed longest ago, so the
                # next mechanical pass is never more than one review away.
                ai_review_queue.sort(key=lambda item: (
                    not isinstance(item[2], dict),
                    float(item[0].get("last_ollama_exit_review_ts", 0.0) or 0.0),
                ))
                ai_reviews_this_cycle = 0
                for pos, curr_price, post_event_review in ai_review_queue:
                    if ai_reviews_this_cycle >= 1:
                        break
                    ticker = pos.get("ticker")
                    pid = pos.get("position_id")
                    if self._ai_exit_management_enabled(pos):
                        try:
                            now_ts = time.time()
                            last_review_ts = float(pos.get("last_ollama_exit_review_ts", 0.0) or 0.0)
                            review_position = dict(pos)
                            force_post_event_review = False
                            post_event_key = ""
                            if isinstance(post_event_review, dict):
                                review_position["post_earnings_review"] = post_event_review
                                post_event_key = str(post_event_review.get("earnings_time_utc") or "unknown")
                                force_post_event_review = (
                                    str(pos.get("last_post_earnings_ai_review_event") or "") != post_event_key
                                )
                            if force_post_event_review or (now_ts - last_review_ts) >= ai_exit_cooldown:
                                from core.ollama_decision import (
                                    evaluate_exit_close_guard,
                                    evaluate_position_exit_with_ollama,
                                )

                                ai_reviews_this_cycle += 1
                                ai_exit = evaluate_position_exit_with_ollama(
                                    review_position,
                                    curr_price,
                                    config=runtime_config,
                                )
                                action = str(ai_exit.get("action") or "hold").lower()
                                confidence = float(ai_exit.get("confidence", 0.0) or 0.0)
                                close_guard = None
                                if action == "close":
                                    close_guard = evaluate_exit_close_guard(
                                        ai_exit,
                                        review_position,
                                        curr_price,
                                        config=runtime_config,
                                    )
                                    ai_exit["close_guard"] = close_guard
                                if not ai_exit.get("available"):
                                    review_status = "pending_ai_unavailable"
                                elif action == "close" and confidence < ai_exit_min_confidence:
                                    review_status = "close_rejected_low_confidence"
                                elif action == "close" and not bool((close_guard or {}).get("allowed")):
                                    review_status = "close_rejected_evidence_guard"
                                elif action == "close":
                                    review_status = "close_approved"
                                else:
                                    review_status = action
                                ai_exit["execution_outcome"] = review_status
                                with self._lock:
                                    self._sync_from_storage(initialize_if_missing=False)
                                    for i, p in enumerate(self.positions):
                                        if p.get("position_id") == pid and p.get("status") == "OPEN":
                                            self.positions[i]["last_ollama_exit_review"] = ai_exit
                                            self.positions[i]["last_ollama_exit_review_ts"] = now_ts
                                            if post_event_key:
                                                self.positions[i]["last_post_earnings_ai_review_event"] = post_event_key
                                                self.positions[i]["last_post_earnings_ai_review_at"] = datetime.now(timezone.utc).isoformat()
                                                unavailable_count = int(
                                                    self.positions[i].get("post_earnings_ai_unavailable_count", 0) or 0
                                                )
                                                if review_status == "pending_ai_unavailable":
                                                    unavailable_count += 1
                                                    if unavailable_count >= 3:
                                                        # Persistent AI unavailability:
                                                        # hand the position back to the
                                                        # mechanical exit path (trail +
                                                        # time exit) instead of holding
                                                        # it on a forever-pending review.
                                                        unavailable_count = 0
                                                        review_status = "fallback_mechanical"
                                                else:
                                                    unavailable_count = 0
                                                self.positions[i]["post_earnings_ai_unavailable_count"] = unavailable_count
                                                self.positions[i]["post_earnings_review_status"] = review_status
                                            break
                                    self._save_unlocked()

                                if (
                                    ai_exit.get("available")
                                    and action == "close"
                                    and confidence >= ai_exit_min_confidence
                                    and bool((close_guard or {}).get("allowed"))
                                ):
                                    with self._lock:
                                        still_open = any(
                                            p.get("position_id") == pid and p.get("status") == "OPEN"
                                            for p in self.positions
                                        )
                                    if still_open:
                                        reason = f"ollama_exit: {ai_exit.get('reason', 'AI close')}"
                                        logger.info(f"Ollama exit: closing {ticker} at {curr_price} ({confidence:.2f})")
                                        self._close_position_from_exit_decision(
                                            pos,
                                            reason=reason,
                                            trigger_price=curr_price,
                                        )
                                        continue
                                if ai_exit.get("available") and action == "close" and not bool((close_guard or {}).get("allowed")):
                                    logger.info(
                                        "Ollama exit: retaining %s; %s",
                                        ticker,
                                        (close_guard or {}).get("reason", "early close not supported"),
                                    )

                                if (
                                    ai_exit.get("available")
                                    and action == "tighten_stop"
                                    and confidence >= ai_tighten_stop_min_confidence
                                ):
                                    self._maybe_apply_ai_tighten_stop(
                                        pos, ai_exit, curr_price, ollama_cfg
                                    )
                        except Exception as exc:
                            logger.debug(f"Ollama exit review skipped for {ticker}: {exc}")

            except Exception as e:
                logger.error(f"PnL Tracker error: {e}")
            time.sleep(3)

    def _daily_snapshot_loop(self):
        import time
        last_snapshot_date = None
        while True:
            try:
                now_utc = datetime.now(timezone.utc)
                # Run at 00:00 UTC approximately
                if now_utc.hour == 0 and now_utc.minute < 5:
                    current_date = now_utc.date()
                    if last_snapshot_date != current_date:
                        self._snapshot_daily_equity()
                        last_snapshot_date = current_date
            except Exception as e:
                logger.error(f"Daily snapshot error: {e}")
            time.sleep(60)

    def _append_equity_snapshot(
        self,
        total_equity: float,
        open_count: int,
        *,
        deployable_cash: Optional[float] = None,
        open_margin: Optional[float] = None,
        mark_to_market_pnl: Optional[float] = None,
    ) -> None:
        """Persist one equity-curve point, deduped by UTC minute (tiered retention).

        Dedup was previously by calendar date (one point/day), which made the
        cockpit chart a sparse 1-point-per-day line. Minute granularity lets
        intraday points from record_equity_point() accumulate into a real live
        curve while two same-minute writers (api + engine) still collapse to one.
        """
        if not self._store:
            return
        now_utc = datetime.now(timezone.utc)
        minute_key = now_utc.strftime("%Y-%m-%dT%H:%M")
        snapshot = {
            "date": now_utc.isoformat(),
            "calendar_date": now_utc.date().isoformat(),
            "minute_key": minute_key,
            "metric": "deployable_capital_equity",
            "pnl_basis": "gross_mark_to_market_before_future_exit_costs",
            "total_equity": round(float(total_equity or 0.0), 2),
            "balance": round(float(self.balance or 0.0), 2),
            "open_positions_count": int(open_count or 0),
            "research_excluded": True,
        }
        if deployable_cash is not None:
            snapshot["deployable_cash"] = round(float(deployable_cash or 0.0), 2)
        if open_margin is not None:
            snapshot["open_margin"] = round(float(open_margin or 0.0), 2)
        if mark_to_market_pnl is not None:
            snapshot["mark_to_market_pnl"] = round(float(mark_to_market_pnl or 0.0), 2)
        try:
            history = self._store.get_json("equity_history", [])
            if not isinstance(history, list):
                history = []

            def _mkey(entry: Any) -> str:
                if not isinstance(entry, dict):
                    return ""
                mk = entry.get("minute_key")
                if isinstance(mk, str) and mk:
                    return mk
                return str(entry.get("date") or "")[:16]  # legacy: "YYYY-MM-DDTHH:MM"

            history = [h for h in history if _mkey(h) != minute_key]
            history.append(snapshot)
            # Tiered retention (minute / hour / day by age, bounded) instead of
            # "newest 500": a point per minute pushed months of history out of
            # the list within hours. See core/portfolio/equity_history.py.
            from core.portfolio.equity_history import compact_equity_history

            history = compact_equity_history(history, now=now_utc)
            self._store.set_json("equity_history", history)
        except Exception as e:
            logger.debug(f"equity snapshot store failed: {e}")

    def record_equity_point(self, price_map: Optional[Dict[str, float]] = None) -> None:
        """Append a live intraday equity point from already-known prices.

        Cheap (no external quote fetch) so callers on the hot path — e.g. the
        capital state builder, which already holds fresh live prices — can record
        a point on every poll. Deduped per-minute by _append_equity_snapshot.
        """
        prices = price_map or {}
        try:
            with self._lock:
                self._sync_for_read()
                total_margin = 0.0
                mv = 0.0
                open_count = 0
                for pos in self.positions:
                    if (
                        str((pos or {}).get("status", "")).upper() != "OPEN"
                        or self._is_research_position(pos)
                    ):
                        continue
                    open_count += 1
                    total_margin += float(pos.get("margin_used", 0.0) or 0.0)
                    px_in = float(pos.get("entry_price", 0.0) or 0.0)
                    qty = float(pos.get("qty", 0.0) or 0.0)
                    ticker = str(pos.get("ticker", "") or "")
                    curr = float(
                        prices.get(ticker)
                        or prices.get(ticker.upper())
                        or pos.get("current_price")
                        or px_in
                        or 0.0
                    )
                    direction = str(pos.get("direction", "LONG")).upper()
                    if curr > 0 and px_in > 0 and qty > 0:
                        mv += (curr - px_in) * qty if direction == "LONG" else (px_in - curr) * qty
                deployable_cash = self._capital_accounting_unlocked()["deployable_cash_balance"]
                total_equity = deployable_cash + total_margin + mv
                self._append_equity_snapshot(
                    total_equity,
                    open_count,
                    deployable_cash=deployable_cash,
                    open_margin=total_margin,
                    mark_to_market_pnl=mv,
                )
        except Exception as e:
            logger.debug(f"record_equity_point skipped: {e}")

    def _snapshot_daily_equity(self):
        from datetime import datetime

        # Phase 1: Copy open positions under the lock, then fetch prices outside.
        with self._lock:
            self._sync_for_read()
            open_snapshot = [
                dict(p)
                for p in self.positions
                if p.get("status") == "OPEN" and not self._is_research_position(p)
            ]

        position_prices: Dict[str, float] = {}
        for pos in open_snapshot:
            ticker = pos.get("ticker")
            direction = pos.get("direction", "LONG").upper()
            closing_side = "SELL" if direction == "LONG" else "BUY"
            try:
                exec_data = self._get_execution_price(ticker, closing_side)
                curr_px = float(exec_data.get("execution_price", 0.0) or 0.0)
                if curr_px > 0:
                    position_prices[ticker] = curr_px
            except Exception:
                pass

        # Phase 2: Compute equity inside the lock (no external calls).
        with self._lock:
            self._sync_for_read()
            open_positions_mv = 0.0
            total_margin = 0.0
            open_count = 0

            for pos in self.positions:
                if pos.get("status") != "OPEN" or self._is_research_position(pos):
                    continue
                open_count += 1
                total_margin += float(pos.get("margin_used", 0.0) or 0.0)
                direction = pos.get("direction", "LONG").upper()
                px_in = float(pos.get("entry_price", 0.0))
                qty = float(pos.get("qty", 0.0))
                ticker = pos.get("ticker", "")
                curr_px = position_prices.get(ticker, 0.0)
                if curr_px > 0 and px_in > 0 and qty > 0:
                    gross_pnl = (curr_px - px_in) * qty if direction == "LONG" else (px_in - curr_px) * qty
                    open_positions_mv += gross_pnl

            deployable_cash = self._capital_accounting_unlocked()["deployable_cash_balance"]
            total_equity = deployable_cash + total_margin + open_positions_mv
            self._append_equity_snapshot(
                total_equity,
                open_count,
                deployable_cash=deployable_cash,
                open_margin=total_margin,
                mark_to_market_pnl=open_positions_mv,
            )

    def _add_transaction(
        self,
        type_: str,
        amount: float,
        current_balance: float,
        details: str,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        transaction_metadata = dict(metadata or {}) if isinstance(metadata, dict) else {}
        scope = str(transaction_metadata.get("account_scope") or "").lower().strip()
        if scope not in {"capital", "research"}:
            if transaction_metadata.get("research_only") is True:
                scope = "research"
            elif str(type_ or "").upper() in {"RESET", "DEPOSIT", "WITHDRAW", "WIN", "LOSS"}:
                scope = "capital"
            else:
                scope = "unclassified"
        raw_balance = float(current_balance or 0.0)
        txn = {
            "timestamp": _now_local_iso(),
            "type": type_,
            "amount": amount,
            "balance": raw_balance,
            "raw_balance": raw_balance,
            "balance_kind": "raw_shared_cash",
            "account_scope": scope,
            "affects_deployable_capital": (
                scope == "capital" if scope in {"capital", "research"} else None
            ),
            "details": details,
        }
        if transaction_metadata:
            transaction_metadata["account_scope"] = scope
            txn["metadata"] = transaction_metadata
        self.history.append(txn)

def read_capital_save_health(capital_manager: Any = None) -> Dict[str, Any]:
    """Capital save health across processes, for the read model and System Health.

    ``status`` is "failing" when any process's last save did not persist,
    "ok" when every known process's last save persisted, "unknown" when no
    process has reported yet. The engine and the API process each publish a
    small file (see ``CapitalManager._write_save_health_file``); this
    process's own record is always the live in-memory one.
    """
    processes: Dict[str, Dict[str, Any]] = {}
    try:
        for path in sorted(CAPITAL_STATE_PATH.parent.glob(f"{SAVE_HEALTH_FILE_PREFIX}.*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(record, dict):
                processes[str(record.get("process_role") or path.stem.split(".")[-1])] = record
    except Exception as exc:
        logger.debug(f"capital save-health files unreadable: {exc}")
    getter = getattr(capital_manager, "get_save_health", None)
    if callable(getter):
        try:
            own = getter()
            if isinstance(own, dict):
                recorded = any(
                    own.get(field) for field in ("last_save_ok_at", "last_save_error_at", "last_mirror_error_at")
                )
                role = str(own.get("process_role") or "this_process")
                if recorded or role not in processes:
                    processes[role] = {**processes.get(role, {}), **own}
        except Exception as exc:
            logger.debug(f"capital save-health of this process unavailable: {exc}")

    def _latest(field: str) -> Optional[str]:
        values = [str(record.get(field)) for record in processes.values() if record.get(field)]
        return max(values) if values else None

    failing = {
        role: record for role, record in processes.items()
        if int(record.get("consecutive_save_failures") or 0) > 0
    }
    reported = [
        record for record in processes.values()
        if record.get("last_save_ok_at") or record.get("last_save_error_at")
    ]
    worst = max(failing.values(), key=lambda record: str(record.get("last_save_error_at") or ""), default=None)
    return {
        "status": "failing" if failing else ("ok" if reported else "unknown"),
        "consecutive_save_failures": max(
            (int(record.get("consecutive_save_failures") or 0) for record in processes.values()),
            default=0,
        ),
        "total_save_failures": sum(int(record.get("total_save_failures") or 0) for record in processes.values()),
        "last_save_ok_at": _latest("last_save_ok_at"),
        # While failing: the error of the failing process. Otherwise the most
        # recent past error (kept for the record), or None.
        "last_save_error": (
            worst.get("last_save_error") if worst is not None
            else next(
                (
                    record.get("last_save_error")
                    for record in sorted(
                        processes.values(),
                        key=lambda record: str(record.get("last_save_error_at") or ""),
                        reverse=True,
                    )
                    if record.get("last_save_error")
                ),
                None,
            )
        ),
        "last_save_error_at": _latest("last_save_error_at"),
        "failing_processes": sorted(failing),
        "consecutive_mirror_failures": max(
            (int(record.get("consecutive_mirror_failures") or 0) for record in processes.values()),
            default=0,
        ),
        "last_mirror_error": next(
            (record.get("last_mirror_error") for record in processes.values() if record.get("last_mirror_error")),
            None,
        ),
        "processes": processes,
    }


# Global getter
def get_capital_manager() -> CapitalManager:
    return CapitalManager()
