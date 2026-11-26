from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from api import main


def test_test_mode_lifespan_does_not_start_persistent_schedulers(monkeypatch):
    backup = MagicMock()
    auto_start = MagicMock(return_value={"started": True})
    pe_start = MagicMock(return_value={"started": True})
    sweep = MagicMock()
    monkeypatch.setattr(main, "IS_TEST_MODE", True)
    monkeypatch.setattr(main, "_sweep_orphan_tmp_files", sweep)
    monkeypatch.setattr("core.disaster_recovery.get_backup_scheduler", lambda: backup)
    monkeypatch.setattr("api.research_factory_service.auto_cycle_startup", auto_start)
    monkeypatch.setattr("api.research_factory_service.pe_scan_startup", pe_start)

    with TestClient(main.app):
        pass

    backup.start.assert_not_called()
    auto_start.assert_not_called()
    pe_start.assert_not_called()
    sweep.assert_not_called()
