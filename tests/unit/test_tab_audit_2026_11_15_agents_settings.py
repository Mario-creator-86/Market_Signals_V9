"""Regression tests for the 2026-11-15 audit of the Agents tab and Global Settings.

Each test pins one defect that was confirmed in the running app:

* the Agents tab said "entries enabled" on the Execution/Risk cards while the
  paper broker refused every new entry (EXECUTION_STATE_CONFLICT);
* engine-managed cards showed "Engine managed / Not measured" although the
  engine publishes heartbeats and run durations, and "0 need attention" could
  not notice a worker that stopped completing cycles;
* GET /api/config returned broker/messenger secrets in full, and masked the
  *name* of each AI provider's environment variable, so every /api/ai/* route
  resolved provider keys through the variable "***REDACTED***";
* POST /api/config accepted NaN / out-of-range values and reported success even
  when nothing reached the disk.
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import api.agent_runtime_service as runtime_service
import api.config_service as config_service
import core.config as config_module
from api.agents_service import build_control_plane_payload, build_entry_gate


# ── helpers ──────────────────────────────────────────────────────────────────

def _config(**execution):
    execution_cfg = {"enabled": True, "mode": "paper", "autonomous_candidates_enabled": True}
    execution_cfg.update(execution)
    return SimpleNamespace(
        agents={"execution": execution_cfg, "risk": {"enabled": True}},
        config={},
        news={},
        strategy_v2={},
        bot={"enabled": True, "mode": "paper"},
    )


def _control(*, ready, reasons=(), status="HEALTHY", kill=False, mode="PAPER_AUTO", conflicts=()):
    return {
        "readiness": {
            "ready": ready,
            "mode": mode,
            "reasons": list(reasons),
            "kill_switch_active": kill,
            "risk_constitution_active": True,
            "reduce_fraction_pct": 20.0,
            "reconciliation_status": status,
        },
        "reconciliation": {"status": status, "conflicts": list(conflicts)},
    }


_RUNNING_BOT = {"running": True, "mode": "paper", "circuit_breaker_active": False}
_WORKERS = {"execution": {"running": True}, "risk": {"running": True}}


# ── Agents tab: one truth for "can a new entry open" ─────────────────────────

def test_cards_do_not_claim_entries_enabled_while_broker_refuses_new_risk():
    control = _control(
        ready=False,
        reasons=["EXECUTION_STATE_CONFLICT must be resolved"],
        status="EXECUTION_STATE_CONFLICT",
        conflicts=[{
            "code": "filled_order_position_missing",
            "order_intent_id": "ord_1",
            "position_id": "pos_1",
        }],
    )
    payload = build_control_plane_payload(
        config=_config(),
        bot_status=_RUNNING_BOT,
        agents_status=_WORKERS,
        engine_running=True,
        execution_control=control,
    )

    for name in ("execution", "risk"):
        assert payload["agents"][name]["accepting_new_entries"] is False
        assert payload["agents"][name]["operating_mode"] == "monitoring_only"
        assert payload["agents"][name]["entry_block_reason"] == "Paper broker reconciliation"
    assert payload["bot"]["entries_enabled"] is False
    assert payload["bot"]["autonomous_execution_active"] is False

    gate = payload["entry_gate"]
    assert gate["open"] is False and gate["status"] == "blocked"
    assert [item["key"] for item in gate["blockers"]] == ["broker_reconciliation"]
    blocker = gate["blockers"][0]
    # The failing gate is named, with the evidence and the action that clears it.
    assert "EXECUTION_STATE_CONFLICT" in blocker["detail"]
    assert "ord_1" in blocker["detail"] and "pos_1" in blocker["detail"]
    assert "Reconcile broker" in blocker["action"]
    assert gate["ready_count"] == gate["total"] - 1
    assert "Paper broker reconciliation" in gate["summary"]


def test_entries_enabled_only_when_every_gate_passes():
    payload = build_control_plane_payload(
        config=_config(),
        bot_status=_RUNNING_BOT,
        agents_status=_WORKERS,
        engine_running=True,
        execution_control=_control(ready=True),
    )
    gate = payload["entry_gate"]
    assert gate["open"] is True and gate["blockers"] == []
    assert all(item["action"] == "" for item in gate["gates"])
    assert payload["agents"]["execution"]["accepting_new_entries"] is True
    assert payload["agents"]["risk"]["operating_mode"] == "entry_enabled"
    assert "entry_block_reason" not in payload["agents"]["execution"]


def test_unverifiable_readiness_is_not_reported_as_enabled():
    payload = build_control_plane_payload(
        config=_config(),
        bot_status=_RUNNING_BOT,
        agents_status=_WORKERS,
        engine_running=True,
        execution_control=None,
        execution_control_error="paper-entry readiness unavailable",
    )
    gate = payload["entry_gate"]
    assert gate["status"] == "unverified" and gate["policy_verified"] is False
    assert payload["agents"]["execution"]["accepting_new_entries"] is False


@pytest.mark.parametrize(
    "bot, config, control, expected_key",
    [
        ({**_RUNNING_BOT, "running": False}, _config(), _control(ready=True), "master_switch"),
        ({**_RUNNING_BOT, "circuit_breaker_active": True}, _config(), _control(ready=True), "circuit_breaker"),
        (_RUNNING_BOT, _config(autonomous_candidates_enabled=False), _control(ready=True), "candidate_worker"),
        (
            _RUNNING_BOT,
            _config(),
            _control(ready=False, kill=True, reasons=["STOP AUTONOMOUS TRADING is active"]),
            "kill_switch",
        ),
        (
            _RUNNING_BOT,
            _config(),
            _control(ready=False, mode="ANALYZE", reasons=["owner must select PAPER_AUTO"]),
            "execution_mode",
        ),
        (
            _RUNNING_BOT,
            _config(),
            _control(ready=False, status="UNVERIFIED", reasons=["paper broker reconciliation has not passed"]),
            "broker_reconciliation",
        ),
    ],
)
def test_each_blocking_gate_is_named_with_an_action(bot, config, control, expected_key):
    gate = build_entry_gate(
        bot=bot,
        engine_running=True,
        agents_config=config.agents,
        execution_control=control,
    )
    assert gate["open"] is False
    assert [item["key"] for item in gate["blockers"]] == [expected_key]
    assert gate["blockers"][0]["action"].strip()


def test_unknown_readiness_reason_still_blocks_and_is_shown_verbatim():
    gate = build_entry_gate(
        bot=_RUNNING_BOT,
        engine_running=True,
        agents_config=_config().agents,
        execution_control=_control(ready=False, reasons=["a brand new policy reason"]),
    )
    assert gate["open"] is False
    assert gate["blockers"][0]["detail"] == "a brand new policy reason"


# ── Agent fleet: real run evidence and real health ───────────────────────────

def _iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _write_heartbeats(tmp_path, monkeypatch, agents):
    path = tmp_path / "agent_status_engine.json"
    path.write_text(json.dumps({"agents": agents}), encoding="utf-8")
    monkeypatch.setattr(runtime_service, "_ENGINE_HEARTBEAT_PATH", path)
    monkeypatch.setattr(
        runtime_service, "_ENGINE_HB_CACHE", {"mtime": None, "data": {}, "read_at": 0.0}
    )
    return path


def test_engine_card_gets_the_heartbeat_and_duration_the_engine_published(tmp_path, monkeypatch):
    _write_heartbeats(tmp_path, monkeypatch, {
        "scanner": {
            "running": True,
            "last_heartbeat": _iso(5),
            "last_run_duration_ms": 16778.28,
        },
    })
    result = runtime_service.snapshot_agent_runtimes(
        config={"agents": {"scanner": {"enabled": True}}},
        # A leftover measured in the API process must not replace the engine's value.
        observed_status={"scanner": {"last_run_duration_ms": 1.0}},
        engine_running=True,
        agent_names=["scanner"],
    )["scanner"]

    assert result["running"] is True and result["liveness_source"] == "engine_heartbeat"
    assert result["last_run_duration_ms"] == 16778.28
    assert result["last_activity_at"] == result["last_heartbeat"]
    assert result["seconds_since_activity"] < 60
    assert result["health"] == "ok" and result["overdue"] is False


def test_engine_worker_with_a_dead_heartbeat_needs_attention(tmp_path, monkeypatch):
    _write_heartbeats(tmp_path, monkeypatch, {
        "sentiment": {"running": True, "last_heartbeat": _iso(3 * 3600)},
        "scanner": {"running": True, "last_heartbeat": _iso(3)},
    })
    result = runtime_service.snapshot_agent_runtimes(
        config={"agents": {"sentiment": {"enabled": True}, "scanner": {"enabled": True}}},
        observed_status={},
        engine_running=True,
        agent_names=["sentiment", "scanner"],
    )
    assert result["scanner"]["health"] == "ok"
    assert result["sentiment"]["health"] == "attention"
    assert result["sentiment"]["overdue"] is True
    assert "heartbeat" in result["sentiment"]["health_reason"]


def test_silent_engine_sidecar_is_reported_not_assumed_alive(tmp_path, monkeypatch):
    path = _write_heartbeats(tmp_path, monkeypatch, {
        "risk": {"running": True, "last_heartbeat": _iso(4000)},
    })
    old = time.time() - 4000
    os.utime(path, (old, old))
    result = runtime_service.snapshot_agent_runtimes(
        config={"agents": {"risk": {"enabled": True}}},
        observed_status={},
        engine_running=True,
        agent_names=["risk"],
    )["risk"]
    assert result["liveness_source"] == "engine_process"
    assert result["health"] == "attention" and result["overdue"] is True


@pytest.mark.parametrize(
    "runtime, health, overdue",
    [
        # Thread alive, but four intervals without a completed cycle.
        ({"enabled": True, "running": True, "next_run_in": 300, "last_run": "AGE:4000"}, "attention", True),
        ({"enabled": True, "running": True, "next_run_in": 300, "last_run": "AGE:200"}, "ok", False),
        # A scheduled market pause is not a fault and must not look overdue.
        (
            {"enabled": True, "running": True, "next_run_in": 300, "last_run": "AGE:90000",
             "last_error": "market_open_paused"},
            "ok",
            False,
        ),
        ({"enabled": True, "running": True, "last_error": "provider timeout"}, "error", False),
        ({"enabled": True, "running": False}, "attention", False),
        ({"enabled": False, "running": False}, "disabled", False),
        ({"enabled": True, "running": True, "stalled": True}, "attention", True),
    ],
)
def test_health_reflects_last_run_evidence(runtime, health, overdue):
    now = time.time()
    record = dict(runtime)
    for key, value in list(record.items()):
        if isinstance(value, str) and value.startswith("AGE:"):
            record[key] = datetime.fromtimestamp(now - float(value[4:]), tz=timezone.utc).isoformat()
    assessed = runtime_service.assess_agent_health(record, now=now)
    assert assessed["health"] == health
    assert assessed["overdue"] is overdue
    if health in {"attention", "error"}:
        assert assessed["health_reason"]


def test_next_run_in_is_exposed_as_the_schedule_interval():
    assessed = runtime_service.assess_agent_health(
        {"enabled": True, "running": True, "next_run_in": 900, "last_success": _iso(30)}
    )
    assert assessed["schedule_interval_seconds"] == 900.0
    assert assessed["last_activity_at"] == assessed["last_success"]


# ── Global Settings: secrets ─────────────────────────────────────────────────

_SECRET_CONFIG = {
    "ai": {"providers": {
        "openai": {"enabled": True, "type": "openai", "api_key_env": "MS_AUDIT_OPENAI_KEY"},
        "xai": {"enabled": True, "type": "openai_compatible", "api_key_env": "MS_AUDIT_XAI_KEY"},
    }},
    "ollama": {"api_key_env": "OLLAMA_API_KEY"},
    "etoro": {"public_key": "pub-value", "private_key": "priv-value"},
    "telegram": {"bot_token": "123:abc", "chat_id": "42"},
    "news": {"finnhub_api_key": "fh-value", "marketaux_api_key": ""},
    "limits": {"max_tokens": 4096},
}


def test_config_response_masks_broker_and_messenger_secrets():
    redacted = config_service.redact_secrets(_SECRET_CONFIG)
    assert redacted["etoro"]["private_key"] == config_service._REDACTED
    assert redacted["etoro"]["public_key"] == config_service._REDACTED
    assert redacted["telegram"]["bot_token"] == config_service._REDACTED
    assert redacted["news"]["finnhub_api_key"] == config_service._REDACTED
    serialized = json.dumps(redacted)
    for secret in ("priv-value", "pub-value", "123:abc", "fh-value"):
        assert secret not in serialized
    # Non-secrets stay usable.
    assert redacted["limits"]["max_tokens"] == 4096
    assert redacted["news"]["marketaux_api_key"] == ""


def test_env_variable_names_are_not_masked():
    redacted = config_service.redact_secrets(_SECRET_CONFIG)
    assert redacted["ai"]["providers"]["openai"]["api_key_env"] == "MS_AUDIT_OPENAI_KEY"
    assert redacted["ollama"]["api_key_env"] == "OLLAMA_API_KEY"


def test_ai_tab_reports_the_key_of_each_provider_not_a_shared_placeholder(monkeypatch):
    from api.ai_config_service import list_ai_providers

    monkeypatch.setenv("MS_AUDIT_OPENAI_KEY", "sk-test")
    monkeypatch.delenv("MS_AUDIT_XAI_KEY", raising=False)
    # What the old masking made every provider look up.
    monkeypatch.setenv(config_service._REDACTED, "stray-key")

    rows = {row["name"]: row for row in list_ai_providers(config_service.redact_secrets(_SECRET_CONFIG))}
    assert rows["openai"]["has_key"] is True
    assert rows["xai"]["has_key"] is False


def test_saving_a_provider_key_through_the_api_config_targets_its_own_variable(tmp_path, monkeypatch):
    from api.ai_config_service import set_provider_api_key

    monkeypatch.chdir(tmp_path)  # the service writes a CWD-relative .env
    monkeypatch.setenv("MS_AUDIT_OPENAI_KEY", "old")
    monkeypatch.setenv(config_service._REDACTED, "placeholder")
    monkeypatch.delenv(config_service._REDACTED)

    result = set_provider_api_key(config_service.redact_secrets(_SECRET_CONFIG), "openai", "sk-new")

    assert result["has_key"] is True
    assert os.environ["MS_AUDIT_OPENAI_KEY"] == "sk-new"
    assert config_service._REDACTED not in os.environ
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert env_text.strip() == "MS_AUDIT_OPENAI_KEY=sk-new"


def test_masked_placeholders_never_overwrite_stored_secrets():
    inbound = config_service.redact_secrets(_SECRET_CONFIG)
    stripped = config_service._strip_redacted(inbound)
    assert "private_key" not in stripped["etoro"]
    assert "bot_token" not in stripped["telegram"]
    assert stripped["telegram"]["chat_id"] == "42"


# ── Global Settings: validation and durable save ─────────────────────────────

@pytest.mark.parametrize(
    "patch, fragment",
    [
        ({"signal_settings": {"min_win_rate": float("nan")}}, "finite"),
        ({"strategy_v2": {"min_rr": float("inf")}}, "finite"),
        ({"deep": {"list": [1, {"x": float("-inf")}]}}, "finite"),
        ({"signal_settings": {"min_win_rate": -1}}, "Min Win Rate"),
        ({"signal_settings": {"min_win_rate": 101}}, "Min Win Rate"),
        ({"signal_settings": {"min_win_rate": None}}, "Min Win Rate"),
        ({"signal_settings": {"min_win_rate": "55"}}, "Min Win Rate"),
        ({"signal_settings": {"profit_target_pct": 0}}, "Take Profit"),
        ({"signal_settings": {"stop_loss_pct": -3}}, "Stop Loss"),
        ({"agents": {"risk": {"position_size_pct": 0}}}, "Risk per Trade"),
        ({"agents": {"risk": {"position_size_pct": 250}}}, "Risk per Trade"),
        ({"agents": {"risk": {"position_size_pct": True}}}, "Risk per Trade"),
    ],
)
def test_unusable_settings_are_rejected(patch, fragment):
    with pytest.raises(config_service.ConfigValidationError) as excinfo:
        config_service.validate_config_patch(patch)
    assert fragment in str(excinfo.value)


def test_valid_settings_pass_validation():
    config_service.validate_config_patch({
        "signal_settings": {"min_win_rate": 0, "profit_target_pct": None, "stop_loss_pct": 2.5},
        "agents": {"risk": {"position_size_pct": 2}},
        "ui": {"theme": "dark"},
    })
    with pytest.raises(config_service.ConfigValidationError):
        config_service.validate_config_patch(["not", "an", "object"])


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "get_sqlite_store", lambda: None)
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({
            "bot": {"enabled": False, "min_edge_score": 80},
            "signal_settings": {"min_win_rate": 40, "stop_loss_pct": 3},
            "telegram": {"bot_token": "123:abc"},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_service, "CONFIG_PATH", path)
    return path


def test_save_merges_and_keeps_unrelated_and_secret_values(config_file):
    inbound = config_service.get_config()
    assert inbound["telegram"]["bot_token"] == config_service._REDACTED
    inbound["signal_settings"]["min_win_rate"] = 55  # round-trip of the masked payload

    assert config_service.save_config(inbound) is True

    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved["signal_settings"]["min_win_rate"] == 55
    assert saved["signal_settings"]["stop_loss_pct"] == 3
    assert saved["bot"]["min_edge_score"] == 80
    assert saved["telegram"]["bot_token"] == "123:abc"


def test_save_does_not_overwrite_a_concurrent_write(config_file, monkeypatch):
    real_leaf_updates = config_service._leaf_updates

    def racing_leaf_updates(patch, current, prefix=()):
        # Another writer (bot start, Risk Manager) lands after this request
        # loaded its snapshot and before it writes.
        if not prefix:
            config_module.ConfigManager(path=config_file).update_value(["bot", "enabled"], True)
        return real_leaf_updates(patch, current, prefix)

    monkeypatch.setattr(config_service, "_leaf_updates", racing_leaf_updates)
    config_service.save_config({"signal_settings": {"min_win_rate": 60}})

    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved["signal_settings"]["min_win_rate"] == 60
    assert saved["bot"]["enabled"] is True


def test_rejected_patch_writes_nothing(config_file):
    before = config_file.read_text(encoding="utf-8")
    with pytest.raises(config_service.ConfigValidationError):
        config_service.save_config({"signal_settings": {"min_win_rate": float("nan")}})
    assert config_file.read_text(encoding="utf-8") == before
    assert not math.isnan(json.loads(before)["signal_settings"]["min_win_rate"])


def test_failed_disk_write_is_reported_not_swallowed(config_file, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(config_module, "_write_json_atomic", refuse)
    with pytest.raises(Exception) as excinfo:
        config_service.save_config({"signal_settings": {"min_win_rate": 70}})
    assert "not persisted" in str(excinfo.value)
    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved["signal_settings"]["min_win_rate"] == 40


# ── Routes ───────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from api.main import app

    return TestClient(app, raise_server_exceptions=False)


def test_control_plane_route_serves_the_entry_gate_and_agent_health(client):
    response = client.get("/api/agents/control-plane")
    assert response.status_code == 200
    data = response.json()

    gate = data["entry_gate"]
    assert isinstance(gate["open"], bool)
    assert gate["status"] in {"open", "blocked", "unverified"}
    assert gate["gates"] and all({"key", "label", "ready", "detail", "action"} <= set(item) for item in gate["gates"])
    assert gate["ready_count"] + len(gate["blockers"]) == gate["total"]
    # A blocked gate always names what clears it.
    assert all(item["action"] for item in gate["blockers"])
    for name in ("execution", "risk"):
        assert data["agents"][name]["accepting_new_entries"] is (
            bool(data["agents"][name]["running"]) and gate["open"]
        )
    for details in data["agents"].values():
        assert details["health"] in {"ok", "attention", "error", "starting", "disabled"}
        assert isinstance(details["overdue"], bool)


def test_config_route_rejects_out_of_range_setting_with_422(client, monkeypatch):
    import api.main as main_module

    writes = []
    real_save = main_module.save_config
    monkeypatch.setattr(
        main_module, "save_config", lambda patch: writes.append(patch) or real_save(patch)
    )
    response = client.post("/api/config", json={"config": {"signal_settings": {"min_win_rate": -5}}})
    assert response.status_code == 422
    assert "Min Win Rate" in response.json()["detail"]
    assert len(writes) == 1  # reached the service, which refused before writing


# ── UI Inspector agrees with the Agents tab about worker health ──────────────

def _inspector_report(monkeypatch, agents):
    from agents.ui_inspector_agent import UIInspectorAgent

    def fetch(_self, url, timeout=10.0):
        if "/api/agents/control-plane" in url:
            return {"bot": {"engine_running": True}, "agents": agents}
        if "/api/cockpit" in url:
            return {"strategy_lab": {"records": []}, "decision_ledger": {"items": []}}
        if "/api/news-tab/state" in url:
            return {"total_articles": 12, "last_update": _iso(30)}
        if "/api/scanner/state" in url:
            return {"hot_tickers": [{"symbol": "AAPL"}], "as_of_utc": _iso(10), "refresh_seconds": 60}
        if "/api/traderadar/state" in url:
            return {"candidates": [{"ticker": "NVDA"}], "as_of": _iso(30)}
        if "/api/premarket" in url:
            return {"candidates": [{"symbol": "MSTR"}], "as_of_utc": _iso(60), "refresh_seconds": 300}
        if "/api/watchlist" in url:
            return {"stocks": ["PYPL"], "crypto": [], "hot_list": []}
        return {}

    monkeypatch.setattr(UIInspectorAgent, "_fetch_json", fetch)
    monkeypatch.setattr(UIInspectorAgent, "_escalate_to_coding_agent", lambda _self, anomalies: len(anomalies))
    agent = UIInspectorAgent(config={"agents": {"ui_inspector": {"enabled": True}}})
    return agent.run_inspection_cycle()


def test_inspector_does_not_escalate_a_scheduled_pause_as_a_runtime_error(monkeypatch):
    report = _inspector_report(monkeypatch, {
        "premarket_scanner": {"enabled": True, "running": True, "last_error": "market_open_paused"},
    })
    kinds = [item["kind"] for item in report["anomalies"] if item.get("tab") == "Agents"]
    assert kinds == []
    assert report["tab_health"]["agents"]["status"] == "HEALTHY"


def test_inspector_flags_a_running_but_overdue_worker(monkeypatch):
    report = _inspector_report(monkeypatch, {
        "trade_radar": {
            "enabled": True,
            "running": True,
            "overdue": True,
            "health_reason": "no completed cycle for 66m (scheduled every 5m)",
        },
        "scanner": {"enabled": True, "running": True, "overdue": False},
    })
    agent_anomalies = [item for item in report["anomalies"] if item.get("tab") == "Agents"]
    assert [item["kind"] for item in agent_anomalies] == ["AGENT_OVERDUE"]
    assert "66m" in agent_anomalies[0]["problem"]
    assert report["tab_health"]["agents"]["status"] == "DEGRADED"
