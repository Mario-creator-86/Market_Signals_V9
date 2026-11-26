"""Deployment-safe HTTP middleware contracts."""

from api import main
from core.deployment_security import (
    DeploymentSecurityError,
    validate_deployment_security,
)
from run_server import _validate_deployment_security


def test_api_responses_include_defensive_browser_headers(client):
    response = client.get("/api/engine/status")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"


def test_production_auth_rejects_query_string_credentials(client, monkeypatch):
    monkeypatch.setattr(main, "_AUTH_ENABLED", True)
    monkeypatch.setattr(main, "_API_KEY", "test-secret")
    monkeypatch.setattr(main, "_IS_PRODUCTION", True)

    rejected = client.post("/api/not-a-real-route?api_key=test-secret")
    accepted_header = client.post("/api/not-a-real-route", headers={"X-API-Key": "test-secret"})

    assert rejected.status_code == 401
    assert accepted_header.status_code == 404


def test_patch_requests_use_the_write_rate_limit(client, monkeypatch):
    main._rate_limit_store.clear()
    monkeypatch.setattr(main, "_RATE_LIMIT_MAX_WRITES", 1)

    first = client.patch("/api/not-a-real-route")
    blocked = client.patch("/api/not-a-real-route")

    assert first.status_code == 404
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"]


def test_public_bind_requires_api_key(monkeypatch):
    monkeypatch.delenv("MS_API_KEY", raising=False)
    monkeypatch.delenv("MS_ENV", raising=False)
    monkeypatch.delenv("MS_REQUIRE_API_KEY", raising=False)

    try:
        _validate_deployment_security("0.0.0.0")
    except SystemExit as exc:
        assert "MS_API_KEY is required" in str(exc)
    else:
        raise AssertionError("public bind without MS_API_KEY must fail")


def test_production_requires_trusted_hosts(monkeypatch):
    monkeypatch.setenv("MS_ENV", "production")
    monkeypatch.setenv("MS_API_KEY", "test-secret-that-is-at-least-32-chars")
    monkeypatch.delenv("MS_TRUSTED_HOSTS", raising=False)

    try:
        _validate_deployment_security("127.0.0.1")
    except SystemExit as exc:
        assert "MS_TRUSTED_HOSTS" in str(exc)
    else:
        raise AssertionError("production without trusted hosts must fail")


def test_production_rejects_weak_api_key():
    try:
        validate_deployment_security(
            environ={
                "MS_ENV": "production",
                "MS_API_KEY": "too-short",
                "MS_TRUSTED_HOSTS": "signals.example.com",
                "MS_TLS_TERMINATED": "1",
            }
        )
    except DeploymentSecurityError as exc:
        assert "at least 32" in str(exc)
    else:
        raise AssertionError("weak production API key must fail")


def test_production_rejects_wildcards_and_insecure_origins():
    base = {
        "MS_ENV": "production",
        "MS_API_KEY": "a" * 40,
        "MS_TRUSTED_HOSTS": "signals.example.com",
        "MS_TLS_TERMINATED": "1",
    }
    for patch, expected in (
        ({"MS_TRUSTED_HOSTS": "*"}, "MS_TRUSTED_HOSTS"),
        ({"MS_ALLOWED_ORIGINS": "*"}, "MS_ALLOWED_ORIGINS"),
        ({"MS_ALLOWED_ORIGINS": "http://signals.example.com"}, "HTTPS"),
        ({"MS_ALLOWED_ORIGINS": "https://user:pass@signals.example.com"}, "Invalid browser origin"),
    ):
        try:
            validate_deployment_security(environ={**base, **patch})
        except DeploymentSecurityError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f"unsafe production config must fail: {patch}")


def test_valid_production_boundary_passes_without_warnings():
    result = validate_deployment_security(
        environ={
            "MS_ENV": "production",
            "MS_API_KEY": "a" * 40,
            "MS_AUDIT_HMAC_KEY": "b" * 40,
            "MS_METRICS_BEARER_TOKEN": "c" * 40,
            "MS_TRUSTED_HOSTS": "signals.example.com",
            "MS_ALLOWED_ORIGINS": "https://signals.example.com",
            "MS_TLS_TERMINATED": "1",
        }
    )

    assert result.production is True
    assert result.warnings == []


def test_production_requires_valid_enabled_traffic_controls():
    base = {
        "MS_ENV": "production",
        "MS_API_KEY": "a" * 40,
        "MS_AUDIT_HMAC_KEY": "b" * 40,
        "MS_METRICS_BEARER_TOKEN": "c" * 40,
        "MS_TRUSTED_HOSTS": "signals.example.com",
        "MS_ALLOWED_ORIGINS": "https://signals.example.com",
        "MS_TLS_TERMINATED": "1",
    }
    for patch, expected in (
        ({"MS_TRAFFIC_CONTROL_ENABLED": "0"}, "cannot be disabled"),
        ({"MS_RATE_LIMIT_READS": "invalid"}, "must be an integer"),
        (
            {
                "MS_API_MAX_INFLIGHT": "2",
                "MS_API_MAX_INFLIGHT_PER_CLIENT": "3",
            },
            "cannot exceed",
        ),
    ):
        try:
            validate_deployment_security(environ={**base, **patch})
        except DeploymentSecurityError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f"unsafe traffic controls must fail startup: {patch}")


def test_production_requires_distinct_strong_operator_audit_key():
    base = {
        "MS_ENV": "production",
        "MS_API_KEY": "a" * 40,
        "MS_TRUSTED_HOSTS": "signals.example.com",
        "MS_ALLOWED_ORIGINS": "https://signals.example.com",
        "MS_TLS_TERMINATED": "1",
    }
    for audit_key, expected in (
        ("", "required"),
        ("too-short", "at least 32"),
        ("a" * 40, "distinct"),
    ):
        try:
            validate_deployment_security(
                environ={**base, "MS_AUDIT_HMAC_KEY": audit_key}
            )
        except DeploymentSecurityError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError("unsafe audit signing key must fail production startup")

    try:
        validate_deployment_security(
            environ={
                **base,
                "MS_AUDIT_HMAC_KEY": "b" * 40,
                "MS_AUDIT_HMAC_PREVIOUS_KEYS": "old-key-is-too-short",
            }
        )
    except DeploymentSecurityError as exc:
        assert "PREVIOUS_KEYS" in str(exc)
    else:
        raise AssertionError("weak previous audit keys must fail production startup")


def test_production_requires_distinct_strong_metrics_token():
    base = {
        "MS_ENV": "production",
        "MS_API_KEY": "a" * 40,
        "MS_AUDIT_HMAC_KEY": "b" * 40,
        "MS_TRUSTED_HOSTS": "signals.example.com",
        "MS_ALLOWED_ORIGINS": "https://signals.example.com",
        "MS_TLS_TERMINATED": "1",
    }
    for token, expected in (
        ("", "required"),
        ("too-short", "at least 32"),
        ("a" * 40, "distinct"),
        ("b" * 40, "distinct"),
    ):
        try:
            validate_deployment_security(
                environ={**base, "MS_METRICS_BEARER_TOKEN": token}
            )
        except DeploymentSecurityError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError("unsafe metrics token must fail production startup")


def test_browser_session_supports_protected_reads_and_csrf_writes(client, monkeypatch):
    secret = "browser-session-test-secret-that-is-long"
    monkeypatch.setattr(main, "_AUTH_ENABLED", True)
    monkeypatch.setattr(main, "_API_KEY", secret)
    monkeypatch.setattr(main, "_PROTECT_READS", True)
    monkeypatch.setattr(main, "_IS_PRODUCTION", False)
    monkeypatch.setattr(main, "_FORCE_HTTPS", False)

    unauthenticated = client.get("/api/engine/status")
    assert unauthenticated.status_code == 401

    login = client.post("/api/auth/login", json={"api_key": secret})
    assert login.status_code == 200
    login_payload = login.json()
    csrf_token = login_payload["csrf_token"]
    assert csrf_token
    assert login.headers["cache-control"] == "no-store"
    assert "httponly" in login.headers["set-cookie"].lower()
    assert "samesite=strict" in login.headers["set-cookie"].lower()

    status = client.get("/api/auth/status")
    assert status.status_code == 200
    assert status.json()["authenticated"] is True
    assert status.json()["csrf_token"] == csrf_token
    assert status.headers["cache-control"] == "no-store"

    protected_read = client.get("/api/engine/status")
    assert protected_read.status_code == 200

    csrf_rejected = client.post("/api/not-a-real-route")
    assert csrf_rejected.status_code == 403

    csrf_accepted = client.post(
        "/api/not-a-real-route",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert csrf_accepted.status_code == 404

    logout = client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert logout.status_code == 200
    assert logout.headers["cache-control"] == "no-store"
    assert client.get("/api/engine/status").status_code == 401


def test_cors_preflight_allows_csrf_header(client):
    response = client.options(
        "/api/auth/logout",
        headers={
            "Origin": "http://localhost:8003",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-CSRF-Token",
        },
    )

    assert response.status_code == 200
    assert "x-csrf-token" in response.headers["access-control-allow-headers"].lower()
