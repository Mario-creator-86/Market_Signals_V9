"""API cache-coherence and warmup request bounds for Pre-Earnings."""

import api.main as api_main


def _state(symbol: str) -> dict:
    return {
        "as_of_utc": "2026-08-02T14:00:00+00:00",
        "items": [{"symbol": symbol}],
        "status": {
            "last_run_time": "2026-08-02T14:00:00+00:00",
            "last_success_time": "2026-08-02T14:00:00+00:00",
        },
    }


def test_newer_persisted_snapshot_wins_over_stale_process_memory(monkeypatch):
    memory = {"state_revision": 10, "items": [{"symbol": "STALE"}]}
    persisted = {"state_revision": 11, "items": [{"symbol": "GCT"}]}
    monkeypatch.setattr(api_main, "_load_json_file_payload", lambda _path: persisted)

    selected = api_main._prefer_newer_pre_earnings_snapshot(memory)

    assert selected["state_revision"] == 11
    assert selected["items"][0]["symbol"] == "GCT"


def test_process_memory_wins_when_its_revision_is_current(monkeypatch):
    memory = {"state_revision": 12, "items": [{"symbol": "GCT"}]}
    persisted = {"state_revision": 11, "items": [{"symbol": "STALE"}]}
    monkeypatch.setattr(api_main, "_load_json_file_payload", lambda _path: persisted)

    selected = api_main._prefer_newer_pre_earnings_snapshot(memory)

    assert selected is memory


def test_get_pre_earnings_busts_60s_cache_after_agent_persists(
    client,
    monkeypatch,
):
    revision = {"value": ("pre_earnings.json", 1, 100)}
    monkeypatch.setattr(
        api_main,
        "_pre_earnings_source_revision",
        lambda: revision["value"],
    )
    api_main._sync_pre_earnings_cache_revision()
    api_main._endpoint_payload_set(
        "pre_earnings_state",
        b'{"items":[{"symbol":"STALE"}],"status":{}}',
    )

    class DummyAgent:
        def get_state(self):
            return _state("GCT")

    monkeypatch.setattr(api_main, "_agent_enabled", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        "agents.pre_earnings_agent.get_pre_earnings_agent",
        lambda: DummyAgent(),
    )

    revision["value"] = ("pre_earnings.json", 2, 120)
    response = client.get("/api/pre-earnings")

    assert response.status_code == 200
    assert response.json()["items"][0]["symbol"] == "GCT"
    assert response.headers["cache-control"] == "no-store"


def test_get_pre_earnings_keeps_cache_when_persisted_revision_is_unchanged(
    client,
    monkeypatch,
):
    revision = ("pre_earnings.json", 1, 100)
    monkeypatch.setattr(
        api_main,
        "_pre_earnings_source_revision",
        lambda: revision,
    )
    api_main._sync_pre_earnings_cache_revision()
    api_main._endpoint_payload_set(
        "pre_earnings_state",
        b'{"items":[{"symbol":"CACHED"}],"status":{}}',
    )
    monkeypatch.setattr(
        "agents.pre_earnings_agent.get_pre_earnings_agent",
        lambda: (_ for _ in ()).throw(AssertionError("cache miss was unexpected")),
    )

    response = client.get("/api/pre-earnings")

    assert response.status_code == 200
    assert response.json()["items"][0]["symbol"] == "CACHED"
    assert response.headers["cache-control"] == "no-store"


def test_refresh_request_clears_cached_get_snapshot(client, monkeypatch):
    api_main._endpoint_payload_set(
        "pre_earnings_state",
        b'{"items":[{"symbol":"STALE"}]}',
    )

    class DummyAgent:
        def start(self):
            return None

        def request_refresh(self):
            return {"status": "accepted"}

    monkeypatch.setattr(api_main, "_agent_enabled", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        "agents.pre_earnings_agent.get_pre_earnings_agent",
        lambda: DummyAgent(),
    )

    response = client.post("/api/pre-earnings/refresh")

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert api_main._endpoint_payload_get("pre_earnings_state", 60) is None


def test_warmup_completion_invalidates_intermediate_get_cache(
    client,
    monkeypatch,
):
    warmup = {
        "job_id": "cov-1",
        "running": True,
        "processed": 0,
        "success": 0,
        "failed": 0,
        "last_progress_at": "2026-08-02T14:00:00+00:00",
        "last_finished": None,
    }

    class DummyAgent:
        def start(self):
            return None

        def request_coverage_warmup(self, **kwargs):
            return {"status": "accepted", "job_id": "cov-1"}

        def get_coverage_warmup_status(self):
            return dict(warmup)

    agent = DummyAgent()
    monkeypatch.setattr(api_main, "_agent_enabled", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        "agents.pre_earnings_agent.get_pre_earnings_agent",
        lambda: agent,
    )

    accepted = client.post(
        "/api/pre-earnings/coverage/warmup",
        json={"symbols": ["GCT"], "only_needs_coverage": False},
    )
    assert accepted.status_code == 200

    api_main._endpoint_payload_set(
        "pre_earnings_state",
        b'{"items":[{"symbol":"INTERMEDIATE"}]}',
    )
    warmup.update(
        {
            "running": False,
            "processed": 1,
            "success": 1,
            "last_progress_at": "2026-08-02T14:01:00+00:00",
            "last_finished": "2026-08-02T14:01:00+00:00",
        }
    )
    completed = client.get("/api/pre-earnings/coverage/warmup")

    assert completed.status_code == 200
    assert completed.headers["cache-control"] == "no-store"
    assert completed.json()["coverage_warmup"]["running"] is False
    assert api_main._endpoint_payload_get("pre_earnings_state", 60) is None


def test_coverage_warmup_normalizes_and_deduplicates_symbols(
    client,
    monkeypatch,
):
    class DummyAgent:
        def __init__(self):
            self.symbols = None

        def start(self):
            return None

        def request_coverage_warmup(self, **kwargs):
            self.symbols = kwargs["symbols"]
            return {"status": "accepted"}

    agent = DummyAgent()
    monkeypatch.setattr(api_main, "_agent_enabled", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        "agents.pre_earnings_agent.get_pre_earnings_agent",
        lambda: agent,
    )

    response = client.post(
        "/api/pre-earnings/coverage/warmup",
        json={"symbols": [" gct ", "GCT", "hut", "BRK.B"]},
    )

    assert response.status_code == 200
    assert agent.symbols == ["GCT", "HUT", "BRK.B"]


def test_coverage_warmup_rejects_invalid_symbols_before_starting_agent(
    client,
    monkeypatch,
):
    monkeypatch.setattr(api_main, "_agent_enabled", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        "agents.pre_earnings_agent.get_pre_earnings_agent",
        lambda: (_ for _ in ()).throw(AssertionError("invalid request reached agent")),
    )

    response = client.post(
        "/api/pre-earnings/coverage/warmup",
        json={"symbols": ["GCT", "BTC-USD", "BAD!"]},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["invalid_symbols"] == ["BTC-USD", "BAD!"]


def test_coverage_warmup_enforces_configured_unique_symbol_limit(
    client,
    monkeypatch,
):
    monkeypatch.setenv("PRE_EARNINGS_COVERAGE_WARMUP_MAX_SYMBOLS", "2")
    monkeypatch.setattr(api_main, "_agent_enabled", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        "agents.pre_earnings_agent.get_pre_earnings_agent",
        lambda: (_ for _ in ()).throw(AssertionError("oversized request reached agent")),
    )

    response = client.post(
        "/api/pre-earnings/coverage/warmup",
        json={"symbols": ["GCT", "GCT", "HUT", "AAPL"]},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["max_symbols"] == 2
    assert response.json()["detail"]["requested_count"] == 3
