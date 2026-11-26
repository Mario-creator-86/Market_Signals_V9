def _control():
    return {
        "mode": "ANALYZE",
        "selectable_modes": ["OFF", "ANALYZE", "RECOMMEND", "PAPER_AUTO"],
        "kill_switch_active": False,
        "reduce_fraction_pct": None,
        "readiness": {"ready": False, "reasons": ["owner must select PAPER_AUTO"]},
        "paper_auto_selection": {"ready": False, "reasons": ["Risk Constitution required"]},
        "reconciliation": {"status": "HEALTHY", "conflicts": []},
    }


def test_execution_control_api_returns_owner_selectable_values(client, monkeypatch):
    monkeypatch.setattr("api.execution_service.get_execution_control", lambda config: _control())
    response = client.get("/api/execution/control")
    assert response.status_code == 200
    assert response.json()["reduce_fraction_pct"] is None
    assert "PAPER_AUTO" in response.json()["selectable_modes"]


def test_execution_control_patch_is_removed_in_favor_of_risk_manager(client):
    response = client.patch(
        "/api/execution/control",
        json={"mode": "PAPER_AUTO", "reduce_fraction_pct": 25.0},
    )
    assert response.status_code == 410
    assert "Risk Manager" in response.json()["detail"]
    assert "/api/agents/risk/constitution" in response.json()["detail"]


def test_execution_control_patch_without_body_still_gives_410(client):
    """Migration-era callers PATCH with no JSON body; they must see the
    'moved to Risk Manager' hint, not a 422 body-validation error."""
    response = client.patch("/api/execution/control")
    assert response.status_code == 410
    assert "Risk Manager" in response.json()["detail"]


def test_execution_control_patch_cannot_remain_a_second_threshold_writer(client):
    response = client.patch(
        "/api/execution/control",
        json={
            "pre_earnings_min_gap_risk": 40,
            "pre_earnings_min_abs_bias": 18,
            "pre_earnings_min_coverage_score": 45,
            "pre_earnings_min_score": 76.5,
            "pre_earnings_require_canonical_decision": False,
        },
    )

    assert response.status_code == 410
    assert "Risk Manager" in response.json()["detail"]


def test_kill_switch_requires_real_json_boolean(client):
    response = client.post("/api/execution/kill-switch", json={"active": "true"})
    assert response.status_code == 422
