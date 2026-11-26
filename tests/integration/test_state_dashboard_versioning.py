import pytest


pytestmark = pytest.mark.integration


def test_state_endpoint_supports_if_version_unchanged(client):
    import time
    # Warm up caches so lazy-loading (like news pulse scores) completes
    for _ in range(3):
        client.get("/api/state?lean=true&include_news=false&include_hotlist=false")
        time.sleep(0.5)

    first = client.get("/api/state?lean=true&include_news=false&include_hotlist=false")
    assert first.status_code == 200
    payload = first.json()
    version = payload.get("state_version")
    assert isinstance(version, str) and len(version) >= 8

    second = client.get(
        f"/api/state?lean=true&include_news=false&include_hotlist=false&if_version={version}&unchanged=true"
    )
    assert second.status_code == 200
    data = second.json()
    assert data.get("unchanged") is True
    assert data.get("version") == version


def test_dashboard_endpoint_supports_if_version_unchanged(client):
    first = client.get("/api/dashboard")
    assert first.status_code == 200
    payload = first.json()
    version = payload.get("dashboard_state_version")
    assert isinstance(version, str) and len(version) >= 8

    second = client.get(f"/api/dashboard?if_version={version}&unchanged=true")
    assert second.status_code == 200
    data = second.json()
    assert data.get("unchanged") is True
    assert data.get("version") == version
