from core.observability import reset_observability_state

import pytest

pytestmark = pytest.mark.integration


def test_request_budget_and_cache_guard(client, block_external_http):
    endpoint = "/api/news-tab/state"

    # Warm once so subsequent reads should stay in cache.
    assert client.get(endpoint).status_code == 200

    reset_observability_state()
    last = None
    max_request_external = 0
    for _ in range(11):
        last = client.get(endpoint)
        assert last.status_code == 200
        # Per-request attribution: the cache-served handler must not hit a provider.
        # (Global counters race with background agent loops, so assert on this request.)
        max_request_external = max(
            max_request_external, int(last.headers.get("x-request-external-calls", "0"))
        )

    assert last is not None
    assert last.headers.get("x-cache") in {"HIT", "MISS"}
    assert last.headers.get("x-cache-guard") == "ACTIVE"
    assert max_request_external == 0

    diag = client.get("/api/health/diagnostics")
    assert diag.status_code == 200
    metrics = diag.json().get("request_metrics", {})

    assert metrics.get("internal_calls_by_endpoint_total", {}).get("/api/news-tab/state", 0) >= 10
