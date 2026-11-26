from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agents.backtest_agent import get_backtest_agent
from core.config import ConfigManager
from core.agent_toggles import is_agent_enabled
from core.strategy_v2.context_builder import infer_asset_class
from core.strategy_v2.performance_cache import (
    STRATEGY_RESEARCH_CACHE_PATH,
    StrategyPerformanceCache,
)
from core.agent_metrics import track_agent_run
from core.observability import update_agent_status
from core.request_context import get_request_context, request_context

logger = logging.getLogger("StrategyResearchAgent")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _numeric_or_none(value: Any) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return None


def _legacy_proxy_oos_metrics(walk_forward: Dict[str, Any], costs: Dict[str, Any]) -> Dict[str, Any]:
    """Aggregate OOS windows for research display without promoting V2.

    ``BacktestAgent.run_v2_walk_forward`` currently uses the legacy
    QuickBacktester trade-mode approximation, not the live StrategySelector.
    Its test windows are real historical observations, but they cannot prove a
    V2 candidate's live behaviour.  Keep them clearly labelled research-only
    and let the selector fail closed until a faithful V2 validator supplies
    ``faithful_v2_walk_forward`` provenance.
    """
    windows = walk_forward.get("windows") if isinstance(walk_forward, dict) else []
    windows = windows if isinstance(windows, list) else []
    tests = [item.get("test") for item in windows if isinstance(item, dict) and isinstance(item.get("test"), dict)]
    valid_tests = [
        item
        for item in tests
        if _numeric_or_none(item.get("expectancy_R")) is not None
        and _numeric_or_none(item.get("max_drawdown")) is not None
    ]
    if tests and not valid_tests:
        raise ValueError(f"walk-forward returned no numeric OOS windows: {str(tests[0].get('expectancy_R', ''))[:160]}")
    tests = valid_tests
    sample_size = sum(_safe_int(item.get("sample_size"), 0) for item in tests)
    wins = sum(_safe_int(item.get("wins"), 0) for item in tests)
    losses = sum(_safe_int(item.get("losses"), 0) for item in tests)
    weighted_expectancy = (
        sum(float(item.get("expectancy_R", 0.0) or 0.0) * _safe_int(item.get("sample_size"), 0) for item in tests)
        / sample_size
        if sample_size
        else 0.0
    )
    # Individual window PFs cannot be averaged into a portfolio PF.  Leaving
    # it at zero is intentionally conservative and prevents a proxy result
    # from satisfying the selector's profitability threshold.
    return {
        "win_rate": (wins / (wins + losses)) if (wins + losses) else 0.0,
        "profit_factor": 0.0,
        "expectancy_R": weighted_expectancy,
        "avg_R": weighted_expectancy,
        "max_drawdown": max((float(item.get("max_drawdown", 0.0) or 0.0) for item in tests), default=0.0),
        "sample_size": sample_size,
        "last_run": _iso_now(),
        "costs_used": costs,
        "walk_forward": {
            "avg_train_expectancy_R": walk_forward.get("avg_train_expectancy_R", 0.0),
            "avg_test_expectancy_R": walk_forward.get("avg_test_expectancy_R", 0.0),
            "overfit_penalty": walk_forward.get("overfit_penalty", 0.0),
            "windows": len(windows),
        },
        "validation_status": "research_only",
        "validation_method": "legacy_proxy_walk_forward",
        "validation_reason": "Legacy proxy does not reproduce the live StrategySelector",
        "validation_as_of": _iso_now(),
    }


class StrategyResearchAgent:
    def __init__(self, config_manager: Optional[ConfigManager] = None):
        self.config_manager = config_manager or ConfigManager()
        self.backtest_agent = get_backtest_agent(self.config_manager)
        # Legacy proxy walk-forwards are useful for research prioritization but
        # are not a faithful V2/AI replay.  Keep them physically separate from
        # the live performance cache; otherwise a periodic research job can
        # overwrite realized Capital feedback or appear to validate a strategy.
        self.research_cache = StrategyPerformanceCache(STRATEGY_RESEARCH_CACHE_PATH)
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._pending_force = False
        self._pending_symbols: List[str] = []

    @property
    def is_running(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive())

    def get_runtime_status(self) -> Dict[str, Any]:
        cfg = self.config_manager.strategy_v2.get("research_agent", {}) or {}
        return {
            "running": self.is_running,
            "next_run_in": max(60, _safe_int(cfg.get("interval_sec"), 900)) if self.is_running else None,
        }

    def start(self) -> None:
        with self._lock:
            if self.is_running:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="StrategyResearchAgent")
            self._thread.start()
        logger.info("StrategyResearchAgent started")

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)
        logger.info("StrategyResearchAgent stopped")

    def request_refresh(self, force: bool = False, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        syms = [str(s or "").upper().strip() for s in (symbols or []) if str(s or "").strip()]
        with self._lock:
            if force:
                self._pending_force = True
            if syms:
                self._pending_symbols.extend(syms)
        self._wake_event.set()
        return {"status": "accepted", "force": bool(force), "symbols": syms}

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            force = False
            symbols: List[str] = []
            with self._lock:
                force = self._pending_force
                symbols = list(dict.fromkeys(self._pending_symbols))
                self._pending_force = False
                self._pending_symbols = []

            try:
                with request_context(reason="AGENT_REFRESH", path="/agent/strategy-research", cache_only=False):
                    with track_agent_run("strategy_research"):
                        self.run_cycle(force=force, symbols=symbols)
            except Exception as e:
                logger.error(f"StrategyResearchAgent cycle failed: {e}")
                update_agent_status(
                    "strategy_research",
                    enabled=is_agent_enabled("strategy_research", config=self.config_manager.config, default=True),
                    last_error=str(e),
                    last_run=_iso_now(),
                )

            cfg = self.config_manager.strategy_v2.get("research_agent", {}) or {}
            interval = max(60, _safe_int(cfg.get("interval_sec"), 900))
            self._wake_event.wait(timeout=interval)
            self._wake_event.clear()

    def _watchlist_symbols(self) -> List[str]:
        wl = self.config_manager.watchlist or {}
        symbols = []
        symbols.extend(wl.get("order", []) or [])
        symbols.extend(wl.get("yahoo", []) or [])
        symbols.extend(wl.get("binance", []) or [])
        return list(dict.fromkeys([str(s or "").upper().strip() for s in symbols if str(s or "").strip()]))

    @staticmethod
    def _has_local_ohlc_cache(symbol: str) -> bool:
        s = str(symbol or "").upper().strip()
        from core.config import data_path  # not relative to the working directory
        cache_dir = data_path("cache_backtest")
        if not cache_dir.exists():
            return False
        for suffix in ["_5min.csv", "_1h.csv", "_15min.csv", "_1min.csv"]:
            if (cache_dir / f"{s}{suffix}").exists():
                return True
        return False

    @staticmethod
    def _external_budget_remaining() -> Optional[int]:
        """Return the current agent-run budget without consuming a slot.

        A research cycle expands every symbol into several strategies and both
        directions.  Once the shared provider budget is empty, continuing that
        Cartesian product only records dozens of predictable blocked calls and
        degrades system health.  Stop planning remote work at the boundary and
        let the next scheduled cycle resume from persisted/fresh cache state.
        """
        ctx = get_request_context()
        budget = getattr(ctx, "external_budget", None) if ctx is not None else None
        if budget is None:
            return None
        try:
            remaining = budget.snapshot().get("remaining")
            return None if remaining is None else max(0, int(remaining))
        except Exception:
            return None

    def run_cycle(self, force: bool = False, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        started_perf = time.perf_counter()
        self.config_manager.reload_if_changed()
        self.config_manager.reload_watchlist_if_changed()

        if not is_agent_enabled("strategy_research", config=self.config_manager.config, default=True):
            duration_ms = (time.perf_counter() - started_perf) * 1000.0
            update_agent_status(
                "strategy_research",
                enabled=False,
                last_run=_iso_now(),
                next_run_in=None,
                avg_duration_ms=round(duration_ms, 2),
            )
            return {"status": "disabled", "processed": 0}

        v2_cfg = self.config_manager.strategy_v2 or {}
        research_cfg = v2_cfg.get("research_agent", {}) or {}
        if not bool(research_cfg.get("enabled", True)):
            duration_ms = (time.perf_counter() - started_perf) * 1000.0
            update_agent_status(
                "strategy_research",
                enabled=False,
                last_run=_iso_now(),
                next_run_in=None,
                avg_duration_ms=round(duration_ms, 2),
            )
            return {"status": "disabled", "processed": 0}

        local_only = bool(research_cfg.get("local_cache_only", False))
        max_symbols = max(1, _safe_int(research_cfg.get("max_symbols_per_cycle"), 8))
        selection_days = max(30, _safe_int(research_cfg.get("selection_days"), 90))
        train_days = max(10, _safe_int(research_cfg.get("walk_forward_train_days"), 20))
        test_days = max(5, _safe_int(research_cfg.get("walk_forward_test_days"), 10))
        max_windows = max(1, _safe_int(research_cfg.get("max_windows"), 4))

        source_symbols = symbols if symbols else self._watchlist_symbols()
        source_symbols = list(dict.fromkeys([str(s or "").upper().strip() for s in source_symbols if str(s or "").strip()]))[:max_symbols]

        enabled_map = v2_cfg.get("enabled_strategies_by_asset_class", {}) or {}
        costs_map = v2_cfg.get("costs_by_asset", {}) or {}
        ttl_map = v2_cfg.get("performance_ttl_seconds", {}) or {}

        processed = 0
        updated_entries = 0
        skipped_fresh = 0
        skipped_local_only = 0
        skipped_external_budget = 0
        external_budget_deferred = False
        skipped_invalid = 0
        invalid_reasons: List[str] = []

        for symbol in source_symbols:
            if external_budget_deferred:
                break
            asset_class = infer_asset_class(symbol)
            enabled = enabled_map.get(asset_class, []) or []
            if not isinstance(enabled, list):
                enabled = []
            if local_only and not self._has_local_ohlc_cache(symbol):
                skipped_local_only += 1
                continue

            ttl_sec = _safe_int(ttl_map.get(asset_class), _safe_int(ttl_map.get("DEFAULT"), 43200))
            costs = costs_map.get(asset_class, {}) if isinstance(costs_map.get(asset_class), dict) else {}

            for strategy_id in enabled:
                if external_budget_deferred:
                    break
                sid = str(strategy_id or "").upper().strip()
                if not sid:
                    continue
                for direction in ["LONG", "SHORT"]:
                    processed += 1
                    existing = self.research_cache.get_entry(symbol, asset_class, sid, direction)
                    if not force and StrategyPerformanceCache.is_fresh(existing, ttl_sec):
                        skipped_fresh += 1
                        continue

                    remaining = self._external_budget_remaining()
                    if remaining is not None and remaining <= 0:
                        skipped_external_budget += 1
                        external_budget_deferred = True
                        logger.info(
                            "Strategy research deferred remaining work at the external-call budget boundary"
                        )
                        break

                    wf = self.backtest_agent.run_v2_walk_forward(
                        ticker=symbol,
                        strategy_id=sid,
                        direction=direction,
                        asset_class=asset_class,
                        days=selection_days,
                        train_days=train_days,
                        test_days=test_days,
                        max_windows=max_windows,
                        costs=costs,
                        variant_mode="STRICT",
                    )

                    try:
                        metrics = _legacy_proxy_oos_metrics(wf, costs)
                    except (ValueError, TypeError) as exc:
                        skipped_invalid += 1
                        invalid_reasons.append(f"{symbol}/{sid}/{direction}: {str(exc)[:200]}")
                        logger.warning("StrategyResearchAgent skipping invalid walk-forward: %s", exc)
                        continue
                    self.research_cache.upsert_entry(symbol, asset_class, sid, direction, metrics)
                    updated_entries += 1

        payload = {
            "status": "ok",
            "processed": processed,
            "updated_entries": updated_entries,
            "skipped_fresh": skipped_fresh,
            "skipped_local_only": skipped_local_only,
            "skipped_external_budget": skipped_external_budget,
            "external_budget_deferred": external_budget_deferred,
            "skipped_invalid": skipped_invalid,
            "invalid_reasons": invalid_reasons[:5],
            "symbols": source_symbols,
            "as_of": _iso_now(),
        }
        duration_ms = (time.perf_counter() - started_perf) * 1000.0
        update_agent_status(
            "strategy_research",
            enabled=True,
            last_run=payload["as_of"],
            last_success=payload["as_of"],
            last_error=(invalid_reasons[0] if (skipped_invalid and not updated_entries) else None),
            avg_duration_ms=round(duration_ms, 2),
            next_run_in=max(60, _safe_int(research_cfg.get("interval_sec"), 900)),
        )
        return payload


_research_agent_instance: Optional[StrategyResearchAgent] = None
_research_agent_lock = threading.Lock()


def get_strategy_research_agent(config_manager: Optional[ConfigManager] = None) -> StrategyResearchAgent:
    global _research_agent_instance
    with _research_agent_lock:
        if _research_agent_instance is None:
            _research_agent_instance = StrategyResearchAgent(config_manager=config_manager)
        return _research_agent_instance


def get_strategy_research_agent_if_ready() -> Optional[StrategyResearchAgent]:
    return _research_agent_instance
