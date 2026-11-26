import json

from tools import backup_manager


class _FakeManager:
    def __init__(self):
        self.restore_calls = []

    def restore_snapshot(self, archive, *, target_root, apply):
        self.restore_calls.append(
            {
                "archive": str(archive),
                "target_root": str(target_root),
                "apply": apply,
            }
        )
        return {
            "status": "restored" if apply else "dry_run",
            "verified": True,
            "files": [],
        }

    def create_snapshot(self, **kwargs):
        return {"status": "created", "verified": True}


def test_restore_defaults_to_verified_dry_run(monkeypatch, capsys):
    manager = _FakeManager()
    monkeypatch.setattr(backup_manager, "_manager", lambda *args, **kwargs: manager)

    exit_code = backup_manager.main(["restore", "snapshot.zip"])

    assert exit_code == 0
    assert manager.restore_calls[0]["apply"] is False
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"


def test_restore_apply_requires_exact_acknowledgement(capsys):
    exit_code = backup_manager.main(["restore", "snapshot.zip", "--apply"])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert "REPLACE_RUNTIME_STATE" in payload["error"]
