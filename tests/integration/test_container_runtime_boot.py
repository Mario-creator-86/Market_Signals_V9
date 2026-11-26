import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from core.operator_audit import OperatorAuditLedger


ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.mark.integration
@pytest.mark.smoke
def test_container_entrypoint_boots_fail_closed_production_runtime(tmp_path):
    port = _free_port()
    api_key = "runtime-smoke-key-0123456789abcdef"
    audit_key = "runtime-audit-key-fedcba9876543210"
    metrics_token = "runtime-metrics-token-0011223344556677"
    runtime = tmp_path / "runtime"
    data = tmp_path / "data"
    environment = os.environ.copy()
    environment.update(
        {
            "TEST_MODE": "0",
            "MS_TEST_MODE": "0",
            "MS_ENV": "production",
            "MS_HOST": "127.0.0.1",
            "MS_PORT": str(port),
            "MS_API_KEY": api_key,
            "MS_AUDIT_HMAC_KEY": audit_key,
            "MS_METRICS_BEARER_TOKEN": metrics_token,
            "MS_REQUIRE_API_KEY": "1",
            "MS_PROTECT_READS": "1",
            "MS_TRUSTED_HOSTS": "localhost,127.0.0.1",
            "MS_ALLOWED_ORIGINS": "https://signals.example.test",
            "MS_TLS_TERMINATED": "1",
            "MS_DISABLE_ENGINE_AUTOSTART": "1",
            "MS_DISABLE_WARMUP": "1",
            "MS_DISABLE_STATE_PREFETCH": "1",
            "MS_DISABLE_BG_WORKER": "1",
            "MS_BACKUP_ENABLED": "0",
            "MS_RUNTIME_DIR": str(runtime),
            "MS_DATA_DIR": str(data),
            "MS_CONFIG_PATH": str(runtime / "config.json"),
            "MS_WATCHLIST_PATH": str(runtime / "watchlist.json"),
            "MS_STATE_PATH": str(runtime / "state.json"),
            "MS_SIGNALS_PATH": str(runtime / "signals_state.json"),
            "MS_CAPITAL_STATE_PATH": str(runtime / "capital.json"),
            "MS_SQLITE_DB_PATH": str(data / "market_signals.db"),
            "MS_LOG_DIR": str(tmp_path / "logs"),
            "MS_AUDIT_LOG_DIR": str(tmp_path / "audit_logs"),
            "MS_BACKUP_DIR": str(tmp_path / "backups"),
                "MS_NEWS_BACKUP_PATH": str(data / "news_backup.json"),
                "MS_OPERATOR_AUDIT_PATH": str(data / "operator_audit.jsonl"),
            }
    )
    stdout_path = tmp_path / "server.stdout.log"
    stderr_path = tmp_path / "server.stderr.log"
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "tools" / "container_entrypoint.py")],
            cwd=ROOT,
            env=environment,
            stdout=stdout,
            stderr=stderr,
        )
        try:
            health_url = f"http://127.0.0.1:{port}/api/health"
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(health_url, timeout=1) as response:
                        if response.status == 200:
                            break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.2)
            else:
                pytest.fail("Production runtime did not become healthy in 45 seconds.")

            if process.poll() is not None:
                pytest.fail(
                    "Production runtime exited during startup.\n"
                    + stderr_path.read_text(encoding="utf-8", errors="replace")
                )

            protected_url = f"http://127.0.0.1:{port}/api/state"
            with pytest.raises(urllib.error.HTTPError) as unauthorized:
                urllib.request.urlopen(protected_url, timeout=3)
            assert unauthorized.value.code == 401

            request = urllib.request.Request(
                protected_url,
                headers={"X-API-Key": api_key},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                assert response.status == 200

            metrics_request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/metrics",
                headers={"Authorization": f"Bearer {metrics_token}"},
            )
            with urllib.request.urlopen(metrics_request, timeout=10) as response:
                assert response.status == 200
                assert response.read().endswith(b"# EOF\n")

            mutation = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/not-a-real-route",
                method="POST",
                headers={"X-API-Key": api_key},
            )
            with pytest.raises(urllib.error.HTTPError) as missing_route:
                urllib.request.urlopen(mutation, timeout=10)
            assert missing_route.value.code == 404

            assert (runtime / "config.json").is_file()
            assert (runtime / "watchlist.json").is_file()
            assert (runtime / "state.json").is_file()
            assert (runtime / "signals_state.json").is_file()
            assert (runtime / "capital.json").is_file()
            assert (data / "market_signals.db").is_file()
            assert (data / "operator_audit.jsonl").is_file()
            audit_verification = OperatorAuditLedger(
                path=data / "operator_audit.jsonl",
                key=audit_key,
                required=True,
            ).verify()
            assert audit_verification["verified"] is True
            assert audit_verification["record_count"] == 1
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
