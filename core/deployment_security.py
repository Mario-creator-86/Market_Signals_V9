"""Shared deployment-security validation for every server entrypoint."""

from __future__ import annotations

import os
import hmac
from dataclasses import dataclass
from typing import List, Mapping, Optional
from urllib.parse import urlparse


@dataclass(frozen=True)
class DeploymentSecurity:
    environment: str
    production: bool
    host: str
    warnings: List[str]


class DeploymentSecurityError(ValueError):
    """Raised when a deployment would expose an unsafe configuration."""


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _csv(value: object) -> List[str]:
    return list(
        dict.fromkeys(
            part.strip()
            for part in str(value or "").split(",")
            if part.strip()
        )
    )


def _is_public_bind(host: str) -> bool:
    normalized = str(host or "").strip().lower()
    return normalized not in {"", "127.0.0.1", "localhost", "::1"}


def _validate_origins(
    origins: List[str],
    *,
    production: bool,
    allow_insecure: bool,
) -> None:
    if "*" in origins:
        raise DeploymentSecurityError(
            "MS_ALLOWED_ORIGINS cannot contain '*' when authenticated requests are enabled."
        )
    for origin in origins:
        parsed = urlparse(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise DeploymentSecurityError(
                f"Invalid browser origin in MS_ALLOWED_ORIGINS: {origin!r}."
            )
        hostname = str(parsed.hostname or "").lower()
        local_origin = hostname in {"localhost", "127.0.0.1", "::1"}
        if production and parsed.scheme != "https" and not local_origin and not allow_insecure:
            raise DeploymentSecurityError(
                "Production browser origins must use HTTPS. "
                "Set MS_ALLOW_INSECURE_ORIGINS=1 only behind a trusted private boundary."
            )


def validate_deployment_security(
    host: Optional[str] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> DeploymentSecurity:
    """Validate secrets and network boundaries before application startup."""

    env = environ if environ is not None else os.environ
    deploy_env = str(env.get("MS_ENV", "development") or "development").strip().lower()
    production = deploy_env in {"production", "prod"}
    resolved_host = str(host or env.get("MS_HOST", "127.0.0.1") or "127.0.0.1").strip()
    api_key = str(env.get("MS_API_KEY", "") or "").strip()
    require_key = production or _is_public_bind(resolved_host) or _truthy(
        env.get("MS_REQUIRE_API_KEY")
    )
    if require_key and not api_key:
        raise DeploymentSecurityError(
            "MS_API_KEY is required when MS_ENV=production, MS_REQUIRE_API_KEY=1, "
            "or MS_HOST is publicly reachable."
        )

    if production:
        from core.api_traffic_control import (
            TrafficControlConfig,
            TrafficControlConfigurationError,
        )

        try:
            traffic_config = TrafficControlConfig.from_environment(env, strict=True)
        except TrafficControlConfigurationError as exc:
            raise DeploymentSecurityError(str(exc)) from exc
        if not traffic_config.enabled:
            raise DeploymentSecurityError(
                "MS_TRAFFIC_CONTROL_ENABLED cannot be disabled in production."
            )
        try:
            min_key_length = max(
                24,
                min(128, int(str(env.get("MS_API_KEY_MIN_LENGTH", "32") or "32"))),
            )
        except (TypeError, ValueError):
            min_key_length = 32
        if len(api_key) < min_key_length:
            raise DeploymentSecurityError(
                f"MS_API_KEY must contain at least {min_key_length} characters in production."
            )

    trusted_hosts = _csv(env.get("MS_TRUSTED_HOSTS"))
    if production and not trusted_hosts:
        raise DeploymentSecurityError("MS_TRUSTED_HOSTS must list the production hostnames.")
    if production and "*" in trusted_hosts:
        raise DeploymentSecurityError(
            "MS_TRUSTED_HOSTS cannot contain the unrestricted '*' wildcard in production."
        )

    origins = _csv(env.get("MS_ALLOWED_ORIGINS"))
    _validate_origins(
        origins,
        production=production,
        allow_insecure=_truthy(env.get("MS_ALLOW_INSECURE_ORIGINS")),
    )

    audit_key = str(env.get("MS_AUDIT_HMAC_KEY", "") or "").strip()
    previous_audit_keys = _csv(env.get("MS_AUDIT_HMAC_PREVIOUS_KEYS"))
    if production:
        if not audit_key:
            raise DeploymentSecurityError(
                "MS_AUDIT_HMAC_KEY is required in production for tamper-evident operator auditing."
            )
        if len(audit_key) < 32:
            raise DeploymentSecurityError(
                "MS_AUDIT_HMAC_KEY must contain at least 32 characters in production."
            )
        if hmac.compare_digest(audit_key, api_key):
            raise DeploymentSecurityError(
                "MS_AUDIT_HMAC_KEY must be distinct from MS_API_KEY."
            )
        if any(len(previous_key) < 32 for previous_key in previous_audit_keys):
            raise DeploymentSecurityError(
                "Every MS_AUDIT_HMAC_PREVIOUS_KEYS entry must contain at least 32 characters."
            )
        metrics_token = str(env.get("MS_METRICS_BEARER_TOKEN", "") or "").strip()
        if not metrics_token:
            raise DeploymentSecurityError(
                "MS_METRICS_BEARER_TOKEN is required in production for read-only metric scraping."
            )
        if len(metrics_token) < 32:
            raise DeploymentSecurityError(
                "MS_METRICS_BEARER_TOKEN must contain at least 32 characters in production."
            )
        if hmac.compare_digest(metrics_token, api_key) or hmac.compare_digest(
            metrics_token,
            audit_key,
        ):
            raise DeploymentSecurityError(
                "MS_METRICS_BEARER_TOKEN must be distinct from API and audit keys."
            )

    warnings: List[str] = []
    if production and not origins:
        warnings.append(
            "MS_ALLOWED_ORIGINS is unset; only same-origin browser requests will be accepted."
        )
    if production and not (
        _truthy(env.get("MS_FORCE_HTTPS"))
        or _truthy(env.get("MS_TLS_TERMINATED"))
    ):
        warnings.append(
            "Neither MS_FORCE_HTTPS nor MS_TLS_TERMINATED is set; verify HTTPS at the edge."
        )

    return DeploymentSecurity(
        environment=deploy_env,
        production=production,
        host=resolved_host,
        warnings=warnings,
    )
