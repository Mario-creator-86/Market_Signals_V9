from pathlib import Path

from core.runtime_layout import migrate_legacy_runtime_layout


def test_legacy_runtime_migration_is_non_destructive_and_idempotent(tmp_path):
    base = tmp_path / "project"
    runtime = base / "runtime"
    data = base / "data"
    base.mkdir()
    (base / "config.json").write_text('{"mode":"paper"}', encoding="utf-8")
    (base / "watchlist.json").write_text('{"stocks":["AAPL"]}', encoding="utf-8")
    (base / "calendar_events_cache.json").write_text('{"earnings":{}}', encoding="utf-8")

    first = migrate_legacy_runtime_layout(base, runtime, data)
    second = migrate_legacy_runtime_layout(base, runtime, data)

    assert len(first) == 3
    assert second == []
    assert (runtime / "config.json").read_text(encoding="utf-8") == '{"mode":"paper"}'
    assert (runtime / "watchlist.json").read_text(encoding="utf-8") == '{"stocks":["AAPL"]}'
    assert (data / "calendar_events_cache.json").read_text(encoding="utf-8") == '{"earnings":{}}'
    assert (base / "config.json").is_file()
    assert (base / "watchlist.json").is_file()


def test_existing_runtime_destination_wins(tmp_path):
    base = tmp_path / "project"
    runtime = base / "runtime"
    data = base / "data"
    runtime.mkdir(parents=True)
    (base / "config.json").write_text("legacy", encoding="utf-8")
    (runtime / "config.json").write_text("operator", encoding="utf-8")

    copied = migrate_legacy_runtime_layout(base, runtime, data)

    assert copied == []
    assert (runtime / "config.json").read_text(encoding="utf-8") == "operator"
