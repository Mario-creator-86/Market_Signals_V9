"""Runtime lifecycle control and status for the Agent Fleet UI.

The persisted ``enabled`` flag is the operator's intent.  This module reconciles
that intent with the actual worker lifecycle and exposes thread liveness without
forcing lazy singletons to initialize merely to render the control plane.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional


ENGINE_MANAGED_AGENTS = frozenset({"execution", "risk", "scanner", "sentiment"})

# The engine subprocess dumps its per-agent status here (see
# core.observability._maybe_dump_engine_agent_status); the API reads it to
# derive real per-agent liveness instead of inferring "running" from the
# engine process being up.
# Same file as the writer: the configured data folder (MS_DATA_DIR).
from core.config import DATA_DIR as _DATA_DIR  # noqa: E402

_ENGINE_HEARTBEAT_PATH = Path(_DATA_DIR) / "agent_status_engine.json"
_ENGINE_HEARTBEAT_MAX_AGE_SECONDS = 120.0
_ENGINE_HEARTBEAT_FRESH_SECONDS = 600.0
_ENGINE_HB_CACHE: Dict[str, Any] = {"mtime": None, "data": {}, "read_at": 0.0}


def _iso_age_seconds(value: Any, now: Optional[float] = None) -> Optional[float]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    ref = now if now is not None else time.time()
    return max(0.0, ref - parsed.timestamp())


def _engine_agent_heartbeats() -> Dict[str, Any]:
    """Per-agent status published by the engine process (mtime-cached, fail-open)."""
    try:
        stat = _ENGINE_HEARTBEAT_PATH.stat()
    except OSError:
        return {}
    now = time.time()
    if now - stat.st_mtime > _ENGINE_HEARTBEAT_MAX_AGE_SECONDS:
        return {}  # engine heartbeat silent — do not trust stale records
    if (
        _ENGINE_HB_CACHE["mtime"] == stat.st_mtime
        and now - float(_ENGINE_HB_CACHE["read_at"]) < 1.0
    ):
        return _ENGINE_HB_CACHE["data"]
    try:
        payload = json.loads(_ENGINE_HEARTBEAT_PATH.read_text(encoding="utf-8"))
        data = payload.get("agents", {}) if isinstance(payload, dict) else {}
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
    _ENGINE_HB_CACHE.update({"mtime": stat.st_mtime, "data": data, "read_at": now})
    return data


def _engine_observed_running(name: str) -> Optional[bool]:
    """True/False when a fresh engine heartbeat knows the agent's state, else None."""
    record = _engine_agent_heartbeats().get(name)
    if not isinstance(record, dict):
        return None
    age = _iso_age_seconds(record.get("last_heartbeat"))
    if age is None or age > _ENGINE_HEARTBEAT_FRESH_SECONDS:
        return None
    return bool(record.get("running"))


# Fields of the engine sidecar worth showing on a card. The API process cannot
# measure an engine-owned worker itself, so without these every engine card
# read "Engine managed / Not measured" although the engine publishes them.
_ENGINE_HEARTBEAT_DETAIL_FIELDS = (
    "last_heartbeat",
    "last_run",
    "last_success",
    "last_run_duration_ms",
    "avg_duration_ms",
    "note",
    # Outcome of the last run (core.agent_metrics._run_outcome_fields).
    "last_run_ok",
    "last_run_error",
    "last_run_error_type",
    "last_run_error_at",
    "consecutive_failures",
    "last_run_finished_at",
)
# Run-outcome fields every agent row carries (see ``normalize_run_outcome``).
RUN_OUTCOME_FIELDS = (
    "last_run_ok",
    "last_run_error",
    "last_run_error_type",
    "last_run_error_at",
    "consecutive_failures",
    "last_run_finished_at",
)
# This many failed runs in a row => the agent needs attention.
RUN_FAILURE_ATTENTION_THRESHOLD = 2
# An engine worker whose own heartbeat is this old while the engine sidecar is
# still being written is stuck or dead (the slowest loop, sentiment, beats
# every 600s).
_ENGINE_HEARTBEAT_STALLED_SECONDS = 1800.0
_BENIGN_PAUSES = frozenset({"market_open_paused", "outside_schedule", "market_closed"})
_ACTIVITY_FIELDS = ("last_run", "last_success", "last_heartbeat", "last_scan_at")


def _engine_heartbeat_details(name: str) -> Dict[str, Any]:
    """Real last-run evidence the engine published for one of its agents."""
    record = _engine_agent_heartbeats().get(name)
    if not isinstance(record, dict):
        # No usable record. If the sidecar exists but the engine stopped
        # writing it, report how long the engine has been silent.
        try:
            silent_for = time.time() - _ENGINE_HEARTBEAT_PATH.stat().st_mtime
        except OSError:
            return {}
        if silent_for > _ENGINE_HEARTBEAT_MAX_AGE_SECONDS:
            return {"heartbeat_age_seconds": round(max(0.0, silent_for), 1)}
        return {}
    details = {
        key: record[key]
        for key in _ENGINE_HEARTBEAT_DETAIL_FIELDS
        if record.get(key) not in (None, "")
    }
    if isinstance(record.get("last_run_ok"), bool):
        # The engine published a run outcome: forward all of it, including the
        # empty strings / zero a successful run uses to clear an older error.
        details.update(normalize_run_outcome(record))
    age = _iso_age_seconds(record.get("last_heartbeat"))
    if age is not None:
        details["heartbeat_age_seconds"] = round(age, 1)
    # ``update_agent_status`` never clears a field, so an error recorded by an
    # old crash would otherwise be shown forever on a worker that restarted.
    if record.get("running") is False and str(record.get("last_error") or "").strip():
        details["last_error"] = str(record["last_error"])
    return details


def normalize_run_outcome(record: Any) -> Dict[str, Any]:
    """The six last-run outcome fields in one stable shape.

    ``last_run_ok`` is True/False, or None when the agent has not completed a
    tracked run yet (never invented). Text fields are "" when empty and
    ``consecutive_failures`` is an int >= 0.
    """
    source = record if isinstance(record, Mapping) else {}
    ok = source.get("last_run_ok")
    try:
        failures = max(0, int(source.get("consecutive_failures") or 0))
    except (TypeError, ValueError):
        failures = 0
    return {
        "last_run_ok": ok if isinstance(ok, bool) else None,
        "last_run_error": str(source.get("last_run_error") or "").strip(),
        "last_run_error_type": str(source.get("last_run_error_type") or "").strip(),
        "last_run_error_at": str(source.get("last_run_error_at") or "").strip(),
        "consecutive_failures": failures,
        "last_run_finished_at": str(source.get("last_run_finished_at") or "").strip(),
    }


def run_failure_reason(runtime: Mapping[str, Any], now: Optional[float] = None) -> str:
    """Why the last-run outcome needs attention, or "" when it does not.

    Attention when the agent failed ``RUN_FAILURE_ATTENTION_THRESHOLD`` runs in
    a row, or when its last run failed and that failure is newer than its last
    success (or it has never succeeded).
    """
    outcome = normalize_run_outcome(runtime)
    failures = outcome["consecutive_failures"]
    error = outcome["last_run_error"] or outcome["last_run_error_type"] or "no error text recorded"
    if failures >= RUN_FAILURE_ATTENTION_THRESHOLD:
        return f"{failures} runs failed in a row: {error}"
    if outcome["last_run_ok"] is not False:
        return ""
    ref = now if now is not None else time.time()
    error_age = _iso_age_seconds(outcome["last_run_error_at"] or outcome["last_run_finished_at"], ref)
    success_age = _iso_age_seconds(runtime.get("last_success"), ref)
    if success_age is None or (error_age is not None and error_age < success_age):
        return f"last run failed: {error}"
    return ""


def assess_agent_health(runtime: Dict[str, Any], *, now: Optional[float] = None) -> Dict[str, Any]:
    """Annotate one runtime record with evidence-based health.

    ``running`` only proves a thread is alive. A worker whose last run failed,
    or that has not completed a cycle for several of its own intervals, needs
    attention even though its thread still exists.
    """
    ref = now if now is not None else time.time()
    ages = [
        (age, field)
        for field in _ACTIVITY_FIELDS
        for age in [_iso_age_seconds(runtime.get(field), ref)]
        if age is not None
    ]
    if ages:
        age, field = min(ages)
        runtime["last_activity_at"] = runtime.get(field)
        runtime["seconds_since_activity"] = round(age, 1)

    # Local workers publish their configured cycle length as ``next_run_in``
    # when a run completes; it is an interval, not a live countdown.
    interval: Optional[float] = None
    raw_interval = runtime.get("next_run_in")
    if isinstance(raw_interval, (int, float)) and not isinstance(raw_interval, bool) and raw_interval > 0:
        interval = float(raw_interval)
        runtime["schedule_interval_seconds"] = interval

    enabled = bool(runtime.get("enabled"))
    running = bool(runtime.get("running"))
    error = str(runtime.get("last_error") or "").strip()
    benign = error.lower() in _BENIGN_PAUSES
    lifecycle = str(runtime.get("lifecycle_state") or "").lower()
    overdue = False
    reason = ""

    if not enabled:
        health = "disabled"
    elif lifecycle == "error" or (error and not benign):
        health = "error"
        reason = error or "worker reported an error"
    elif runtime.get("initializing") is True or lifecycle == "starting":
        health = "starting"
    elif not running:
        health = "attention"
        reason = "enabled but its worker is not running"
    else:
        health = "ok"
        age_value = runtime.get("seconds_since_activity")
        if runtime.get("stalled") is True:
            health, overdue = "attention", True
            reason = "worker cycle is stalled"
        elif interval is not None and age_value is not None and not benign:
            limit = max(3.0 * interval, interval + 900.0)
            if age_value > limit:
                health, overdue = "attention", True
                reason = (
                    f"no completed cycle for {int(age_value // 60)}m "
                    f"(scheduled every {int(round(interval / 60.0)) or 1}m)"
                )
        elif (
            runtime.get("managed_by") == "engine"
            and runtime.get("heartbeat_age_seconds") is not None
            and float(runtime["heartbeat_age_seconds"]) > _ENGINE_HEARTBEAT_STALLED_SECONDS
        ):
            health, overdue = "attention", True
            reason = f"no engine heartbeat for {int(float(runtime['heartbeat_age_seconds']) // 60)}m"
        # A live thread whose runs keep failing is not healthy either.
        failed = run_failure_reason(runtime, ref)
        if failed:
            health = "attention"
            reason = f"{reason}; {failed}" if reason else failed
    runtime["overdue"] = overdue
    runtime["health"] = health
    runtime["health_reason"] = reason
    return runtime
LOCAL_LOOP_AGENTS = frozenset(
    {
        "trade_radar",
        "strategy_research",
        "premarket_scanner",
        "pre_earnings",
        "news_tab",
        "economic_calendar",
        "research_factory",
        "coding_agent",
        "critic_agent",
        "ui_inspector",
    }
)


class AgentRuntimeError(RuntimeError):
    """Raised when an enabled worker cannot be brought to a live state."""


def _config_dict(config: Any) -> Dict[str, Any]:
    if isinstance(config, dict):
        return config
    value = getattr(config, "config", None)
    return value if isinstance(value, dict) else {}


def _enabled(config: Any, agent_name: str, default: bool = False) -> bool:
    agents = _config_dict(config).get("agents", {}) or {}
    value = agents.get(agent_name, {}) if isinstance(agents, dict) else {}
    if isinstance(value, dict):
        return bool(value.get("enabled", default))
    if isinstance(value, bool):
        return value
    return bool(default)


def _base_runtime(
    *,
    enabled: bool,
    running: bool,
    managed_by: str,
    initializing: bool = False,
    ready: Optional[bool] = None,
) -> Dict[str, Any]:
    if not enabled:
        lifecycle_state = "disabled"
    elif running:
        lifecycle_state = "running"
    elif initializing:
        lifecycle_state = "starting"
    else:
        lifecycle_state = "stopped"
    return {
        "enabled": bool(enabled),
        "running": bool(running),
        "initializing": bool(initializing),
        "ready": bool(running if ready is None else ready),
        "lifecycle_state": lifecycle_state,
        "managed_by": managed_by,
    }


def _instance_status(instance: Any, *, enabled: bool, managed_by: str = "api") -> Dict[str, Any]:
    if instance is None:
        return _base_runtime(enabled=enabled, running=False, managed_by=managed_by)
    getter = getattr(instance, "get_runtime_status", None)
    if callable(getter):
        details = getter() or {}
        running = bool(details.get("running"))
        payload = _base_runtime(enabled=enabled, running=running, managed_by=managed_by)
        payload.update({key: value for key, value in details.items() if value is not None})
        payload["enabled"] = bool(enabled)
        payload["running"] = running
        payload["lifecycle_state"] = "running" if running and enabled else ("disabled" if not enabled else "stopped")
        if running and not details.get("last_error"):
            payload["last_error"] = ""
        return payload

    running_attr = getattr(instance, "is_running", False)
    running = bool(running_attr() if callable(running_attr) else running_attr)
    return _base_runtime(enabled=enabled, running=running, managed_by=managed_by)


def get_agent_runtime_status(
    agent_name: str,
    *,
    config: Any,
    engine_running: bool,
) -> Dict[str, Any]:
    """Return live lifecycle state without creating a new worker."""
    name = str(agent_name or "").strip().lower()
    enabled = _enabled(config, name)

    if name in ENGINE_MANAGED_AGENTS:
        observed = _engine_observed_running(name)
        if observed is not None:
            running = bool(observed and enabled and engine_running)
            payload = _base_runtime(
                enabled=enabled,
                running=running,
                ready=running,
                managed_by="engine",
            )
            payload["liveness_source"] = "engine_heartbeat"
            payload.update(_engine_heartbeat_details(name))
            return payload
        payload = _base_runtime(
            enabled=enabled,
            running=bool(enabled and engine_running),
            ready=bool(enabled and engine_running),
            managed_by="engine",
        )
        # Heartbeat too old to prove liveness: keep its age so the stall is
        # visible instead of being hidden behind "engine process is up".
        payload.update(_engine_heartbeat_details(name))
        return payload

    if name == "trade_radar":
        from agents.trade_radar_agent import get_trade_radar_agent_if_ready

        return _instance_status(get_trade_radar_agent_if_ready(), enabled=enabled)
    if name == "strategy_research":
        from agents.strategy_research_agent import get_strategy_research_agent_if_ready

        return _instance_status(get_strategy_research_agent_if_ready(), enabled=enabled)
    if name == "premarket_scanner":
        from agents.premarket_scanner_agent import get_premarket_scanner_agent_if_ready

        return _instance_status(get_premarket_scanner_agent_if_ready(), enabled=enabled)
    if name == "pre_earnings":
        from agents.pre_earnings_agent import get_pre_earnings_agent_if_ready

        return _instance_status(get_pre_earnings_agent_if_ready(), enabled=enabled)
    if name == "news_tab":
        from agents.news_tab_agent import get_news_tab_agent_status

        status = get_news_tab_agent_status(_config_dict(config)) or {}
        running = bool(status.get("running"))
        initializing = bool(status.get("initializing"))
        payload = _base_runtime(
            enabled=enabled,
            running=running,
            initializing=initializing,
            ready=bool(status.get("ready")),
            managed_by="api",
        )
        if status.get("error"):
            payload["last_error"] = status["error"]
            payload["lifecycle_state"] = "error"
        else:
            payload["last_error"] = ""
        if status.get("last_success"):
            payload["last_success"] = status["last_success"]
        return payload
    if name == "economic_calendar":
        from agents.economic_calendar_agent import get_economic_calendar_agent_if_ready

        return _instance_status(get_economic_calendar_agent_if_ready(), enabled=enabled)

    if name == "research_factory":
        from api.research_factory_service import auto_cycle_status

        st = auto_cycle_status() or {}
        running = bool(st.get("running"))
        payload = _base_runtime(
            enabled=enabled,
            running=running,
            ready=bool(st.get("enabled", enabled)),
            managed_by="api",
        )
        if st.get("last_error"):
            payload["last_error"] = st["last_error"]
            payload["lifecycle_state"] = "error"
        elif running:
            payload["lifecycle_state"] = "running"
        if st.get("last_run"):
            payload["last_success"] = st["last_run"]
        return payload

    if name in {"coding_agent", "critic_agent"}:
        from core.development_factory.watchdog import get_repair_watchdog

        try:
            wd = get_repair_watchdog(_config_dict(config))
            st = wd.status() if wd else {}
            running = bool(st.get("running"))
        except Exception:
            st = {}
            running = False
        payload = _base_runtime(
            enabled=enabled,
            running=running,
            ready=bool(running and enabled),
            managed_by="api",
        )
        payload["runtime_component"] = "repair_watchdog"
        payload["shared_runtime"] = True
        payload["runtime_role"] = "implementation" if name == "coding_agent" else "independent_review"
        for key in (
            "thread_alive", "stalled", "cycle_started_at", "cycle_age_seconds",
            "last_heartbeat", "stale_after_seconds", "cycles_completed",
        ):
            if key in st:
                payload[key] = st[key]
        if st.get("last_error"):
            payload["last_error"] = st["last_error"]
            payload["lifecycle_state"] = "error"
        elif running:
            payload["lifecycle_state"] = "running"
        if st.get("last_run"):
            payload["last_success"] = st["last_run"]
        return payload

    if name == "ui_inspector":
        from agents.ui_inspector_agent import get_ui_inspector_agent_if_ready

        return _instance_status(get_ui_inspector_agent_if_ready(), enabled=enabled)

    return _base_runtime(enabled=enabled, running=False, managed_by="unknown")


def snapshot_agent_runtimes(
    *,
    config: Any,
    observed_status: Mapping[str, Any],
    engine_running: bool,
    agent_names: Any,
) -> Dict[str, Dict[str, Any]]:
    """Merge observability metrics with authoritative lifecycle liveness."""
    result: Dict[str, Dict[str, Any]] = {}
    for name in agent_names:
        metrics = dict((observed_status or {}).get(name) or {})
        try:
            runtime = get_agent_runtime_status(
                name,
                config=config,
                engine_running=engine_running,
            )
        except Exception as exc:
            runtime = _base_runtime(
                enabled=_enabled(config, name),
                running=False,
                managed_by="api",
            )
            runtime["lifecycle_state"] = "error"
            runtime["last_error"] = str(exc)
        if name in ENGINE_MANAGED_AGENTS:
            # The API process cannot inspect worker threads owned by the engine
            # process. Prefer a real observed heartbeat when available and label
            # the process-level fallback honestly instead of presenting it as a
            # per-agent liveness probe.
            if not isinstance(metrics.get("running"), bool):
                observed = _engine_observed_running(name)
                if observed is not None:
                    metrics["running"] = observed
                    metrics["liveness_source"] = "engine_heartbeat"
            if isinstance(metrics.get("running"), bool):
                observed_running = bool(metrics["running"] and runtime.get("enabled") and engine_running)
                runtime["running"] = observed_running
                runtime["ready"] = observed_running
                runtime["lifecycle_state"] = (
                    "running" if observed_running else
                    ("disabled" if not runtime.get("enabled") else "stopped")
                )
                runtime["liveness_source"] = (
                    "engine_heartbeat"
                    if metrics.get("liveness_source") == "engine_heartbeat"
                    else "observed_agent_status"
                )
            else:
                runtime["liveness_source"] = "engine_process"
        if name in ENGINE_MANAGED_AGENTS:
            # API-process leftovers must not masquerade as the engine worker's
            # own run evidence when the engine published real values.
            for key in _ENGINE_HEARTBEAT_DETAIL_FIELDS:
                if key in runtime:
                    metrics.pop(key, None)
        metrics.update(runtime)
        result[name] = assess_agent_health(metrics)
    return result


def set_agent_runtime(
    agent_name: str,
    enabled: bool,
    *,
    config: Any,
    engine_running: Callable[[], bool],
    start_engine: Callable[[], Any],
    test_mode: bool = False,
) -> Dict[str, Any]:
    """Idempotently reconcile one persisted toggle with its actual runtime."""
    name = str(agent_name or "").strip().lower()
    desired = bool(enabled)

    if test_mode:
        return {
            **_base_runtime(
                enabled=desired,
                running=False,
                ready=desired,
                managed_by="test",
            ),
            "lifecycle_state": "test_suppressed" if desired else "disabled",
        }

    if name in ENGINE_MANAGED_AGENTS:
        if desired and not engine_running():
            started = start_engine()
            if started is False or not engine_running():
                raise AgentRuntimeError(f"Engine could not start {name}")
        return _base_runtime(
            enabled=desired,
            running=bool(desired and engine_running()),
            ready=bool(desired and engine_running()),
            managed_by="engine",
        )

    if name == "trade_radar":
        from agents.trade_radar_agent import get_trade_radar_agent, get_trade_radar_agent_if_ready

        instance = get_trade_radar_agent() if desired else get_trade_radar_agent_if_ready()
        if instance is not None:
            instance.start() if desired else instance.stop()
    elif name == "strategy_research":
        from agents.strategy_research_agent import (
            get_strategy_research_agent,
            get_strategy_research_agent_if_ready,
        )

        instance = get_strategy_research_agent() if desired else get_strategy_research_agent_if_ready()
        if instance is not None:
            instance.start() if desired else instance.stop()
    elif name == "premarket_scanner":
        from agents.premarket_scanner_agent import (
            get_premarket_scanner_agent,
            get_premarket_scanner_agent_if_ready,
        )

        instance = get_premarket_scanner_agent() if desired else get_premarket_scanner_agent_if_ready()
        if instance is not None:
            instance.start() if desired else instance.stop()
    elif name == "pre_earnings":
        from agents.pre_earnings_agent import get_pre_earnings_agent, get_pre_earnings_agent_if_ready

        instance = get_pre_earnings_agent() if desired else get_pre_earnings_agent_if_ready()
        if instance is not None:
            instance.start() if desired else instance.stop()
    elif name == "news_tab":
        from agents.news_tab_agent import kickoff_news_tab_agent_init, shutdown_news_tab_agent

        if desired:
            kickoff_news_tab_agent_init(_config_dict(config))
        else:
            shutdown_news_tab_agent()
    elif name == "economic_calendar":
        from agents.economic_calendar_agent import (
            get_economic_calendar_agent,
            get_economic_calendar_agent_if_ready,
        )

        instance = get_economic_calendar_agent(_config_dict(config)) if desired else get_economic_calendar_agent_if_ready()
        if instance is not None:
            instance.start() if desired else instance.stop()
    elif name == "research_factory":
        from api.research_factory_service import auto_cycle_start, auto_cycle_stop

        if desired:
            auto_cycle_start()
        else:
            auto_cycle_stop()
    elif name in {"coding_agent", "critic_agent"}:
        from core.development_factory.watchdog import shutdown_repair_watchdog, watchdog_startup

        if desired:
            result = watchdog_startup(_config_dict(config))
            if not result.get("started"):
                other = "critic_agent" if name == "coding_agent" else "coding_agent"
                if not _enabled(config, other, default=True):
                    return _base_runtime(
                        enabled=desired,
                        running=False,
                        ready=False,
                        managed_by="api",
                    ) | {
                        "lifecycle_state": "waiting_dependency",
                        "dependency": other,
                        "runtime_component": "repair_watchdog",
                    }
        else:
            shutdown_repair_watchdog()
    elif name == "ui_inspector":
        from agents.ui_inspector_agent import (
            get_ui_inspector_agent,
            get_ui_inspector_agent_if_ready,
        )

        instance = get_ui_inspector_agent(_config_dict(config)) if desired else get_ui_inspector_agent_if_ready()
        if instance is not None:
            instance.start() if desired else instance.stop()
    else:
        raise AgentRuntimeError(f"No runtime adapter for {name}")

    status = get_agent_runtime_status(
        name,
        config=config,
        engine_running=engine_running(),
    )
    if desired and not status.get("running") and not status.get("initializing"):
        raise AgentRuntimeError(f"{name} did not enter a running state")
    if not desired and status.get("running"):
        raise AgentRuntimeError(f"{name} did not stop")
    return status
