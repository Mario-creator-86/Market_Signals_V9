"""Tests for core.rdagent_worker — subprocess manager for RD-Agent."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── is_enabled ─────────────────────────────────────────────────────────────────

def test_is_enabled_from_env(monkeypatch):
    """RD-Agent is enabled when RD_AGENT_ENABLED=1 in env."""
    monkeypatch.setenv("RD_AGENT_ENABLED", "1")
    from importlib import reload
    import core.rdagent_worker as rw
    reload(rw)
    assert rw.is_enabled() is True


def test_is_enabled_false_by_default(monkeypatch):
    """RD-Agent is disabled when env var is absent and config is not set."""
    monkeypatch.delenv("RD_AGENT_ENABLED", raising=False)
    import core.rdagent_worker as rw
    assert rw.is_enabled(None) is False


def test_is_enabled_from_config():
    """RD-Agent is enabled when config rdagent.enabled is True."""
    import core.rdagent_worker as rw

    class FakeConfig:
        config = {"rdagent": {"enabled": True}}

    assert rw.is_enabled(FakeConfig()) is True


# ── _endpoint ──────────────────────────────────────────────────────────────────

def test_endpoint_default(monkeypatch):
    monkeypatch.delenv("RD_AGENT_ENDPOINT", raising=False)
    import core.rdagent_worker as rw
    assert rw._endpoint() == "http://127.0.0.1:19000"


def test_endpoint_from_env(monkeypatch):
    monkeypatch.setenv("RD_AGENT_ENDPOINT", "http://10.0.0.1:20000")
    import core.rdagent_worker as rw
    assert rw._endpoint() == "http://10.0.0.1:20000"


# ── probe_rdagent ──────────────────────────────────────────────────────────────

def test_probe_rdagent_false_when_disabled(monkeypatch):
    monkeypatch.delenv("RD_AGENT_ENABLED", raising=False)
    import core.rdagent_worker as rw
    assert rw.probe_rdagent(None) is False


def test_probe_rdagent_false_when_endpoint_down(monkeypatch):
    monkeypatch.setenv("RD_AGENT_ENABLED", "1")
    monkeypatch.setenv("RD_AGENT_ENDPOINT", "http://127.0.0.1:59999")
    import core.rdagent_worker as rw
    result = rw.probe_rdagent(None, timeout=0.5)
    assert result is False


# ── ensure_rdagent_worker ──────────────────────────────────────────────────────

def test_ensure_rdagent_worker_disabled(monkeypatch):
    """Returns not_enabled result when RD-Agent is not configured."""
    monkeypatch.delenv("RD_AGENT_ENABLED", raising=False)
    import core.rdagent_worker as rw
    result = rw.ensure_rdagent_worker(None)
    assert result["enabled"] is False
    assert result["started"] is False
    assert result["healthy"] is False


def test_ensure_rdagent_worker_no_python310(monkeypatch, tmp_path):
    """Returns UNAVAILABLE detail when Python 3.10 is not found."""
    monkeypatch.setenv("RD_AGENT_ENABLED", "1")
    monkeypatch.setenv("RD_AGENT_ENDPOINT", "http://127.0.0.1:59997")

    import core.rdagent_worker as rw
    # Patch probe to always return False (not running)
    monkeypatch.setattr(rw, "probe_rdagent", lambda *a, **k: False)
    # Patch _python310 to return None (not found)
    monkeypatch.setattr(rw, "_python310", lambda *a: None)

    result = rw.ensure_rdagent_worker(None)
    assert result["enabled"] is True
    assert result["healthy"] is False
    assert "Python 3.10" in result["detail"] or "python" in result["detail"].lower()


def test_ensure_rdagent_worker_already_healthy(monkeypatch):
    """Returns healthy immediately when probe succeeds (already running)."""
    monkeypatch.setenv("RD_AGENT_ENABLED", "1")
    import core.rdagent_worker as rw
    monkeypatch.setattr(rw, "probe_rdagent", lambda *a, **k: True)

    result = rw.ensure_rdagent_worker(None)
    assert result["enabled"] is True
    assert result["healthy"] is True
    assert result["started"] is False  # not started by us


# ── stop_rdagent_worker ────────────────────────────────────────────────────────

def test_stop_rdagent_worker_no_process():
    """stop_rdagent_worker returns False when no process was started."""
    import core.rdagent_worker as rw
    rw._RDAGENT_PROCESS = None
    assert rw.stop_rdagent_worker() is False


# ── status persistence ─────────────────────────────────────────────────────────

def test_write_and_read_status(tmp_path, monkeypatch):
    """Status is written to and read from the status JSON file."""
    import core.rdagent_worker as rw
    monkeypatch.setattr(rw, "STATUS_PATH", tmp_path / "rdagent_status.json")

    rw._write_status("HEALTHY", "test detail", pid=1234, endpoint="http://x:1")
    status = rw.read_status()
    assert status["status"] == "HEALTHY"
    assert status["detail"] == "test detail"
    assert status["pid"] == 1234


def test_read_status_returns_unknown_when_missing(tmp_path, monkeypatch):
    """read_status returns UNKNOWN when status file does not exist."""
    import core.rdagent_worker as rw
    monkeypatch.setattr(rw, "STATUS_PATH", tmp_path / "nonexistent.json")
    status = rw.read_status()
    assert status["status"] == "UNKNOWN"
