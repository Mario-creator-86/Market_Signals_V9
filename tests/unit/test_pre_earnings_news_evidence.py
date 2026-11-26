"""Does reading news improve AI calls? The lab must be able to answer that."""
import importlib.util
import pathlib
import random
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agents import pre_earnings_agent as pe
from core import pre_earnings_learning as lab

_HERE = pathlib.Path(__file__).parent


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_news_helpers_{name}", _HERE / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_dossier = _load("test_pre_earnings_verification_dossier.py")
_synthetic = _load("test_pre_earnings_learning.py")


def _claim(claims, name):
    return next(c for c in claims if c["claim"] == name)


@pytest.mark.unit
def test_registration_records_how_much_news_the_ai_read(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "PRE_EARNINGS_FEEDBACK_PATH", tmp_path / "pre_earnings_feedback.json")
    agent = _dossier._serving_agent()
    item = _dossier._long_item()
    with patch.object(pe, "_utc_now", lambda: datetime(2026, 9, 7, 12, tzinfo=timezone.utc)):
        agent._register_open_predictions([item], agent._get_config())
    record = next(iter(agent._feedback["open_predictions"].values()))
    assert record["predicted_news_articles_read"] == 9
    assert record["predicted_news_sources_read"] == 3


@pytest.mark.unit
def test_structured_rebuild_keeps_saved_news_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "PRE_EARNINGS_FEEDBACK_PATH", tmp_path / "pre_earnings_feedback.json")
    agent = _dossier._serving_agent()
    now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
    with patch.object(pe, "_utc_now", lambda: now):
        agent._register_open_predictions([_dossier._long_item()], agent._get_config())
        rebuilt = _dossier._long_item()
        rebuilt["ai_decision"] = {"used": False}
        agent._register_open_predictions([rebuilt], agent._get_config())
    record = next(iter(agent._feedback["open_predictions"].values()))
    assert record["predicted_news_articles_read"] == 9


@pytest.mark.unit
def test_lab_separates_news_backed_from_thin_news_ai_calls():
    rng = random.Random(21)
    rows = _synthetic._rows(1600, edge=False, seed=4)
    for row in rows:
        up = row["realized_signed_move_pct"] > 0
        if rng.random() < 0.5:
            # Read plenty of news: right 75% of the time.
            row["predicted_news_articles_read"] = rng.randint(3, 12)
            correct = rng.random() < 0.75
        else:
            # Thin news: a coin flip.
            row["predicted_news_articles_read"] = rng.randint(0, 2)
            correct = rng.random() < 0.5
        row["predicted_ai_verdict"] = ("LONG" if up else "SHORT") if correct else ("SHORT" if up else "LONG")
        row["predicted_ai_confidence"] = 0.6
    claims = lab.build_learning_report(rows)["direction_claims"]
    backed = _claim(claims, "ai_verdict_news_backed")
    thin = _claim(claims, "ai_verdict_thin_news")
    assert backed["verdict"] == lab.EDGE_PROVEN
    assert thin["verdict"] == lab.NO_EDGE
    assert backed["events"] + thin["events"] == _claim(claims, "ai_verdict")["events"]


@pytest.mark.unit
def test_legacy_outcomes_without_news_counts_are_not_guessed():
    rows = _synthetic._rows(900)
    for row in rows:
        row["predicted_ai_verdict"] = "LONG"
    claims = lab.build_learning_report(rows)["direction_claims"]
    assert _claim(claims, "ai_verdict_news_backed")["events"] == 0
    assert _claim(claims, "ai_verdict_thin_news")["events"] == 0
    assert _claim(claims, "ai_verdict")["events"] > 0
