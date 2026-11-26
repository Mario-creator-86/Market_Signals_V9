import time

import pytest


pytestmark = pytest.mark.integration


@pytest.mark.smoke
def test_fmp_timeout_does_not_break_health(client, monkeypatch):
    import requests

    def _timeout(*args, **kwargs):
        raise requests.Timeout("simulated timeout")

    monkeypatch.setattr("requests.request", _timeout)

    # Endpoint may degrade but should not block API process.
    scanner = client.get("/api/scanner/state")
    assert scanner.status_code == 200

    started = time.perf_counter()
    health = client.get("/api/health")
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    assert health.status_code == 200
    # The intent is "the API process is NOT blocked by the timeout" -- a blocked
    # handler would exceed the request timeout (tens of seconds), not 200ms.
    # Under the full-suite process-fatigue regime (GIL contention from ~1000
    # accumulated tests + background daemon threads), the lean /api/health
    # handler can take 200-500ms wall-clock even when server-side cost is ~10ms
    # (measured isolated: passes in <60ms). 1500ms keeps a 3x headroom over the
    # worst observed fatigue while still proving non-blocking (a hung process
    # would blow past this by orders of magnitude). Same family as the
    # test_performance_guardrails_cached_endpoints fatigue flake (Runda-6 F).
    assert elapsed_ms < 1500, f"/api/health blocked too long: {elapsed_ms:.0f}ms"


def test_premarket_agent_sets_last_error_on_failure(monkeypatch):
    from agents.premarket_scanner_agent import get_premarket_scanner_agent

    agent = get_premarket_scanner_agent()
    monkeypatch.setattr(agent, "_is_enabled", lambda: True)
    monkeypatch.setattr(agent, "_is_us_market_open", lambda now_utc=None: False)
    monkeypatch.setattr(agent, "_load_movers", lambda cfg: (_ for _ in ()).throw(RuntimeError("simulated failure")))

    result = agent.run_once()
    status = result.get("status", {})
    assert "simulated failure" in str(status.get("last_error", ""))


def test_scanner_state_endpoint_is_non_blocking_on_slow_refresh(client, monkeypatch):
    import time
    from agents.scanner_agent import get_scanner_agent

    scanner = get_scanner_agent()
    scanner.cached_hot_list = []
    scanner.last_scan_time = 0

    def _slow_scan():
        time.sleep(2.0)
        return []

    monkeypatch.setattr(scanner, "run_scan", _slow_scan)

    started = time.perf_counter()
    response = client.get("/api/scanner/state")
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert response.status_code == 200
    # Non-blocking guarantee: /api/scanner/state must return the cached state
    # WITHOUT waiting for the (now 2.0s) slow scan. The bound must stay BELOW
    # the 2.0s slow scan to prove the endpoint does not block on it. Under
    # full-suite GIL fatigue (~1100+ tests) the lean cached-state handler
    # reaches 200-500ms wall-clock even at ~10ms server-side cost. 1000ms <
    # 2000ms slow scan -> still proves non-blocking; isolation passes in <60ms.
    assert elapsed_ms < 1000, f"/api/scanner/state blocked too long: {elapsed_ms:.0f}ms"
