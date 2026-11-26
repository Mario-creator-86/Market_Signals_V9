"""Lifecycle regressions for the Agent Fleet runtime reconciler."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from api.agent_runtime_service import get_agent_runtime_status, set_agent_runtime


class _FakeWorker:
    def __init__(self):
        self.running = False
        self.starts = 0
        self.stops = 0

    def start(self):
        self.starts += 1
        self.running = True

    def stop(self):
        self.stops += 1
        self.running = False

    def get_runtime_status(self):
        return {"running": self.running, "next_run_in": 600 if self.running else None}


def _config(name: str, enabled: bool = True):
    return SimpleNamespace(config={"agents": {name: {"enabled": enabled}}})


@pytest.mark.parametrize(
    ("name", "module_name", "getter", "ready_getter"),
    [
        ("trade_radar", "agents.trade_radar_agent", "get_trade_radar_agent", "get_trade_radar_agent_if_ready"),
        (
            "strategy_research",
            "agents.strategy_research_agent",
            "get_strategy_research_agent",
            "get_strategy_research_agent_if_ready",
        ),
        (
            "premarket_scanner",
            "agents.premarket_scanner_agent",
            "get_premarket_scanner_agent",
            "get_premarket_scanner_agent_if_ready",
        ),
        ("pre_earnings", "agents.pre_earnings_agent", "get_pre_earnings_agent", "get_pre_earnings_agent_if_ready"),
        (
            "economic_calendar",
            "agents.economic_calendar_agent",
            "get_economic_calendar_agent",
            "get_economic_calendar_agent_if_ready",
        ),
    ],
)
def test_local_loop_on_starts_and_off_stops_without_duplicate_instance(
    monkeypatch, name, module_name, getter, ready_getter
):
    worker = _FakeWorker()
    module = ModuleType(module_name)
    setattr(module, getter, lambda *_args, **_kwargs: worker)
    setattr(module, ready_getter, lambda: worker)
    monkeypatch.setitem(sys.modules, module_name, module)
    config = _config(name)

    started = set_agent_runtime(
        name,
        True,
        config=config,
        engine_running=lambda: True,
        start_engine=lambda: True,
    )
    stopped = set_agent_runtime(
        name,
        False,
        config=_config(name, enabled=False),
        engine_running=lambda: True,
        start_engine=lambda: True,
    )

    assert started["running"] is True
    assert started["lifecycle_state"] == "running"
    assert stopped["running"] is False
    assert worker.starts == 1
    assert worker.stops == 1


def test_engine_agent_start_does_not_start_trading_bot():
    engine = {"running": False, "starts": 0}

    def start_engine():
        engine["starts"] += 1
        engine["running"] = True
        return True

    status = set_agent_runtime(
        "execution",
        True,
        config=_config("execution"),
        engine_running=lambda: engine["running"],
        start_engine=start_engine,
    )

    assert status == {
        "enabled": True,
        "running": True,
        "initializing": False,
        "ready": True,
        "lifecycle_state": "running",
        "managed_by": "engine",
    }
    assert engine["starts"] == 1


def test_thread_liveness_wins_over_old_or_missing_run_timestamp(monkeypatch):
    worker = _FakeWorker()
    worker.running = True
    module_name = "agents.pre_earnings_agent"
    module = ModuleType(module_name)
    module.get_pre_earnings_agent_if_ready = lambda: worker
    monkeypatch.setitem(sys.modules, module_name, module)

    status = get_agent_runtime_status(
        "pre_earnings",
        config=_config("pre_earnings"),
        engine_running=True,
    )

    assert status["running"] is True
    assert status["lifecycle_state"] == "running"
    assert "last_run" not in status


def test_coding_agent_status_comes_from_real_repair_watchdog(monkeypatch):
    module_name = "core.development_factory.watchdog"
    module = ModuleType(module_name)
    module.get_repair_watchdog = lambda *_args, **_kwargs: SimpleNamespace(
        status=lambda: {"running": False, "last_error": ""}
    )
    monkeypatch.setitem(sys.modules, module_name, module)

    status = get_agent_runtime_status(
        "coding_agent",
        config=_config("coding_agent"),
        engine_running=True,
    )

    assert status["running"] is False
    assert status["lifecycle_state"] == "stopped"
    assert status["managed_by"] == "api"
    assert status["runtime_component"] == "repair_watchdog"
    assert status["shared_runtime"] is True
    assert status["runtime_role"] == "implementation"


def test_engine_runtime_prefers_observed_agent_liveness():
    from api.agent_runtime_service import snapshot_agent_runtimes

    result = snapshot_agent_runtimes(
        config=_config("scanner"),
        observed_status={"scanner": {"running": False, "last_error": "worker stopped"}},
        engine_running=True,
        agent_names=["scanner"],
    )

    assert result["scanner"]["running"] is False
    assert result["scanner"]["ready"] is False
    assert result["scanner"]["liveness_source"] == "observed_agent_status"
