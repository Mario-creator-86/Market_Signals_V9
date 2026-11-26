"""
observability.py — in-process metrics + structured logging helpers.

V2 additions:
- p50 / p95 / p99 latency per endpoint (sliding 1-minute window)
- error counts per external source (Ollama, FMP, Yahoo, Binance …)
- cache hit / miss rate per endpoint group
- request_id propagation helpers
"""

import json
import logging
import os
import threading
import time
import tracemalloc
import uuid
from bisect import insort
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


logger = logging.getLogger("Observability")

_START_TS = time.time()
_LOCK = threading.RLock()

# ── existing bounded history ──────────────────────────────────────────────────
_EXTERNAL_TRACES: deque = deque(maxlen=500)
_CACHE_DECISIONS: deque = deque(maxlen=1000)

_INTERNAL_TOTAL = 0
_EXTERNAL_TOTAL = 0

_INTERNAL_TS: deque = deque(maxlen=5000)
_EXTERNAL_TS: deque = deque(maxlen=5000)

_INTERNAL_BY_ENDPOINT_TOTAL: Dict[str, int] = defaultdict(int)
_INTERNAL_BY_ENDPOINT_MIN: Dict[str, int] = defaultdict(int)
_INTERNAL_BY_STATUS_TOTAL: Dict[str, int] = defaultdict(int)
_INTERNAL_BY_STATUS_MIN: Dict[str, int] = defaultdict(int)
_EXTERNAL_BY_ENDPOINT_TOTAL: Dict[str, int] = defaultdict(int)
_EXTERNAL_BY_ENDPOINT_MIN: Dict[str, int] = defaultdict(int)
_EXTERNAL_BY_SERVICE_TOTAL: Dict[str, int] = defaultdict(int)
_EXTERNAL_BY_SERVICE_MIN: Dict[str, int] = defaultdict(int)

_AGENT_STATUS: Dict[str, Any] = {}

# ── NEW: latency tracking ─────────────────────────────────────────────────────
# Each entry: (timestamp, path, duration_ms)
_LATENCY_WINDOW: deque = deque(maxlen=10000)  # 10 k entries max
_LATENCY_WINDOW_SEC = 60.0  # sliding 1-minute window

# ── NEW: error counts per source ──────────────────────────────────────────────
# key = service name  (e.g. "ollama", "yahoo", "binance")
_ERROR_COUNTS_BY_SOURCE: Dict[str, int] = defaultdict(int)
_ERROR_COUNTS_LAST_MIN: Dict[str, int] = defaultdict(int)
# timestamps of errors for /min window
_ERROR_TS: deque = deque(maxlen=5000)   # (ts, source)

# ── NEW: cache hit/miss per endpoint group ────────────────────────────────────
# key = endpoint prefix  value = {"hits": int, "misses": int}
_CACHE_STATS: Dict[str, Dict[str, int]] = defaultdict(lambda: {"hits": 0, "misses": 0})

# tracemalloc is a DEBUGGING tracer: once started it records a traceback for
# every allocation in the whole process. Starting it unconditionally at import
# made every JSON-heavy read model in the API and engine processes several
# times slower (measured 2026-11-15: json.loads of the 4 MB Pre-Earnings
# snapshot 17 ms -> 59 ms, json.dumps of the 1 MB cockpit payload 8 ms ->
# 41 ms) while holding the GIL against the agent threads. It is therefore
# opt-in; the memory gauges fall back to the OS resident-set size.
if str(os.getenv("MS_TRACEMALLOC", "0")).strip().lower() in {"1", "true", "yes", "on"}:
    if not tracemalloc.is_tracing():
        tracemalloc.start()


# ── helpers ───────────────────────────────────────────────────────────────────

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _endpoint_group(path: str) -> str:
    """Collapse /api/news-tab/article/abc123 → /api/news-tab/article."""
    parts = str(path or "").split("/")
    # Keep first 4 segments max
    return "/".join(parts[:4])


def _percentile(sorted_vals: List[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = int(len(sorted_vals) * pct / 100.0)
    idx = max(0, min(len(sorted_vals) - 1, idx))
    return round(sorted_vals[idx], 2)


def _prune_old(now_ts: Optional[float] = None) -> None:
    now_ts = now_ts or time.time()
    cutoff = now_ts - 60.0

    while _INTERNAL_TS and _INTERNAL_TS[0][0] < cutoff:
        _, path, status_family = _INTERNAL_TS.popleft()
        if path in _INTERNAL_BY_ENDPOINT_MIN:
            _INTERNAL_BY_ENDPOINT_MIN[path] = max(0, _INTERNAL_BY_ENDPOINT_MIN[path] - 1)
            if _INTERNAL_BY_ENDPOINT_MIN[path] == 0:
                _INTERNAL_BY_ENDPOINT_MIN.pop(path, None)
        if status_family in _INTERNAL_BY_STATUS_MIN:
            _INTERNAL_BY_STATUS_MIN[status_family] = max(
                0,
                _INTERNAL_BY_STATUS_MIN[status_family] - 1,
            )
            if _INTERNAL_BY_STATUS_MIN[status_family] == 0:
                _INTERNAL_BY_STATUS_MIN.pop(status_family, None)

    while _EXTERNAL_TS and _EXTERNAL_TS[0][0] < cutoff:
        _, service, endpoint = _EXTERNAL_TS.popleft()
        skey = f"{service}::{endpoint}"
        if endpoint in _EXTERNAL_BY_ENDPOINT_MIN:
            _EXTERNAL_BY_ENDPOINT_MIN[endpoint] = max(0, _EXTERNAL_BY_ENDPOINT_MIN[endpoint] - 1)
            if _EXTERNAL_BY_ENDPOINT_MIN[endpoint] == 0:
                _EXTERNAL_BY_ENDPOINT_MIN.pop(endpoint, None)
        if service in _EXTERNAL_BY_SERVICE_MIN:
            _EXTERNAL_BY_SERVICE_MIN[service] = max(0, _EXTERNAL_BY_SERVICE_MIN[service] - 1)
            if _EXTERNAL_BY_SERVICE_MIN[service] == 0:
                _EXTERNAL_BY_SERVICE_MIN.pop(service, None)
        _ = skey  # suppress unused-var linter warn

    # prune error window
    while _ERROR_TS and _ERROR_TS[0][0] < cutoff:
        _, src = _ERROR_TS.popleft()
        _ERROR_COUNTS_LAST_MIN[src] = max(0, _ERROR_COUNTS_LAST_MIN[src] - 1)
        if _ERROR_COUNTS_LAST_MIN[src] == 0:
            _ERROR_COUNTS_LAST_MIN.pop(src, None)


# ── public API ────────────────────────────────────────────────────────────────

def record_internal_request(path: str, status: int, duration_ms: float) -> None:
    """Called by timing_middleware for every internal API request."""
    global _INTERNAL_TOTAL
    now_ts = time.time()
    clean_path = _endpoint_group(str(path or ""))
    clean_status = max(100, min(599, int(status or 200)))
    status_family = f"{clean_status // 100}xx"
    with _LOCK:
        _INTERNAL_TOTAL += 1
        _INTERNAL_BY_ENDPOINT_TOTAL[clean_path] += 1
        _INTERNAL_BY_STATUS_TOTAL[status_family] += 1
        _INTERNAL_TS.append((now_ts, clean_path, status_family))
        _INTERNAL_BY_ENDPOINT_MIN[clean_path] += 1
        _INTERNAL_BY_STATUS_MIN[status_family] += 1
        # NEW: latency window
        _LATENCY_WINDOW.append(
            (now_ts, clean_path, float(duration_ms or 0.0), clean_status)
        )
        _prune_old(now_ts)


def _extract_endpoint(url_or_endpoint: str) -> str:
    if not url_or_endpoint:
        return ""
    parsed = urlparse(str(url_or_endpoint))
    return parsed.path or str(url_or_endpoint)


def record_external_call(
    *,
    service: str,
    endpoint: str,
    duration_ms: float,
    status: Optional[int],
    cache_reason: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    global _EXTERNAL_TOTAL
    now_ts = time.time()
    service_name = str(service or "unknown")
    ep = _extract_endpoint(endpoint)
    trace = {
        "ts": _utc_now_iso(),
        "service": service_name,
        "endpoint": ep,
        "duration_ms": round(float(duration_ms or 0.0), 2),
        "status": status if status is not None else "NA",
        "cache_reason": cache_reason or "NA",
        "error": error,
    }
    with _LOCK:
        _EXTERNAL_TOTAL += 1
        _EXTERNAL_BY_ENDPOINT_TOTAL[ep] += 1
        _EXTERNAL_BY_SERVICE_TOTAL[service_name] += 1
        _EXTERNAL_TS.append((now_ts, service_name, ep))
        _EXTERNAL_BY_ENDPOINT_MIN[ep] += 1
        _EXTERNAL_BY_SERVICE_MIN[service_name] += 1
        _EXTERNAL_TRACES.append(trace)

    # Attribute this call to the in-flight request (if any) so callers can tell whether
    # the request handler itself hit a provider, independent of background daemon threads
    # that share the same process-wide counters. Background threads carry no request
    # context, so their calls are correctly excluded from the per-request tally.
    try:
        from core.request_context import get_request_context

        ctx = get_request_context()
        if ctx is not None and isinstance(ctx.metadata, dict):
            ctx.metadata["external_calls"] = int(ctx.metadata.get("external_calls", 0) or 0) + 1
    except Exception:
        pass

    # Count provider errors independently of request-context attribution.
    is_error = bool(error) or (status is not None and int(status or 200) >= 400)
    if is_error:
        with _LOCK:
            _ERROR_COUNTS_BY_SOURCE[service_name] += 1
            _ERROR_COUNTS_LAST_MIN[service_name] += 1
            _ERROR_TS.append((now_ts, service_name))
            _prune_old(now_ts)

    logger.info(
        json.dumps(
            {
                "kind": "external_call",
                "service": service_name,
                "endpoint": ep,
                "duration_ms": trace["duration_ms"],
                "status": trace["status"],
                "cache_reason": trace["cache_reason"],
                "error": error,
            },
            ensure_ascii=True,
        )
    )


def record_external_error(*, source: str, error: str) -> None:
    """Lightweight helper — call when an external request raises an exception."""
    now_ts = time.time()
    with _LOCK:
        _ERROR_COUNTS_BY_SOURCE[source] += 1
        _ERROR_COUNTS_LAST_MIN[source] += 1
        _ERROR_TS.append((now_ts, source))
        _prune_old(now_ts)


def record_cache_decision(
    *,
    key: str,
    decision: str,
    reason: str,
    source: str,
    ttl_seconds: Optional[int] = None,
    age_seconds: Optional[float] = None,
    context_reason: Optional[str] = None,
    endpoint_group: Optional[str] = None,
) -> None:
    """Record a cache HIT or MISS. NEW: updates per-endpoint-group hit/miss counters."""
    payload = {
        "ts": _utc_now_iso(),
        "key": str(key),
        "decision": str(decision or "UNKNOWN").upper(),
        "reason": str(reason or "unknown"),
        "source": str(source or "unknown"),
        "ttl_seconds": ttl_seconds,
        "age_seconds": round(float(age_seconds), 3) if age_seconds is not None else None,
        "context_reason": context_reason,
    }
    try:
        from core.request_context import get_request_context

        ctx = get_request_context()
        budget = getattr(ctx, "external_budget", None) if ctx is not None else None
        if budget is not None:
            budget.record_cache_decision(payload["decision"])
    except Exception:
        pass
    hit = payload["decision"] in {"HIT", "CACHE_HIT", "CACHED"}
    group = endpoint_group or source or "unknown"
    with _LOCK:
        _CACHE_DECISIONS.append(payload)
        # NEW: update cache hit/miss stats
        if hit:
            _CACHE_STATS[group]["hits"] += 1
        else:
            _CACHE_STATS[group]["misses"] += 1

    logger.debug(
        json.dumps(
            {
                "kind": "cache_decision",
                "key": payload["key"],
                "decision": payload["decision"],
                "reason": payload["reason"],
                "source": payload["source"],
                "age_seconds": payload["age_seconds"],
                "ttl_seconds": payload["ttl_seconds"],
                "context_reason": payload["context_reason"],
            },
            ensure_ascii=True,
        )
    )


def update_agent_status(name: str, **fields: Any) -> Dict[str, Any]:
    key = str(name or "unknown")
    with _LOCK:
        current = dict(_AGENT_STATUS.get(key, {}))
        for field_name, value in fields.items():
            if value is not None:
                current[field_name] = value
        _AGENT_STATUS[key] = current
        _maybe_dump_engine_agent_status()
        return dict(current)


# ── cross-process engine agent heartbeat ────────────────────────────────────
# The engine runs its agents (execution/risk/scanner/sentiment) in a separate
# process (MS_PROCESS_ROLE=engine); the API cannot inspect those threads. The
# engine therefore dumps its per-agent status (throttled) to a sidecar file
# the API reads to derive real liveness instead of inferring it from
# "engine process is up".
# Both sides (this writer and api.agent_runtime_service, the reader) resolve
# the file in the configured data folder, never relative to the working directory.
from core.config import DATA_DIR as _DATA_DIR  # noqa: E402

_ENGINE_STATUS_DUMP_PATH = Path(_DATA_DIR) / "agent_status_engine.json"
_ENGINE_STATUS_DUMP_MIN_INTERVAL = 2.0
_last_engine_status_dump = 0.0


def _maybe_dump_engine_agent_status() -> None:
    global _last_engine_status_dump
    if os.getenv("MS_PROCESS_ROLE", "").strip().lower() != "engine":
        return
    now = time.monotonic()
    if now - _last_engine_status_dump < _ENGINE_STATUS_DUMP_MIN_INTERVAL:
        return
    _last_engine_status_dump = now
    try:
        payload = {
            "written_at": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "agents": {k: dict(v) for k, v in _AGENT_STATUS.items()},
        }
        _ENGINE_STATUS_DUMP_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = _ENGINE_STATUS_DUMP_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, _ENGINE_STATUS_DUMP_PATH)
    except Exception:
        pass  # diagnostics must never break the caller


# ── NEW: latency percentile snapshot ─────────────────────────────────────────

def snapshot_latency_percentiles(window_sec: float = 60.0) -> Dict[str, Any]:
    """
    Returns p50/p95/p99 for all internal endpoints within the last `window_sec`.
    Also returns a global aggregate.
    """
    now_ts = time.time()
    cutoff = now_ts - float(window_sec)
    by_endpoint: Dict[str, List[float]] = defaultdict(list)
    all_durations: List[float] = []

    with _LOCK:
        for (ts, path, dur, _status) in _LATENCY_WINDOW:
            if ts < cutoff:
                continue
            by_endpoint[path].append(dur)
            all_durations.append(dur)

    # Sort each bucket
    per_endpoint = {}
    for path, durations in by_endpoint.items():
        durations.sort()
        per_endpoint[path] = {
            "count": len(durations),
            "p50_ms": _percentile(durations, 50),
            "p95_ms": _percentile(durations, 95),
            "p99_ms": _percentile(durations, 99),
            "max_ms": round(max(durations), 2),
        }

    all_durations.sort()
    global_stats = {
        "count": len(all_durations),
        "p50_ms": _percentile(all_durations, 50),
        "p95_ms": _percentile(all_durations, 95),
        "p99_ms": _percentile(all_durations, 99),
        "max_ms": round(max(all_durations), 2) if all_durations else 0.0,
    }
    return {
        "window_sec": window_sec,
        "global": global_stats,
        "per_endpoint": per_endpoint,
    }


def snapshot_http_slo(
    *,
    window_sec: Optional[float] = None,
    availability_target_pct: Optional[float] = None,
    p95_target_ms: Optional[float] = None,
    minimum_requests: Optional[int] = None,
) -> Dict[str, Any]:
    """Compute a bounded rolling API SLO from the same request evidence."""
    try:
        resolved_window = float(
            window_sec
            if window_sec is not None
            else os.getenv("MS_SLO_WINDOW_SECONDS", "300")
        )
    except (TypeError, ValueError):
        resolved_window = 300.0
    resolved_window = max(60.0, min(3600.0, resolved_window))
    try:
        resolved_availability = float(
            availability_target_pct
            if availability_target_pct is not None
            else os.getenv("MS_SLO_AVAILABILITY_TARGET_PCT", "99.9")
        )
    except (TypeError, ValueError):
        resolved_availability = 99.9
    resolved_availability = max(90.0, min(99.999, resolved_availability))
    try:
        resolved_p95 = float(
            p95_target_ms
            if p95_target_ms is not None
            else os.getenv("MS_SLO_P95_TARGET_MS", "500")
        )
    except (TypeError, ValueError):
        resolved_p95 = 500.0
    resolved_p95 = max(25.0, min(60_000.0, resolved_p95))
    try:
        resolved_minimum = int(
            minimum_requests
            if minimum_requests is not None
            else os.getenv("MS_SLO_MINIMUM_REQUESTS", "20")
        )
    except (TypeError, ValueError):
        resolved_minimum = 20
    resolved_minimum = max(1, min(10_000, resolved_minimum))

    cutoff = time.time() - resolved_window
    durations: List[float] = []
    server_errors = 0
    client_errors = 0
    with _LOCK:
        for timestamp, _path, duration_ms, status_code in _LATENCY_WINDOW:
            if timestamp < cutoff:
                continue
            durations.append(float(duration_ms))
            if int(status_code) >= 500:
                server_errors += 1
            elif int(status_code) >= 400:
                client_errors += 1
    durations.sort()
    request_count = len(durations)
    availability_ratio = (
        (request_count - server_errors) / request_count
        if request_count
        else 1.0
    )
    target_ratio = resolved_availability / 100.0
    allowed_error_ratio = max(1e-9, 1.0 - target_ratio)
    observed_error_ratio = (
        server_errors / request_count
        if request_count
        else 0.0
    )
    budget_consumed_ratio = observed_error_ratio / allowed_error_ratio
    p95_ms = _percentile(durations, 95)
    enough_data = request_count >= resolved_minimum
    availability_compliant = availability_ratio >= target_ratio
    latency_compliant = p95_ms <= resolved_p95
    compliant = (
        availability_compliant and latency_compliant
        if enough_data
        else None
    )
    return {
        "window_seconds": resolved_window,
        "request_count": request_count,
        "server_error_count": server_errors,
        "client_error_count": client_errors,
        "availability_ratio": round(availability_ratio, 8),
        "availability_target_ratio": round(target_ratio, 8),
        "availability_compliant": availability_compliant if enough_data else None,
        "p95_ms": p95_ms,
        "p95_target_ms": resolved_p95,
        "latency_compliant": latency_compliant if enough_data else None,
        "error_budget_consumed_ratio": round(budget_consumed_ratio, 8),
        "error_budget_remaining_ratio": round(
            max(0.0, 1.0 - budget_consumed_ratio),
            8,
        ),
        "minimum_requests": resolved_minimum,
        "enough_data": enough_data,
        "compliant": compliant,
        "state": (
            "compliant"
            if compliant is True
            else "breached"
            if compliant is False
            else "insufficient_data"
        ),
    }


# ── NEW: error rate snapshot ──────────────────────────────────────────────────

def snapshot_error_counts() -> Dict[str, Any]:
    """Returns error counts per external source, both all-time and last minute."""
    with _LOCK:
        _prune_old()
        return {
            "errors_total_by_source": dict(_ERROR_COUNTS_BY_SOURCE),
            "errors_last_minute_by_source": dict(_ERROR_COUNTS_LAST_MIN),
        }


# ── NEW: cache hit rate snapshot ─────────────────────────────────────────────

def snapshot_cache_hit_rates() -> Dict[str, Any]:
    """Returns cache hit rates per endpoint/source group."""
    with _LOCK:
        result = {}
        for group, stats in _CACHE_STATS.items():
            hits = stats.get("hits", 0)
            misses = stats.get("misses", 0)
            total = hits + misses
            result[group] = {
                "hits": hits,
                "misses": misses,
                "total": total,
                "hit_rate_pct": round(100.0 * hits / total, 1) if total > 0 else None,
            }
        return result


# ── existing snapshots ────────────────────────────────────────────────────────

def snapshot_request_metrics() -> Dict[str, Any]:
    with _LOCK:
        _prune_old()
        return {
            "internal_calls_total": int(_INTERNAL_TOTAL),
            "external_calls_total": int(_EXTERNAL_TOTAL),
            "internal_calls_last_minute": int(sum(_INTERNAL_BY_ENDPOINT_MIN.values())),
            "external_calls_last_minute": int(sum(_EXTERNAL_BY_ENDPOINT_MIN.values())),
            "internal_calls_by_endpoint_total": dict(_INTERNAL_BY_ENDPOINT_TOTAL),
            "internal_calls_by_endpoint_last_minute": dict(_INTERNAL_BY_ENDPOINT_MIN),
            "internal_calls_by_status_total": dict(_INTERNAL_BY_STATUS_TOTAL),
            "internal_calls_by_status_last_minute": dict(_INTERNAL_BY_STATUS_MIN),
            "external_calls_by_endpoint_total": dict(_EXTERNAL_BY_ENDPOINT_TOTAL),
            "external_calls_by_endpoint_last_minute": dict(_EXTERNAL_BY_ENDPOINT_MIN),
            "external_calls_by_service_total": dict(_EXTERNAL_BY_SERVICE_TOTAL),
            "external_calls_by_service_last_minute": dict(_EXTERNAL_BY_SERVICE_MIN),
        }


def snapshot_external_traces(limit: int = 100) -> Dict[str, Any]:
    lim = max(1, min(1000, int(limit or 100)))
    with _LOCK:
        items = list(_EXTERNAL_TRACES)[-lim:]
    return {"count": len(items), "items": items}


def snapshot_cache_decisions(limit: int = 100) -> Dict[str, Any]:
    lim = max(1, min(2000, int(limit or 100)))
    with _LOCK:
        items = list(_CACHE_DECISIONS)[-lim:]
    return {"count": len(items), "items": items}


def snapshot_agent_status() -> Dict[str, Any]:
    with _LOCK:
        return {k: dict(v) for k, v in _AGENT_STATUS.items()}


def uptime_seconds() -> float:
    return max(0.0, time.time() - _START_TS)


def app_version() -> str:
    return os.getenv("APP_VERSION", "9.0.0")


def commit_hash() -> Optional[str]:
    value = (os.getenv("GIT_COMMIT") or os.getenv("COMMIT_HASH") or "").strip()
    return value or None


def _os_rss_bytes() -> Optional[int]:
    """Resident-set size from the OS without third-party packages."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class _ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            get_info = getattr(kernel32, "K32GetProcessMemoryInfo", None)
            if get_info is None:
                get_info = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
            get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessMemoryCounters), wintypes.DWORD]
            get_info.restype = wintypes.BOOL
            if get_info(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
            return None
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            resident_pages = int(handle.read().split()[1])
        return resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
    except Exception:
        return None


def memory_snapshot() -> Dict[str, Any]:
    payload: Dict[str, Any] = {"tracemalloc_enabled": bool(tracemalloc.is_tracing())}
    if tracemalloc.is_tracing():
        current, peak = tracemalloc.get_traced_memory()
        payload["tracemalloc_current_bytes"] = int(current)
        payload["tracemalloc_peak_bytes"] = int(peak)
    rss = _os_rss_bytes()
    if rss is not None:
        payload["rss_bytes"] = rss
    try:
        import psutil  # type: ignore
        proc = psutil.Process(os.getpid())
        mem = proc.memory_info()
        payload.update(
            {
                "rss_bytes": int(getattr(mem, "rss", 0)),
                "vms_bytes": int(getattr(mem, "vms", 0)),
            }
        )
    except Exception:
        pass
    return payload


def reset_observability_state() -> None:
    global _INTERNAL_TOTAL, _EXTERNAL_TOTAL
    with _LOCK:
        _INTERNAL_TOTAL = 0
        _EXTERNAL_TOTAL = 0
        _INTERNAL_TS.clear()
        _EXTERNAL_TS.clear()
        _EXTERNAL_TRACES.clear()
        _CACHE_DECISIONS.clear()
        _INTERNAL_BY_ENDPOINT_TOTAL.clear()
        _INTERNAL_BY_ENDPOINT_MIN.clear()
        _INTERNAL_BY_STATUS_TOTAL.clear()
        _INTERNAL_BY_STATUS_MIN.clear()
        _EXTERNAL_BY_ENDPOINT_TOTAL.clear()
        _EXTERNAL_BY_ENDPOINT_MIN.clear()
        _EXTERNAL_BY_SERVICE_TOTAL.clear()
        _EXTERNAL_BY_SERVICE_MIN.clear()
        _AGENT_STATUS.clear()
        _LATENCY_WINDOW.clear()
        _ERROR_COUNTS_BY_SOURCE.clear()
        _ERROR_COUNTS_LAST_MIN.clear()
        _ERROR_TS.clear()
        _CACHE_STATS.clear()
