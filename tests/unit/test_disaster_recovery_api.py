import json
import sqlite3
from pathlib import Path

from core.disaster_recovery import BackupManager, FileSpec


class _TestScheduler:
    def __init__(self, manager: BackupManager):
        self.manager = manager

    def status(self):
        snapshots = self.manager.list_snapshots()
        return {
            "enabled": False,
            "running": False,
            "snapshot_count": len(snapshots),
            "latest_snapshot": snapshots[0] if snapshots else None,
            "last_error": None,
        }


def _scheduler(tmp_path: Path) -> _TestScheduler:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"mode": "paper"}), encoding="utf-8")
    database = tmp_path / "data" / "market_signals.db"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE runtime_state (value TEXT)")
    connection.execute("INSERT INTO runtime_state VALUES ('healthy')")
    connection.commit()
    connection.close()
    manager = BackupManager(
        project_root=tmp_path,
        database_path=database,
        backup_dir=tmp_path / "backups",
        file_specs=[FileSpec("config.json", config, required=True)],
    )
    return _TestScheduler(manager)


def test_backup_ops_create_list_and_verify(client, monkeypatch, tmp_path):
    import core.disaster_recovery as disaster_recovery

    scheduler = _scheduler(tmp_path)
    monkeypatch.setattr(disaster_recovery, "_BACKUP_SCHEDULER", scheduler)

    created = client.post("/api/ops/backups", json={"label": "api-contract"})
    assert created.status_code == 200
    created_payload = created.json()
    assert created_payload["verified"] is True

    listed = client.get("/api/ops/backups?verify=true&limit=1")
    assert listed.status_code == 200
    listed_payload = listed.json()
    assert listed_payload["status"]["snapshot_count"] == 1
    assert len(listed_payload["backups"]) == 1
    assert listed_payload["backups"][0]["verified"] is True
    assert listed_payload["backups"][0]["label"] == "api-contract"

    backup_name = Path(created_payload["path"]).name
    verified = client.post(f"/api/ops/backups/{backup_name}/verify")
    assert verified.status_code == 200
    assert verified.json()["verified"] is True
    assert "manifest" not in verified.json()


def test_backup_verify_rejects_unsafe_name(client, monkeypatch, tmp_path):
    import core.disaster_recovery as disaster_recovery

    monkeypatch.setattr(disaster_recovery, "_BACKUP_SCHEDULER", _scheduler(tmp_path))

    response = client.post("/api/ops/backups/..%5Coutside.zip/verify")

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid backup name."
