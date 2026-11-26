"""Phase 13 unit tests — /api/ledger/* contracts (bounded, honest, additive)."""
import json

import pytest

from core.decision_ledger.store import (
    get_decision_ledger_store, reset_decision_ledger_store_singleton,
)
from core.decision_ledger.types import SOURCE_SIGNAL_PATH, LedgerEntry


class StubProvider:
    name = "stub"

    def generate(self, prompt, **kwargs):
        from core.ai.provider import AIResponse

        return AIResponse(text=json.dumps({
            "recommendation": "watch", "confidence": 0.5, "size_factor": 0.3,
            "leverage": 1, "sentiment": "neutral", "supports_direction": True,
            "reason": "replay", "risks": [],
        }), model="stub-api")


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path, monkeypatch):
    """Keep API contract tests independent of suite order and operator data."""
    reset_decision_ledger_store_singleton()
    monkeypatch.setenv("MS_DECISION_LEDGER_PATH", str(tmp_path / "decision_ledger.db"))
    yield
    reset_decision_ledger_store_singleton()


def _seed_entry(decision_id="dec_api_1"):
    store = get_decision_ledger_store(None)
    entry = LedgerEntry(
        decision_id=decision_id, created_at="2026-08-16T15:00:00+00:00", ticker="TSLA",
        action="NO_TRADE", horizon="SWING", source=SOURCE_SIGNAL_PATH, status="rejected",
        strategy="MOMENTUM", ai={"available": False, "provider": "ollama",
                                 "model": "", "prompt_version": "trade_review_v1"},
        ai_proposal={"action": "NO_TRADE"}, validation={"ok": True, "stage": "RISK_GATE"},
        critic={"available": False}, risk={"accepted": False, "reason": "veto"},
        capital_allowed={"usd_size": 0.0}, executed={"order_intent_id": None, "state": "NONE"},
        data_existed={"signal_id": "sig_api"},
    )
    store.append_entry(entry)
    return entry


def test_ledger_decisions_endpoint(client):
    entry = _seed_entry("dec_api_list")
    response = client.get("/api/ledger/decisions?limit=10")
    assert response.status_code == 200
    data = response.json()
    assert data["coverage"] == "PERSISTENT_LEDGER"
    assert any(row["decision_id"] == entry.decision_id for row in data["decisions"])
    row = next(row for row in data["decisions"] if row["decision_id"] == entry.decision_id)
    assert row["what_happened"] == "NO_TRADE (rejected)"
    assert row["ai_model"]["prompt_version"] == "trade_review_v1"


def test_ledger_decision_detail_and_404(client):
    entry = _seed_entry("dec_api_detail")
    found = client.get(f"/api/ledger/decisions/{entry.decision_id}")
    assert found.status_code == 200
    assert found.json()["entry"]["decision_id"] == entry.decision_id
    assert found.json()["outcomes"] == []
    missing = client.get("/api/ledger/decisions/dec_does_not_exist")
    assert missing.status_code == 404


def test_no_trade_evaluation_endpoint_shape(client):
    _seed_entry("dec_api_nt")
    response = client.get("/api/ledger/no-trade-evaluation?threshold_pct=1.0")
    assert response.status_code == 200
    data = response.json()
    for key in ("total_recorded", "correctly_rejected", "incorrectly_rejected_profitable",
                "inconclusive", "rejection_precision", "bars_source"):
        assert key in data
    # No stored bars for TSLA in the test historical store → honest inconclusive.
    assert data["tickers_with_bars"] == 0


def test_model_lab_endpoint_lists_roles_without_global_score(client):
    response = client.get("/api/ledger/model-lab")
    assert response.status_code == 200
    data = response.json()
    assert "quant" in data["roles_available"]
    assert "risk_critic" in data["roles_available"]
    assert "note" in data and "global" not in data["note"].lower().replace("no global", "ok")
    invalid = client.get("/api/ledger/model-lab?role=nonsense")
    assert invalid.status_code == 422


def test_replay_endpoint_validates_and_404s(client):
    missing = client.post("/api/ledger/replay", json={"decision_id": "dec_missing"})
    assert missing.status_code == 404
    empty = client.post("/api/ledger/replay", json={})
    assert empty.status_code == 422
    entry = _seed_entry("dec_api_replay")
    from unittest.mock import patch

    from core.ai.decision import Action, Decision
    from core.ai.pipeline import ValidationResult
    from core.ai.decision_engine import EvaluationResult

    stub_result = EvaluationResult(
        Decision(action=Action.WATCH),
        ValidationResult(ok=True, decision=Decision(action=Action.WATCH), stage="OK"),
        None, {"available": False, "reason": "stubbed provider", "provider": "ai"}, False,
        prompt="stub prompt", model="", prompt_version="trade_review_v1",
    )
    with patch("core.decision_ledger.replay.evaluate_trade_decision", return_value=stub_result):
        response = client.post("/api/ledger/replay", json={"decision_id": entry.decision_id})
    # AI-unavailable replay records honestly (GOLD-014 semantics) and never orders.
    assert response.status_code == 200
    data = response.json()
    store = get_decision_ledger_store(None)
    replay_entry = store.get_entry(data["replay_decision_id"])
    assert replay_entry.source == "REPLAY"
    assert replay_entry.executed["state"] == "NONE"
