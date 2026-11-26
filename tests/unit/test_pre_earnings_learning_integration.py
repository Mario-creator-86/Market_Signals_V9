"""Evidence lab wired into PreEarningsAgent: shadow calls, archive, report, API."""
import importlib.util
import json
import pathlib
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agents import pre_earnings_agent as pe
from core import pre_earnings_learning as lab

_HERE = pathlib.Path(__file__).parent


def _load_helpers(name):
    spec = importlib.util.spec_from_file_location(f"_helpers_{name}", _HERE / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_dossier = _load_helpers("test_pre_earnings_verification_dossier.py")
_synthetic = _load_helpers("test_pre_earnings_learning.py")


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "PRE_EARNINGS_FEEDBACK_PATH", tmp_path / "pre_earnings_feedback.json")
    return _dossier._serving_agent()


def _register(agent, item, now):
    with patch.object(pe, "_utc_now", lambda: now):
        agent._register_open_predictions([item], agent._get_config())
    return next(iter(agent._feedback["open_predictions"].values()))


@pytest.mark.unit
def test_registration_records_shadow_call_before_event(agent):
    agent._feedback["learning_model"] = lab.fit_learning_model(_synthetic._rows(1600, edge=True))
    item = _dossier._long_item()
    record = _register(agent, item, datetime(2026, 9, 7, 12, tzinfo=timezone.utc))
    assert record["learning_model_version"] == agent._feedback["learning_model"]["version"]
    assert 0.0 < record["shadow_direction_prob_up"] < 1.0
    assert record["shadow_prior_up_rate"] is not None


@pytest.mark.unit
def test_shadow_call_is_frozen_once_the_event_has_started(agent):
    agent._feedback["learning_model"] = lab.fit_learning_model(_synthetic._rows(1600, edge=True))
    item = _dossier._long_item()
    before = _register(agent, item, datetime(2026, 9, 7, 12, tzinfo=timezone.utc))
    frozen = {key: before.get(key) for key in lab.SHADOW_FIELDS}
    agent._feedback["learning_model"] = lab.fit_learning_model(_synthetic._rows(1600, edge=False, seed=99))
    after = _register(agent, item, datetime(2026, 9, 11, 12, tzinfo=timezone.utc))
    assert {key: after.get(key) for key in lab.SHADOW_FIELDS} == frozen


@pytest.mark.unit
def test_learning_model_refits_only_when_new_outcomes_settle(agent):
    agent._feedback["outcomes"] = _synthetic._rows(400)
    assert agent._refresh_learning_model() is True
    assert agent._feedback["learning_model"]["source_outcome_count"] == 400
    assert agent._refresh_learning_model() is False
    agent._feedback["outcomes"].extend(_synthetic._rows(5, seed=3, start=datetime(2026, 6, 1, tzinfo=timezone.utc)))
    for i, row in enumerate(agent._feedback["outcomes"][-5:]):
        row["prediction_key"] = f"NEW{i}|{row['event_utc']}"
    assert agent._refresh_learning_model() is True


@pytest.mark.unit
def test_persist_archives_outcomes_trimmed_from_live_store(agent):
    rows = _synthetic._rows(4005)
    for i, row in enumerate(rows):
        row["symbol"] = f"T{i}"
        row["prediction_key"] = f"T{i}|{row['event_utc']}"
    agent._feedback["outcomes"] = rows
    agent._persist_feedback_store()
    assert len(agent._feedback["outcomes"]) == 4000
    archived = agent._outcome_archive_path().read_text(encoding="utf-8").strip().splitlines()
    assert len(archived) == 5
    assert len(agent._learning_outcomes()) == 4005


@pytest.mark.unit
def test_learning_report_feeds_top10_direction_edge_summary(agent):
    assert agent._direction_edge_summary() is None
    agent._feedback["outcomes"] = _synthetic._rows(1600, edge=False)
    report = agent.get_learning_report()
    assert report["direction_edge_proven"] is False
    summary = agent._direction_edge_summary()
    assert summary["direction_edge_proven"] is False
    assert summary["headline"] == report["headline"]
    json.dumps(report, allow_nan=False)


@pytest.mark.unit
def test_learning_report_endpoint(agent, monkeypatch):
    import api.main as main

    agent._feedback["outcomes"] = _synthetic._rows(900)
    main._endpoint_payload_clear("pre_earnings_learning_report")
    monkeypatch.setattr(pe, "get_pre_earnings_agent_if_ready", lambda: agent)
    response = main.api_pre_earnings_learning_report()
    body = json.loads(response.body)
    assert body["schema_version"] == lab.SCHEMA_VERSION
    assert {c["claim"] for c in body["direction_claims"]} >= {"heuristic_score", "top10_direction"}
    main._endpoint_payload_clear("pre_earnings_learning_report")
    monkeypatch.setattr(pe, "get_pre_earnings_agent_if_ready", lambda: None)
    assert main.api_pre_earnings_learning_report().status_code == 503
