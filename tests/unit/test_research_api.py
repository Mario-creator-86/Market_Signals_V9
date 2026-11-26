"""Phase 14 unit tests — /api/research/* contracts (research-only surfaces)."""


def test_research_endpoints_are_read_safe(client):
    weaknesses = client.get("/api/research/weaknesses")
    assert weaknesses.status_code == 200
    payload = weaknesses.json()
    assert {"observations", "weaknesses", "trades_loaded"} <= set(payload)

    hypotheses = client.get("/api/research/hypotheses")
    assert hypotheses.status_code == 200
    assert "hypotheses" in hypotheses.json()

    experiments = client.get("/api/research/experiments")
    assert experiments.status_code == 200
    data = experiments.json()
    assert "status_counts" in data and isinstance(data["experiments"], list)

    drift = client.get("/api/research/drift")
    assert drift.status_code == 200
    assert "REVALIDATION" in drift.json()["note"]

    # /api/research/memory was removed in the 2026-09-03 cleanup (UI-dead,
    # store-only surface); the endpoint returning 404 is the expected contract.


def test_research_run_cycle_is_bounded(client):
    response = client.post("/api/research/run-cycle", json={"max_experiments": 2})
    assert response.status_code == 200
    data = response.json()
    assert "status_counts" in data
    assert len(data["experiments"]) <= 2
    # research-only: the response states no orders/risk/promotion
    assert "no promotion" in data["notes"]

    invalid = client.post("/api/research/run-cycle", json={"max_experiments": "abc"})
    assert invalid.status_code == 422
