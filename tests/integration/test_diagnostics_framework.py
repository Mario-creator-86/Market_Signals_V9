import threading
import time

import pytest

from core.observability import reset_observability_state

pytestmark = pytest.mark.integration


@pytest.mark.smoke
def test_health_diagnostics_contract(client):
    response = client.get("/api/health/diagnostics")
    assert response.status_code == 200
    payload = response.json()

    assert payload["status"] in {"ok", "degraded", "down"}
    assert isinstance(payload.get("degraded_reasons"), list)
    assert isinstance(payload.get("uptime_seconds"), (int, float))
    assert isinstance(payload.get("agents"), dict)
    assert all(
        "last_run_external_budget" in details
        for details in payload["agents"].values()
    )
    assert isinstance(payload.get("cache_inventory"), dict)
    assert isinstance(payload["cache_inventory"].get("runtime"), dict)
    assert "endpoint_response" in payload["cache_inventory"]["runtime"]["layers"]
    assert isinstance(payload.get("http_governor"), dict)
    assert isinstance(payload["http_governor"].get("endpoint_cooldowns"), list)
    assert isinstance(payload.get("api_traffic"), dict)
    assert payload["api_traffic"]["enabled"] is True
    assert payload["api_traffic"]["max_inflight"] >= 1
    assert payload["api_traffic"]["max_request_body_bytes"] >= 65_536
    assert isinstance(payload.get("disaster_recovery"), dict)
    assert "snapshot_count" in payload["disaster_recovery"]
    assert "latest_snapshot" in payload["disaster_recovery"]
    assert isinstance(payload.get("operator_audit"), dict)
    assert "ready" in payload["operator_audit"]
    assert "record_count" in payload["operator_audit"]
    assert isinstance(payload.get("service_level_objectives"), dict)
    assert payload["service_level_objectives"]["state"] in {
        "compliant",
        "breached",
        "insufficient_data",
    }
    assert isinstance(payload.get("request_metrics"), dict)
    assert isinstance(payload.get("memory"), dict)


def test_health_diagnostics_explains_degraded_hot_endpoint(client, monkeypatch):
    monkeypatch.setattr(
        "api.main.snapshot_latency_percentiles",
        lambda window_sec=60.0: {
            "window_sec": window_sec,
            "global": {"count": 3, "p95_ms": 900.0},
            "per_endpoint": {
                "/api/state": {
                    "count": 3,
                    "p50_ms": 700.0,
                    "p95_ms": 900.0,
                    "p99_ms": 900.0,
                    "max_ms": 900.0,
                }
            },
        },
    )

    response = client.get("/api/health/diagnostics?fresh=true")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    reason = next(item for item in payload["degraded_reasons"] if item["code"] == "hot_endpoint_latency")
    assert reason["endpoint"] == "/api/state"
    assert reason["p95_ms"] == 900.0
    assert reason["threshold_ms"] == 500


def test_optional_generic_feed_cooldown_does_not_degrade_health(client, monkeypatch):
    monkeypatch.setattr(
        "api.main.snapshot_http_governor",
        lambda: {
            "circuits": {},
            "endpoint_cooldowns": [{
                "provider": "generic",
                "host": "optional.example",
                "endpoint": "/feed",
                "status": 403,
                "retry_in_seconds": 60.0,
                "blocked_count": 1,
            }],
            "policy_blocks": {},
            "rate_limits_per_minute": {},
        },
    )
    monkeypatch.setattr(
        "api.main.snapshot_latency_percentiles",
        lambda window_sec=60.0: {"window_sec": window_sec, "global": {}, "per_endpoint": {}},
    )
    monkeypatch.setattr(
        "api.main._external_connectivity_snapshot",
        lambda: {"enabled": False, "connected_providers": 0, "providers": {}},
    )

    response = client.get("/api/health/diagnostics?fresh=true")

    assert response.status_code == 200
    assert not any(
        reason.get("code") == "upstream_endpoint_cooldown"
        for reason in response.json()["degraded_reasons"]
    )


def test_critical_provider_cooldown_is_explained(client, monkeypatch):
    monkeypatch.setattr(
        "api.main.snapshot_http_governor",
        lambda: {
            "circuits": {},
            "endpoint_cooldowns": [{
                "provider": "marketaux",
                "host": "api.marketaux.com",
                "endpoint": "/v1/news/all",
                "status": 402,
                "retry_in_seconds": 600.0,
                "blocked_count": 3,
            }],
            "policy_blocks": {},
            "rate_limits_per_minute": {},
        },
    )
    monkeypatch.setattr(
        "api.main.snapshot_latency_percentiles",
        lambda window_sec=60.0: {"window_sec": window_sec, "global": {}, "per_endpoint": {}},
    )
    monkeypatch.setattr(
        "api.main._external_connectivity_snapshot",
        lambda: {"enabled": False, "connected_providers": 0, "providers": {}},
    )

    response = client.get("/api/health/diagnostics?fresh=true")

    assert response.status_code == 200
    payload = response.json()
    reason = next(
        item for item in payload["degraded_reasons"]
        if item["code"] == "upstream_endpoint_cooldown"
    )
    assert payload["status"] == "degraded"
    assert reason["providers"] == ["marketaux"]


def test_agent_budget_exhaustion_is_explained(client, monkeypatch):
    monkeypatch.setattr(
        "api.main.snapshot_agent_status",
        lambda: {
            "premarket_scanner": {
                "enabled": True,
                "last_error": None,
                "last_run_external_budget": 10,
                "last_run_external_fetches": 10,
                "last_run_external_blocked": 2,
            }
        },
    )
    monkeypatch.setattr(
        "api.main.snapshot_latency_percentiles",
        lambda window_sec=60.0: {"window_sec": window_sec, "global": {}, "per_endpoint": {}},
    )
    monkeypatch.setattr(
        "api.main._external_connectivity_snapshot",
        lambda: {"enabled": False, "connected_providers": 0, "providers": {}},
    )

    response = client.get("/api/health/diagnostics?fresh=true")

    assert response.status_code == 200
    payload = response.json()
    reason = next(
        item for item in payload["degraded_reasons"]
        if item["code"] == "agent_external_budget_exhausted"
    )
    assert payload["status"] == "degraded"
    assert reason["agents"] == {"premarket_scanner": 2}


@pytest.mark.smoke
def test_no_external_calls_when_cache_is_warm(client, block_external_http):
    endpoints = [
        "/api/news",
        "/api/earnings?days=7",
        "/api/premarket",
        "/api/pre-earnings",
    ]

    # Warm cache once.
    for ep in endpoints:
        warm = None
        for _ in range(3):
            warm = client.get(ep)
            assert warm.status_code == 200
            if ep != "/api/news" or warm.headers.get("x-cache") == "HIT":
                break

    reset_observability_state()

    for ep in endpoints:
        response = client.get(ep)
        assert response.status_code == 200
        x_cache = response.headers.get("x-cache")
        if ep == "/api/news":
            assert x_cache in {"HIT", "MISS"}
        else:
            assert x_cache == "HIT"
        # Assert on the PER-REQUEST external-call tally
        # (x-request-external-calls), not the process-wide external_calls_total
        # counter. record_external_call (core/observability) only attributes a
        # call to an in-flight request context; background daemon threads carry
        # no context, so their provider calls bump the global counter but NOT
        # any per-request header. The global counter is therefore polluted by
        # background workers surviving from earlier tests (scanner/premarket
        # refresh threads) and is not a clean measure of "this warm-cache hit
        # made no provider calls". The per-request header measures exactly
        # that — what this guardrail intends.
        assert int(response.headers.get("x-request-external-calls") or 0) == 0, (
            f"{ep} made external calls on a warm-cache hit "
            f"(x-request-external-calls={response.headers.get('x-request-external-calls')})"
        )


def test_news_refresh_is_non_blocking(client, monkeypatch):
    class _SlowNewsAgent:
        def refresh_all_news(self, _tickers):
            time.sleep(1.2)

    monkeypatch.setattr("agents.news_tab_agent.get_news_tab_agent", lambda: _SlowNewsAgent())
    monkeypatch.setattr("agents.news_tab_agent.get_news_tab_agent_status", lambda: {"initializing": False, "error": None})
    monkeypatch.setattr("agents.news_tab_agent.get_news_tab_agent_if_ready", lambda: _SlowNewsAgent())

    started = time.perf_counter()
    response = client.post("/api/news-tab/refresh")
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert response.status_code == 200
    assert response.json().get("status") in {"started", "disabled"}
    # Non-blocking guarantee: the handler is fire-and-forget (spawns a daemon
    # that sleeps 1.2s) so it must return well BEFORE the 1.2s slow op finishes.
    # Under the full-suite GIL-fatigue regime (~1100+ accumulated tests + daemon
    # threads) the lean POST handler can take 200-500ms wall-clock even when
    # server-side cost is ~10ms. 1000ms still proves non-blocking (a handler that
    # waited on the 1.2s slow op would exceed it); isolation passes in <60ms.
    assert elapsed_ms < 1000, f"/api/news-tab/refresh blocked too long: {elapsed_ms:.0f}ms"


def test_health_stays_fast_during_background_work(client, monkeypatch):
    class _SlowNewsAgent:
        def refresh_all_news(self, _tickers):
            time.sleep(1.5)

    monkeypatch.setattr("agents.news_tab_agent.get_news_tab_agent", lambda: _SlowNewsAgent())
    monkeypatch.setattr("agents.news_tab_agent.get_news_tab_agent_status", lambda: {"initializing": False, "error": None})
    monkeypatch.setattr("agents.news_tab_agent.get_news_tab_agent_if_ready", lambda: _SlowNewsAgent())

    t = threading.Thread(target=lambda: client.post("/api/news-tab/refresh"), daemon=True)
    t.start()
    time.sleep(0.05)

    started = time.perf_counter()
    health = client.get("/api/health")
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    assert health.status_code == 200
    # Non-blocking guarantee: /api/health must stay fast while the background
    # news refresh daemon (1.5s slow op) runs. The bound must be BELOW the 1.5s
    # bg op to prove health is not blocked by it. Under full-suite GIL fatigue
    # (~1100+ tests) the lean health handler reaches 200-500ms wall-clock even
    # at ~10ms server-side cost. 1000ms < 1500ms bg op -> still proves
    # non-blocking; isolation passes in <60ms.
    assert elapsed_ms < 1000, f"/api/health blocked too long during bg work: {elapsed_ms:.0f}ms"
