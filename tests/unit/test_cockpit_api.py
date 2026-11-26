from unittest.mock import patch


def _payload():
    return {
        "schema_version": 1,
        "state": "SUCCESS",
        "generated_at": "2026-08-13T12:00:00+00:00",
        "header": {},
        "sections": {},
        "strategy_lab": {},
        "decision_ledger": {},
        "research_factory": {},
        "system_health": {},
        "source_errors": {},
        "deferred": {},
    }


def test_cockpit_endpoint_is_bounded_and_versioned(client):
    with patch("api.main._endpoint_payload_get", return_value=None), \
         patch("api.main._endpoint_payload_set") as cache_set, \
         patch("api.main.get_cockpit_payload", return_value=_payload()) as build:
        response = client.get("/api/cockpit")

    assert response.status_code == 200
    data = response.json()
    assert data["state"] == "SUCCESS"
    assert data["cockpit_state_version"]
    build.assert_called_once_with()
    cache_set.assert_called_once()


def test_cockpit_unchanged_response_uses_stable_version(client):
    with patch("api.main._endpoint_payload_get", return_value=None), \
         patch("api.main._endpoint_payload_set"), \
         patch("api.main.get_cockpit_payload", return_value=_payload()):
        first = client.get("/api/cockpit").json()
    with patch("api.main._endpoint_payload_get", return_value=None), \
         patch("api.main._endpoint_payload_set"), \
         patch("api.main.get_cockpit_payload", return_value=_payload()):
        second = client.get(f"/api/cockpit?if_version={first['cockpit_state_version']}")

    assert second.status_code == 200
    assert second.json() == {"unchanged": True, "version": first["cockpit_state_version"]}


def test_fresh_cockpit_read_bypasses_endpoint_cache(client):
    with patch("api.main._endpoint_payload_get") as cache_get, \
         patch("api.main._endpoint_payload_set"), \
         patch("api.main.get_cockpit_payload", return_value=_payload()) as build:
        response = client.get("/api/cockpit?fresh=true")

    assert response.status_code == 200
    cache_get.assert_not_called()
    build.assert_called_once_with()


def test_audit_F5_cockpit_version_stable_across_volatile_rebuilds(client):
    """F5: rebuilds that only refresh checked_at/now_et/age counters must keep
    the content version identical so unchanged-polling actually saves work."""
    import json as _json

    base = {
        "state": "SUCCESS",
        "generated_at": "2026-08-16T12:00:00+00:00",
        "header": {"market": {"now_et": "2026-08-16T12:00:01-04:00"}},
        "system_health": {"services": [{"id": "backend", "checked_at": "2026-08-16T12:00:00+00:00"}]},
        "source_freshness": {"pre_earnings": {"age_seconds": 30.4}},
    }
    from api.main import _cockpit_stable_version

    v1 = _cockpit_stable_version(base)
    volatile = _json.loads(_json.dumps(base))
    volatile["generated_at"] = "2026-08-16T12:00:31+00:00"
    volatile["header"]["market"]["now_et"] = "2026-08-16T12:00:32-04:00"
    volatile["system_health"]["services"][0]["checked_at"] = "2026-08-16T12:00:31+00:00"
    volatile["source_freshness"]["pre_earnings"]["age_seconds"] = 44.9  # same minute bucket
    assert _cockpit_stable_version(volatile) == v1
    changed = _json.loads(_json.dumps(base))
    changed["source_freshness"]["pre_earnings"]["age_seconds"] = 95.0  # next minute bucket
    assert _cockpit_stable_version(changed) != v1
    content = _json.loads(_json.dumps(base))
    content["header"]["kill_switch_active"] = True  # real content change
    assert _cockpit_stable_version(content) != v1
