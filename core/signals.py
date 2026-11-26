# core/signals.py - signal management (open/closed, statistici)

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from .config import SIGNALS_PATH, BUCHAREST_TZ, get_bucharest_time, ConfigManager
from .capital_manager import get_capital_manager, resolve_deployable_balance
from .state_persistence import load_signals_payload, save_signals_payload
from .trade_decision import summarize_position_explainability

logger = logging.getLogger("MarketEngine")


class SignalManager:
    ACTIVE_STATUSES = {"Pending", "Open"}
    def __init__(self, path: Path = SIGNALS_PATH):
        self.path = path
        self.open_signals: Dict[str, dict] = {}
        self.closed_signals: List[dict] = []
        # Per-ticker cooldown tracking: ticker -> last signal time
        self.last_signal_time: Dict[str, datetime] = {}
        # When a ticker was last stopped out; used for the longer post-stop lockout.
        self.last_stop_time: Dict[str, datetime] = {}
        self.last_mtime: float = 0.0
        # Serializes load/save: concurrent whole-payload writes from separate
        # instances were silently dropping each other's updates.
        self._lock = threading.RLock()
        self.load()

    @classmethod
    def _is_active_signal(cls, signal: dict) -> bool:
        status = str((signal or {}).get("status", "") or "")
        return status in cls.ACTIVE_STATUSES

    @staticmethod
    def _is_performance_eligible_outcome(signal: dict) -> bool:
        """Only outcomes with recorded price provenance may train signal gates."""
        return bool(
            isinstance(signal, dict)
            and signal.get("outcome_data_valid") is True
            and signal.get("research_only") is not True
            and str(signal.get("status", "")).upper() in {"WIN", "LOST"}
        )

    @staticmethod
    def _is_verified_execution_quality(value: object) -> bool:
        if not isinstance(value, dict) or value.get("is_valid") is False:
            return False
        level = str(value.get("level") or value.get("quality") or "").upper().strip()
        return level == "VERIFIED"

    @staticmethod
    def _effective_score(signal: dict) -> float:
        try:
            for key in ("signal_score", "score", "raw_signal_score", "raw_score"):
                value = float((signal or {}).get(key, 0) or 0)
                if value > 0:
                    return value
        except Exception:
            return 0.0
        return 0.0

    @classmethod
    def _normalize_signal_scores(cls, signal: dict) -> dict:
        if not isinstance(signal, dict):
            return signal
        score = cls._effective_score(signal)
        if score <= 0:
            return signal
        try:
            if float(signal.get("signal_score", 0) or 0) <= 0:
                signal["signal_score"] = score
        except Exception:
            signal["signal_score"] = score
        try:
            if float(signal.get("score", 0) or 0) <= 0:
                signal["score"] = score
        except Exception:
            signal["score"] = score
        signal.setdefault("raw_signal_score", signal.get("raw_score", score))
        return signal

    @staticmethod
    def _mark_research_only_when_unvalidated(signal: dict) -> dict:
        """Keep non-V2 signals visible without allowing automated promotion."""
        if not isinstance(signal, dict):
            return signal
        # This marker is emitted only after the paper-only Capital gate accepts
        # the configured S7 experiment.  Do not reclassify it while persisting
        # the signal; live mode still fails closed in the Capital entry guard.
        if bool(signal.get("paper_capital_experiment")):
            return signal
        strategy_source = str(signal.get("strategy_source") or "V1").upper().strip()
        if strategy_source == "V2":
            return signal
        signal["research_only"] = True
        signal["capital_execution_eligible"] = False
        signal.setdefault(
            "capital_execution_block_reason",
            "Automated Capital execution requires a faithful V2 OOS validation",
        )
        return signal

    @classmethod
    def _should_be_open(cls, signal: dict) -> bool:
        score = cls._effective_score(signal)
        try:
            threshold = float((signal or {}).get("signal_threshold", 60) or 60)
        except Exception:
            threshold = 60.0
        open_cutoff = max(70.0, threshold + 10.0)
        return score >= open_cutoff

    @classmethod
    def _normalize_open_signals(cls, signals: dict) -> Dict[str, dict]:
        if not isinstance(signals, dict):
            return {}
        out: Dict[str, dict] = {}
        for sid, signal in signals.items():
            if not isinstance(signal, dict):
                continue
            # Drop garbage tickers produced by the pre-fix empty-base USDT/USDC
            # conversion (e.g. "-USD" from a bare "USDT" symbol). These are not
            # real assets and would render as a blank ticker on Home / fail every
            # price fetch. Auto-heals persisted state on load (directive: nothing
            # invented, every value correct). The upstream converters now guard
            # empty bases, so no new garbage is produced — this purges legacy rows.
            ticker = str(signal.get("ticker") or signal.get("symbol") or "").strip()
            if not ticker or ticker.startswith("-") or ticker.endswith("-USD") and ticker == "-USD":
                logger.info("Dropping garbage-ticker open signal %s (ticker=%r)", sid, ticker)
                continue
            out[str(sid)] = cls._mark_research_only_when_unvalidated(
                cls._normalize_signal_scores(signal)
            )
        return out

    @classmethod
    def _normalize_closed_signals(cls, signals: List[dict]) -> List[dict]:
        if not isinstance(signals, list):
            return []
        normalized: List[dict] = []
        for signal in signals:
            if not isinstance(signal, dict):
                normalized.append(signal)
                continue
            signal = cls._mark_research_only_when_unvalidated(
                cls._normalize_signal_scores(signal)
            )
            if (
                str(signal.get("status", "")).upper() in {"WIN", "LOST"}
                and not isinstance(signal.get("outcome_data_valid"), bool)
            ):
                # Historical outcomes predate price-source provenance. Preserve
                # them for audit, but never let them alter present quality gates.
                signal["outcome_data_valid"] = False
                signal.setdefault("outcome_validation", "legacy_without_price_provenance")
            normalized.append(signal)
        return normalized

    @staticmethod
    def _is_autonomous_signal(signal: dict) -> bool:
        if not isinstance(signal, dict):
            return False
        source = str(signal.get("source") or "").lower()
        autonomous_source = str(signal.get("autonomous_source") or "").strip()
        signal_mode = str(signal.get("signal_mode") or signal.get("mode") or "").upper()
        strategy_type = str(signal.get("strategy_type") or signal.get("strategy_id") or "").upper()
        return bool(autonomous_source) or source == "autonomous_monitor" or signal_mode.startswith("AUTONOMOUS_") or strategy_type.startswith("SCANNER_AI_")

    def _find_open_paper_position(self, sid: str, signal: dict, positions: Optional[list] = None) -> Optional[dict]:
        if not sid or not isinstance(signal, dict):
            return None
        try:
            # PERF: accept a pre-fetched positions list so hot callers (restore loop)
            # don't trigger one capital storage-sync per signal (N+1 I/O). Each
            # get_positions()/get_position() runs _sync_from_storage(); calling it
            # ~280x in restore_open_paper_signals cost ~36s and timed out /signals.
            if positions is None:
                positions = get_capital_manager().get_positions()
            paper_position_id = str(signal.get("paper_position_id", "") or "").strip()
            if paper_position_id:
                for position in positions:
                    if isinstance(position, dict) and str(position.get("position_id", "")).strip() == paper_position_id and str(position.get("status", "")).upper() == "OPEN":
                        return position

            for position in positions:
                if not isinstance(position, dict) or str(position.get("status", "")).upper() != "OPEN":
                    continue
                candidate_ids = {
                    str(position.get("signal_id") or "").strip(),
                    str(position.get("source_signal_id") or "").strip(),
                }
                for value in position.get("source_signal_ids") or []:
                    candidate_ids.add(str(value or "").strip())
                if sid in candidate_ids:
                    return position
        except Exception as exc:
            logger.debug(f"Could not inspect paper position for signal {sid}: {exc}")
        return None

    @staticmethod
    def _attach_open_paper_position_metadata(signal: dict, position: dict) -> bool:
        if not isinstance(signal, dict) or not isinstance(position, dict):
            return False

        changed = False

        def set_value(key: str, value) -> None:
            nonlocal changed
            if signal.get(key) != value:
                signal[key] = value
                changed = True

        set_value("paper_trade", True)
        set_value("paper_position_id", position.get("position_id"))
        set_value("paper_status", position.get("status", "OPEN"))
        set_value("paper_opened_at", position.get("opened_at"))
        set_value("executed_entry_price", position.get("entry_price"))
        set_value("executed_qty", position.get("qty"))
        set_value("paper_margin_used", position.get("margin_used"))
        set_value("paper_commission_usd", position.get("commission_usd", position.get("fees_estimate")))
        set_value("strategy_type", position.get("strategy_type", signal.get("strategy_type")))
        set_value("entry_reason", position.get("entry_reason", signal.get("entry_reason")))
        set_value("decision_context", dict(position.get("decision_context") or signal.get("decision_context") or {}))
        set_value("explainability", summarize_position_explainability(position))

        for key in ("stale_paper_position_id", "paper_sync_warning"):
            if key in signal:
                signal.pop(key, None)
                changed = True

        return changed

    # ---- I/O ----

    def load(self) -> None:
        with self._lock:
            self._load_locked()

    def _load_locked(self) -> None:
        if self.path == SIGNALS_PATH:
            try:
                data = load_signals_payload({"open": {}, "closed": [], "last_signal_time": {}})
                self.open_signals = self._normalize_open_signals(data.get("open", {}))
                self.closed_signals = self._normalize_closed_signals(data.get("closed", []))
                _removed = self.dedupe_open_signals()
                if _removed:
                    logger.info(f"Deduped {_removed} duplicate open signal(s) on load")
                    self._save_locked()

                # Load per-ticker cooldown times
                # Runda-6 D3: RESET before merge (mirror open_signals full-replace at
                # :180). Pre-fix this merged saved_times INTO the in-memory dict
                # without clearing, so a cross-process clear_all (API writes {} to
                # disk) did NOT propagate the cooldown reset to the engine
                # subprocess -- the merge loop was a no-op on {} and the engine
                # retained stale cooldowns, blocking signals the user had just
                # cleared. Rebuild purely from disk so clear_all propagates fully.
                self.last_signal_time = {}
                saved_times = data.get("last_signal_time", {})
                for ticker, time_str in saved_times.items():
                    try:
                        dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = BUCHAREST_TZ.localize(dt)
                        self.last_signal_time[ticker] = dt
                    except Exception:
                        pass
                if self.path.exists():
                    self.last_mtime = self.path.stat().st_mtime
                logger.info(f"Loaded signals: {len(self.open_signals)} open, {len(self.closed_signals)} closed")
            except Exception as e:
                logger.error(f"Eroare load signals: {e}")
                # Backup corrupt file
                try:
                    backup_path = self.path.with_suffix(".corrupt.json")
                    import shutil
                    shutil.copy2(self.path, backup_path)
                    logger.info(f"Backed up corrupt signals file to {backup_path}")
                except Exception as ex:
                    logger.error(f"Failed to backup corrupt file: {ex}")
            return

        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.open_signals = self._normalize_open_signals(data.get("open", {}))
                self.closed_signals = self._normalize_closed_signals(data.get("closed", []))
                _removed = self.dedupe_open_signals()
                if _removed:
                    logger.info(f"Deduped {_removed} duplicate open signal(s) on load")

                # Check integrity - if file is empty JSON or invalid structure, warn
                if not self.open_signals and not self.closed_signals and self.path.stat().st_size > 100:
                    logger.warning(f"Warning: signals file {self.path} exists but resulted in empty data.")

                # Load per-ticker cooldown times
                # Runda-6 D3: RESET before merge (mirror open_signals full-replace
                # at :216). See the SIGNALS_PATH branch above for the full rationale
                # (cross-process clear_all propagation).
                self.last_signal_time = {}
                saved_times = data.get("last_signal_time", {})
                for ticker, time_str in saved_times.items():
                    try:
                        dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = BUCHAREST_TZ.localize(dt)
                        self.last_signal_time[ticker] = dt
                    except Exception:
                        pass
                self.last_mtime = self.path.stat().st_mtime
                logger.info(f"Loaded signals: {len(self.open_signals)} open, {len(self.closed_signals)} closed")
            except Exception as e:
                logger.error(f"Eroare load signals: {e}")
                # Backup corrupt file
                try:
                    backup_path = self.path.with_suffix(".corrupt.json")
                    import shutil
                    shutil.copy2(self.path, backup_path)
                    logger.info(f"Backed up corrupt signals file to {backup_path}")
                except Exception as ex:
                    logger.error(f"Failed to backup corrupt file: {ex}")

    def dedupe_open_signals(self) -> int:
        """Collapse duplicate unexecuted signals while preserving paper positions.

        The autonomous scanner mints a fresh hourly id for the same coin each cycle
        (id = hash of source:ticker:action:YYYYMMDDHH). The newest unexecuted
        autonomous decision is the only current one for a ticker, including when
        direction changed. Signals linked to paper positions are never removed.
        """
        def _created(sid: str) -> float:
            s = self.open_signals.get(sid) or {}
            value = s.get("created_at") or s.get("timestamp")
            if not value:
                return 0.0
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = BUCHAREST_TZ.localize(parsed)
                return parsed.timestamp()
            except Exception:
                return 0.0

        def _has_paper_link(signal: dict) -> bool:
            return bool((signal or {}).get("paper_position_id"))

        keep_by_key: Dict[tuple, str] = {}
        keep_autonomous_by_ticker: Dict[str, str] = {}
        remove: set[str] = set()
        for sid in sorted(list(self.open_signals.keys()), key=_created, reverse=True):
            sig = self.open_signals.get(sid)
            if not isinstance(sig, dict):
                continue
            if _has_paper_link(sig):
                continue
            ticker = str(sig.get("ticker") or sig.get("symbol") or "").upper().strip()
            action = str(sig.get("action") or sig.get("type") or "").upper().strip()
            if not ticker:
                continue
            if self._is_autonomous_signal(sig):
                if ticker in keep_autonomous_by_ticker:
                    remove.add(sid)
                else:
                    keep_autonomous_by_ticker[ticker] = sid
            else:
                key = (ticker, action)
                if key in keep_by_key:
                    remove.add(sid)
                else:
                    keep_by_key[key] = sid
        for sid in remove:
            self.open_signals.pop(sid, None)
        return len(remove)

    def supersede_unexecuted_autonomous_signals(self, ticker: str, replacement_id: str) -> int:
        """Archive obsolete autonomous candidates before registering a fresh decision."""
        normalized_ticker = str(ticker or "").upper().strip()
        normalized_replacement = str(replacement_id or "").strip()
        if not normalized_ticker:
            return 0

        with self._lock:
            superseded: List[tuple[str, dict]] = []
            for sid, signal in list(self.open_signals.items()):
                if sid == normalized_replacement or not isinstance(signal, dict):
                    continue
                signal_ticker = str(signal.get("ticker") or signal.get("symbol") or "").upper().strip()
                if signal_ticker != normalized_ticker or not self._is_autonomous_signal(signal):
                    continue
                # A paper-position link is authoritative lifecycle state; it can
                # only be closed by the Capital manager, never by signal refresh.
                if signal.get("paper_position_id"):
                    continue
                superseded.append((sid, signal))

            for sid, signal in superseded:
                self.open_signals.pop(sid, None)
                archived = dict(signal)
                archived["status"] = "SUPERSEDED"
                archived["closed_at"] = get_bucharest_time().isoformat()
                archived["close_reason"] = f"Superseded by fresh autonomous decision {normalized_replacement}"
                archived["superseded_by_signal_id"] = normalized_replacement
                archived["outcome_data_valid"] = False
                archived["capital_settled"] = False
                self.closed_signals.insert(0, archived)

            if superseded:
                self._save_locked()
            return len(superseded)

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        try:
            # Create a rolling backup before overwriting if file exists and has content
            if self.path.exists() and self.path.stat().st_size > 0:
                try:
                    # Keep one backup
                    backup_path = self.path.with_suffix(".bak.json")
                    import shutil
                    shutil.copy2(self.path, backup_path)
                except Exception as e:
                    logger.warning(f"Failed to create signal backup: {e}")

            # Serialize last_signal_time to ISO format strings
            serialized_times = {
                ticker: dt.isoformat() for ticker, dt in self.last_signal_time.items()
            }
            self.open_signals = self._normalize_open_signals(self.open_signals)
            self.closed_signals = self._normalize_closed_signals(self.closed_signals)

            payload = {
                "open": self.open_signals,
                "closed": self.closed_signals,
                "last_signal_time": serialized_times,
                "updated_at": get_bucharest_time().isoformat(),
            }

            if self.path == SIGNALS_PATH:
                save_signals_payload(payload)
            else:
                # Write to temp file first, then rename (atomic write)
                temp_path = self.path.with_suffix(".tmp")
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)

                # Atomic replace
                temp_path.replace(self.path)
            
        except Exception as e:
            logger.error(f"Error saving signals: {e}")

    # ---- signal operations ----

    def delete_signal(self, signal_id: str) -> bool:
        """
        Delete a signal (open or closed) by ID.
        Returns True if deleted, False if not found.
        """
        # Critical section under self._lock: concurrent mutators (add_signal,
        # update_outcomes, close_signal_manual) otherwise race on open/closed and
        # the trailing save() can drop another thread's update.
        with self._lock:
            deleted = False

            # Try deleting from open signals
            if signal_id in self.open_signals:
                del self.open_signals[signal_id]
                deleted = True
                logger.info(f"Deleted open signal {signal_id}")

            # Try deleting from closed signals
            if not deleted:
                initial_len = len(self.closed_signals)
                self.closed_signals = [s for s in self.closed_signals if s.get("id") != signal_id]
                if len(self.closed_signals) < initial_len:
                    deleted = True
                    logger.info(f"Deleted closed signal {signal_id}")

            if deleted:
                self.save()

        return deleted


    def clear_all(self) -> None:
        """Clear all signals (open and closed) and save empty state."""
        with self._lock:
            self.open_signals = {}
            self.closed_signals = []
            self.last_signal_time = {}
            self.save()
        logger.info("SignalManager: All signals cleared.")

    def cleanup_orphaned_signals(self, current_watchlist: set, tickers_data: dict = None) -> int:
        """
        Remove open signals for tickers that are no longer in the watchlist.
        
        Args:
            current_watchlist: Set of tickers currently in watchlist
            tickers_data: Optional dict of ticker -> {price, ...} for exit prices
            
        Returns:
            Number of signals removed
        """
        to_remove = []
        for sid, signal in self.open_signals.items():
            ticker = signal.get("ticker", "")
            is_autonomous = bool(signal.get("autonomous_source")) or str(signal.get("source") or "").lower() == "autonomous_monitor"
            if is_autonomous:
                continue
            if ticker and ticker not in current_watchlist:
                to_remove.append(sid)
                logger.info(f"🗑️ Removing orphaned signal {sid} (ticker {ticker} not in watchlist)")
        
        for sid in to_remove:
            closed_signal = self.open_signals.pop(sid)
            closed_signal["status"] = "CLOSED"
            closed_signal["closed_at"] = get_bucharest_time().isoformat()
            closed_signal["close_reason"] = "Ticker removed from watchlist"
            
            # Set exit price from tickers_data if available, otherwise use entry price
            ticker = closed_signal.get("ticker", "")
            if tickers_data and ticker in tickers_data:
                exit_price = tickers_data[ticker].get("price", closed_signal.get("entry", 0))
            else:
                exit_price = closed_signal.get("entry", 0)  # Use entry price as fallback
            closed_signal["exit_price"] = exit_price
            
            # Calculate P&L percentage
            entry_price = closed_signal.get("entry", 0)
            action = closed_signal.get("action", closed_signal.get("type", ""))
            
            if entry_price > 0 and exit_price > 0:
                if action == "BUY":
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                elif action == "SHORT":
                    pnl_pct = ((entry_price - exit_price) / entry_price) * 100
                else:
                    pnl_pct = 0.0
                
                closed_signal["pnl_pct"] = round(pnl_pct, 2)
            else:
                closed_signal["pnl_pct"] = 0.0
            
            self.closed_signals.append(closed_signal)
        
        if to_remove:
            self.save()
            
        return len(to_remove)

    def cleanup_invalid_signals(self, config: ConfigManager, tickers_data: dict = None) -> int:
        """
        Remove open signals that no longer match current ticker settings.

        Rules:
        - signals_enabled = False => remove
        - allowed_strategies set and current strategy not in list => remove
        - min_win_rate > 0 and current 30d win_rate below threshold => remove
        """
        to_remove = []

        for sid, signal in self.open_signals.items():
            ticker = (signal.get("ticker") or "").upper()
            if not ticker:
                continue

            # Autonomous executions are owned by the paper-position lifecycle.
            # Watchlist strategy filters must not hide a still-open paper trade.
            if self._is_autonomous_signal(signal) or self._find_open_paper_position(sid, signal):
                continue

            t_settings = config.get_effective_ticker_settings(ticker) or {}
            if t_settings.get("signals_enabled") is False:
                to_remove.append((sid, "Signals disabled in settings"))
                continue

            allowed = t_settings.get("allowed_strategies") or []
            if allowed:
                norm_allowed = set()
                for s in allowed:
                    try:
                        r, t = s.split("/")
                    except Exception:
                        continue
                    if r == "PRO":
                        r = "STRONG"
                    norm_allowed.add(f"{r}/{t}")

                risk_mode = (signal.get("risk_mode") or "MEDIUM").upper()
                trade_mode = (
                    signal.get("signal_mode")
                    or signal.get("trade_mode")
                    or signal.get("mode")
                    or "PULLBACK"
                )
                trade_mode = str(trade_mode).upper()
                current = f"{risk_mode}/{trade_mode}"
                if current not in norm_allowed:
                    to_remove.append((sid, f"Strategy {current} not allowed"))
                    continue

            min_wr = float(t_settings.get("min_win_rate", 0) or 0)
            if min_wr > 0:
                bt30 = signal.get("quick_backtest_30d")
                # Field absent entirely (old signal without backtest) → don't close
                if bt30 is None:
                    continue
                bt30 = bt30 if isinstance(bt30, dict) else {}
                # Backtest marked as pending → don't close
                if bt30.get("pending"):
                    continue
                total = bt30.get("total", bt30.get("total_signals", 0))
                try:
                    total = int(total or 0)
                except Exception:
                    total = 0

                wr = bt30.get("win_rate")
                try:
                    wr_val = float(wr)
                except Exception:
                    wr_val = None

                if total <= 0 or wr_val is None:
                    to_remove.append((sid, f"No 30d backtest history for min WR {min_wr:.1f}%"))
                    continue
                if wr_val < min_wr:
                    to_remove.append((sid, f"Win rate {wr_val:.1f}% < min {min_wr:.1f}%"))
                    continue

        for sid, reason in to_remove:
            closed_signal = self.open_signals.pop(sid, None)
            if not closed_signal:
                continue
            closed_signal["status"] = "CLOSED"
            closed_signal["closed_at"] = get_bucharest_time().isoformat()
            closed_signal["close_reason"] = reason

            # Set exit price from tickers_data if available, otherwise use entry price
            if tickers_data and closed_signal.get("ticker") in tickers_data:
                exit_price = tickers_data[closed_signal["ticker"]].get("price", closed_signal.get("entry", 0))
            else:
                exit_price = closed_signal.get("entry", 0)
            closed_signal["exit_price"] = exit_price

            self.closed_signals.insert(0, closed_signal)
            logger.info(f"🧹 Removed invalid signal {sid}: {reason}")

        if to_remove:
            self.save()

        return len(to_remove)

    def cleanup_expired_unexecuted_signals(self, config: ConfigManager) -> int:
        """Archive stale candidates that never became paper positions."""
        try:
            max_age_seconds = max(
                1,
                int(float((config.signal or {}).get("max_live_age_seconds", 900) or 900)),
            )
        except (TypeError, ValueError):
            max_age_seconds = 900

        now = get_bucharest_time()
        expired: List[tuple[str, dict, float]] = []
        with self._lock:
            for sid, signal in list(self.open_signals.items()):
                if not isinstance(signal, dict) or signal.get("paper_position_id"):
                    continue
                created_raw = signal.get("created_at") or signal.get("timestamp")
                if not created_raw:
                    continue
                try:
                    created_at = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
                    if created_at.tzinfo is None:
                        created_at = BUCHAREST_TZ.localize(created_at)
                    age_seconds = max(0.0, (now - created_at).total_seconds())
                except (TypeError, ValueError):
                    continue
                if age_seconds > max_age_seconds:
                    expired.append((str(sid), signal, age_seconds))

            for sid, signal, age_seconds in expired:
                self.open_signals.pop(sid, None)
                archived = dict(signal)
                archived["status"] = "EXPIRED"
                archived["closed_at"] = now.isoformat()
                archived["close_reason"] = (
                    f"Unexecuted setup expired after {int(age_seconds)}s "
                    f"(live window {max_age_seconds}s)"
                )
                archived["outcome_data_valid"] = False
                archived["capital_settled"] = False
                self.closed_signals.insert(0, archived)

            if expired:
                self._save_locked()
                logger.info("Archived %s expired unexecuted signal(s)", len(expired))
        return len(expired)

    def archive_positions_removed_by_reset(
        self,
        positions: List[dict],
        *,
        reason: str,
        reset_at: str,
    ) -> int:
        """Archive executed signals whose Capital positions were cleared by reset.

        A reset is neither an unexecuted expiry nor a verified trading outcome.
        Preserve the execution link and exclude the record from performance so
        reconciliation cannot later make it eligible for duplicate execution.
        """
        position_ids = {
            str((position or {}).get("position_id") or "").strip()
            for position in positions or []
            if isinstance(position, dict)
        }
        signal_ids = {
            str(
                (position or {}).get("signal_id")
                or (position or {}).get("source_signal_id")
                or ""
            ).strip()
            for position in positions or []
            if isinstance(position, dict)
        }
        position_ids.discard("")
        signal_ids.discard("")
        if not position_ids and not signal_ids:
            return 0

        archived_count = 0
        with self._lock:
            for sid, signal in list(self.open_signals.items()):
                if not isinstance(signal, dict):
                    continue
                paper_position_id = str(signal.get("paper_position_id") or "").strip()
                if paper_position_id not in position_ids and str(sid).strip() not in signal_ids:
                    continue

                self.open_signals.pop(sid, None)
                archived = dict(signal)
                archived.update({
                    "status": "CANCELLED",
                    "closed_at": reset_at or get_bucharest_time().isoformat(),
                    "close_reason": f"Capital reset: {reason or 'Manual Reset'}",
                    "paper_trade": True,
                    "paper_status": "RESET",
                    "paper_position_status": "RESET",
                    "outcome_data_valid": False,
                    "outcome_validation": "excluded_capital_reset",
                    "capital_settled": True,
                    "capital_settlement_type": "RESET",
                })
                if paper_position_id:
                    archived["paper_position_id"] = paper_position_id
                archived.pop("stale_paper_position_id", None)
                archived.pop("paper_sync_warning", None)
                self.closed_signals.insert(0, archived)
                archived_count += 1

            if archived_count:
                self._save_locked()
                logger.info(
                    "Archived %s executed signal(s) cleared by Capital reset",
                    archived_count,
                )
        return archived_count

    def restore_open_paper_signals(self) -> int:
        """
        Repair signals that were moved to closed while their paper position is still open.

        The UI reads the signal store for "Signals Live", while capital is the
        source of truth for executed paper positions. If a settings cleanup hides
        an autonomous signal but the attached position remains open, move it back.
        """
        restored = 0
        remaining_closed: List[dict] = []

        # PERF: fetch capital positions ONCE for the whole scan. Previously each
        # closed signal called _find_open_paper_position -> cm.get_positions(),
        # and every get_positions() does a storage sync; ~280 syncs cost ~36s and
        # made /signals time out (the UI then showed no signals).
        try:
            _all_positions = get_capital_manager().get_positions()
        except Exception:
            _all_positions = []

        for signal in self.closed_signals:
            if not isinstance(signal, dict):
                remaining_closed.append(signal)
                continue

            sid = str(signal.get("id") or "").strip()
            if not sid or sid in self.open_signals:
                remaining_closed.append(signal)
                continue

            position = self._find_open_paper_position(sid, signal, _all_positions)
            if not position:
                remaining_closed.append(signal)
                continue

            signal["status"] = "Open"
            self._attach_open_paper_position_metadata(signal, position)

            for key in (
                "closed_at",
                "close_reason",
                "exit_price",
                "pnl_pct",
                "pnl_amount",
                "paper_position_status",
                "invested",
            ):
                signal.pop(key, None)

            self.open_signals[sid] = signal
            restored += 1
            logger.info(f"Restored open paper signal {sid} from closed store")

        if restored:
            self.closed_signals = remaining_closed
            self.save()

        return restored

    def add_signal(self, signal_data: dict) -> str:
        signal_data = self._mark_research_only_when_unvalidated(signal_data)
        sid = signal_data.get("id")

        # Validate signal ID
        if not sid:
            raise ValueError("Signal must have an ID")

        # Validate TP/SL ordering for BUY vs SHORT signals
        action = signal_data.get("action", signal_data.get("type", ""))
        # Support both 'entry' and 'entry_price' for backwards compatibility
        entry_price = signal_data.get("entry", signal_data.get("entry_price", 0.0))
        tp = signal_data.get("tp", 0.0)
        sl = signal_data.get("sl", 0.0)

        # Skip signals with invalid TP/SL
        if tp <= 0 or sl <= 0:
            raise ValueError(f"Signal {sid} has invalid TP/SL values: TP={tp}, SL={sl}")

        # Validate TP/SL ordering based on action type
        if action == "BUY":
            if tp <= entry_price:
                raise ValueError(f"BUY signal {sid}: TP ({tp}) must be > entry_price ({entry_price})")
            if sl >= entry_price:
                raise ValueError(f"BUY signal {sid}: SL ({sl}) must be < entry_price ({entry_price})")
        elif action == "SHORT":
            if tp >= entry_price:
                raise ValueError(f"SHORT signal {sid}: TP ({tp}) must be < entry_price ({entry_price})")
            if sl <= entry_price:
                raise ValueError(f"SHORT signal {sid}: SL ({sl}) must be > entry_price ({entry_price})")

        # --- POSITION SIZING ---
        try:
            cm = get_capital_manager()
            balance = resolve_deployable_balance(cm)
            
            # Get risk % from config
            config = ConfigManager()
            risk_cfg = config.agents.get("risk", {}) or {}
            base_risk_pct = risk_cfg.get("position_size_pct", 2.0)

            size_factor = signal_data.get("size_factor")
            if size_factor is None:
                size_factor = 1.0
            try:
                size_factor = float(size_factor)
            except Exception:
                size_factor = 1.0

            # If position_size_pct is precomputed, honor it; otherwise scale by size_factor
            position_size_pct = signal_data.get("position_size_pct")
            if position_size_pct is None:
                position_size_pct = base_risk_pct * size_factor
            else:
                try:
                    position_size_pct = float(position_size_pct)
                except Exception:
                    position_size_pct = base_risk_pct * size_factor

            max_position_size_pct = None
            cap_amount = None
            if "max_position_size_pct" in risk_cfg:
                try:
                    max_position_size_pct = max(0.05, float(risk_cfg.get("max_position_size_pct") or 0.0))
                except Exception:
                    max_position_size_pct = None
            if max_position_size_pct is not None and position_size_pct > max_position_size_pct:
                position_size_pct = max_position_size_pct
                signal_data["position_size_capped"] = True
                signal_data["position_size_cap_pct"] = round(max_position_size_pct, 3)

            invested_amount = balance * (position_size_pct / 100.0)
            if max_position_size_pct is not None:
                cap_amount = balance * (max_position_size_pct / 100.0)
                if invested_amount > cap_amount:
                    invested_amount = cap_amount
                    signal_data["position_size_capped"] = True
                    signal_data["position_size_cap_pct"] = round(max_position_size_pct, 3)
            
            # Validation: Block if balance too low (e.g. < $10)
            if balance < 10.0:
                 logger.warning(f"Insufficient funds for signal {sid} (Balance: ${balance:.2f})")
                 # We could raise Error or just log. 
                 # Raising error prevents signal creation.
                 raise ValueError(f"Insufficient funds: ${balance:.2f}")

            # Optional: Enforce min trade size (e.g. $10)
            min_trade_usd = 10.0
            if cap_amount is not None and cap_amount > 0:
                min_trade_usd = min(min_trade_usd, cap_amount)
            if invested_amount < min_trade_usd:
                invested_amount = min_trade_usd
                if balance < invested_amount:
                     raise ValueError(f"Insufficient funds: ${balance:.2f} < Min Trade ${min_trade_usd:.2f}")

            signal_data["invested_amount"] = round(invested_amount, 2)
            signal_data["base_risk_pct"] = round(base_risk_pct, 3)
            signal_data["risk_pct_used"] = round(position_size_pct, 3)
            signal_data["size_factor"] = round(size_factor, 3)
            
        except ValueError as e:
            raise e
        except Exception as e:
            logger.error(f"Error calculating position size for {sid}: {e}")
            # Fallback to safe default or allow 0?
            # Allow creation with 0 invested if error? No, safer to block.
            # But for robustness, let's log and default to 0 (paper trading won't affect capital).
            signal_data["invested_amount"] = 0.0

        # --- DOLLAR RISK & SIGNAL AGE ---
        # dollar_risk = invested amount * stop distance (loss if SL is hit)
        try:
            invested = float(signal_data.get("invested_amount", 0.0) or 0.0)
            entry = float(signal_data.get("entry", 0.0) or 0.0)
            sl = float(signal_data.get("sl", 0.0) or 0.0)
            if invested > 0 and entry > 0 and sl > 0:
                stop_distance_pct = abs(entry - sl) / entry
                signal_data["dollar_risk"] = round(invested * stop_distance_pct, 2)
            else:
                signal_data["dollar_risk"] = 0.0
        except Exception:
            signal_data["dollar_risk"] = 0.0

        # signal_age: seconds since creation, computed at load time
        created_at_str = signal_data.get("created_at", "")
        if created_at_str:
            try:
                created_dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                if created_dt.tzinfo is None:
                    created_dt = BUCHAREST_TZ.localize(created_dt)
                age_seconds = (get_bucharest_time() - created_dt).total_seconds()
                signal_data["signal_age_seconds"] = round(max(0, age_seconds), 1)
            except Exception:
                signal_data["signal_age_seconds"] = 0.0
        else:
            signal_data["signal_age_seconds"] = 0.0

        # Critical section: mutate the shared open_signals / cooldown dicts and persist.
        # Held under self._lock so a concurrent update_outcomes / close_signal_manual /
        # back_signal_with_strict iteration cannot see a half-applied signal and so two
        # concurrent add_signal saves cannot clobber each other (the class docstring at
        # line 27-29 warns about exactly this). RLock is reentrant -> save() inside is fine.
        # No capital_manager call happens inside this section (capital is touched earlier
        # in position sizing only via reads), so there is no signals->capital lock ordering
        # that could invert capital_manager.close_position's capital->signals callback.
        with self._lock:
            self.open_signals[sid] = signal_data

            # Update per-ticker cooldown time
            ticker = signal_data.get("ticker", "")
            if ticker:
                self.last_signal_time[ticker] = get_bucharest_time()

            self.save()
        return sid

    def attach_paper_position(self, signal_id: str, position: dict) -> bool:
        sid = str(signal_id or "").strip()
        if not sid or sid not in self.open_signals or not isinstance(position, dict):
            return False

        signal = self.open_signals[sid]
        self._attach_open_paper_position_metadata(signal, position)
        self.save()
        return True

    def _sync_closed_signal_capital(
        self,
        *,
        sid: str,
        signal: dict,
        exit_price: float,
        fallback_reason: str,
        ticker: str,
        pnl_pct: float,
    ) -> Optional[dict]:
        try:
            cm = get_capital_manager()

            paper_position_id = str(signal.get("paper_position_id", "") or "").strip()
            if paper_position_id:
                position = cm.get_position(paper_position_id)
                if position and str(position.get("status", "")).upper() != "CLOSED":
                    position = cm.close_position(
                        position_id=paper_position_id,
                        exit_price=exit_price,
                        reason=fallback_reason,
                        sync_signal=False,
                    )
                return position if isinstance(position, dict) else None

            invested_amount = float(signal.get("invested_amount", 0.0) or 0.0)
            if invested_amount <= 0:
                # No capital was ever sized/reserved for this signal — settling
                # an invented stake would credit phantom money to the balance.
                signal["pnl_amount"] = 0.0
                signal["invested"] = 0.0
                signal["capital_settled"] = False
                return None

            # Surgical capital-integrity settlement. The signal has reserved
            # capital (invested_amount>0) but no paper_position_id link, so we
            # locate the Capital reservation by signal_id/source_signal_id and
            # settle status-aware. The previous implementation credited P&L via
            # add_trade_pnl regardless of the matched position's status, which:
            #   - double-credited a CLOSED match (close_position already booked
            #     cash_pnl + released margin at close time -> balance inflated),
            #   - leaked an OPEN match (credited P&L but never closed the position
            #     -> margin stuck reserved forever, fewer new signals possible),
            #   - double-credited a history-only match (position already settled).
            # add_trade_pnl only does balance += pnl; it releases no margin and
            # marks no position CLOSED, so it is incompatible with a real
            # reservation. We now route through close_position (the authoritative
            # settlement) for OPEN, and refuse to re-credit for CLOSED/history.
            match_open = None
            match_closed = None
            for _pos in (cm.get_positions() or []):
                _pos = _pos or {}
                if not (str(_pos.get("signal_id") or "").strip() == sid
                        or str(_pos.get("source_signal_id") or "").strip() == sid):
                    continue
                _st = str(_pos.get("status", "") or "").upper()
                if _st == "OPEN" and match_open is None:
                    match_open = _pos
                elif _st == "CLOSED" and match_closed is None:
                    match_closed = _pos

            # OPEN match -> settle via close_position (mirrors the primary path
            # at 767-776): releases margin + credits cash_pnl once + marks CLOSED.
            # Returns the CLOSED dict so the caller (_finalize_from_paper_position)
            # populates the signal from the authoritative position record.
            if match_open is not None:
                _pid = str(match_open.get("position_id") or "").strip()
                if not _pid:
                    logger.warning(f"Phantom guard: OPEN match for {sid} has no position_id; blocking direct P&L")
                    signal["pnl_amount"] = 0.0
                    signal["invested"] = 0.0
                    signal["capital_settled"] = False
                    return None
                try:
                    position = cm.close_position(
                        position_id=_pid,
                        exit_price=exit_price,
                        reason=fallback_reason,
                        sync_signal=False,  # avoid capital->signals lock inversion (H2)
                    )
                    return position if isinstance(position, dict) else None
                except (KeyError, ValueError) as exc:
                    # Position closed/vanished between the guard scan and close
                    # (concurrent closer). Do not invent P&L; treat as unreserved.
                    logger.warning(f"sync_closed: OPEN match {sid} vanished on close ({exc}); blocking direct P&L")
                    signal["pnl_amount"] = 0.0
                    signal["invested"] = 0.0
                    signal["capital_settled"] = False
                    return None

            # CLOSED match -> already settled (close_position booked cash_pnl +
            # released margin when it closed). Do NOT re-credit (would double
            # the P&L). Return the CLOSED dict so the caller finalizes the
            # signal from the authoritative net_pnl/pnl_pct/usd_size.
            if match_closed is not None:
                return dict(match_closed)

            # History-only: capital was reserved (OPEN_POSITION in history) but
            # the position is no longer active (closed+trimmed past [-300:] at
            # save, or reset). The P&L was already settled at close time; crediting
            # now would double-book (closed) or book against a reset balance.
            # Conservative: mark settled, credit nothing.
            for _h in (getattr(cm, "history", []) or [])[-100:]:
                if sid in str(_h) and "OPEN_POSITION" in str(_h.get("type", "")):
                    signal["pnl_amount"] = 0.0
                    signal["invested"] = round(invested_amount, 2)
                    signal["capital_settled"] = True
                    logger.info(f"sync_closed: signal {sid} settled via prior paper close (history-only); no direct P&L")
                    return None

            # No reservation anywhere -> phantom guard: never invent P&L on
            # capital that was never actually reserved.
            signal["pnl_amount"] = 0.0
            signal["invested"] = 0.0
            signal["capital_settled"] = False
            logger.warning(f"Phantom guard: signal {sid} invested>0 but no Capital reservation found; blocking direct P&L")
            return None
        except Exception as e:
            logger.error(f"Failed to update capital for signal {sid}: {e}")
            return None

    def _finalize_from_paper_position(self, sid: str, signal: dict, position: dict, fallback_reason: str = "") -> Optional[str]:
        paper_status = str((position or {}).get("status", "") or "").upper()
        if paper_status != "CLOSED":
            return None

        exit_price = float((position or {}).get("exit_price", signal.get("exit_price", signal.get("entry", 0.0))) or 0.0)
        entry_price = float(
            (signal or {}).get("executed_entry_price", (position or {}).get("entry_price", signal.get("entry", 0.0))) or 0.0
        )
        net_pnl = float((position or {}).get("net_pnl", 0.0) or 0.0)
        pnl_pct = float((position or {}).get("pnl_pct_notional", 0.0) or 0.0)
        if pnl_pct == 0.0 and entry_price > 0 and exit_price > 0:
            action = signal.get("action", signal.get("type", ""))
            if action == "BUY":
                pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0
            elif action == "SHORT":
                pnl_pct = ((entry_price - exit_price) / entry_price) * 100.0

        signal["status"] = "WIN" if net_pnl > 0 else ("LOST" if net_pnl < 0 else "CLOSED")
        signal["closed_at"] = str((position or {}).get("closed_at") or get_bucharest_time().isoformat())
        signal["close_reason"] = str((position or {}).get("close_reason") or fallback_reason or "Paper position closed")
        signal["exit_price"] = round(exit_price, 8) if exit_price > 0 else signal.get("exit_price", 0.0)
        signal["pnl_pct"] = round(pnl_pct, 4)
        signal["pnl_amount"] = round(net_pnl, 6)
        signal["paper_position_status"] = "CLOSED"
        signal["paper_trade"] = True
        signal["paper_position_id"] = (position or {}).get("position_id", signal.get("paper_position_id"))
        signal["invested"] = round(float((position or {}).get("usd_size", signal.get("invested_amount", 0.0)) or 0.0), 6)
        signal["strategy_type"] = (position or {}).get("strategy_type", signal.get("strategy_type"))
        signal["entry_reason"] = (position or {}).get("entry_reason", signal.get("entry_reason"))
        signal["decision_context"] = dict((position or {}).get("decision_context") or signal.get("decision_context") or {})
        signal["explainability"] = summarize_position_explainability(position)
        entry_verified = self._is_verified_execution_quality((position or {}).get("entry_price_quality"))
        exit_verified = self._is_verified_execution_quality((position or {}).get("exit_price_quality"))
        signal["outcome_data_valid"] = bool(entry_verified and exit_verified)
        signal["outcome_validation"] = (
            "verified_capital_execution_prices"
            if signal["outcome_data_valid"]
            else "unverified_capital_execution_price"
        )

        self.closed_signals.append(signal)
        closed_ticker = signal.get("ticker", "")
        if closed_ticker:
            self.last_signal_time[closed_ticker] = get_bucharest_time()
            if signal.get("status") == "LOST":
                self.__dict__.setdefault("last_stop_time", {})[closed_ticker] = get_bucharest_time()
        logger.info(f"Paper signal closed: {sid} - {signal['status']} (PnL: {signal.get('pnl_pct', 0)}%)")
        return signal["status"] if signal["status"] in {"WIN", "LOST"} else None

    def sync_paper_positions(self) -> Dict[str, str]:
        outcomes: Dict[str, str] = {}
        to_finalize: List[tuple[str, dict, dict]] = []
        changed = False
        cm = get_capital_manager()

        for sid, signal in list(self.open_signals.items()):
            pid = str((signal or {}).get("paper_position_id", "") or "").strip()
            if not pid:
                position = self._find_open_paper_position(sid, signal)
                if isinstance(position, dict):
                    changed = self._attach_open_paper_position_metadata(signal, position) or changed
                continue

            position = cm.get_position(pid)
            if not isinstance(position, dict):
                signal["stale_paper_position_id"] = pid
                signal.pop("paper_position_id", None)
                signal["paper_status"] = "MISSING"
                signal["paper_sync_warning"] = "Referenced paper position is missing; signal is eligible for re-execution"
                changed = True
                continue

            next_status = position.get("status", signal.get("paper_status"))
            if str(position.get("status", "")).upper() == "CLOSED":
                to_finalize.append((sid, self.open_signals.pop(sid), position))
            else:
                changed = self._attach_open_paper_position_metadata(signal, position) or changed
                if signal.get("paper_status") != next_status:
                    signal["paper_status"] = next_status
                    changed = True

        for sid, signal, position in to_finalize:
            result = self._finalize_from_paper_position(sid, signal, position)
            ticker = signal.get("ticker", "")
            if ticker and result:
                outcomes[ticker] = result

        if to_finalize or changed:
            self.save()
        return outcomes

    def sync_closed_paper_position(self, position: dict) -> Optional[str]:
        if not isinstance(position, dict):
            return None
        if str(position.get("status", "") or "").upper() != "CLOSED":
            return None

        pid = str(position.get("position_id", "") or "").strip()
        sid = str(position.get("signal_id") or position.get("source_signal_id") or "").strip()
        if sid not in self.open_signals:
            sid = ""
            for candidate_id, signal in self.open_signals.items():
                if str((signal or {}).get("paper_position_id", "") or "").strip() == pid:
                    sid = candidate_id
                    break
        if not sid or sid not in self.open_signals:
            return None

        signal = self.open_signals.pop(sid)
        result = self._finalize_from_paper_position(
            sid,
            signal,
            position,
            fallback_reason=str(position.get("close_reason") or "Paper position closed"),
        )
        self.save()
        return result

    def update_outcomes(
        self,
        ticker: str,
        current_price: float,
        *,
        outcome_evidence: Optional[dict] = None,
        skip_paper_positions: bool = False,
    ) -> Optional[str]:
        """
        Check if any open signals for a ticker have hit TP or SL.

        Args:
            ticker: The ticker symbol to check
            current_price: Current market price

        Returns:
            'WIN' if take profit hit, 'LOST' if stop loss hit, None if signal still open.

        Note:
            Automatically moves signals from open to closed when TP/SL is reached.
        """
        to_close: List[str] = []
        result: Optional[str] = None
        status_changed = False

        # Snapshot the dict before iterating: a concurrent add_signal / delete_signal /
        # clear_all mutating open_signals during this loop would otherwise raise
        # "RuntimeError: dictionary changed size during iteration" (the exact failure
        # mode the class docstring warns about). We do NOT hold self._lock across this
        # whole method because the close loop below calls capital_manager (which can
        # itself call back into SignalManager under capital's lock); holding signals
        # lock across that would invert the capital->signals lock order and deadlock.
        for sid, s in list(self.open_signals.items()):
            if s.get("status") == "Pending" and self._should_be_open(s):
                s["status"] = "Open"
                status_changed = True

            if s.get("ticker") != ticker or not self._is_active_signal(s):
                continue
            if skip_paper_positions and str(s.get("paper_position_id", "") or "").strip():
                # Capital's exit manager must obtain an execution-grade price
                # itself.  A scanner/UI quote can never settle a paper position.
                continue

            action = s.get("action", s.get("type", ""))
            tp, sl = s.get("tp", 0.0), s.get("sl", 0.0)

            # Skip signals with invalid TP/SL (0 or None)
            if not tp or not sl or tp <= 0 or sl <= 0:
                logger.warning(f"Skipping signal {sid} with invalid TP/SL: TP={tp}, SL={sl}")
                continue

            if action == "BUY":
                if current_price >= tp:
                    s["status"], s["exit_price"] = "WIN", current_price
                    s["close_reason"] = "Take Profit Hit (BUY)"
                    to_close.append(sid)
                elif current_price <= sl:
                    s["status"], s["exit_price"] = "LOST", current_price
                    s["close_reason"] = "Stop Loss Hit (BUY)"
                    to_close.append(sid)
            else:  # SHORT
                if current_price <= tp:
                    s["status"], s["exit_price"] = "WIN", current_price
                    s["close_reason"] = "Take Profit Hit (SHORT)"
                    to_close.append(sid)
                elif current_price >= sl:
                    s["status"], s["exit_price"] = "LOST", current_price
                    s["close_reason"] = "Stop Loss Hit (SHORT)"
                    to_close.append(sid)

            if sid in to_close:
                s["closed_at"] = get_bucharest_time().isoformat()
                evidence = dict(outcome_evidence or {})
                is_verified = bool(evidence.get("is_verified")) and str(evidence.get("status", "")).lower() == "verified"
                s["outcome_data_valid"] = is_verified
                s["outcome_validation"] = (
                    "verified_price_source_concordance"
                    if is_verified
                    else "unverified_outcome_price"
                )
                if evidence:
                    s["outcome_price_integrity"] = evidence
                result = s["status"]

        for sid in to_close:
            # Guard the pop: another concurrent closer (close_signal_manual / a second
            # update_outcomes) may have already removed this sid from open_signals.
            # pop without a default would raise KeyError; pop(sid, None) + skip is safe.
            closed_signal = self.open_signals.pop(sid, None)
            if closed_signal is None:
                continue
            
            # Calculate P&L percentage
            entry_price = closed_signal.get("entry", 0)
            exit_price = closed_signal.get("exit_price", 0)
            action = closed_signal.get("action", closed_signal.get("type", ""))
            
            if entry_price > 0 and exit_price > 0:
                if action == "BUY":
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                elif action == "SHORT":
                    pnl_pct = ((entry_price - exit_price) / entry_price) * 100
                else:
                    pnl_pct = 0.0
                closed_signal["pnl_pct"] = round(pnl_pct, 2)

                position = self._sync_closed_signal_capital(
                    sid=sid,
                    signal=closed_signal,
                    exit_price=exit_price,
                    fallback_reason=closed_signal.get("close_reason", "Signal exit"),
                    ticker=ticker,
                    pnl_pct=pnl_pct,
                )
                if position:
                    finalize_result = self._finalize_from_paper_position(sid, closed_signal, position)
                    if finalize_result:
                        result = finalize_result
                    continue

            else:
                closed_signal["pnl_pct"] = 0.0
            
            self.closed_signals.append(closed_signal)
            # Update per-ticker cooldown when signal closes
            closed_ticker = closed_signal.get("ticker", "")
            if closed_ticker:
                self.last_signal_time[closed_ticker] = get_bucharest_time()
                if closed_signal.get("status") == "LOST":
                    self.__dict__.setdefault("last_stop_time", {})[closed_ticker] = get_bucharest_time()
            logger.info(f"📊 Signal closed: {sid} - {closed_signal['status']} (PnL: {closed_signal.get('pnl_pct', 0)}%)")

        if to_close:
            self.save()

        return result

    def has_open_signal_for_ticker(self, ticker: str) -> Optional[dict]:
        for s in self.open_signals.values():
            if s.get("ticker") == ticker and self._is_active_signal(s):
                return s
        return None

    def can_open_new_signal_for_ticker(
        self, ticker: str, min_minutes: int = 15, stop_lockout_minutes: int = 0
    ) -> bool:
        """
        Check if enough time has passed since the last signal for this ticker.

        Args:
            ticker: The ticker symbol to check
            min_minutes: Minimum minutes required between signals (default 15)

        Returns:
            True if a new signal can be opened, False if still in cooldown.
        """
        now = get_bucharest_time()
        # After a stop-out the same setup usually re-fires inside the same
        # adverse move, so a losing close locks the ticker out for longer.
        last_stop = getattr(self, "last_stop_time", {}).get(ticker)
        if stop_lockout_minutes > 0 and last_stop is not None:
            if (now - last_stop).total_seconds() / 60.0 < stop_lockout_minutes:
                return False

        if ticker not in self.last_signal_time:
            return True

        last_time = self.last_signal_time[ticker]
        elapsed_minutes = (now - last_time).total_seconds() / 60.0

        if elapsed_minutes < min_minutes:
            return False

        return True

    def close_signal_manual(
        self,
        ticker: str,
        reason: str,
        exit_price: float = None,
        exclude_sids: Optional[set] = None,
    ) -> None:
        """Close every active open signal for ``ticker`` (settling P&L via capital).

        ``exclude_sids``: optional set of signal ids to SKIP. Used by the STRICT
        replacement path, which adds the new STRICT signal first and only then
        closes the prior CUSTOM signal(s) for the ticker — passing the new signal's
        id here ensures we don't close the replacement we just opened.
        """
        exclude = exclude_sids or set()
        to_close: List[str] = []
        for sid, s in list(self.open_signals.items()):
            if sid in exclude:
                continue
            if s.get("ticker") == ticker and self._is_active_signal(s):
                s["status"] = "CLOSED"
                s["closed_at"] = get_bucharest_time().isoformat()
                s["close_reason"] = reason
                
                # EX1: settle capital for EVERY manually-closed signal, even when
                # the caller has no exit price. The engine's REPLACED/REVERSAL
                # paths (engine.py:2983/3042) fall back to exit_price=None when
                # their own price fetch failed; pre-fix that skipped the entire
                # _sync_closed_signal_capital block, so the linked paper position
                # was never closed -- margin reserved forever (margin leak: fewer
                # new signals possible, balance slowly trapped in dead positions).
                # Fall back to the signal's entry price (conservative ~0 P&L; no
                # phantom gain/loss invented) so the capital position is still
                # settled and margin released. If entry is also missing, pass 0
                # so close_position fetches a live execution price itself (the
                # position must not stay open because the reason label drifted).
                entry_price = float(s.get("entry", 0) or 0)
                action = s.get("action", s.get("type", ""))
                if exit_price is None:
                    effective_exit = entry_price  # 0 -> close_position fetches live
                else:
                    effective_exit = float(exit_price)
                s["exit_price"] = effective_exit

                # Calculate P&L percentage
                if entry_price > 0 and effective_exit > 0:
                    if action == "BUY":
                        pnl_pct = ((effective_exit - entry_price) / entry_price) * 100
                    elif action == "SHORT":
                        pnl_pct = ((entry_price - effective_exit) / entry_price) * 100
                    else:
                        pnl_pct = 0.0
                    s["pnl_pct"] = round(pnl_pct, 2)
                else:
                    pnl_pct = 0.0
                    s["pnl_pct"] = 0.0

                position = self._sync_closed_signal_capital(
                    sid=sid,
                    signal=s,
                    exit_price=effective_exit,
                    fallback_reason=reason,
                    ticker=ticker,
                    pnl_pct=pnl_pct,
                )
                if position:
                    closed_signal = self.open_signals.pop(sid, s)
                    self._finalize_from_paper_position(sid, closed_signal, position, fallback_reason=reason)
                    continue

                to_close.append(sid)
        for sid in to_close:
            # Guard the pop: sid may already have been removed by the paper-position
            # branch above (which pops at line ~1082) or by a concurrent closer.
            closed = self.open_signals.pop(sid, None)
            if closed is not None:
                self.closed_signals.append(closed)
        if to_close:
            self.save()

    def back_signal_with_strict(self, ticker: str, strict_signal: dict) -> bool:
        """
        Mark an existing CUSTOM signal as backed by a STRICT signal.
        Returns True if an open signal was updated.
        """
        now = get_bucharest_time().isoformat()
        # Critical section: iterate open_signals + mutate in place + save. Without the
        # lock a concurrent add_signal/clear_all could change the dict mid-iteration
        # ("dictionary changed size during iteration") or the save could drop its update.
        with self._lock:
            updated = False
            for sid, s in list(self.open_signals.items()):
                if s.get("ticker") != ticker or not self._is_active_signal(s):
                    continue
                s["strict_backed"] = True
                s["strict_backed_at"] = now
                s["strict_variant"] = strict_signal.get("variant")
                s["strict_risk_mode"] = strict_signal.get("risk_mode")
                s["strict_signal_mode"] = strict_signal.get("signal_mode")
                s["strict_signal_score"] = strict_signal.get("signal_score")
                s["strict_probability_score"] = strict_signal.get("probability_score")
                s["strict_mtf_alignment"] = strict_signal.get("mtf_alignment")
                updated = True
                break
            if updated:
                self.save()
        return updated

    def reload_if_changed(self) -> bool:
        """Reload signals if file has changed on disk."""
        if not self.path.exists():
            return False
        
        current_mtime = self.path.stat().st_mtime
        if current_mtime > self.last_mtime:
            logger.info("Signals file changed on disk, reloading...")
            self.load()
            return True
        return False

    def get_stats(self) -> dict:
        eligible = [s for s in self.closed_signals if self._is_performance_eligible_outcome(s)]
        wins = sum(1 for s in eligible if s.get("status") == "WIN")
        losses = sum(1 for s in eligible if s.get("status") == "LOST")
        total = wins + losses
        observed_outcomes = sum(
            1
            for s in self.closed_signals
            if isinstance(s, dict) and str(s.get("status", "")).upper() in {"WIN", "LOST"}
        )
        win_rate = round(wins / total * 100, 1) if total else 0.0
        return {
            "open": len(self.open_signals),
            "closed": len(self.closed_signals),
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "verified_outcomes": total,
            "unverified_outcomes": max(0, observed_outcomes - total),
            "performance_basis": "verified_price_provenance",
        }

    def get_variant_stats(
        self,
        variant: str,
        days: int = 30,
        variant_mode: str = None,
        *,
        verified_only: bool = False,
    ) -> dict:
        """
        Get performance statistics for a specific signal variant.

        Args:
            variant: The variant to analyze ('A', 'B', or 'C')
            days: Number of days to look back (default 30)

        Returns:
            Dictionary containing:
            - wins: Number of winning signals
            - losses: Number of losing signals
            - total: Total signals
            - win_pct: Win percentage
            - lose_pct: Loss percentage
        """
        cutoff = get_bucharest_time() - timedelta(days=days)
        wins, losses = 0, 0

        mode_filter = (variant_mode or "").upper()
        for s in self.closed_signals:
            try:
                if verified_only and not self._is_performance_eligible_outcome(s):
                    continue
                created = datetime.fromisoformat(
                    s.get("created_at", "").replace("Z", "+00:00")
                )
                if created.tzinfo is None:
                    created = BUCHAREST_TZ.localize(created)
                if created >= cutoff and s.get("variant") == variant:
                    if mode_filter:
                        s_mode = str(s.get("variant_mode") or "STRICT").upper()
                        if s_mode != mode_filter:
                            continue
                    if s.get("status") == "WIN":
                        wins += 1
                    elif s.get("status") == "LOST":
                        losses += 1
            except Exception:
                pass

        total = wins + losses
        return {
            "wins": wins,
            "losses": losses,
            "total": total,
            "win_pct": round(wins / total * 100, 1) if total else 0.0,
            "lose_pct": round(losses / total * 100, 1) if total else 0.0,
        }

    def update_signal_status(self, tickers_data: dict) -> Dict[str, str]:
        """Return dict ticker -> outcome ('WIN' / 'LOST')."""
        outcomes: Dict[str, str] = {}
        for ticker, data in tickers_data.items():
            if not isinstance(data, dict) or data.get("price") is None:
                continue
            integrity = data.get("price_integrity")
            if data.get("price_verified") is not True or not isinstance(integrity, dict) or not integrity.get("is_verified"):
                logger.debug("Skipping signal outcome for %s: price evidence is not verified", ticker)
                continue
            if data.get("price") is not None:
                result = self.update_outcomes(
                    ticker,
                    float(data["price"]),
                    outcome_evidence=integrity,
                    skip_paper_positions=True,
                )
                if result:
                    outcomes[ticker] = result
        return outcomes


# ---------------------------------------------------------------------------
# Process-wide singleton: every in-process caller must share one instance,
# otherwise concurrent whole-payload saves drop each other's updates.
# ---------------------------------------------------------------------------
_signal_manager_singleton: Optional[SignalManager] = None
_signal_manager_guard = threading.RLock()


def get_signal_manager() -> SignalManager:
    """Return the shared SignalManager for the default SIGNALS_PATH."""
    global _signal_manager_singleton
    with _signal_manager_guard:
        if _signal_manager_singleton is None:
            _signal_manager_singleton = SignalManager()
        return _signal_manager_singleton
