import json

from core import config as config_module


def test_watchlist_empty_lists_are_preserved_on_reload(tmp_path, monkeypatch):
    watchlist_path = tmp_path / "watchlist.json"
    config_path = tmp_path / "config.json"
    watchlist_path.write_text(
        json.dumps(
            {
                "stocks": [],
                "crypto": [],
                "hot_list": [],
                "order": [],
                "settings": {},
                "schema_version": 2,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(config_module, "WATCHLIST_PATH", watchlist_path)
    monkeypatch.setattr(config_module, "get_sqlite_store", lambda: None)

    cfg = config_module.ConfigManager(path=config_path)
    reloaded = cfg.watchlist or {}
    assert reloaded.get("yahoo") == []
    assert reloaded.get("binance") == []

    persisted = json.loads(watchlist_path.read_text(encoding="utf-8"))
    assert persisted.get("stocks") == []
    assert persisted.get("crypto") == []


def test_watchlist_missing_keys_still_uses_fallback_defaults(tmp_path, monkeypatch):
    watchlist_path = tmp_path / "watchlist.json"
    config_path = tmp_path / "config.json"
    watchlist_path.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")

    monkeypatch.setattr(config_module, "WATCHLIST_PATH", watchlist_path)
    monkeypatch.setattr(config_module, "get_sqlite_store", lambda: None)

    cfg = config_module.ConfigManager(path=config_path)
    reloaded = cfg.watchlist or {}
    assert reloaded.get("yahoo") == config_module.FALLBACK_STOCKS
    assert reloaded.get("binance") == config_module.FALLBACK_CRYPTOS
