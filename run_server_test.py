"""Start an isolated Market Signals V9 server for browser/E2E tests.

The normal application defaults intentionally use the repository-local runtime
files for backwards compatibility. Browser tests are mutating, so they must
never reuse those files or start the trading engine. This entrypoint establishes
the isolation boundary before importing any application module.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def configure_test_environment(runtime_root: Path | None = None) -> Path:
    """Configure fail-closed, disposable runtime paths and return their root."""

    root = Path(runtime_root or tempfile.mkdtemp(prefix="market-signals-v9-e2e-")).resolve()
    runtime_dir = root / "runtime"
    data_dir = root / "data"
    log_dir = root / "logs"
    audit_dir = root / "audit_logs"
    backup_dir = root / "backups"
    for path in (runtime_dir, data_dir, log_dir, audit_dir, backup_dir):
        path.mkdir(parents=True, exist_ok=True)

    port = str(int(os.getenv("E2E_PORT", "8010")))
    values = {
        "TEST_MODE": "1",
        "MS_ENV": "test",
        "MS_HOST": "127.0.0.1",
        "MS_PORT": port,
        "MS_DISABLE_ENGINE_AUTOSTART": "1",
        "MS_DISABLE_WARMUP": "1",
        "MS_DISABLE_STATE_PREFETCH": "1",
        "MS_DISABLE_BG_WORKER": "1",
        "MS_DISABLE_SCANNER_AGENT": "1",
        "MS_DISABLE_SCANNER_LAZY_REFRESH": "1",
        "MS_DISABLE_LIVE_PRICE_REMOTE": "1",
        "MS_ENGINE_MODE": "disabled",
        "MS_RUNTIME_DIR": str(runtime_dir),
        "MS_DATA_DIR": str(data_dir),
        "MS_CONFIG_PATH": str(runtime_dir / "config.json"),
        "MS_WATCHLIST_PATH": str(runtime_dir / "watchlist.json"),
        "MS_STATE_PATH": str(runtime_dir / "state.json"),
        "MS_SIGNALS_PATH": str(runtime_dir / "signals_state.json"),
        "MS_CAPITAL_STATE_PATH": str(runtime_dir / "capital.json"),
        "MS_SQLITE_DB_PATH": str(data_dir / "market_signals.db"),
        "MS_LOG_DIR": str(log_dir),
        "MS_AUDIT_LOG_DIR": str(audit_dir),
        "MS_BACKUP_DIR": str(backup_dir),
    }
    os.environ.update(values)
    return root


def main() -> None:
    configure_test_environment()

    import uvicorn
    from api.main import app

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ["MS_PORT"]),
        reload=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
