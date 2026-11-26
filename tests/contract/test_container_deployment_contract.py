from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_runs_as_non_root_with_healthcheck_and_pinned_python_line():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM python:3.12-slim-bookworm" in dockerfile
    assert "USER market-signals:market-signals" in dockerfile
    assert dockerfile.index("COPY . /app") < dockerfile.index(
        "USER market-signals:market-signals"
    )
    assert "HEALTHCHECK" in dockerfile
    assert "/api/health" in dockerfile
    assert "ENTRYPOINT" in dockerfile
    assert "MS_API_KEY=" not in dockerfile


def test_production_compose_is_fail_closed_and_persists_all_runtime_state():
    compose = (ROOT / "compose.production.yml").read_text(encoding="utf-8")

    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:" in compose and "- ALL" in compose
    assert "127.0.0.1:${MS_PUBLIC_PORT:-8003}:8003" in compose
    assert "${MS_API_KEY:?" in compose
    assert "${MS_AUDIT_HMAC_KEY:?" in compose
    assert "${MS_METRICS_BEARER_TOKEN:?" in compose
    assert "${MS_PUBLIC_HOST:?" in compose
    assert "${MS_PUBLIC_ORIGIN:?" in compose
    assert "MS_PROTECT_READS: \"1\"" in compose
    assert "MS_TRAFFIC_CONTROL_ENABLED: \"1\"" in compose
    assert "MS_MAX_REQUEST_BODY_BYTES:" in compose
    assert "MS_API_MAX_INFLIGHT:" in compose
    for target in (
        "/app/runtime",
        "/app/data",
        "/app/backups",
        "/app/logs",
        "/app/audit_logs",
    ):
        assert target in compose


def test_container_context_excludes_secrets_runtime_state_and_debug_artifacts():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in ignored
    assert "config.json" in ignored
    assert "state.json" in ignored
    assert "signals_state.json" in ignored
    assert "data/**" in ignored
    assert "tests" in ignored


def test_ci_builds_and_boots_hardened_production_image():
    workflow = (ROOT / ".github" / "workflows" / "quality-gate.yml").read_text(
        encoding="utf-8"
    )

    assert "container-runtime:" in workflow
    assert "docker build --tag market-signals:ci ." in workflow
    assert "--read-only" in workflow
    assert "--cap-drop ALL" in workflow
    assert "MS_ENV=production" in workflow
    assert "unauthenticated_code" in workflow
