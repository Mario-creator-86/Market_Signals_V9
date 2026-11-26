import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple


ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)
REPORT_JSON = OUT_DIR / "release_check_report.json"
REPORT_TXT = OUT_DIR / "release_check_report.txt"


def fresh_loopback_port() -> int:
    """Reserve a presently-unused port for one isolated E2E subprocess."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run_cmd(cmd: List[str], timeout: int = 900, env: dict | None = None) -> Tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        return proc.returncode, output.strip()
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}"


def main() -> int:
    issues = []
    checks = []

    env = os.environ.copy()
    env.setdefault("TEST_MODE", "1")
    env.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")
    env.setdefault("MS_DISABLE_WARMUP", "1")
    env.setdefault("MS_DISABLE_STATE_PREFETCH", "1")

    def record(name: str, cmd: List[str], timeout: int = 900, check_env: dict | None = None):
        code, output = run_cmd(cmd, timeout=timeout, env=check_env or env)
        checks.append({"name": name, "cmd": cmd, "code": code, "output": output[:20000]})
        if code != 0:
            issues.append(f"{name} failed (exit={code})")

    record(
        "smoke_suite",
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-m",
            "smoke and not e2e",
            "tests/unit",
            "tests/integration",
            "tests/contract",
            "tests/perf/test_load_smoke.py",
        ],
        timeout=1200,
    )
    e2e_env = env.copy()
    e2e_env["RUN_E2E"] = "1"
    # Release checks must never inherit mutable/rate-limit state from a prior
    # browser session. An explicit E2E_BASE_URL remains the only opt-in reuse.
    if not e2e_env.get("E2E_BASE_URL"):
        e2e_env["E2E_PORT"] = str(fresh_loopback_port())
    record(
        "e2e_smoke",
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests_e2e/test_ui_smoke.py",
            "tests_e2e/test_quality_tabs.py",
            "-k",
            "quality_tabs_navigation_and_console_clean or refresh_buttons_follow_agent_disabled_state",
        ],
        timeout=1200,
        check_env=e2e_env,
    )
    record(
        "no_external_calls",
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/integration/test_diagnostics_framework.py::test_no_external_calls_when_cache_is_warm",
        ],
        timeout=300,
    )
    record(
        "lint_syntax",
        [sys.executable, "-m", "compileall", "-q", "api", "core", "agents", "tests", "tests_e2e"],
        timeout=300,
    )

    record(
        "diagnostics_endpoint",
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/integration/test_diagnostics_framework.py::test_health_diagnostics_contract",
        ],
        timeout=300,
    )
    record(
        "production_security_contract",
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/unit/test_api_security.py",
            "tests/unit/test_api_traffic_control.py",
            "tests/contract/test_operator_auth_ui_contract.py",
        ],
        timeout=300,
    )
    record(
        "disaster_recovery_contract",
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/unit/test_disaster_recovery.py",
            "tests/unit/test_disaster_recovery_api.py",
            "tests/unit/test_backup_manager_cli.py",
        ],
        timeout=300,
    )
    record(
        "container_deployment_contract",
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/unit/test_container_entrypoint.py",
            "tests/contract/test_container_deployment_contract.py",
            "tests/integration/test_container_runtime_boot.py",
        ],
        timeout=300,
    )
    record(
        "operator_audit_contract",
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/unit/test_operator_audit.py",
            "tests/unit/test_operator_audit_api.py",
        ],
        timeout=300,
    )
    record(
        "monitoring_slo_contract",
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/unit/test_openmetrics_exporter.py",
            "tests/contract/test_monitoring_contract.py",
        ],
        timeout=300,
    )
    record(
        "supply_chain_contract",
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/unit/test_supply_chain_policy.py",
        ],
        timeout=300,
    )

    # Basic unhandled exception scan.
    scan_files = [
        ROOT / "logs" / "api_server.err",
        ROOT / "logs" / "api_server_live.err",
        ROOT / "output" / "playwright" / "console_errors.txt",
    ]
    unhandled_hits = []
    pattern = re.compile(r"unhandled|traceback|exception", re.IGNORECASE)
    for p in scan_files:
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            if pattern.search(line):
                unhandled_hits.append(f"{p.name}: {line[:180]}")
                if len(unhandled_hits) >= 50:
                    break
        if len(unhandled_hits) >= 50:
            break
    if unhandled_hits:
        issues.append("Unhandled errors found in runtime logs")

    status = "PASS" if not issues else "FAIL"
    top_issues = issues[:10]
    suggestions = [
        "Ruleaza local `make test-smoke` inainte de push.",
        "Verifica `/api/health/diagnostics` pentru agenti cu `last_error`.",
        "Verifica lantul operatorului cu `python tools/operator_audit.py verify`.",
        "Ruleaza `make supply-chain` pentru audit CVE si un SBOM CycloneDX nou.",
        "Daca pica contract tests, actualizeaza schema endpoint-ului in tests/contract/schemas/.",
    ]

    report = {
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "issues_count": len(issues),
        "top_issues": top_issues,
        "suggestions": suggestions,
        "checks": checks,
        "unhandled_log_hits": unhandled_hits[:50],
    }

    REPORT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    REPORT_TXT.write_text(
        "\n".join(
            [
                f"Release Check: {status}",
                f"Generated: {report['generated_at']}",
                f"Issues: {len(issues)}",
                "",
                "Top issues:",
                *([f"- {x}" for x in top_issues] or ["- none"]),
                "",
                "Suggestions:",
                *[f"- {x}" for x in suggestions],
            ]
        ),
        encoding="utf-8",
    )

    print(f"[release_check] {status}")
    print(f"[release_check] report: {REPORT_JSON}")
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
