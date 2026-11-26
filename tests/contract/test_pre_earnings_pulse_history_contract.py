import json
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from agents import pre_earnings_agent as pe
from agents.pre_earnings_agent import PreEarningsAgent


SCHEMA_PATH = (
    Path(__file__).parent
    / "schemas"
    / "pre_earnings_pulse_history.schema.json"
)


@pytest.mark.contract
def test_pulse_history_schema_is_valid():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)


@pytest.mark.contract
def test_persisted_pulse_registry_matches_contract(tmp_path):
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._pulse_store_lock = threading.RLock()
    agent._pulse_store_path = tmp_path / "pre_earnings_pulse_history.json"
    agent._pulse_store = agent._default_pulse_store()
    cfg = dict(PreEarningsAgent.DEFAULT_CONFIG)
    event_utc = pe._utc_now() + timedelta(days=5)
    items = [
        {
            "symbol": "VCYT",
            "earnings_time_utc": pe._iso(event_utc),
            "session": "AMC",
        },
        {
            "symbol": "CLSK",
            "earnings_time_utc": pe._iso(event_utc),
            "session": "AMC",
        },
    ]
    pulse = {
        "summary": "AI and crypto names dominate.",
        "sentiment": "neutral",
        "top_long": [{"symbol": "VCYT", "reason": "Constructive setup."}],
        "top_short": [{"symbol": "CLSK", "reason": "Relative weakness."}],
        "catalysts": "Earnings guidance.",
        "risks": "Elevated post-report volatility.",
        "model": "glm-5.2",
        "generated_at": pe._iso(pe._utc_now()),
    }

    with agent._pulse_store_lock:
        agent._merge_pulse_snapshot_locked(
            pulse,
            items,
            cfg,
            source="llm",
            persist=True,
        )

    persisted = json.loads(agent._pulse_store_path.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(persisted),
        key=lambda error: list(error.absolute_path),
    )
    assert errors == []


@pytest.mark.contract
def test_legacy_sidecar_is_normalized_on_restart_and_persisted_on_next_import(
    tmp_path,
    monkeypatch,
):
    generated_at = pe._iso(pe._utc_now() - timedelta(minutes=15))
    event_utc = pe._iso(pe._utc_now() + timedelta(days=5))
    legacy_pick = {
        "symbol": "VCYT",
        "reason": "Legacy active setup.",
        "source": "legacy_state",
        "eligible": True,
        "event_key": f"VCYT|{event_utc[:10]}",
        "event_date": event_utc[:10],
        "earnings_time_utc": event_utc,
        "session": "AMC",
    }
    legacy_active_pick = {
        **legacy_pick,
        "first_seen_at": generated_at,
        "last_seen_at": generated_at,
        "sources": ["legacy_state"],
        "occurrences": 1,
    }
    legacy_sidecar = {
        "schema_version": 1,
        "created_at": generated_at,
        "updated_at": generated_at,
        "limits": {
            "active_per_side": 500,
            "generated_picks_per_side": 3,
            "reason_characters": 500,
            "history_snapshots": "unbounded",
            "archive_entries": "unbounded",
        },
        "next_snapshot_id": 2,
        "latest": {
            "snapshot_id": 1,
            "generated_at": generated_at,
            "source": "legacy_state",
            "content_fingerprint": "0" * 64,
            "summary": "Legacy pulse.",
            "sentiment": "bullish",
            "catalysts": "Legacy catalyst.",
            "risks": "Legacy risk.",
            "model": "glm-5.2",
        },
        "active": {
            "top_long": [legacy_active_pick],
            "top_short": [],
        },
        "archive": [],
        "snapshots": [
            {
                "snapshot_id": 1,
                "generated_at": generated_at,
                "recorded_at": generated_at,
                "source": "legacy_state",
                "content_fingerprint": "0" * 64,
                "summary": "Legacy pulse.",
                "sentiment": "bullish",
                "top_long": [legacy_pick],
                "top_short": [],
                "catalysts": "Legacy catalyst.",
                "risks": "Legacy risk.",
                "model": "glm-5.2",
                "active_counts_after_merge": {
                    "top_long": 1,
                    "top_short": 0,
                },
            }
        ],
    }
    history_path = tmp_path / "pre_earnings_pulse_history.json"
    original_text = json.dumps(legacy_sidecar, indent=2)
    history_path.write_text(original_text, encoding="utf-8")
    monkeypatch.setattr(pe, "PRE_EARNINGS_PULSE_HISTORY_PATH", history_path)
    monkeypatch.setattr(pe, "PRE_EARNINGS_OUTPUT_PATH", tmp_path / "pre_earnings.json")
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
        PreEarningsAgent,
        "_filter_schedule_conflict_items",
        lambda self, items, cfg: list(items),
    )

    restarted = PreEarningsAgent()

    # Loading is read-only: legacy bytes stay untouched until a normal write.
    assert history_path.read_text(encoding="utf-8") == original_text
    normalized = restarted._pulse_store["snapshots"][0]
    assert normalized["first_generated_at"] == generated_at
    assert normalized["last_generated_at"] == generated_at
    assert normalized["sources"] == ["legacy_state"]
    assert normalized["occurrences"] == 1
    assert normalized["content_fingerprint"] != "0" * 64
    assert restarted._pulse_store["latest"]["sources"] == ["legacy_state"]
    assert (
        restarted._pulse_store["latest"]["content_fingerprint"]
        == normalized["content_fingerprint"]
    )
    assert (
        restarted._pulse_store["limits"]["history_snapshots"]
        == "unbounded_unique"
    )

    new_event = pe._utc_now() + timedelta(days=6)
    items = [
        {
            "symbol": "VCYT",
            "earnings_time_utc": event_utc,
            "session": "AMC",
        },
        {
            "symbol": "CNO",
            "earnings_time_utc": pe._iso(new_event),
            "session": "AMC",
        },
    ]
    restarted._state["items"] = items
    restarted.import_pulse_snapshot(
        {
            "summary": "New material pulse.",
            "sentiment": "neutral",
            "top_long": [{"symbol": "CNO", "reason": "New qualifier."}],
            "top_short": [],
            "catalysts": "New catalyst.",
            "risks": "New risk.",
            "model": "glm-5.2",
            "generated_at": pe._iso(pe._utc_now()),
        },
        items,
        source="llm",
    )

    persisted = json.loads(history_path.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(persisted),
        key=lambda error: list(error.absolute_path),
    )
    assert errors == []
    assert len(persisted["snapshots"]) == 2
    assert persisted["snapshots"][0]["sources"] == ["legacy_state"]
