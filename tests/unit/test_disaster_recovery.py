import json
import hashlib
import sqlite3
import zipfile
from pathlib import Path

import pytest

from core.disaster_recovery import (
    BACKUP_PREFIX,
    BackupError,
    BackupManager,
    BackupVerificationError,
    FileSpec,
    RestoreSafetyError,
)
from core.operator_audit import OperatorAuditLedger


def _database(path: Path, value: str = "original") -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE critical_state (id INTEGER PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO critical_state(value) VALUES (?)", (value,))
    connection.commit()
    return connection


def _manager(root: Path, *, retention_count: int = 7, allow_restore: bool = False):
    config = root / "config.json"
    state = root / "state.json"
    config.write_text(json.dumps({"mode": "paper"}), encoding="utf-8")
    state.write_text(json.dumps({"balance": 1250.5}), encoding="utf-8")
    return BackupManager(
        project_root=root,
        database_path=root / "data" / "market_signals.db",
        backup_dir=root / "backups",
        file_specs=[
            FileSpec("config.json", config, required=True),
            FileSpec("state.json", state, required=True),
        ],
        retention_count=retention_count,
        retention_days=30,
        allow_restore=allow_restore,
    )


def test_snapshot_captures_wal_database_and_manifest_hashes(tmp_path):
    connection = _database(tmp_path / "data" / "market_signals.db")
    manager = _manager(tmp_path)
    try:
        result = manager.create_snapshot(label="unit")
    finally:
        connection.close()

    archive_path = Path(result["path"])
    verification = manager.verify_snapshot(archive_path)

    assert result["verified"] is True
    assert archive_path.name.startswith(BACKUP_PREFIX)
    assert verification["verified"] is True
    assert verification["sqlite_integrity"]["ok"] is True
    assert verification["entry_count"] == 3

    with zipfile.ZipFile(archive_path, "r") as archive:
        extracted = tmp_path / "extracted.sqlite"
        extracted.write_bytes(archive.read("payload/database.sqlite"))
    restored = sqlite3.connect(extracted)
    try:
        assert restored.execute("SELECT value FROM critical_state").fetchone()[0] == "original"
    finally:
        restored.close()


def test_restore_dry_run_and_guarded_roundtrip(tmp_path):
    connection = _database(tmp_path / "data" / "market_signals.db")
    connection.close()
    manager = _manager(tmp_path, allow_restore=True)
    snapshot = manager.create_snapshot(label="roundtrip")

    dry_run = manager.restore_snapshot(snapshot["path"])
    assert dry_run["status"] == "dry_run"
    assert len(dry_run["files"]) == 3

    (tmp_path / "config.json").write_text(json.dumps({"mode": "mutated"}), encoding="utf-8")
    changed = sqlite3.connect(tmp_path / "data" / "market_signals.db")
    changed.execute("UPDATE critical_state SET value='mutated'")
    changed.commit()
    changed.close()

    restored = manager.restore_snapshot(snapshot["path"], apply=True)
    assert restored["status"] == "restored"
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8")) == {
        "mode": "paper"
    }
    database = sqlite3.connect(tmp_path / "data" / "market_signals.db")
    try:
        assert database.execute("SELECT value FROM critical_state").fetchone()[0] == "original"
    finally:
        database.close()


def test_restore_apply_requires_explicit_manager_boundary(tmp_path):
    connection = _database(tmp_path / "data" / "market_signals.db")
    connection.close()
    manager = _manager(tmp_path, allow_restore=False)
    snapshot = manager.create_snapshot()

    with pytest.raises(RestoreSafetyError, match="explicit acknowledgement"):
        manager.restore_snapshot(snapshot["path"], apply=True)


def test_tampered_archive_is_rejected(tmp_path):
    connection = _database(tmp_path / "data" / "market_signals.db")
    connection.close()
    manager = _manager(tmp_path)
    snapshot = manager.create_snapshot()
    source_path = Path(snapshot["path"])
    tampered_path = manager.backup_dir / f"{BACKUP_PREFIX}tampered.zip"

    with zipfile.ZipFile(source_path, "r") as source, zipfile.ZipFile(
        tampered_path, "w", compression=zipfile.ZIP_DEFLATED
    ) as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "payload/files/state.json":
                data += b"\n "
            target.writestr(info, data)

    with pytest.raises(BackupVerificationError, match="mismatch"):
        manager.verify_snapshot(tampered_path)


def test_retention_keeps_only_configured_snapshot_count(tmp_path):
    connection = _database(tmp_path / "data" / "market_signals.db")
    connection.close()
    manager = _manager(tmp_path, retention_count=2)

    for label in ("one", "two", "three"):
        manager.create_snapshot(label=label)

    snapshots = manager.list_snapshots()
    assert len(snapshots) == 2
    assert all(row["name"].startswith(BACKUP_PREFIX) for row in snapshots)


def test_required_invalid_json_aborts_without_partial_archive(tmp_path):
    connection = _database(tmp_path / "data" / "market_signals.db")
    connection.close()
    manager = _manager(tmp_path)
    (tmp_path / "state.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(BackupError, match="Required JSON source is invalid"):
        manager.create_snapshot()

    assert list(manager.backup_dir.glob("*.partial")) == []
    assert list(manager.backup_dir.glob(f"{BACKUP_PREFIX}*.zip")) == []


def test_default_manifest_paths_follow_container_runtime(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("MS_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("MS_CONFIG_PATH", str(runtime / "config.json"))
    monkeypatch.setenv("MS_WATCHLIST_PATH", str(runtime / "watchlist.json"))
    monkeypatch.setenv("MS_STATE_PATH", str(runtime / "state.json"))
    monkeypatch.setenv("MS_SIGNALS_PATH", str(runtime / "signals_state.json"))
    monkeypatch.setenv("MS_CAPITAL_STATE_PATH", str(runtime / "capital.json"))

    manager = BackupManager(
        project_root=tmp_path,
        database_path=tmp_path / "data" / "market_signals.db",
        backup_dir=tmp_path / "backups",
    )
    restore_paths = {spec.restore_path for spec in manager.file_specs if spec.required}

    assert restore_paths == {
        "runtime/config.json",
        "runtime/watchlist.json",
        "runtime/state.json",
        "runtime/signals_state.json",
        "runtime/capital.json",
    }


def test_backup_locks_and_verifies_operator_audit_chain(monkeypatch, tmp_path):
    audit_key = "backup-audit-key-0123456789abcdef"
    audit_path = tmp_path / "data" / "operator_audit.jsonl"
    monkeypatch.setenv("MS_AUDIT_HMAC_KEY", audit_key)
    monkeypatch.setenv("MS_OPERATOR_AUDIT_PATH", str(audit_path))
    audit = OperatorAuditLedger(path=audit_path, key=audit_key, required=True)
    audit.append_http_event(
        request_id="backup-contract",
        method="POST",
        path="/api/config",
        status_code=200,
        duration_ms=5,
        actor="api_key",
        client_address="127.0.0.1",
    )
    connection = _database(tmp_path / "data" / "market_signals.db")
    connection.close()
    manager = _manager(tmp_path)
    manager.file_specs.append(
        FileSpec("data/operator_audit.jsonl", audit_path, required=True)
    )

    snapshot = manager.create_snapshot(label="audit-chain")
    verification = manager.verify_snapshot(snapshot["path"])

    assert verification["verified"] is True
    assert verification["operator_audit_integrity"]["verified"] is True
    assert verification["operator_audit_integrity"]["record_count"] == 1

    source_path = Path(snapshot["path"])
    forged_path = manager.backup_dir / f"{BACKUP_PREFIX}forged-audit.zip"
    with zipfile.ZipFile(source_path, "r") as source:
        members = {name: source.read(name) for name in source.namelist()}
    manifest = json.loads(members["manifest.json"])
    audit_entry = next(
        entry
        for entry in manifest["entries"]
        if (entry.get("content_verification") or {}).get("type")
        == "operator_hmac_chain"
    )
    member_name = audit_entry["archive_path"]
    forged_record = json.loads(members[member_name].decode("utf-8"))
    forged_record["status_code"] = 500
    forged_payload = (
        json.dumps(forged_record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    members[member_name] = forged_payload
    audit_entry["size"] = len(forged_payload)
    audit_entry["sha256"] = hashlib.sha256(forged_payload).hexdigest()
    members["manifest.json"] = json.dumps(manifest).encode("utf-8")
    with zipfile.ZipFile(
        forged_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as forged:
        for name, payload in members.items():
            forged.writestr(name, payload)

    with pytest.raises(BackupVerificationError, match="Operator audit verification"):
        manager.verify_snapshot(forged_path)
