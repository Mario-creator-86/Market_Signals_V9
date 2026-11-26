from __future__ import annotations

import os
from pathlib import Path

from run_server_test import configure_test_environment


def test_e2e_server_uses_disposable_fail_closed_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("E2E_PORT", "8123")

    root = configure_test_environment(tmp_path / "isolated")

    assert root == (tmp_path / "isolated").resolve()
    assert os.environ["TEST_MODE"] == "1"
    assert os.environ["MS_HOST"] == "127.0.0.1"
    assert os.environ["MS_PORT"] == "8123"
    assert os.environ["MS_DISABLE_ENGINE_AUTOSTART"] == "1"
    assert os.environ["MS_DISABLE_BG_WORKER"] == "1"
    assert os.environ["MS_ENGINE_MODE"] == "disabled"

    isolated_paths = {
        "MS_CONFIG_PATH",
        "MS_WATCHLIST_PATH",
        "MS_STATE_PATH",
        "MS_SIGNALS_PATH",
        "MS_CAPITAL_STATE_PATH",
        "MS_SQLITE_DB_PATH",
        "MS_LOG_DIR",
        "MS_AUDIT_LOG_DIR",
        "MS_BACKUP_DIR",
    }
    for key in isolated_paths:
        assert Path(os.environ[key]).resolve().is_relative_to(root)
