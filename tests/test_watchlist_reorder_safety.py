import json

from api import actions_service


def test_reorder_watchlist_preserves_set_and_dedupes(tmp_path, monkeypatch):
    watchlist_path = tmp_path / "watchlist.json"
    watchlist_path.write_text(
        json.dumps(
            {
                "stocks": ["AAPL", "MSFT"],
                "crypto": ["BTC-USD"],
                "order": ["AAPL", "MSFT", "BTC-USD"],
                "hot_list": ["MSFT", "BTC-USD"],
                "settings": {"AAPL": {"signals_enabled": True}},
                "schema_version": 2,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(actions_service, "WATCHLIST_PATH", watchlist_path)
    monkeypatch.setattr(actions_service, "get_state", lambda *args, **kwargs: {"status": "ok"})

    actions_service.reorder_watchlist(["MSFT", "MSFT", "AAPL"])

    saved = json.loads(watchlist_path.read_text(encoding="utf-8"))
    assert saved["order"] == ["MSFT", "AAPL", "BTC-USD"]
    assert saved["stocks"] == ["MSFT", "AAPL"]
    assert saved["crypto"] == ["BTC-USD"]
    assert saved["hot_list"] == ["MSFT", "BTC-USD"]

