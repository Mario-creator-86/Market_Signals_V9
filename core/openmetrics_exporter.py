"""Bounded-cardinality OpenMetrics exporter for production monitoring."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional

from core.data_quality import get_health_dashboard
from core.observability import (
    app_version,
    commit_hash,
    memory_snapshot,
    snapshot_agent_status,
    snapshot_cache_hit_rates,
    snapshot_error_counts,
    snapshot_http_slo,
    snapshot_latency_percentiles,
    snapshot_request_metrics,
    uptime_seconds,
)


OPENMETRICS_CONTENT_TYPE = (
    "application/openmetrics-text; version=1.0.0; charset=utf-8"
)
_PREFIX = "market_signals_"


def _label_value(value: object) -> str:
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')[:160]
    )


def _number(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if not math.isfinite(number):
        number = 0.0
    if number == int(number):
        return str(int(number))
    return format(number, ".12g")


def _limited_items(
    values: Mapping[str, Any],
    *,
    limit: int = 100,
) -> Iterable[tuple[str, Any]]:
    return sorted(
        ((str(key), value) for key, value in values.items()),
        key=lambda item: item[0],
    )[: max(1, min(250, int(limit)))]


class _Writer:
    def __init__(self):
        self.lines: list[str] = []
        self._declared: set[str] = set()

    def declare(self, name: str, help_text: str, metric_type: str = "gauge") -> None:
        if name in self._declared:
            return
        self._declared.add(name)
        self.lines.append(f"# HELP {name} {help_text}")
        self.lines.append(f"# TYPE {name} {metric_type}")

    def sample(
        self,
        name: str,
        value: object,
        labels: Optional[Mapping[str, object]] = None,
    ) -> None:
        label_text = ""
        if labels:
            encoded = ",".join(
                f'{key}="{_label_value(label_value)}"'
                for key, label_value in sorted(labels.items())
            )
            label_text = "{" + encoded + "}"
        self.lines.append(f"{name}{label_text} {_number(value)}")

    def metric(
        self,
        suffix: str,
        value: object,
        *,
        help_text: str,
        metric_type: str = "gauge",
        labels: Optional[Mapping[str, object]] = None,
    ) -> None:
        name = f"{_PREFIX}{suffix}"
        self.declare(name, help_text, metric_type)
        self.sample(name, value, labels)

    def render(self) -> str:
        return "\n".join([*self.lines, "# EOF", ""])


def render_openmetrics() -> str:
    """Render a scrape without external calls or unbounded user-controlled labels."""
    writer = _Writer()
    collection_ok = 1

    writer.metric(
        "build_info",
        1,
        help_text="Application build identity.",
        labels={
            "version": app_version(),
            "commit": commit_hash() or "unknown",
        },
    )
    writer.metric(
        "process_uptime_seconds",
        uptime_seconds(),
        help_text="Seconds since the API process started.",
    )

    memory = memory_snapshot()
    for kind in ("tracemalloc_current_bytes", "tracemalloc_peak_bytes", "rss_bytes"):
        if kind in memory:
            writer.metric(
                "process_memory_bytes",
                memory[kind],
                help_text="API process memory by measurement kind.",
                labels={"kind": kind},
            )

    request_metrics = snapshot_request_metrics()
    writer.metric(
        "http_requests_total",
        request_metrics.get("internal_calls_total", 0),
        help_text="Total HTTP requests observed by the API.",
        metric_type="counter",
        labels={"endpoint": "all"},
    )
    for endpoint, count in _limited_items(
        request_metrics.get("internal_calls_by_endpoint_total") or {}
    ):
        writer.metric(
            "http_requests_total",
            count,
            help_text="Total HTTP requests observed by the API.",
            metric_type="counter",
            labels={"endpoint": endpoint},
        )
    for status_family, count in _limited_items(
        request_metrics.get("internal_calls_by_status_total") or {},
        limit=10,
    ):
        writer.metric(
            "http_responses_total",
            count,
            help_text="Total HTTP responses grouped by status family.",
            metric_type="counter",
            labels={"status_family": status_family},
        )

    try:
        from core.api_traffic_control import get_api_traffic_controller

        traffic = get_api_traffic_controller().snapshot()
        writer.metric(
            "api_traffic_control_enabled",
            1 if traffic.get("enabled") else 0,
            help_text="Whether API admission controls are enabled.",
        )
        writer.metric(
            "api_inflight_requests",
            int(traffic.get("active_requests", 0) or 0),
            help_text="Requests currently admitted by the API traffic controller.",
        )
        writer.metric(
            "api_inflight_limit",
            int(traffic.get("max_inflight", 0) or 0),
            help_text="Configured process-wide API in-flight request limit.",
        )
        writer.metric(
            "api_rate_limit_buckets",
            int(traffic.get("bucket_count", 0) or 0),
            help_text="Current bounded client and request-class rate buckets.",
        )
        writer.metric(
            "api_request_body_limit_bytes",
            int(traffic.get("max_request_body_bytes", 0) or 0),
            help_text="Maximum accepted API request-body size.",
        )
        rejection_counts = dict(traffic.get("rejections_total") or {})
        for reason in (
            "rate_read",
            "rate_write",
            "rate_auth",
            "bucket_capacity",
            "client_concurrency",
            "global_concurrency",
            "request_body_too_large",
            "invalid_content_length",
        ):
            rejection_counts.setdefault(reason, 0)
        for reason, count in _limited_items(rejection_counts, limit=20):
            writer.metric(
                "api_admission_rejections_total",
                count,
                help_text="API admission rejections by bounded reason.",
                metric_type="counter",
                labels={"reason": reason},
            )
    except Exception:
        collection_ok = 0

    latency = snapshot_latency_percentiles(window_sec=60.0)
    for quantile_name in ("p50_ms", "p95_ms", "p99_ms"):
        writer.metric(
            "http_request_duration_seconds",
            float((latency.get("global") or {}).get(quantile_name, 0.0)) / 1000.0,
            help_text="Rolling HTTP request duration quantiles.",
            labels={"endpoint": "all", "quantile": quantile_name.removesuffix("_ms")},
        )
    for endpoint, values in _limited_items(latency.get("per_endpoint") or {}):
        for quantile_name in ("p50_ms", "p95_ms", "p99_ms"):
            writer.metric(
                "http_request_duration_seconds",
                float((values or {}).get(quantile_name, 0.0)) / 1000.0,
                help_text="Rolling HTTP request duration quantiles.",
                labels={
                    "endpoint": endpoint,
                    "quantile": quantile_name.removesuffix("_ms"),
                },
            )

    for provider, count in _limited_items(
        request_metrics.get("external_calls_by_service_total") or {},
        limit=50,
    ):
        writer.metric(
            "external_requests_total",
            count,
            help_text="External provider calls by provider.",
            metric_type="counter",
            labels={"provider": provider},
        )
    errors = snapshot_error_counts()
    for provider, count in _limited_items(
        errors.get("errors_total_by_source") or {},
        limit=50,
    ):
        writer.metric(
            "external_errors_total",
            count,
            help_text="External provider errors by provider.",
            metric_type="counter",
            labels={"provider": provider},
        )

    for group, values in _limited_items(snapshot_cache_hit_rates(), limit=75):
        hits = int((values or {}).get("hits", 0) or 0)
        misses = int((values or {}).get("misses", 0) or 0)
        writer.metric(
            "cache_operations_total",
            hits,
            help_text="Cache operations by bounded group and result.",
            metric_type="counter",
            labels={"group": group, "result": "hit"},
        )
        writer.metric(
            "cache_operations_total",
            misses,
            help_text="Cache operations by bounded group and result.",
            metric_type="counter",
            labels={"group": group, "result": "miss"},
        )
        if hits + misses:
            writer.metric(
                "cache_hit_ratio",
                hits / (hits + misses),
                help_text="Cache hit ratio by bounded group.",
                labels={"group": group},
            )

    for agent, details in _limited_items(snapshot_agent_status(), limit=75):
        writer.metric(
            "agent_enabled",
            1 if (details or {}).get("enabled") else 0,
            help_text="Whether a background agent is enabled.",
            labels={"agent": agent},
        )
        writer.metric(
            "agent_error",
            1 if (details or {}).get("last_error") else 0,
            help_text="Whether a background agent reports a last error.",
            labels={"agent": agent},
        )
        writer.metric(
            "agent_external_calls_blocked",
            int((details or {}).get("last_run_external_blocked", 0) or 0),
            help_text="External calls blocked by the last agent-cycle budget.",
            labels={"agent": agent},
        )

    try:
        from core.disaster_recovery import get_backup_scheduler

        backup = get_backup_scheduler().status()
        writer.metric(
            "backup_enabled",
            1 if backup.get("enabled") else 0,
            help_text="Whether scheduled disaster-recovery backups are enabled.",
        )
        writer.metric(
            "backup_scheduler_running",
            1 if backup.get("running") else 0,
            help_text="Whether the backup scheduler thread is running.",
        )
        writer.metric(
            "backup_snapshots",
            int(backup.get("snapshot_count", 0) or 0),
            help_text="Number of retained recovery snapshots.",
        )
        latest = backup.get("latest_snapshot") or {}
        created_at = latest.get("created_at")
        if created_at:
            parsed = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            age = max(
                0.0,
                (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds(),
            )
            writer.metric(
                "backup_latest_age_seconds",
                age,
                help_text="Age of the latest retained recovery snapshot.",
            )
        writer.metric(
            "backup_last_error",
            1 if backup.get("last_error") else 0,
            help_text="Whether the backup scheduler reports an error.",
        )
    except Exception:
        collection_ok = 0

    try:
        from core.operator_audit import get_operator_audit_ledger

        audit = get_operator_audit_ledger().status()
        writer.metric(
            "operator_audit_ready",
            1 if audit.get("ready") else 0,
            help_text="Whether the operator HMAC chain is writable and verified.",
        )
        writer.metric(
            "operator_audit_records",
            int(audit.get("record_count", 0) or 0),
            help_text="Number of records in the operator audit chain.",
        )
    except Exception:
        collection_ok = 0
        writer.metric(
            "operator_audit_ready",
            0,
            help_text="Whether the operator HMAC chain is writable and verified.",
        )

    quality = get_health_dashboard().get_live_dashboard()
    for level in ("verified", "estimated", "stale", "unavailable"):
        writer.metric(
            "data_streams",
            int(quality.get(level, 0) or 0),
            help_text="Data streams grouped by quality level.",
            labels={"quality": level},
        )

    slo = snapshot_http_slo()
    writer.metric(
        "slo_window_requests",
        slo["request_count"],
        help_text="Requests observed in the configured SLO window.",
    )
    writer.metric(
        "slo_availability_ratio",
        slo["availability_ratio"],
        help_text="Rolling availability ratio where HTTP 5xx consumes budget.",
    )
    writer.metric(
        "slo_availability_target_ratio",
        slo["availability_target_ratio"],
        help_text="Configured rolling availability target.",
    )
    writer.metric(
        "slo_p95_seconds",
        float(slo["p95_ms"]) / 1000.0,
        help_text="Rolling p95 API latency in seconds.",
    )
    writer.metric(
        "slo_p95_target_seconds",
        float(slo["p95_target_ms"]) / 1000.0,
        help_text="Configured p95 API latency objective in seconds.",
    )
    writer.metric(
        "slo_error_budget_remaining_ratio",
        slo["error_budget_remaining_ratio"],
        help_text="Remaining rolling HTTP 5xx error budget ratio.",
    )
    writer.metric(
        "slo_compliant",
        1 if slo.get("compliant") is True else 0,
        help_text="Whether evaluated availability and latency SLOs are compliant.",
    )
    writer.metric(
        "slo_enough_data",
        1 if slo.get("enough_data") else 0,
        help_text="Whether the SLO window contains the minimum sample size.",
    )

    writer.metric(
        "openmetrics_exporter_success",
        collection_ok,
        help_text="Whether all local metric collectors succeeded.",
    )
    return writer.render()
