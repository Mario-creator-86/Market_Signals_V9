"""Cross-process-safe config mutation contracts."""

import json

import core.config as config_module


class _SnapshotStore:
    def __init__(self, payload, synced_mtime=None):
        self.payload = payload
        self.synced_mtime = synced_mtime
        self.synced = {}

    def get_json(self, _key, default=None):
        return self.payload if self.payload is not None else default

    def set_json(self, key, value):
        self.synced[key] = value

    def get_text(self, _key, default=None):
        return self.synced_mtime if self.synced_mtime is not None else default

    def set_text(self, _key, _value):
        return None


def test_atomic_value_update_preserves_change_from_stale_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "get_sqlite_store", lambda: None)
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"bot": {"enabled": False, "min_edge_score": 80}}),
        encoding="utf-8",
    )
    first = config_module.ConfigManager(path=path)
    stale = config_module.ConfigManager(path=path)

    first.update_value(["bot", "enabled"], True)
    stale.update_value(["bot", "min_edge_score"], 85)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["bot"]["enabled"] is True
    assert saved["bot"]["min_edge_score"] == 85


def test_signal_runtime_view_restores_scalar_defaults_for_json_null(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "get_sqlite_store", lambda: None)
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({
            "signal_settings": {
                "profit_target_pct": None,
                "stop_loss_pct": None,
                "exit_mode": "FIXED_PCT",
            },
        }),
        encoding="utf-8",
    )

    manager = config_module.ConfigManager(path=path)

    assert manager.config["signal_settings"]["profit_target_pct"] is None
    assert manager.signal["profit_target_pct"] == config_module.DEFAULT_CONFIG["signal_settings"]["profit_target_pct"]
    assert manager.signal["stop_loss_pct"] == config_module.DEFAULT_CONFIG["signal_settings"]["stop_loss_pct"]
    assert manager.signal["exit_mode"] == "FIXED_PCT"


def test_newer_manual_config_file_is_authoritative_over_sqlite_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"bot": {"min_edge_score": 81}}), encoding="utf-8")
    store = _SnapshotStore(
        {"bot": {"min_edge_score": 55}},
        synced_mtime=path.stat().st_mtime - 10.0,
    )
    monkeypatch.setattr(config_module, "get_sqlite_store", lambda: store)

    manager = config_module.ConfigManager(path=path)

    assert manager.bot["min_edge_score"] == 81
    assert store.synced[config_module.SQLITE_KEY_CONFIG]["bot"]["min_edge_score"] == 81


def test_risk_manager_sqlite_snapshot_wins_over_stale_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({
            "agents": {"risk": {"max_open_trades": 3, "max_daily_loss_pct": 1.0}},
            "risk_constitution": {"owner_confirmed": True, "max_positions": 3},
        }),
        encoding="utf-8",
    )
    store = _SnapshotStore(
        {
            "agents": {"risk": {"max_open_trades": 40, "max_daily_loss_pct": 10.0}},
            "risk_constitution": {"owner_confirmed": True, "max_positions": 40},
        },
        synced_mtime=path.stat().st_mtime,
    )
    monkeypatch.setattr(config_module, "get_sqlite_store", lambda: store)

    manager = config_module.ConfigManager(path=path)

    assert manager.agents["risk"]["max_open_trades"] == 40
    assert manager.agents["risk"]["max_daily_loss_pct"] == 10.0
    assert manager.config["risk_constitution"]["max_positions"] == 40


def test_unrelated_atomic_update_cannot_replace_risk_manager_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({
            "bot": {"enabled": False},
            "agents": {"risk": {"max_open_trades": 3}},
            "risk_constitution": {"owner_confirmed": True, "max_positions": 3},
        }),
        encoding="utf-8",
    )
    store = _SnapshotStore(
        {
            "bot": {"enabled": False},
            "agents": {"risk": {"max_open_trades": 40}},
            "risk_constitution": {"owner_confirmed": True, "max_positions": 40},
        },
        synced_mtime=path.stat().st_mtime,
    )
    monkeypatch.setattr(config_module, "get_sqlite_store", lambda: store)
    manager = config_module.ConfigManager(path=path)

    manager.update_value(["bot", "enabled"], True)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["bot"]["enabled"] is True
    assert saved["agents"]["risk"]["max_open_trades"] == 40
    assert saved["risk_constitution"]["max_positions"] == 40
