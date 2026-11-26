from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
import threading

import pytest

from agents import pre_earnings_agent as pe
from agents.pre_earnings_agent import PreEarningsAgent


NOW = datetime(2026, 8, 2, 15, 0, tzinfo=timezone.utc)


class _Config:
    config = {}

    def reload_if_changed(self):
        return None


class _StopOnWait:
    def __init__(self, stop_event):
        self.stop_event = stop_event
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.stop_event.set()
        return True

    def clear(self):
        return None

    def set(self):
        return None


class _UnexpectedWait:
    def wait(self, timeout=None):
        raise AssertionError(f"a due or explicit cycle unexpectedly waited {timeout}")

    def clear(self):
        return None

    def set(self):
        return None


def _loop_agent(*, items, last_success, pending=False):
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    agent._stop_event = threading.Event()
    agent._pending_refresh = pending
    agent._state = {
        "items": list(items),
        "status": {
            "last_success_time": pe._iso(last_success) if last_success else None,
        },
        "disabled": False,
    }
    agent.config_manager = _Config()
    agent._get_config = lambda: {
        "startup_delay_seconds": 0,
        "refresh_seconds": 600,
        "external_call_budget": 3,
    }
    agent._is_enabled = lambda: True
    return agent


@pytest.mark.unit
def test_fresh_persisted_restart_waits_only_remaining_interval(monkeypatch):
    agent = _loop_agent(
        items=[{"symbol": "PLTR"}],
        last_success=NOW - timedelta(seconds=125),
    )
    agent._wake_event = _StopOnWait(agent._stop_event)
    agent.run_cycle = MagicMock()
    monkeypatch.setattr(pe, "_utc_now", lambda: NOW)
    monkeypatch.setattr(pe, "track_agent_run", lambda *args, **kwargs: nullcontext())

    agent._loop()

    agent.run_cycle.assert_not_called()
    assert agent._wake_event.waits == [pytest.approx(475.0)]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("items", "last_success"),
    [
        ([{"symbol": "PLTR"}], NOW - timedelta(seconds=601)),
        ([], NOW - timedelta(seconds=10)),
        ([{"symbol": "PLTR"}], None),
    ],
)
def test_stale_empty_or_never_successful_restart_is_due_immediately(
    monkeypatch,
    items,
    last_success,
):
    agent = _loop_agent(items=items, last_success=last_success)
    agent._wake_event = _UnexpectedWait()

    def run_once():
        agent._stop_event.set()
        return {}

    agent.run_cycle = MagicMock(side_effect=run_once)
    monkeypatch.setattr(pe, "_utc_now", lambda: NOW)
    monkeypatch.setattr(pe, "track_agent_run", lambda *args, **kwargs: nullcontext())

    agent._loop()

    agent.run_cycle.assert_called_once_with()


@pytest.mark.unit
def test_explicit_refresh_bypasses_fresh_persisted_schedule(monkeypatch):
    agent = _loop_agent(
        items=[{"symbol": "PLTR"}],
        last_success=NOW - timedelta(seconds=10),
        pending=True,
    )
    agent._wake_event = _UnexpectedWait()

    def run_once():
        agent._stop_event.set()
        return {}

    agent.run_cycle = MagicMock(side_effect=run_once)
    monkeypatch.setattr(pe, "_utc_now", lambda: NOW)
    monkeypatch.setattr(pe, "track_agent_run", lambda *args, **kwargs: nullcontext())

    agent._loop()

    agent.run_cycle.assert_called_once_with()
    assert agent._pending_refresh is False


@pytest.mark.unit
def test_request_refresh_marks_pending_and_wakes_waiting_loop():
    agent = _loop_agent(
        items=[{"symbol": "PLTR"}],
        last_success=NOW - timedelta(seconds=10),
    )
    agent._wake_event = MagicMock()

    result = agent.request_refresh()

    assert result == {"status": "accepted"}
    assert agent._pending_refresh is True
    agent._wake_event.set.assert_called_once_with()


@pytest.mark.unit
def test_get_state_countdown_uses_last_success_not_process_last_run(monkeypatch):
    agent = _loop_agent(
        items=[{"symbol": "PLTR"}],
        last_success=NOW - timedelta(seconds=125),
    )
    agent._state["status"]["last_run_time"] = pe._iso(NOW)
    agent._state_read_fallback = dict(agent._state)
    agent._default_coverage_warmup_status = MagicMock(return_value={})
    agent._synchronize_direction_decision = MagicMock()
    monkeypatch.setattr(pe, "_utc_now", lambda: NOW)

    state = agent.get_state()

    assert state["status"]["next_run_in"] == 475


@pytest.mark.unit
def test_restart_schedule_uses_later_completed_snapshot_timestamp(monkeypatch):
    agent = _loop_agent(
        items=[{"symbol": "PLTR"}],
        last_success=NOW - timedelta(seconds=500),
    )
    agent._state["as_of_utc"] = pe._iso(NOW - timedelta(seconds=20))
    monkeypatch.setattr(pe, "_utc_now", lambda: NOW)

    assert agent._seconds_until_scheduled_refresh(agent._get_config()) == pytest.approx(580.0)
