"""Phase 14 unit tests — §111 automatic research-cycle scheduler + API wiring.

Guarantees under test:
- config parsing is tolerant (missing section → disabled, never crashes)
- interval/experiment bounds are clamped so misconfiguration cannot hammer journals
- the scheduler survives a crashing cycle (fail-open) and records last_error
- lifecycle: start is idempotent, stop is safe, run-now uses the same bounded
  pipeline as POST /api/research/run-cycle
- API endpoints expose status/start/stop/run without any trading capability
"""
import threading
import time

import pytest

from core.research_factory import scheduler as sched_mod
from core.research_factory.scheduler import (
    DEFAULT_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    ResearchCycleScheduler,
    auto_cycle_config,
    get_research_scheduler,
    reset_research_scheduler_singleton,
    shutdown_research_scheduler,
)


@pytest.fixture(autouse=True)
def _clean_singleton():
    reset_research_scheduler_singleton()
    yield
    reset_research_scheduler_singleton()


def _fake_cycle_result(**overrides):
    base = {
        "weaknesses_detected": 1,
        "experiments": [{"experiment_id": "exp_1", "final_status": "SHADOW"}],
        "status_counts": {"SHADOW": 1},
        "drift": {"revalidations": []},
        "notes": "research-only: no promotion",
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------ config

def test_auto_cycle_config_defaults_when_missing():
    cfg = auto_cycle_config(None)
    assert cfg == {
        "enabled": False,
        "interval_seconds": DEFAULT_INTERVAL_SECONDS,
        "max_experiments": 3,
        "startup_delay_seconds": 60,
    }


def test_auto_cycle_config_reads_configmanager_shape():
    class FakeConfigManager:
        config = {
            "research_factory": {
                "auto_cycle": {
                    "enabled": True,
                    "interval_seconds": 3600,
                    "max_experiments": 2,
                    "startup_delay_seconds": 5,
                }
            }
        }

    cfg = auto_cycle_config(FakeConfigManager())
    assert cfg == {
        "enabled": True,
        "interval_seconds": 3600,
        "max_experiments": 2,
        "startup_delay_seconds": 5,
    }


def test_auto_cycle_config_accepts_plain_dict():
    cfg = auto_cycle_config({"research_factory": {"auto_cycle": {"enabled": "true"}}})
    assert cfg["enabled"] is True


def test_example_config_ships_auto_cycle_disabled():
    import json

    root = pytest.root_path if hasattr(pytest, "root_path") else None
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent.parent / "config.example.json"
    example = json.loads(path.read_text(encoding="utf-8"))
    section = example.get("research_factory", {}).get("auto_cycle")
    assert section is not None, "config.example.json must document research_factory.auto_cycle"
    assert section.get("enabled") is False, "default must be OFF (owner-gated §111)"
    assert section.get("interval_seconds", 0) >= MIN_INTERVAL_SECONDS


# -------------------------------------------------------------- constructor

def test_constructor_clamps_unsafe_values():
    s = ResearchCycleScheduler(interval_seconds=10, max_experiments=99, startup_delay=-5)
    assert s.interval_seconds == MIN_INTERVAL_SECONDS
    assert s.max_experiments == 5
    assert s.startup_delay == 0


def test_constructor_keeps_sane_values():
    s = ResearchCycleScheduler(interval_seconds=7200, max_experiments=2, startup_delay=30)
    assert s.interval_seconds == 7200
    assert s.max_experiments == 2
    assert s.startup_delay == 30


# ---------------------------------------------------------------- lifecycle

def test_start_stop_lifecycle():
    s = ResearchCycleScheduler(startup_delay=3600)
    assert s.status()["running"] is False
    assert s.start() is True
    assert s.status()["running"] is True
    # idempotent start
    assert s.start() is False
    assert s.stop() is True
    assert s.status()["running"] is False
    assert s.status()["enabled"] is False


def test_stop_when_never_started_is_safe():
    s = ResearchCycleScheduler()
    assert s.stop() is False


# ------------------------------------------------------------- cycle plumbing

def test_run_once_now_records_success(monkeypatch):
    calls = []

    def fake_cycle(**kwargs):
        calls.append(kwargs)
        return _fake_cycle_result()

    monkeypatch.setattr("core.research_factory.pipeline.run_research_cycle", fake_cycle)
    s = ResearchCycleScheduler()
    result = s.run_once_now()
    assert result["weaknesses_detected"] == 1
    status = s.status()
    assert status["cycles_completed"] == 1
    assert status["last_error"] == ""
    assert status["last_result"]["experiments"] == 1
    assert status["last_result"]["status_counts"] == {"SHADOW": 1}
    # bounded experiments honored
    assert calls[0]["max_experiments"] == 3


def test_run_once_now_fail_open_records_error(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("journal corrupted")

    monkeypatch.setattr("core.research_factory.pipeline.run_research_cycle", boom)
    s = ResearchCycleScheduler()
    with pytest.raises(RuntimeError):
        s.run_once_now()
    status = s.status()
    assert status["cycles_completed"] == 0
    assert "RuntimeError" in status["last_error"]
    assert status["last_run"] is not None


def test_loop_survives_crash_and_scheduler_keeps_working(monkeypatch):
    state = {"calls": 0, "lock": threading.Lock()}

    def crashing_cycle(**kwargs):
        with state["lock"]:
            state["calls"] += 1
        raise ValueError("journal corrupted")

    monkeypatch.setattr("core.research_factory.pipeline.run_research_cycle", crashing_cycle)
    s = ResearchCycleScheduler(interval_seconds=7200, startup_delay=0)
    assert s.start() is True

    # wait for the first (crashing) cycle to be attempted
    deadline = time.time() + 10
    while time.time() < deadline:
        with state["lock"]:
            if state["calls"] >= 1:
                break
        time.sleep(0.05)
    with state["lock"]:
        assert state["calls"] == 1

    # fail-open: the daemon thread is still alive after the crash
    assert s._thread is not None and s._thread.is_alive()
    status = s.status()
    assert status["running"] is True
    assert "ValueError" in status["last_error"]

    # the same scheduler object still completes a cycle once the fault clears
    monkeypatch.setattr(
        "core.research_factory.pipeline.run_research_cycle",
        lambda **kwargs: _fake_cycle_result(),
    )
    s.run_once_now()
    status = s.status()
    assert status["cycles_completed"] == 1
    assert status["last_error"] == ""

    assert s.stop() is True


# ----------------------------------------------------------------- singleton

def test_singleton_seeded_from_config_and_shutdown():
    class FakeConfigManager:
        config = {"research_factory": {"auto_cycle": {
            "enabled": True, "interval_seconds": 5400, "max_experiments": 4,
        }}}

    first = get_research_scheduler(FakeConfigManager())
    again = get_research_scheduler(None)
    assert first is again
    assert first.interval_seconds == 5400
    assert first.max_experiments == 4

    assert shutdown_research_scheduler() is True
    assert shutdown_research_scheduler() is False  # idempotent
    # a fresh singleton is rebuilt from config on next request
    third = get_research_scheduler(FakeConfigManager())
    assert third is not first


# ------------------------------------------------------------------ service

def test_service_auto_cycle_functions(monkeypatch):
    from api import research_factory_service as svc

    monkeypatch.setattr(svc, "_runtime_config", lambda: None)
    monkeypatch.setattr(
        "core.research_factory.pipeline.run_research_cycle",
        lambda **kwargs: _fake_cycle_result(),
    )

    status = svc.auto_cycle_status()
    assert status["running"] is False
    assert "no promotion" in status["note"]

    started = svc.auto_cycle_start()
    assert started["started"] is True
    assert started["running"] is True

    run = svc.auto_cycle_run_now()
    assert run["cycles_completed"] == 1
    assert run["result"]["experiments"] == 1

    stopped = svc.auto_cycle_stop()
    assert stopped["stopped"] is True
    assert stopped["running"] is False

    shutdown = svc.auto_cycle_shutdown()
    assert shutdown == {"stopped": True}


def test_service_startup_gated_by_config(monkeypatch):
    from api import research_factory_service as svc

    monkeypatch.setattr(svc, "_runtime_config", lambda: None)
    assert svc.auto_cycle_startup() == {"started": False, "enabled": False}

    class Enabled:
        config = {"research_factory": {"auto_cycle": {
            "enabled": True, "interval_seconds": 7200, "max_experiments": 3,
            "startup_delay_seconds": 3600,
        }}}

    monkeypatch.setattr(svc, "_runtime_config", lambda: Enabled())
    try:
        result = svc.auto_cycle_startup()
        assert result == {"started": True, "enabled": True}
    finally:
        shutdown_research_scheduler()


def test_service_startup_repairs_agent_on_scheduler_off_split_brain(monkeypatch):
    from api import research_factory_service as svc

    updates = []

    class SplitBrainConfig:
        config = {
            "agents": {"research_factory": {"enabled": True}},
            "research_factory": {"auto_cycle": {"enabled": False}},
        }

        def update_values(self, values):
            updates.extend(values)

    class FakeScheduler:
        def __init__(self):
            self.running = False

        def start(self):
            self.running = True
            return True

        def status(self):
            return {"running": self.running}

    config = SplitBrainConfig()
    scheduler = FakeScheduler()
    monkeypatch.setattr(svc, "_runtime_config", lambda: config)
    monkeypatch.setattr(svc, "_scheduler", lambda: scheduler)

    result = svc.auto_cycle_startup()

    assert result == {"started": True, "enabled": True}
    assert (["agents", "research_factory", "enabled"], True) in updates
    assert (["research_factory", "auto_cycle", "enabled"], True) in updates


def test_service_stop_keeps_agent_toggle_and_scheduler_intent_aligned(monkeypatch):
    from api import research_factory_service as svc

    updates = []

    class Config:
        config = {}

        def update_values(self, values):
            updates.extend(values)

    class FakeScheduler:
        def stop(self):
            return True

        def status(self):
            return {"running": False, "enabled": False}

    monkeypatch.setattr(svc, "_runtime_config", lambda: Config())
    monkeypatch.setattr(svc, "_scheduler", lambda: FakeScheduler())

    result = svc.auto_cycle_stop()

    assert result["stopped"] is True
    assert (["agents", "research_factory", "enabled"], False) in updates
    assert (["research_factory", "auto_cycle", "enabled"], False) in updates


# --------------------------------------------------------------- API layer

def test_api_auto_cycle_endpoints(client, monkeypatch):
    monkeypatch.setattr(
        "core.research_factory.pipeline.run_research_cycle",
        lambda **kwargs: _fake_cycle_result(),
    )

    r = client.get("/api/research/auto-cycle")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is False
    assert "note" in body

    r = client.post("/api/research/auto-cycle/start")
    assert r.status_code == 200
    assert r.json()["started"] is True

    r = client.post("/api/research/auto-cycle/run")
    assert r.status_code == 200
    body = r.json()
    assert body["cycles_completed"] == 1
    assert body["result"]["status_counts"] == {"SHADOW": 1}

    r = client.post("/api/research/auto-cycle/stop")
    assert r.status_code == 200
    assert r.json()["stopped"] is True


def test_api_auto_cycle_status_schema(client):
    body = client.get("/api/research/auto-cycle").json()
    for key in ("running", "enabled", "interval_seconds", "max_experiments",
                "cycles_completed", "last_run", "last_result", "last_error"):
        assert key in body, f"missing status key: {key}"


def test_cycle_includes_lifecycle_advancement(monkeypatch, tmp_path):
    """The scheduled cycle also advances the Strategy Registry lifecycle on
    accumulated evidence (RESEARCH->CHALLENGER->SHADOW; never CHAMPION)."""
    monkeypatch.setenv("MS_RESEARCH_HYPOTHESES_PATH", str(tmp_path / "h.json"))
    monkeypatch.setenv("MS_RESEARCH_EXPERIMENTS_PATH", str(tmp_path / "e.json"))
    monkeypatch.setattr(
        "core.research_factory.pipeline.run_research_cycle",
        lambda **kwargs: {"weaknesses_detected": 0, "experiments": [], "status_counts": {}, "drift": {}},
    )
    advanced = [{"strategy_id": "S1_TREND_PULLBACK", "from": "RESEARCH", "to": "CHALLENGER"}]
    monkeypatch.setattr(
        "core.research_factory.lifecycle_advance.advance_registry_lifecycles",
        lambda registry=None: advanced,
    )
    scheduler = ResearchCycleScheduler(startup_delay=0)
    result = scheduler.run_once_now()
    assert result["lifecycle_advancements"] == advanced
    assert scheduler.status()["last_result"]["lifecycle_advancements"] == 1
