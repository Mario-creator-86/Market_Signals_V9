"""Continuous staged Pre-Earnings scanner tests."""
from datetime import datetime, timedelta, timezone

import pytest

from core.pre_earnings.continuous import (
    DEFAULT_INTERVAL_SECONDS, DEFAULT_REFRESH_WAIT_SECONDS, MIN_INTERVAL_SECONDS,
    ContinuousPreEarningsScanner,
    continuous_scan_config,
)


class _Cfg:
    def __init__(self, section):
        self.config = {"pre_earnings": {"continuous_scan": section}}


def test_config_parsing_tolerant():
    assert continuous_scan_config(_Cfg({"enabled": True, "interval_seconds": 60})) == {
        "enabled": True, "interval_seconds": 60,
        "refresh_wait_seconds": DEFAULT_REFRESH_WAIT_SECONDS,
    }
    assert continuous_scan_config(_Cfg(None)) == {
        "enabled": False, "interval_seconds": DEFAULT_INTERVAL_SECONDS,
        "refresh_wait_seconds": DEFAULT_REFRESH_WAIT_SECONDS,
    }
    assert continuous_scan_config(None)["enabled"] is False


def test_interval_floor():
    assert ContinuousPreEarningsScanner(interval_seconds=5).interval_seconds == MIN_INTERVAL_SECONDS


class _FakeAgent:
    def __init__(self, symbols):
        self._state = {"items": [{"symbol": s} for s in symbols]}
        self.refreshed = []
        self.ai_ran = []

    def refresh_symbol_analysis(self, symbol):
        self.refreshed.append(symbol)
        return {"status": "ok"}

    def force_ai_for_symbol(self, symbol):
        self.ai_ran.append(symbol)
        return {"status": "ok", "ai_decision": {"verdict": "LONG", "confidence": 0.7}}


def test_scan_next_completes_each_candidate_before_new_cycle(monkeypatch):
    agent = _FakeAgent(["AAA", "BBB", "CCC"])
    monkeypatch.setattr("agents.pre_earnings_agent.get_pre_earnings_agent", lambda: agent)
    scanner = ContinuousPreEarningsScanner(startup_delay=0)

    first = scanner.scan_next()
    assert first["symbol"] == "AAA" and first["data"] == "ok" and first["ai"] == "ok"
    assert first["verdict"] == "LONG"

    scanner.scan_next()
    third = scanner.scan_next()
    assert agent.refreshed == ["AAA", "BBB", "CCC"] == agent.ai_ran
    # wraps around continuously
    fourth = scanner.scan_next()
    assert fourth["symbol"] == "AAA"

    status = scanner.status()
    assert status["scanned"] == 4 and status["ai_verdicts"] == 4
    assert status["last_symbol"] == "AAA" and status["last_error"] == ""
    assert status["completed_cycles"] == 1


def test_active_window_is_scanned_nearest_first(monkeypatch):
    agent = _FakeAgent([])
    agent._state = {
        "calendar_universe": {
            "window_start": "2026-08-30",
            "window_end": "2026-09-29",
        },
        "items": [
            {"symbol": "LATE", "earnings_time_utc": "2026-09-20T20:00:00Z"},
            {"symbol": "OLD", "earnings_time_utc": "2026-08-29T20:00:00Z"},
            {"symbol": "NEAR", "earnings_time_utc": "2026-09-01T20:00:00Z"},
            {"symbol": "VERIFY", "earnings_time_utc": "2026-09-02T20:00:00Z", "event_status": "DATE_CONFLICT"},
            {"symbol": "DONE", "earnings_time_utc": "2026-09-02T20:00:00Z", "event_status": "REPORTED"},
        ],
    }
    monkeypatch.setattr("agents.pre_earnings_agent.get_pre_earnings_agent", lambda: agent)
    scanner = ContinuousPreEarningsScanner(startup_delay=0)

    assert scanner.scan_next()["symbol"] == "NEAR"
    assert scanner.scan_next()["symbol"] == "VERIFY"
    assert scanner.scan_next()["symbol"] == "LATE"
    assert agent.ai_ran == ["NEAR", "VERIFY", "LATE"]


def test_waits_for_persisted_refresh_before_ai(monkeypatch):
    class _AsyncAgent(_FakeAgent):
        def __init__(self):
            super().__init__(["AAA"])
            self.completed_at = None

        def refresh_symbol_analysis(self, symbol):
            self.refreshed.append(symbol)
            self.completed_at = datetime.now(timezone.utc) + timedelta(milliseconds=1)
            return {"status": "ok", "coverage": {"status": "accepted"}}

        def get_refresh_status(self):
            return {"status": {
                "last_targeted_rescore_symbol": "AAA",
                "last_targeted_rescore_time": self.completed_at.isoformat(),
            }}

        def get_symbol_analysis_snapshot(self, symbol):
            return {
                "status": "ok",
                "symbol": symbol,
                "data_verified_at": self.completed_at.isoformat(),
                "enrichment_value_sources": {"news": "provider"},
            }

        def force_ai_for_symbol(self, symbol):
            assert self.completed_at is not None
            self.ai_ran.append(symbol)
            return {"status": "ok", "ai_decision": {
                "verdict": "LONG",
                "confidence": 0.7,
                "generated_at": (self.completed_at + timedelta(milliseconds=1)).isoformat(),
            }}

    agent = _AsyncAgent()
    monkeypatch.setattr("agents.pre_earnings_agent.get_pre_earnings_agent", lambda: agent)
    scanner = ContinuousPreEarningsScanner(startup_delay=0)

    outcome = scanner.scan_next()
    assert outcome["data"] == "fresh"
    assert outcome["ai"] == "ok"
    assert outcome["fresh_sequence_verified"] is True
    assert outcome["data_sources"] == {"news": "provider"}


def test_checkpoint_resumes_after_restart(monkeypatch, tmp_path):
    agent = _FakeAgent(["AAA", "BBB", "CCC"])
    monkeypatch.setattr("agents.pre_earnings_agent.get_pre_earnings_agent", lambda: agent)
    checkpoint = tmp_path / "continuous.json"

    first = ContinuousPreEarningsScanner(startup_delay=0, checkpoint_path=checkpoint)
    assert first.scan_next()["symbol"] == "AAA"

    resumed = ContinuousPreEarningsScanner(startup_delay=0, checkpoint_path=checkpoint)
    assert resumed.scan_next()["symbol"] == "BBB"
    assert resumed.status()["cycle_processed"] == 2


def test_state_metadata_defines_exact_30_day_window(monkeypatch):
    agent = _FakeAgent([])
    agent._state = {
        "as_of_utc": "2026-08-30T18:00:00Z",
        "lookahead_days": 30,
        "items": [
            {"symbol": "IN", "earnings_time_utc": "2026-09-29T23:59:59-04:00"},
            {"symbol": "OUT", "earnings_time_utc": "2026-09-30T00:00:00-04:00"},
        ],
    }
    monkeypatch.setattr("agents.pre_earnings_agent.get_pre_earnings_agent", lambda: agent)
    scanner = ContinuousPreEarningsScanner(startup_delay=0)

    assert scanner.scan_next()["symbol"] == "IN"
    assert scanner.status()["cycle_total"] == 1


def test_concurrent_trigger_does_not_duplicate_current_symbol(monkeypatch):
    agent = _FakeAgent(["AAA"])
    monkeypatch.setattr("agents.pre_earnings_agent.get_pre_earnings_agent", lambda: agent)
    scanner = ContinuousPreEarningsScanner(startup_delay=0)

    assert scanner._scan_lock.acquire(blocking=False) is True
    try:
        outcome = scanner.scan_next()
    finally:
        scanner._scan_lock.release()
    assert outcome["error"] == "scan_already_running"
    assert agent.refreshed == [] and agent.ai_ran == []


def test_scan_survives_agent_failures(monkeypatch):
    class _BrokenAI(_FakeAgent):
        def force_ai_for_symbol(self, symbol):
            raise RuntimeError("ollama down")

    agent = _BrokenAI(["XXX"])
    monkeypatch.setattr("agents.pre_earnings_agent.get_pre_earnings_agent", lambda: agent)
    scanner = ContinuousPreEarningsScanner(startup_delay=0)
    outcome = scanner.scan_next()
    assert outcome["data"] == "ok" and outcome["ai"] == "error"
    assert "ai:" in outcome["error"]
    # the scanner stays usable for the next interval (fail-open)
    assert scanner.status()["scanned"] == 1


def test_scan_with_empty_universe_is_honest(monkeypatch):
    agent = _FakeAgent([])
    monkeypatch.setattr("agents.pre_earnings_agent.get_pre_earnings_agent", lambda: agent)
    scanner = ContinuousPreEarningsScanner(startup_delay=0)
    outcome = scanner.scan_next()
    assert outcome["ok"] is False and outcome["error"] == "no_pre_earnings_universe"
    assert scanner.status()["cycle_processed"] == 0


def test_start_stop_lifecycle():
    scanner = ContinuousPreEarningsScanner(startup_delay=0)
    assert scanner.start() is True
    assert scanner.status()["running"] is True
    assert scanner.stop() is True
    assert scanner.status()["running"] is False


def test_api_endpoints(client, monkeypatch):
    agent = _FakeAgent(["AAA"])
    monkeypatch.setattr("agents.pre_earnings_agent.get_pre_earnings_agent", lambda: agent)

    r = client.get("/api/pre-earnings/continuous-scan")
    assert r.status_code == 200
    assert "no trading capability" in r.json()["note"]

    r = client.post("/api/pre-earnings/continuous-scan/once")
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"]["symbol"] == "AAA" and body["outcome"]["ai"] == "ok"
