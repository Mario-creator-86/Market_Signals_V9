import copy
import json
import threading
from datetime import timedelta

import pytest

from agents import pre_earnings_agent as pe
from agents.pre_earnings_agent import PreEarningsAgent


def _pltr_item(event_utc):
    return {
        "symbol": "PLTR",
        "earnings_time_utc": pe._iso(event_utc),
        "session": "AMC",
        "gap_risk": 32,
        "coverage_score": 82,
        "structured_direction_bias_score": 50,
        "direction_bias_score": 50,
        "direction_label": "LONG",
        "badges": ["LONG_BIAS"],
        "bias_reasons": ["Structured direction = LONG +50."],
        # One news row also makes this fixture independent of local earnings
        # history when the persisted schedule-conflict filter runs.
        "top_news": [{"title": "Palantir earnings preview", "url": "https://example.test/pltr"}],
        "ai_decision": {
            "used": True,
            "verdict": "LONG",
            "confidence": 0.81,
            "sentiment": 0.62,
            "rationale": "Demand and guidance remain constructive.",
            "catalyst_label": "GUIDANCE",
            "model": "glm-5.2",
            "event_utc": pe._iso(event_utc),
            "generated_at": pe._iso(pe._utc_now()),
            "source": "forced",
        },
        "ollama": {
            "decision": "LONG",
            "decision_confidence": 0.81,
            "sentiment": 0.62,
            "decision_rationale": "Demand and guidance remain constructive.",
        },
    }


@pytest.mark.unit
def test_pltr_ai_survives_stale_snapshot_overwrite_and_agent_restart(
    tmp_path,
    monkeypatch,
):
    output_path = tmp_path / "pre_earnings.json"
    feedback_path = tmp_path / "pre_earnings_feedback.json"
    ai_cache_path = tmp_path / "pre_earnings_ai_cache.json"
    monkeypatch.setattr(pe, "PRE_EARNINGS_OUTPUT_PATH", output_path)
    monkeypatch.setattr(pe, "PRE_EARNINGS_FEEDBACK_PATH", feedback_path)
    monkeypatch.setattr(pe, "PRE_EARNINGS_AI_CACHE_PATH", ai_cache_path)
    monkeypatch.setattr(
        PreEarningsAgent,
        "_filter_schedule_conflict_items",
        lambda self, items, cfg: list(items),
    )

    llm_calls = {"count": 0}

    def unexpected_llm_call(*args, **kwargs):
        llm_calls["count"] += 1
        raise AssertionError("loading persisted PLTR analysis must not call the LLM")

    monkeypatch.setattr(pe.ollama, "generate", unexpected_llm_call)
    event_utc = pe._utc_now() + timedelta(days=3)

    first = PreEarningsAgent()
    first._state = {
        "items": [_pltr_item(event_utc)],
        "status": {},
        "flags": {},
        "disabled": False,
    }
    first._synchronize_direction_decision(
        first._state["items"][0],
        first._get_config(),
    )
    with first._lock:
        first._persist_locked()

    sidecar = json.loads(ai_cache_path.read_text(encoding="utf-8"))
    cached = list(sidecar["analyses"].values())
    assert len(cached) == 1
    assert cached[0]["symbol"] == "PLTR"
    assert cached[0]["ai_decision"]["verdict"] == "LONG"

    # Simulate the former full-cycle race: a late/stale snapshot still has the
    # PLTR row, but overwrites its paid AI fields with the structured-only row.
    stale_snapshot = json.loads(output_path.read_text(encoding="utf-8"))
    stale_snapshot["items"][0].pop("ai_decision")
    stale_snapshot["items"][0]["ollama"] = None
    assert "structured_direction_label" in stale_snapshot["items"][0]
    assert "llm_direction_score" in stale_snapshot["items"][0]
    assert "decision_alignment" in stale_snapshot["items"][0]
    output_path.write_text(
        json.dumps(stale_snapshot, ensure_ascii=False),
        encoding="utf-8",
    )

    restarted = PreEarningsAgent()
    pltr = restarted._state["items"][0]
    assert pltr["symbol"] == "PLTR"
    assert pltr["ai_decision"]["used"] is True
    assert pltr["ai_decision"]["verdict"] == "LONG"
    assert pltr["ai_decision"]["source"] == "persisted_cache"
    assert pltr["ollama"]["decision"] == "LONG"
    assert llm_calls["count"] == 0

    # The next normal atomic save repairs the canonical snapshot as well.
    with restarted._lock:
        restarted._persist_locked()
    repaired = json.loads(output_path.read_text(encoding="utf-8"))
    assert repaired["items"][0]["ai_decision"]["verdict"] == "LONG"

    repaired_bytes = output_path.read_bytes()
    second_restart = PreEarningsAgent()
    assert output_path.read_bytes() == repaired_bytes
    assert second_restart._state["items"][0]["ai_decision"]["verdict"] == "LONG"
    assert llm_calls["count"] == 0


@pytest.mark.unit
def test_ai_sidecar_does_not_cross_into_a_new_quarter():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    agent._ai_cache = {}
    old_event = pe._utc_now() + timedelta(days=2)
    old_item = _pltr_item(old_event)
    with agent._lock:
        assert agent._cache_ai_from_item_locked(old_item) is True

    new_item = copy.deepcopy(old_item)
    new_item["earnings_time_utc"] = pe._iso(old_event + timedelta(days=90))
    new_item.pop("ai_decision")
    new_item["ollama"] = None

    assert agent._carry_forward_ai_decision(
        new_item,
        previous_item=None,
        cfg={"ollama_decision_weight": 0.35},
    ) is False
    assert "ai_decision" not in new_item


@pytest.mark.unit
def test_structured_only_reregistration_keeps_existing_ai_provenance():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._feedback = {"open_predictions": {}, "outcomes": []}
    event_utc = pe._utc_now() + timedelta(days=2)
    enriched = _pltr_item(event_utc)
    cfg = {"high_gap_threshold_pct": 5.0}

    assert agent._register_open_predictions([enriched], cfg) is True
    key = next(iter(agent._feedback["open_predictions"]))
    original = copy.deepcopy(agent._feedback["open_predictions"][key])
    assert original["predicted_ai_verdict"] == "LONG"

    structured_only = copy.deepcopy(enriched)
    structured_only.pop("ai_decision")
    structured_only["ollama"] = None
    structured_only["gap_risk"] = 38
    assert agent._register_open_predictions([structured_only], cfg) is True

    updated = agent._feedback["open_predictions"][key]
    assert updated["predicted_gap_risk"] == 38
    assert updated["predicted_ai_verdict"] == "LONG"
    assert updated["predicted_ai_confidence"] == original[
        "predicted_ai_confidence"
    ]
    assert updated["predicted_ai_catalyst"] == original[
        "predicted_ai_catalyst"
    ]
    assert updated["ai_model"] == original["ai_model"]
