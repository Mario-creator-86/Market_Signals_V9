import json

import pytest

import api.main as api_main
from agents import pre_earnings_agent as pe


pytestmark = pytest.mark.integration


def test_cache_only_get_serves_persisted_snapshot_without_starting_agent(
    client,
    monkeypatch,
    tmp_path,
):
    output_path = tmp_path / "pre_earnings.json"
    output_path.write_text(
        json.dumps(
            {
                "as_of_utc": "2026-08-02T15:00:00+00:00",
                "refresh_seconds": 600,
                "items": [{"symbol": "PLTR", "direction_label": "LONG"}],
                "status": {
                    "last_run_time": "2026-08-02T15:00:00+00:00",
                    "last_success_time": "2026-08-02T15:00:00+00:00",
                },
                "disabled": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(pe, "PRE_EARNINGS_OUTPUT_PATH", output_path)
    monkeypatch.setattr(api_main, "IS_TEST_MODE", False)
    monkeypatch.setattr(api_main, "_agent_enabled", lambda *args, **kwargs: True)
    monkeypatch.setattr(pe, "get_pre_earnings_agent_if_ready", lambda: None)
    monkeypatch.setattr(
        pe,
        "get_pre_earnings_agent",
        lambda: (_ for _ in ()).throw(
            AssertionError("cache-only GET must not initialize the agent")
        ),
    )
    api_main._endpoint_payload_clear("pre_earnings_state")

    response = client.get("/api/pre-earnings")

    assert response.status_code == 200
    assert response.headers["x-cache-policy"] == "CACHE_ONLY"
    assert response.headers["x-request-external-calls"] == "0"
    assert response.json()["items"][0]["symbol"] == "PLTR"


def test_cache_only_get_prefers_ready_agent_normalized_memory_state(
    client,
    monkeypatch,
):
    class ReadyAgent:
        def get_state(self):
            return {
                "as_of_utc": "2026-08-09T10:00:00+00:00",
                "items": [{"symbol": "SLS"}],
                "top_predictions": [{"rank": 1, "symbol": "SLS"}],
                "ranking": {"state": "FRESH", "news_articles_scanned": 12},
                "disabled": False,
            }

    monkeypatch.setattr(api_main, "IS_TEST_MODE", False)
    monkeypatch.setattr(api_main, "_agent_enabled", lambda *args, **kwargs: True)
    monkeypatch.setattr(pe, "get_pre_earnings_agent_if_ready", lambda: ReadyAgent())
    monkeypatch.setattr(
        api_main,
        "_load_json_file_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ready memory state must win over disk")
        ),
    )
    api_main._endpoint_payload_clear("pre_earnings_state")

    response = client.get("/api/pre-earnings")

    assert response.status_code == 200
    assert response.headers["x-cache-policy"] == "CACHE_ONLY"
    assert response.headers["x-request-external-calls"] == "0"
    assert response.json()["top_predictions"][0]["symbol"] == "SLS"
    assert response.json()["ranking"]["news_articles_scanned"] == 12
