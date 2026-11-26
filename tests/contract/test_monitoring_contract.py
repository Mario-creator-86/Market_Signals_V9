from pathlib import Path

import yaml

from api import main
from core.observability import reset_observability_state, snapshot_request_metrics


ROOT = Path(__file__).resolve().parents[2]


def test_openmetrics_endpoint_contract(client):
    response = client.get("/api/metrics")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith(
        "application/openmetrics-text"
    )
    assert response.text.endswith("# EOF\n")
    assert "market_signals_slo_availability_ratio" in response.text
    assert "market_signals_operator_audit_ready" in response.text
    assert "market_signals_backup_snapshots" in response.text
    assert "market_signals_api_inflight_requests" in response.text
    assert "market_signals_api_admission_rejections_total" in response.text


def test_slo_endpoint_is_machine_readable(client):
    response = client.get("/api/ops/slo")

    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] in {"compliant", "breached", "insufficient_data"}
    assert 0 <= payload["availability_ratio"] <= 1
    assert payload["availability_target_ratio"] > 0
    assert payload["p95_target_ms"] > 0
    assert isinstance(payload["request_count"], int)


def test_metrics_bearer_is_read_only_and_scoped(client, monkeypatch):
    operator_key = "operator-key-for-metrics-contract-012345"
    metrics_token = "metrics-only-token-contract-0123456789"
    client.cookies.clear()
    monkeypatch.setattr(main, "_AUTH_ENABLED", True)
    monkeypatch.setattr(main, "_PROTECT_READS", True)
    monkeypatch.setattr(main, "_API_KEY", operator_key)
    monkeypatch.setattr(main, "_METRICS_BEARER_TOKEN", metrics_token)

    unauthenticated = client.get("/api/metrics")
    metrics = client.get(
        "/api/metrics",
        headers={"Authorization": f"Bearer {metrics_token}"},
    )
    other_endpoint = client.get(
        "/api/state",
        headers={"Authorization": f"Bearer {metrics_token}"},
    )
    operator_access = client.get(
        "/api/metrics",
        headers={"X-API-Key": operator_key},
    )

    assert unauthenticated.status_code == 401
    assert metrics.status_code == 200
    assert other_endpoint.status_code == 401
    assert operator_access.status_code == 200


def test_request_metric_labels_use_route_templates_and_unrouted_bucket(client):
    reset_observability_state()

    client.get("/api/hotlist/not-a-category")
    client.get("/api/hotlist/also-invalid")
    client.get("/api/no-such-resource-one")
    client.get("/api/no-such-resource-two")
    metrics = snapshot_request_metrics()["internal_calls_by_endpoint_total"]

    assert metrics["/api/hotlist/{category}"] == 2
    assert metrics["/api/unrouted"] == 2
    assert all("not-a-category" not in label for label in metrics)
    assert all("no-such-resource" not in label for label in metrics)


def test_prometheus_alert_rules_cover_critical_operational_controls():
    path = ROOT / "deploy" / "monitoring" / "market-signals-alerts.yml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules = [
        rule
        for group in payload["groups"]
        for rule in group["rules"]
    ]
    names = {rule["alert"] for rule in rules}

    assert {
        "MarketSignalsAPIDown",
        "MarketSignalsOperatorAuditUnavailable",
        "MarketSignalsRecoveryPointMissing",
        "MarketSignalsRecoveryPointStale",
        "MarketSignalsAvailabilitySLOBreached",
        "MarketSignalsLatencySLOBreached",
        "MarketSignalsMetricCollectionIncomplete",
        "MarketSignalsAPIConcurrencySaturated",
        "MarketSignalsAPIAdmissionCapacityRejected",
    }.issubset(names)
    assert all(rule.get("for") for rule in rules)
    assert all(rule.get("labels", {}).get("severity") in {"critical", "warning"} for rule in rules)
    assert all(rule.get("annotations", {}).get("runbook") for rule in rules)


def test_prometheus_scrape_uses_scoped_bearer_secret_file():
    path = ROOT / "deploy" / "monitoring" / "prometheus.example.yml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    scrape = payload["scrape_configs"][0]

    assert scrape["metrics_path"] == "/api/metrics"
    assert scrape["authorization"]["type"] == "Bearer"
    assert scrape["authorization"]["credentials_file"].startswith(
        "/etc/prometheus/secrets/"
    )
    assert scrape["static_configs"][0]["targets"] == ["app:8003"]
    assert payload["rule_files"] == [
        "/etc/prometheus/rules/market-signals-alerts.yml"
    ]
