"""Learning mode progress: the engine's worker writes it, the API and the cockpit read it."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from api.learning_service import learning_state
from core.learning_mode.book import LearningBook
from core.learning_mode.worker import LearningWorker

NOW = datetime(2026, 11, 24, 17, 0, tzinfo=timezone.utc)


def _worker(tmp_path, *, config, positions=()):
    return LearningWorker(
        book=LearningBook(tmp_path), config=lambda: config, scanner_state=lambda: {}, news_rows=lambda: [],
        positions=lambda: list(positions), quote=lambda t, s: None,
        ask_mover=lambda *a, **k: {"idea": None, "model": "", "error": ""},
        ask_news=lambda *a, **k: {"ideas": [], "model": "", "error": ""},
        usage=lambda: {}, session=lambda now: {"us_regular": True, "label": "US regular session"}, clock=lambda: NOW)


def test_the_worker_publishes_its_progress_and_the_api_reads_it(tmp_path):
    closed = {"position_id": "p1", "ticker": "GLD", "strategy_type": "AI_NEWS_LONG", "status": "CLOSED", "net_pnl": 4.0,
              "margin_used": 100.0, "opened_at": NOW.isoformat(), "closed_at": NOW.isoformat(),
              "decision_context": {"learning": {"confidence": 0.7, "source": "news"}}}
    _worker(tmp_path, config={"mode": "paper"}, positions=[closed]).run_once()
    state = learning_state(tmp_path, now=NOW + timedelta(minutes=1))
    assert state["status"] == "ran" and state["stale"] is False
    progress = state["progress"]
    assert progress["target"] == 10_000 and progress["closed"] == 1 and progress["net_pnl"] == 4.0
    assert state["opened_today"] == 1 and state["daily_cap"] == 100 and state["paused_reason"] == ""
    assert progress["recent"][0]["ticker"] == "GLD"


def test_a_pause_is_published_with_its_reason(tmp_path):
    _worker(tmp_path, config={"mode": "live"}).run_once()
    state = learning_state(tmp_path, now=NOW)
    assert state["status"] == "off" and "paper only" in state["paused_reason"]


def test_no_progress_yet_and_a_silent_worker_are_both_said(tmp_path):
    assert learning_state(tmp_path / "never", now=NOW)["status"] == "not_started"
    _worker(tmp_path, config={"mode": "paper"}).run_once()
    assert learning_state(tmp_path, now=NOW + timedelta(minutes=11))["stale"] is True
    (tmp_path / "status.json").write_text("{ broken", encoding="utf-8")
    assert learning_state(tmp_path, now=NOW)["status"] == "unreadable"


def test_the_endpoint_is_registered():
    from fastapi.testclient import TestClient

    import api.main as main

    paths = {getattr(route, "path", "") for route in main.app.routes}
    assert "/api/learning/state" in paths
    assert json.loads(json.dumps(TestClient(main.app).get("/api/learning/state").json()))["status"]
