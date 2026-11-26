"""A corrupted watchlist.json must not make every later save raise (2026-09-15 audit)."""
import json


def test_config_save_watchlist_recovers_from_corrupt_file(tmp_path, monkeypatch):
    import core.config as config_mod

    path = tmp_path / "watchlist.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(config_mod, "WATCHLIST_PATH", path)
    cm = object.__new__(config_mod.ConfigManager)
    cm._watchlist = {"alerts": []}
    cm._alerts_snapshot = []
    written = {}
    monkeypatch.setattr(cm, "_save_watchlist_unlocked", lambda: written.update(cm._watchlist), raising=False)
    applied = cm._save_watchlist(alert_baseline=[], desired_alerts=[{"id": "a1", "ticker": "AAPL"}])
    assert applied == {"a1"}
    assert written["alerts"] == [{"id": "a1", "ticker": "AAPL"}]


def test_ui_watchlist_save_recovers_from_corrupt_file(tmp_path, monkeypatch):
    import core.ui_watchlist as ui

    path = tmp_path / "watchlist.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(ui, "WATCHLIST_PATH", path)
    ui._save_watchlist(["AAPL"], ["BTCUSDT"])
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["stocks"] == ["AAPL"] and data["crypto"] == ["BTCUSDT"]
