import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.container_entrypoint import prepare_runtime


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "app"
    project.mkdir()
    (project / "config.example.json").write_text(
        json.dumps({"bot": {"enabled": False}, "environment": "test"}),
        encoding="utf-8",
    )
    return project


def test_prepare_runtime_seeds_durable_paths_without_overwriting(tmp_path):
    project = _project(tmp_path)
    environment = {"MS_RUNTIME_DIR": "", "MS_CONFIG_PATH": ""}

    paths = prepare_runtime(environment, project_root=project)

    config_path = Path(environment["MS_CONFIG_PATH"])
    state_path = Path(environment["MS_STATE_PATH"])
    signals_path = Path(environment["MS_SIGNALS_PATH"])
    capital_path = Path(environment["MS_CAPITAL_STATE_PATH"])
    assert json.loads(config_path.read_text(encoding="utf-8"))["bot"]["enabled"] is False
    assert json.loads(state_path.read_text(encoding="utf-8")) == {}
    assert json.loads(signals_path.read_text(encoding="utf-8")) == {
        "open": {},
        "closed": [],
    }
    assert json.loads(capital_path.read_text(encoding="utf-8"))["balance"] == 1000.0
    assert Path(environment["MS_SQLITE_DB_PATH"]).parent == project / "data"
    assert Path(paths["backup_dir"]).is_dir()

    config_path.write_text(json.dumps({"preserved": True}), encoding="utf-8")
    prepare_runtime(environment, project_root=project)
    assert json.loads(config_path.read_text(encoding="utf-8")) == {"preserved": True}


def test_prepare_runtime_rejects_invalid_configuration_template(tmp_path):
    project = tmp_path / "app"
    project.mkdir()
    (project / "config.example.json").write_text("{invalid", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Invalid configuration template"):
        prepare_runtime({}, project_root=project)

    assert not (project / "runtime" / "config.json").exists()


def test_core_config_honors_container_runtime_path_overrides(tmp_path):
    runtime = tmp_path / "runtime"
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    environment = os.environ.copy()
    environment.update(
        {
            "MS_RUNTIME_DIR": str(runtime),
            "MS_DATA_DIR": str(data),
            "MS_LOG_DIR": str(logs),
            "MS_CONFIG_PATH": str(runtime / "operator-config.json"),
            "MS_WATCHLIST_PATH": str(runtime / "operator-watchlist.json"),
            "MS_TRADE_RADAR_STATE_PATH": str(data / "trade_radar_state.json"),
        }
    )
    command = (
        "import json; from core import config; "
        "print(json.dumps({'config': str(config.CONFIG_PATH), "
        "'watchlist': str(config.WATCHLIST_PATH), "
        "'trade_radar': str(config.TRADE_RADAR_STATE_PATH), "
        "'logs': str(config.LOG_DIR)}))"
    )

    completed = subprocess.run(
        [sys.executable, "-c", command],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(completed.stdout.strip())

    assert Path(payload["config"]) == runtime / "operator-config.json"
    assert Path(payload["watchlist"]) == runtime / "operator-watchlist.json"
    assert Path(payload["trade_radar"]) == data / "trade_radar_state.json"
    assert Path(payload["logs"]) == logs
