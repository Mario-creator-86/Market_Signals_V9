from core.agent_metrics import track_agent_run
from core.cache_manager import cache_manager
from core.http_utils import http_request
from core.http_utils import http_get
from core.observability import snapshot_agent_status


class _Response:
    status_code = 200


def test_agent_metrics_use_context_budget_not_process_global_delta(monkeypatch):
    monkeypatch.setattr(
        "core.http_utils.requests.request",
        lambda *args, **kwargs: _Response(),
    )

    with track_agent_run("budget_test", external_call_budget=3):
        http_request("GET", "https://example.com/one", max_retries=0)
        cache_manager.set("budget-test", {"ok": True}, 30, source="budget-test")
        cache_manager.get("budget-test", 30, source="budget-test")
        cache_manager.get("budget-test-miss", 30, source="budget-test")

    metrics = snapshot_agent_status()["budget_test"]
    assert metrics["last_run_external_fetches"] == 1
    assert metrics["last_run_external_budget"] == 3
    assert metrics["last_run_external_blocked"] == 0
    assert metrics["last_run_external_by_provider"] == {"generic": 1}
    assert metrics["last_run_cache_hits"] == 1
    assert metrics["last_run_cache_misses"] == 1
    assert metrics["last_run_cache_hit_rate"] == 50.0


def test_budget_exhaustion_warns_once_per_agent_run(monkeypatch, caplog):
    monkeypatch.setattr(
        "core.http_utils.requests.request",
        lambda *args, **kwargs: _Response(),
    )

    with caplog.at_level("DEBUG", logger="HttpUtils"):
        with track_agent_run("pre_earnings", external_call_budget=1):
            assert http_get("https://example.com/allowed", max_retries=0) is not None
            assert http_get("https://example.com/blocked-1", max_retries=0) is None
            assert http_get("https://example.com/blocked-2", max_retries=0) is None

    warnings = [
        record
        for record in caplog.records
        if record.levelname == "WARNING"
        and "External-call budget exhausted for agent=pre_earnings" in record.getMessage()
    ]
    assert len(warnings) == 1
