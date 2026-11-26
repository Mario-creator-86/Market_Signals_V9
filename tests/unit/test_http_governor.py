from types import SimpleNamespace

import pytest

from core.http_utils import (
    CircuitOpen,
    http_request,
    snapshot_http_governor,
)
from core.observability import snapshot_error_counts, snapshot_request_metrics
from core.request_context import ExternalCallBudget, request_context


def test_cache_only_policy_block_is_not_counted_as_external_call(monkeypatch):
    monkeypatch.setattr(
        "core.http_utils.requests.request",
        lambda *args, **kwargs: pytest.fail("network must not be reached"),
    )

    with request_context(reason="UI_READ", path="/api/state", cache_only=True) as ctx:
        with pytest.raises(CircuitOpen):
            http_request("GET", "https://query1.finance.yahoo.com/v8/finance/chart/AAPL")

    assert ctx.metadata["cache_reason"] == "FORCED_CACHE_ONLY"
    assert snapshot_request_metrics()["external_calls_total"] == 0
    assert snapshot_http_governor()["policy_blocks"]["cache_only"] == 1


def test_forbidden_domain_attempts_are_governance_blocks_not_external_calls(monkeypatch):
    monkeypatch.setattr(
        "core.http_utils.requests.request",
        lambda *args, **kwargs: pytest.fail("forbidden domain must not be reached"),
    )
    url = "https://financialmodelingprep.com/stable/profile?symbol=AAPL"

    for _ in range(2):
        with pytest.raises(CircuitOpen):
            http_request("GET", url, max_retries=0)

    assert snapshot_request_metrics()["external_calls_total"] == 0
    governor = snapshot_http_governor()
    assert governor["policy_blocks"]["forbidden_domain:financialmodelingprep.com"] == 2


def test_permanent_upstream_failure_opens_endpoint_cooldown(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        return SimpleNamespace(status_code=402)

    monkeypatch.setattr("core.http_utils.requests.request", fake_request)
    url = "https://api.marketaux.com/v1/news/all?api_token=secret"

    first = http_request("GET", url, max_retries=0)
    assert first.status_code == 402
    with pytest.raises(CircuitOpen):
        http_request("GET", url, max_retries=0)

    assert len(calls) == 1
    metrics = snapshot_request_metrics()
    assert metrics["external_calls_total"] == 1
    assert metrics["external_calls_by_service_total"]["marketaux"] == 1
    assert snapshot_error_counts()["errors_total_by_source"]["marketaux"] == 1

    cooldowns = snapshot_http_governor()["endpoint_cooldowns"]
    assert len(cooldowns) == 1
    assert cooldowns[0]["provider"] == "marketaux"
    assert cooldowns[0]["endpoint"] == "/v1/news/all"
    assert cooldowns[0]["status"] == 402
    assert cooldowns[0]["blocked_count"] == 1
    assert "secret" not in str(cooldowns)


def test_different_endpoint_is_not_blocked_by_endpoint_cooldown(monkeypatch):
    statuses = iter([403, 200])

    def fake_request(method, url, **kwargs):
        return SimpleNamespace(status_code=next(statuses))

    monkeypatch.setattr("core.http_utils.requests.request", fake_request)
    base = "https://api.marketaux.com"

    assert http_request("GET", f"{base}/v1/news/all", max_retries=0).status_code == 403
    assert http_request("GET", f"{base}/v1/entity/stats", max_retries=0).status_code == 200
    assert snapshot_request_metrics()["external_calls_total"] == 2


def test_rate_limit_opens_endpoint_cooldown_without_retry_storm(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=429)

    monkeypatch.setattr("core.http_utils.requests.request", fake_request)
    url = "https://example.com/feed/"

    assert http_request("GET", url, max_retries=2).status_code == 429
    with pytest.raises(CircuitOpen):
        http_request("GET", url, max_retries=2)

    assert calls == [url]
    cooldown = snapshot_http_governor()["endpoint_cooldowns"][0]
    assert cooldown["status"] == 429
    assert cooldown["provider"] == "generic"


def test_agent_external_budget_hard_caps_actual_network_attempts(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr("core.http_utils.requests.request", fake_request)
    budget = ExternalCallBudget(limit=2)

    with request_context(
        reason="AGENT_REFRESH",
        path="/agent/test",
        agent_name="test_agent",
        external_budget=budget,
    ):
        assert http_request("GET", "https://example.com/one", max_retries=0).status_code == 200
        assert http_request("GET", "https://example.com/two", max_retries=0).status_code == 200
        with pytest.raises(CircuitOpen):
            http_request("GET", "https://example.com/three", max_retries=0)

    assert len(calls) == 2
    assert snapshot_request_metrics()["external_calls_total"] == 2
    assert budget.snapshot() == {
        "limit": 2,
        "used": 2,
        "remaining": 0,
        "blocked": 1,
        "by_provider": {"generic": 2},
        "blocked_by_provider": {"generic": 1},
        "cache_hits": 0,
        "cache_misses": 0,
    }
    assert snapshot_http_governor()["policy_blocks"]["agent_budget:test_agent"] == 1


def test_explicit_manual_request_can_bypass_provider_circuit(monkeypatch):
    monkeypatch.setattr("core.http_utils._is_circuit_open", lambda provider: True)
    monkeypatch.setattr(
        "core.http_utils.requests.request",
        lambda *args, **kwargs: SimpleNamespace(status_code=200),
    )

    response = http_request(
        "POST",
        "http://localhost:11434/api/chat",
        provider="ollama",
        max_retries=0,
        priority=True,
        bypass_circuit=True,
    )

    assert response.status_code == 200
