"""Prepare durable container state, then replace this process with the API."""

from __future__ import annotations

import json
import os
import runpy
import sys
import tempfile
from pathlib import Path
from typing import Any, MutableMapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.bootstrap-",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
    finally:
        temp_path.unlink(missing_ok=True)


def _copy_config_template(source: Path, destination: Path) -> None:
    try:
        with source.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid configuration template: {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Configuration template must contain a JSON object: {source}")
    _atomic_json(destination, payload)


def prepare_runtime(
    environment: MutableMapping[str, str] | None = None,
    *,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, str]:
    """Set container-safe paths and seed only state that does not exist."""
    env = environment if environment is not None else os.environ
    root = Path(project_root).resolve()

    def configured_path(name: str, default: Path) -> Path:
        raw = str(env.get(name) or "").strip()
        if not raw:
            raw = str(default)
            env[name] = raw
        return Path(raw).resolve()

    runtime_dir = configured_path("MS_RUNTIME_DIR", root / "runtime")
    data_dir = configured_path("MS_DATA_DIR", root / "data")
    log_dir = configured_path("MS_LOG_DIR", root / "logs")
    backup_dir = configured_path("MS_BACKUP_DIR", root / "backups")

    path_defaults = {
        "MS_CONFIG_PATH": runtime_dir / "config.json",
        "MS_WATCHLIST_PATH": runtime_dir / "watchlist.json",
        "MS_STATE_PATH": runtime_dir / "state.json",
        "MS_SIGNALS_PATH": runtime_dir / "signals_state.json",
        "MS_CAPITAL_STATE_PATH": runtime_dir / "capital.json",
        "MS_SQLITE_DB_PATH": data_dir / "market_signals.db",
    }
    for name, path in path_defaults.items():
        if not str(env.get(name) or "").strip():
            env[name] = str(path)

    for directory in (runtime_dir, data_dir, log_dir, backup_dir):
        directory.mkdir(parents=True, exist_ok=True)

    config_path = Path(env["MS_CONFIG_PATH"]).resolve()
    if not config_path.exists():
        _copy_config_template(root / "config.example.json", config_path)

    seeds = {
        Path(env["MS_STATE_PATH"]).resolve(): {},
        Path(env["MS_SIGNALS_PATH"]).resolve(): {"open": {}, "closed": []},
        Path(env["MS_CAPITAL_STATE_PATH"]).resolve(): {
            "balance": 1000.0,
            "history": [],
            "positions": [],
        },
    }
    for path, payload in seeds.items():
        if not path.exists():
            _atomic_json(path, payload)

    return {
        "runtime_dir": str(runtime_dir),
        "data_dir": str(data_dir),
        "log_dir": str(log_dir),
        "backup_dir": str(backup_dir),
        **{
            name.lower(): str(Path(env[name]).resolve())
            for name in path_defaults
        },
    }


def main() -> None:
    os.umask(0o077)
    prepare_runtime()
    sys.argv = [str(PROJECT_ROOT / "run_server.py"), *sys.argv[1:]]
    runpy.run_path(str(PROJECT_ROOT / "run_server.py"), run_name="__main__")


if __name__ == "__main__":
    main()
