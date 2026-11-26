import re

from core import openmetrics_exporter
from core.observability import (
    record_cache_decision,
    record_external_error,
    record_internal_request,
    reset_observability_state,
    snapshot_http_slo,
    snapshot_request_metrics,
    update_agent_status,
)


def test_request_metrics_group_status_and_compute_rolling_slo():
    reset_observability_state()
    for _ in range(99):
        record_internal_request("/api/orders/{order_id}", 200, 20)
    record_internal_request("/api/orders/{order_id}", 500, 900)

    metrics = snapshot_request_metrics()
    slo = snapshot_http_slo(
        window_sec=300,
        availability_target_pct=99.9,
        p95_target_ms=500,
        minimum_requests=20,
    )

    assert metrics["internal_calls_by_endpoint_total"] == {
        "/api/orders/{order_id}": 100
    }
    assert metrics["internal_calls_by_status_total"] == {"2xx": 99, "5xx": 1}
    assert slo["request_count"] == 100
    assert slo["server_error_count"] == 1
    assert slo["availability_ratio"] == 0.99
    assert slo["availability_compliant"] is False
    assert slo["compliant"] is False
    assert slo["state"] == "breached"


def test_openmetrics_output_is_bounded_valid_and_secret_free(monkeypatch):
    reset_observability_state()
    record_internal_request("/api/state", 200, 15)
    record_internal_request("/api/state", 503, 700)
    record_external_error(source='provider"name', error="secret-provider-error")
    record_cache_decision(
        key="private-cache-key",
        decision="HIT",
        reason="fresh",
        source="state-cache",
    )
    update_agent_status("scanner", enabled=True, last_error="private stack trace")

    class _Backup:
        def status(self):
            return {
                "enabled": True,
                "running": True,
                "snapshot_count": 1,
                "latest_snapshot": {"created_at": "2026-07-31T00:00:00+00:00"},
                "last_error": None,
            }

    class _Audit:
        def status(self):
            return {"ready": True, "record_count": 3}

    monkeypatch.setattr(
        "core.disaster_recovery.get_backup_scheduler",
        lambda: _Backup(),
    )
    monkeypatch.setattr(
        "core.operator_audit.get_operator_audit_ledger",
        lambda: _Audit(),
    )

    output = openmetrics_exporter.render_openmetrics()

    assert output.endswith("# EOF\n")
    assert "# TYPE market_signals_http_requests_total counter" in output
    assert 'endpoint="/api/state"' in output
    assert 'provider="provider\\"name"' in output
    assert "market_signals_operator_audit_ready 1" in output
    assert "market_signals_backup_snapshots 1" in output
    assert "market_signals_api_inflight_requests" in output
    assert "market_signals_api_request_body_limit_bytes" in output
    assert "private-cache-key" not in output
    assert "private stack trace" not in output
    assert "secret-provider-error" not in output
    sample_lines = [
        line
        for line in output.splitlines()
        if line and not line.startswith("#")
    ]
    assert all(
        re.match(r"^[a-zA-Z_:][a-zA-Z0-9_:]*(?:\{.*\})? [-+0-9.eE]+$", line)
        for line in sample_lines
    )
