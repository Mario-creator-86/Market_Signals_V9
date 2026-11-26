import concurrent.futures
import time

import pytest


@pytest.mark.smoke
@pytest.mark.perf
def test_load_smoke_30s_cache_first(client, block_external_http):
    """Cache-first endpoints must survive sustained load without app errors.

    The burst far exceeds the per-IP abuse rate limit (240 reads/min), which
    is working as intended — so the limiter store is cleared between waves.
    What this test guards against is 5xx/4xx application failures, not the
    rate-limit policy itself.
    """
    from api.main import _rate_limit_store

    endpoints = [
        "/api/news-tab/state",
        "/api/calendar/events?days=7",
        "/api/premarket",
        "/api/pre-earnings",
    ]
    for ep in endpoints:
        assert client.get(ep).status_code == 200

    duration_sec = 5
    workers = 4
    started = time.time()
    total = 0
    app_failures = 0
    rate_limited = 0

    def _req(ep):
        resp = client.get(ep)
        return resp.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        while (time.time() - started) < duration_sec:
            _rate_limit_store.clear()
            futures = [pool.submit(_req, endpoints[total % len(endpoints)]) for _ in range(workers)]
            for f in concurrent.futures.as_completed(futures):
                total += 1
                code = f.result()
                if code == 429:
                    rate_limited += 1
                elif code != 200:
                    app_failures += 1
    assert app_failures == 0, f"{app_failures} non-429 failures out of {total} requests"
    assert total > 0
