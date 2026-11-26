"""Wave 3 — button verification on a clean-install instance (2026-11-15).

Regression tests for the defects found by operating the app like a user on a
second instance (own data folder, port 8004).
"""
from __future__ import annotations

from pathlib import Path


# ---------------------------------------------------------------------------
# Engine orphan sweep: two instances from one code folder killed each other's
# engine every 30 s until the hourly restart limit left one of them stopped.
# ---------------------------------------------------------------------------

def test_engine_instance_tag_differs_per_data_folder(monkeypatch, tmp_path):
    from api import engine_service

    monkeypatch.setattr(engine_service, "DATA_DIR", tmp_path / "a" / "data")
    tag_a = engine_service._engine_instance_tag()
    monkeypatch.setattr(engine_service, "DATA_DIR", tmp_path / "b" / "data")
    tag_b = engine_service._engine_instance_tag()

    assert tag_a.startswith("--ms-instance=") and tag_b.startswith("--ms-instance=")
    assert tag_a != tag_b
    # Stable for the same folder (a restart must find its own orphans).
    assert engine_service._engine_instance_tag() == tag_b


def test_orphan_sweep_only_matches_this_instances_engines():
    from api import engine_service

    script = engine_service._orphan_sweep_script(Path(r"C:\proj\Market Signals"), "--ms-instance=abc123")

    assert "-m core.engine" in script
    assert "$tag = '--ms-instance=abc123'" in script
    # The tag is part of the kill filter, not only declared.
    assert '$_.CommandLine -like "*$tag*"' in script


def test_engine_subprocess_is_started_with_the_instance_tag(monkeypatch, tmp_path):
    from api import engine_service

    started = {}

    class _Proc:
        pid = 4242

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        started["cmd"] = list(cmd)
        return _Proc()

    swept = {}
    monkeypatch.setattr(engine_service, "_ENGINE_MODE", "subprocess")
    monkeypatch.setattr(engine_service, "_engine_running", False)
    monkeypatch.setattr(engine_service, "_engine_process", None)
    monkeypatch.setattr(engine_service, "DATA_DIR", tmp_path)
    monkeypatch.setattr(engine_service, "_ENGINE_PID_PATH", tmp_path / "engine.pid")
    monkeypatch.setattr(engine_service.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(engine_service, "_cleanup_orphan_engine_processes", lambda base: swept.setdefault("base", base))
    monkeypatch.setattr(engine_service, "_ensure_watchdog", lambda: None)
    monkeypatch.setattr(engine_service, "_close_engine_streams", lambda: None)
    import core.config as config
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")

    assert engine_service.start_engine() is True
    assert started["cmd"][1:3] == ["-m", "core.engine"]
    assert started["cmd"][3] == engine_service._engine_instance_tag()
    assert (tmp_path / "engine.pid").read_text(encoding="utf-8") == "4242"
    # Leave no open log handles / fake process behind for other tests.
    for stream in (engine_service._engine_process_out, engine_service._engine_process_err):
        if stream is not None:
            stream.close()
    monkeypatch.setattr(engine_service, "_engine_process_out", None)
    monkeypatch.setattr(engine_service, "_engine_process_err", None)


# ---------------------------------------------------------------------------
# Per-ticker settings: a partial save must not reset the fields it does not name
# (live on the clean install: saving the settings dialog erased the notebook).
# ---------------------------------------------------------------------------

def _config_with_ticker_settings(initial):
    from core.config import ConfigManager

    manager = object.__new__(ConfigManager)
    manager._ticker_settings = dict(initial)
    manager._saved = 0

    def _save(**_kwargs):
        manager._saved += 1

    manager._save_watchlist = _save
    return manager


def test_saving_trade_settings_keeps_the_notebook():
    notes = '[{"type":"LONG","description":"keep me","entry":"150"}]'
    manager = _config_with_ticker_settings({"NVDA": {"signals_enabled": True, "strategy_notes": notes}})

    manager.set_ticker_settings(
        "NVDA",
        {"min_win_rate": 55, "tp_pct": 4, "sl_pct": 2, "allowed_strategies": ["STRONG/PULLBACK"], "signals_enabled": True},
    )

    saved = manager._ticker_settings["NVDA"]
    assert saved["strategy_notes"] == notes
    assert saved["min_win_rate"] == 55.0 and saved["tp_pct"] == 4.0 and saved["sl_pct"] == 2.0
    assert manager._saved == 1


def test_saving_a_notebook_note_keeps_the_trade_settings():
    manager = _config_with_ticker_settings({
        "NVDA": {
            "signals_enabled": False,
            "strategy_notes": "",
            "min_win_rate": 55.0,
            "tp_pct": 4.0,
            "sl_pct": 2.0,
            "allowed_strategies": ["STRONG/PULLBACK"],
        }
    })

    manager.set_ticker_settings("NVDA", {"strategy_notes": "[]"})

    saved = manager._ticker_settings["NVDA"]
    assert saved["strategy_notes"] == "[]"
    # The owner switched signals off for this ticker: a note must not switch them back on.
    assert saved["signals_enabled"] is False
    assert saved["min_win_rate"] == 55.0 and saved["tp_pct"] == 4.0 and saved["sl_pct"] == 2.0
    assert saved["allowed_strategies"] == ["STRONG/PULLBACK"]


def test_an_explicit_empty_value_still_clears_the_field():
    manager = _config_with_ticker_settings({
        "NVDA": {"signals_enabled": True, "strategy_notes": "old", "min_win_rate": 55.0, "tp_pct": 4.0, "sl_pct": 2.0,
                 "allowed_strategies": []}
    })

    manager.set_ticker_settings("NVDA", {"tp_pct": None, "strategy_notes": ""})

    saved = manager._ticker_settings["NVDA"]
    assert saved["tp_pct"] is None and saved["strategy_notes"] == ""
    assert saved["sl_pct"] == 2.0 and saved["min_win_rate"] == 55.0


def test_first_save_for_a_ticker_gets_full_defaults():
    manager = _config_with_ticker_settings({})

    manager.set_ticker_settings("AAPL", {"strategy_notes": "n"})

    assert manager._ticker_settings["AAPL"] == {
        "signals_enabled": True, "strategy_notes": "n", "min_win_rate": 0.0,
        "tp_pct": None, "sl_pct": None, "allowed_strategies": [],
    }


# ---------------------------------------------------------------------------
# Capital: a manual open / close / stop-target edit / reset was undone on
# screen by the cached /api/state snapshot (live: a position just opened
# vanished 10 s later; after a reset the old win rate and history came back).
# ---------------------------------------------------------------------------

def test_capital_write_drops_cached_state_and_blocks_an_older_rebuild(monkeypatch):
    import json

    import api.main as main

    keys = ("api_state:False:False:False:False::False", "api_state:True:True:False:False:capital:False")
    for key in keys:
        main._endpoint_payload_set(key, b'{"capital": {"balance": 1000.0, "positions": []}}')
    main._endpoint_payload_set("scanner_state", b"{}")
    epoch = main._hotlist_membership_epoch()

    main._invalidate_capital_state_cache()

    for key in keys:
        assert main._endpoint_payload_get_stale(key) == (None, None), key
    assert main._endpoint_payload_get_stale("scanner_state")[0] is not None
    assert main._hotlist_membership_epoch() == epoch + 1

    # A rebuild that started before the write must not store its result.
    cache_key = "api_state:wave3:buttons"
    params = {
        "cache_key": cache_key, "include_flags": set(), "include_news": False, "include_hotlist": False,
        "hotlist_details": False, "lean": False, "state_live_prices": False, "capital_live_prices": False,
    }

    def build_during_a_trade(**_kwargs):
        main._invalidate_capital_state_cache()
        return {"capital": {"balance": 1000.0, "positions": []}}

    monkeypatch.setattr(main, "get_state", build_during_a_trade)
    main._build_and_cache_state_payload(params)
    assert main._endpoint_payload_get_stale(cache_key) == (None, None)

    monkeypatch.setattr(main, "get_state", lambda **_kwargs: {"capital": {"balance": 949.98}})
    main._build_and_cache_state_payload(params)
    cached, _age = main._endpoint_payload_get_stale(cache_key)
    assert json.loads(cached)["capital"]["balance"] == 949.98
    main._endpoint_payload_clear(cache_key)
    main._endpoint_payload_clear("scanner_state")


def test_every_manual_capital_write_endpoint_invalidates_the_state_snapshot():
    import inspect

    import api.main as main

    for handler in (
        main.api_reset_capital,
        main.api_open_capital_position,
        main.api_close_capital_position,
        main.api_update_capital_position_levels,
    ):
        assert "_invalidate_capital_state_cache()" in inspect.getsource(handler), handler.__name__


def test_state_snapshot_older_than_the_stale_bound_is_rebuilt_not_served(monkeypatch):
    import json
    import time

    import api.main as main

    cache_key = "api_state:True:True:False:False:capital:False"
    old = b'{"capital": {"balance": 1000.0, "positions": []}}'
    with main._ENDPOINT_PAYLOAD_CACHE_LOCK:
        main._ENDPOINT_PAYLOAD_CACHE[cache_key] = {"ts": time.time() - 600.0, "payload": old, "ttl_seconds": None}
    kicked = []
    monkeypatch.setattr(main, "_kick_state_rebuild", lambda params: kicked.append(params))
    monkeypatch.setattr(
        main, "get_capital_state",
        lambda **_kwargs: {"capital": {"balance": 949.98, "positions": [{"ticker": "BTC-USD"}]}},
    )

    result = main.api_get_state(include="capital", unchanged=False)

    body = json.loads(result.body) if hasattr(result, "body") else result
    assert body["capital"]["balance"] == 949.98        # built now, not the 10-minute-old snapshot
    assert kicked == []

    # Inside the bound the snapshot still bridges the rebuild.
    with main._ENDPOINT_PAYLOAD_CACHE_LOCK:
        main._ENDPOINT_PAYLOAD_CACHE[cache_key] = {"ts": time.time() - 15.0, "payload": old, "ttl_seconds": None}
    result = main.api_get_state(include="capital", unchanged=False)
    assert json.loads(result.body)["capital"]["balance"] == 1000.0
    assert len(kicked) == 1
    main._endpoint_payload_clear(cache_key)


# ---------------------------------------------------------------------------
# "Run signal cycle" answered "triggered" while nothing could consume the request.
# ---------------------------------------------------------------------------

def _run_cycle_with(monkeypatch, *, worker_enabled):
    import types

    import api.execution_service as execution_service
    import api.main as main
    import api.state_service as state_service
    import core.autonomous_control as autonomous_control

    controller = types.SimpleNamespace(get_status=lambda: {"running": True, "mode": "paper"})
    config = types.SimpleNamespace(agents={"execution": {"enabled": True, "autonomous_candidates_enabled": worker_enabled}})
    queued = []
    monkeypatch.setattr(main, "_get_bot_controller", lambda: controller)
    monkeypatch.setattr(state_service, "get_shared_config", lambda: config)
    monkeypatch.setattr(execution_service, "get_execution_control", lambda _config: {"readiness": {"ready": True}})
    monkeypatch.setattr(
        autonomous_control, "request_autonomous_cycle",
        lambda: queued.append("request") or {"request_id": "autonomous_test"},
    )
    return main, queued


def test_run_cycle_is_refused_when_the_candidate_worker_is_off(monkeypatch):
    import pytest
    from fastapi import HTTPException

    main, queued = _run_cycle_with(monkeypatch, worker_enabled=False)

    with pytest.raises(HTTPException) as refused:
        main.api_run_risk_manager_cycle_once()

    assert refused.value.status_code == 409
    assert "candidate worker is off" in str(refused.value.detail)
    assert queued == []


def test_run_cycle_is_queued_when_the_candidate_worker_is_on(monkeypatch):
    main, queued = _run_cycle_with(monkeypatch, worker_enabled=True)

    assert main.api_run_risk_manager_cycle_once()["request"]["request_id"] == "autonomous_test"
    assert queued == ["request"]


# ---------------------------------------------------------------------------
# Cockpit said "Paper entry ready" while the bot was stopped.
# ---------------------------------------------------------------------------

def test_cockpit_entry_blockers_name_the_master_switch_and_the_worker():
    from api.cockpit_service import _entry_switch_blockers

    stopped = _entry_switch_blockers({"bot": {"running": False}}, {"autonomous_candidates_enabled": False})
    assert stopped == [
        "Bot master switch is off",
        "Internal candidate worker is off (agents.execution.autonomous_candidates_enabled)",
    ]
    assert _entry_switch_blockers({"bot": {"running": True}}, {"autonomous_candidates_enabled": True}) == []
    # No bot status at all (agents source failed) is unknown, not "off".
    assert _entry_switch_blockers({}, {"autonomous_candidates_enabled": True}) == []


def test_candidate_worker_gate_says_where_the_setting_is():
    from api.agents_service import build_entry_gate

    gate = build_entry_gate(
        bot={"running": True, "mode": "paper"},
        engine_running=True,
        agents_config={"execution": {"enabled": True, "autonomous_candidates_enabled": False}, "risk": {"enabled": True}},
        execution_control=None,
        execution_control_error="not loaded",
    )
    worker = next(item for item in gate["gates"] if item["key"] == "candidate_worker")
    assert worker["ready"] is False
    assert "config.json" in worker["action"] and "no switch" in worker["action"]
