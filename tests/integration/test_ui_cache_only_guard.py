import pytest
from unittest.mock import patch


pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "endpoint",
    [
        "/api/state?lean=true&include_news=false&include_hotlist=false",
        "/api/dashboard",
        "/api/news-tab/state",
        "/api/premarket",
        "/api/pre-earnings",
        "/api/scanner/state",
    ],
)
def test_ui_get_endpoints_are_cache_only_and_external_free(client, endpoint):
    scanner_agent = None
    if endpoint == "/api/scanner/state":
        class DummyScannerAgent:
            def get_state(self):
                return {"hot_tickers": [], "refreshing": False, "status": {}, "flags": {}}

            def request_refresh(self):
                return {"status": "accepted"}

        scanner_agent = DummyScannerAgent()

    if scanner_agent is not None:
        with patch("agents.scanner_agent.get_scanner_agent", return_value=scanner_agent):
            response = client.get(endpoint)
    else:
        response = client.get(endpoint)

    assert response.status_code == 200, endpoint
    assert response.headers.get("x-cache-policy") == "CACHE_ONLY", endpoint
    # Per-request attribution excludes background daemon threads, so this stays
    # deterministic under the full suite (global counters race with agent loops).
    assert int(response.headers.get("x-request-external-calls", "0")) == 0, endpoint


def test_cache_only_hotlist_details_never_starts_background_builder(client):
    with patch("api.main._endpoint_payload_get", return_value=None), \
         patch("api.state_service._get_hot_list_details_cached", return_value=[]) as details:
        response = client.get(
            "/api/state?lean=false&include_news=false&include_hotlist=true&hotlist_details=true"
        )

    assert response.status_code == 200
    assert response.headers.get("x-cache-policy") == "CACHE_ONLY"
    assert details.call_count == 1
    assert details.call_args.kwargs["allow_build"] is False
