import json
from datetime import timedelta
from pathlib import Path

import pytest

from agents import pre_earnings_agent as pe
from agents.pre_earnings_agent import PreEarningsAgent


jsonschema = pytest.importorskip("jsonschema")


def _legacy_payload() -> dict:
    event_utc = pe._iso(pe._utc_now() + timedelta(days=3))
    return {
        "as_of_utc": pe._iso(pe._utc_now()),
        "lookahead_days": 30,
        "refresh_seconds": 600,
        "items": [
            {
                "symbol": "PLTR",
                "earnings_time_utc": event_utc,
                "session": "AMC",
                "gap_risk": 48,
                "direction_bias": 0.57,
                "direction_bias_score": 57,
                "structured_direction_bias_score": 57,
                "final_direction_bias_score": 57,
                "direction_label": "LONG",
                "coverage_score": 82,
                "coverage_label": "HIGH",
                "beat_rate_historical": 0.875,
                "beat_rate_beats": 7,
                "beat_rate_samples": 8,
                "beat_rate_source": "yfinance",
                "beat_rate_unavailable_reason": None,
                "range_proxy_pct": 6.4,
                "badges": ["LONG_BIAS"],
                "why": ["Strong structured setup."],
                "what_to_watch": ["Guidance."],
                "top_news": [],
                "ollama": None,
                "legacy_extension": {"must_survive": True},
            }
        ],
        "status": {
            "last_run_time": pe._iso(pe._utc_now()),
            "last_success_time": pe._iso(pe._utc_now()),
            "last_error": None,
            "next_run_in": 600,
        },
        "disabled": False,
        "legacy_top_level": {"must_survive": True},
    }


@pytest.mark.unit
def test_legacy_cache_is_repaired_locally_and_survives_restart(
    tmp_path,
    monkeypatch,
):
    output_path = tmp_path / "pre_earnings.json"
    output_path.write_text(
        json.dumps(_legacy_payload(), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(pe, "PRE_EARNINGS_OUTPUT_PATH", output_path)
    monkeypatch.setattr(
        pe,
        "PRE_EARNINGS_FEEDBACK_PATH",
        tmp_path / "pre_earnings_feedback.json",
    )
    monkeypatch.setattr(
        pe,
        "PRE_EARNINGS_AI_CACHE_PATH",
        tmp_path / "pre_earnings_ai_cache.json",
    )
    monkeypatch.setattr(
        pe,
        "PRE_EARNINGS_PULSE_HISTORY_PATH",
        tmp_path / "pre_earnings_pulse_history.json",
    )
    monkeypatch.setattr(
        PreEarningsAgent,
        "_filter_schedule_conflict_items",
        lambda self, items, cfg: list(items),
    )

    external_calls = {"count": 0}

    def unexpected_external_call(*args, **kwargs):
        external_calls["count"] += 1
        raise AssertionError("legacy cache migration must remain provider-free")

    monkeypatch.setattr(PreEarningsAgent, "run_cycle", unexpected_external_call)
    monkeypatch.setattr(
        PreEarningsAgent,
        "_load_earnings",
        unexpected_external_call,
    )
    monkeypatch.setattr(
        PreEarningsAgent,
        "_load_news_items",
        unexpected_external_call,
    )
    monkeypatch.setattr(pe.ollama, "generate", unexpected_external_call)

    first = PreEarningsAgent()
    repaired = json.loads(output_path.read_text(encoding="utf-8"))
    repaired_item = repaired["items"][0]

    assert external_calls["count"] == 0
    assert len(repaired["items"]) == 1
    assert repaired["legacy_top_level"] == {"must_survive": True}
    assert repaired_item["legacy_extension"] == {"must_survive": True}
    assert repaired_item["structured_direction_label"] == "LONG"
    assert repaired_item["llm_direction_score"] is None
    assert repaired_item["decision_alignment"] == "NO_LLM"

    schema_path = (
        Path(__file__).resolve().parents[1]
        / "contract"
        / "schemas"
        / "pre_earnings.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.validate(repaired, schema)
    jsonschema.validate(first.get_state(), schema)

    repaired_bytes = output_path.read_bytes()
    restarted = PreEarningsAgent()
    assert output_path.read_bytes() == repaired_bytes
    assert restarted._state["items"][0]["structured_direction_label"] == "LONG"
    assert restarted._state["items"][0]["decision_alignment"] == "NO_LLM"
    assert external_calls["count"] == 0
