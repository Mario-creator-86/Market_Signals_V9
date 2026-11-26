"""Bounded API traffic protection for rate, body-size, and concurrency abuse."""

from __future__ import annotations

import ipaddress
import re
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass
from typing import Callable, Deque, Mapping, MutableMapping, Optional

from starlette.responses import JSONResponse


_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_EXEMPT_PATHS = frozenset({"/api/health"})


class TrafficControlConfigurationError(ValueError):
    """Raised when traffic controls are unsafe or malformed."""


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _integer(
    environment: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
    strict: bool,
) -> int:
    raw = environment.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        if strict:
            raise TrafficControlConfigurationError(f"{name} must be an integer.") from exc
        return default
    if strict and not minimum <= value <= maximum:
        raise TrafficControlConfigurationError(
            f"{name} must be between {minimum} and {maximum}."
        )
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class TrafficControlConfig:
    enabled: bool = True
    window_seconds: int = 60
    read_requests: int = 2400
    write_requests: int = 60
    auth_requests: int = 20
    max_buckets: int = 10_000
    max_request_body_bytes: int = 1_048_576
    max_inflight: int = 512
    max_inflight_per_client: int = 128

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
        *,
        strict: bool = False,
    ) -> "TrafficControlConfig":
        config = cls(
            enabled=_truthy(environment.get("MS_TRAFFIC_CONTROL_ENABLED", "1")),
            window_seconds=_integer(
                environment,
                "MS_RATE_LIMIT_WINDOW_SECONDS",
                60,
                minimum=1,
                maximum=3600,
                strict=strict,
            ),
            read_requests=_integer(
                environment,
                "MS_RATE_LIMIT_READS",
                2400,
                minimum=1,
                maximum=1_000_000,
                strict=strict,
            ),
            write_requests=_integer(
                environment,
                "MS_RATE_LIMIT_WRITES",
                60,
                minimum=1,
                maximum=100_000,
                strict=strict,
            ),
            auth_requests=_integer(
                environment,
                "MS_RATE_LIMIT_AUTH_ATTEMPTS",
                20,
                minimum=1,
                maximum=10_000,
                strict=strict,
            ),
            max_buckets=_integer(
                environment,
                "MS_RATE_LIMIT_MAX_BUCKETS",
                10_000,
                minimum=100,
                maximum=100_000,
                strict=strict,
            ),
            max_request_body_bytes=_integer(
                environment,
                "MS_MAX_REQUEST_BODY_BYTES",
                1_048_576,
                minimum=65_536,
                maximum=10_485_760,
                strict=strict,
            ),
            max_inflight=_integer(
                environment,
                "MS_API_MAX_INFLIGHT",
                512,
                minimum=1,
                maximum=1024,
                strict=strict,
            ),
            max_inflight_per_client=_integer(
                environment,
                "MS_API_MAX_INFLIGHT_PER_CLIENT",
                128,
                minimum=1,
                maximum=256,
                strict=strict,
            ),
        )
        if config.max_inflight_per_client > config.max_inflight:
            if strict:
                raise TrafficControlConfigurationError(
                    "MS_API_MAX_INFLIGHT_PER_CLIENT cannot exceed MS_API_MAX_INFLIGHT."
                )
            config = cls(
                **{
                    **config.__dict__,
                    "max_inflight_per_client": config.max_inflight,
                }
            )
        return config


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    category: str
    limit: int
    remaining: int
    reset_after: int
    reason: str = ""


@dataclass(frozen=True)
class ConcurrencyDecision:
    allowed: bool
    reason: str = ""


class ApiTrafficController:
    """Thread-safe, bounded in-process admission controller."""

    def __init__(
        self,
        config: TrafficControlConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self._clock = clock
        self._lock = threading.RLock()
        self._buckets: "OrderedDict[str, Deque[float]]" = OrderedDict()
        self._active_total = 0
        self._active_by_client: MutableMapping[str, int] = defaultdict(int)
        self._accepted_total = 0
        self._rejections: MutableMapping[str, int] = defaultdict(int)
        self._last_maintenance = 0.0

    @property
    def buckets(self) -> MutableMapping[str, Deque[float]]:
        return self._buckets

    @staticmethod
    def normalize_client(value: object) -> str:
        raw = str(value or "unknown").strip()
        try:
            return ipaddress.ip_address(raw).compressed
        except ValueError:
            return "unknown"

    @staticmethod
    def category(method: str, path: str) -> str:
        if str(path or "") == "/api/auth/login":
            return "auth"
        if str(method or "").upper() in _MUTATING_METHODS:
            return "write"
        return "read"

    def _limit(self, category: str, overrides: Optional[Mapping[str, int]]) -> int:
        defaults = {
            "read": self.config.read_requests,
            "write": self.config.write_requests,
            "auth": self.config.auth_requests,
        }
        value = (overrides or {}).get(category, defaults[category])
        return max(1, int(value))

    def _prune_bucket(self, key: str, cutoff: float) -> None:
        bucket = self._buckets.get(key)
        if bucket is None:
            return
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if not bucket:
            self._buckets.pop(key, None)

    def _maintenance(self, now: float) -> None:
        if now - self._last_maintenance < min(10.0, self.config.window_seconds):
            return
        cutoff = now - self.config.window_seconds
        for key in list(self._buckets.keys())[:256]:
            self._prune_bucket(key, cutoff)
        self._last_maintenance = now

    def check_rate(
        self,
        client: str,
        method: str,
        path: str,
        *,
        limit_overrides: Optional[Mapping[str, int]] = None,
    ) -> RateDecision:
        category = self.category(method, path)
        limit = self._limit(category, limit_overrides)
        now = self._clock()
        cutoff = now - self.config.window_seconds
        client_key = self.normalize_client(client)
        key = f"{client_key}:{category}"
        with self._lock:
            self._maintenance(now)
            self._prune_bucket(key, cutoff)
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self.config.max_buckets:
                    self._rejections["bucket_capacity"] += 1
                    return RateDecision(
                        False,
                        category,
                        limit,
                        0,
                        self.config.window_seconds,
                        "bucket_capacity",
                    )
                bucket = deque()
                self._buckets[key] = bucket
            self._buckets.move_to_end(key)
            if len(bucket) >= limit:
                reset_after = max(1, int(self.config.window_seconds - (now - bucket[0])))
                self._rejections[f"rate_{category}"] += 1
                return RateDecision(
                    False,
                    category,
                    limit,
                    0,
                    reset_after,
                    f"rate_{category}",
                )
            bucket.append(now)
            self._accepted_total += 1
            return RateDecision(
                True,
                category,
                limit,
                max(0, limit - len(bucket)),
                self.config.window_seconds,
            )

    def try_enter(self, client: str) -> ConcurrencyDecision:
        client_key = self.normalize_client(client)
        with self._lock:
            if self._active_total >= self.config.max_inflight:
                self._rejections["global_concurrency"] += 1
                return ConcurrencyDecision(False, "global_concurrency")
            if self._active_by_client[client_key] >= self.config.max_inflight_per_client:
                self._rejections["client_concurrency"] += 1
                return ConcurrencyDecision(False, "client_concurrency")
            self._active_total += 1
            self._active_by_client[client_key] += 1
            return ConcurrencyDecision(True)

    def leave(self, client: str) -> None:
        client_key = self.normalize_client(client)
        with self._lock:
            self._active_total = max(0, self._active_total - 1)
            current = max(0, self._active_by_client.get(client_key, 0) - 1)
            if current:
                self._active_by_client[client_key] = current
            else:
                self._active_by_client.pop(client_key, None)

    def record_rejection(self, reason: str) -> None:
        with self._lock:
            self._rejections[str(reason or "unknown")] += 1

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._active_total = 0
            self._active_by_client.clear()
            self._accepted_total = 0
            self._rejections.clear()
            self._last_maintenance = 0.0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "enabled": self.config.enabled,
                "window_seconds": self.config.window_seconds,
                "limits": {
                    "read": self.config.read_requests,
                    "write": self.config.write_requests,
                    "auth": self.config.auth_requests,
                },
                "bucket_count": len(self._buckets),
                "max_buckets": self.config.max_buckets,
                "max_request_body_bytes": self.config.max_request_body_bytes,
                "active_requests": self._active_total,
                "active_clients": len(self._active_by_client),
                "max_inflight": self.config.max_inflight,
                "max_inflight_per_client": self.config.max_inflight_per_client,
                "accepted_total": self._accepted_total,
                "rejections_total": dict(self._rejections),
            }


def normalize_request_id(value: object) -> str:
    candidate = str(value or "").strip()
    return candidate if _REQUEST_ID_RE.fullmatch(candidate) else str(uuid.uuid4())


def _scope_headers(scope: dict) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


def _replace_scope_header(scope: dict, name: str, value: str) -> None:
    encoded_name = name.lower().encode("latin-1")
    headers = [
        (key, item_value)
        for key, item_value in scope.get("headers", [])
        if key.lower() != encoded_name
    ]
    headers.append((encoded_name, value.encode("latin-1")))
    scope["headers"] = headers


def ensure_scope_request_id(scope: dict) -> str:
    """Normalize one bounded request ID and make it visible to inner middleware."""
    headers = _scope_headers(scope)
    request_id = normalize_request_id(
        headers.get("x-request-id") or headers.get("x-correlation-id")
    )
    _replace_scope_header(scope, "x-request-id", request_id)
    return request_id


def _safe_headers(request_id: str, *, retry_after: Optional[int] = None) -> dict[str, str]:
    headers = {
        "Cache-Control": "no-store",
        "X-Request-ID": request_id,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
    }
    if retry_after is not None:
        headers["Retry-After"] = str(max(1, int(retry_after)))
    return headers


class ApiTrafficProtectionMiddleware:
    """Pure ASGI middleware that fails before expensive request processing."""

    def __init__(
        self,
        app,
        *,
        controller: ApiTrafficController,
        rate_limits_provider: Optional[Callable[[], Mapping[str, int]]] = None,
    ):
        self.app = app
        self.controller = controller
        self.rate_limits_provider = rate_limits_provider

    async def _respond(
        self,
        scope: dict,
        receive,
        send,
        *,
        status_code: int,
        request_id: str,
        code: str,
        detail: str,
        retry_after: Optional[int] = None,
        rate: Optional[RateDecision] = None,
    ) -> None:
        headers = _safe_headers(request_id, retry_after=retry_after)
        if rate is not None:
            headers.update(
                {
                    "RateLimit-Limit": str(rate.limit),
                    "RateLimit-Remaining": str(rate.remaining),
                    "RateLimit-Reset": str(rate.reset_after),
                    "X-RateLimit-Limit": str(rate.limit),
                    "X-RateLimit-Remaining": str(rate.remaining),
                }
            )
        response = JSONResponse(
            status_code=status_code,
            headers=headers,
            content={
                "detail": detail,
                "code": code,
                "request_id": request_id,
            },
        )
        await response(scope, receive, send)

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if (
            not self.controller.config.enabled
            or not path.startswith("/api/")
            or path in _EXEMPT_PATHS
        ):
            await self.app(scope, receive, send)
            return

        headers = _scope_headers(scope)
        request_id = ensure_scope_request_id(scope)
        method = str(scope.get("method") or "GET").upper()
        client = (scope.get("client") or ("unknown", 0))[0]

        raw_content_lengths = [
            value.decode("latin-1").strip()
            for key, value in scope.get("headers", [])
            if key.lower() == b"content-length"
        ]
        transfer_encoding = str(headers.get("transfer-encoding") or "").strip().lower()
        ambiguous_framing = (
            len(set(raw_content_lengths)) > 1
            or bool(raw_content_lengths and transfer_encoding not in {"", "identity"})
        )
        if ambiguous_framing:
            self.controller.record_rejection("invalid_content_length")
            await self._respond(
                scope,
                receive,
                send,
                status_code=400,
                request_id=request_id,
                code="invalid_request_framing",
                detail="Ambiguous request framing is not accepted.",
            )
            return

        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except (TypeError, ValueError):
                self.controller.record_rejection("invalid_content_length")
                await self._respond(
                    scope,
                    receive,
                    send,
                    status_code=400,
                    request_id=request_id,
                    code="invalid_content_length",
                    detail="Invalid Content-Length header.",
                )
                return
            if declared_size < 0:
                self.controller.record_rejection("invalid_content_length")
                await self._respond(
                    scope,
                    receive,
                    send,
                    status_code=400,
                    request_id=request_id,
                    code="invalid_content_length",
                    detail="Invalid Content-Length header.",
                )
                return
            if declared_size > self.controller.config.max_request_body_bytes:
                self.controller.record_rejection("request_body_too_large")
                await self._respond(
                    scope,
                    receive,
                    send,
                    status_code=413,
                    request_id=request_id,
                    code="request_body_too_large",
                    detail="Request body exceeds the configured limit.",
                )
                return

        overrides = self.rate_limits_provider() if self.rate_limits_provider else None
        rate = self.controller.check_rate(
            str(client),
            method,
            path,
            limit_overrides=overrides,
        )
        if not rate.allowed:
            await self._respond(
                scope,
                receive,
                send,
                status_code=429,
                request_id=request_id,
                code="rate_limit_exceeded",
                detail="Too many requests. Please try again later.",
                retry_after=rate.reset_after,
                rate=rate,
            )
            return

        concurrency = self.controller.try_enter(str(client))
        if not concurrency.allowed:
            status_code = 503 if concurrency.reason == "global_concurrency" else 429
            await self._respond(
                scope,
                receive,
                send,
                status_code=status_code,
                request_id=request_id,
                code="request_capacity_exceeded",
                detail="Request capacity is temporarily exhausted.",
                retry_after=1,
                rate=rate,
            )
            return

        buffered_messages = []
        replay_index = 0

        async def replay_receive():
            nonlocal replay_index
            if replay_index < len(buffered_messages):
                message = buffered_messages[replay_index]
                replay_index += 1
                return message
            return await receive()

        async def add_admission_headers(message):
            if message.get("type") == "http.response.start":
                response_headers = list(message.get("headers") or [])
                existing = {key.lower() for key, _ in response_headers}
                additions = {
                    b"ratelimit-limit": str(rate.limit).encode("ascii"),
                    b"ratelimit-remaining": str(rate.remaining).encode("ascii"),
                    b"ratelimit-reset": str(rate.reset_after).encode("ascii"),
                    b"x-request-id": request_id.encode("ascii"),
                }
                for key, value in additions.items():
                    if key not in existing:
                        response_headers.append((key, value))
                message["headers"] = response_headers
            await send(message)

        try:
            if method in _BODY_METHODS:
                received_size = 0
                while True:
                    message = await receive()
                    buffered_messages.append(message)
                    if message.get("type") == "http.disconnect":
                        break
                    if message.get("type") != "http.request":
                        continue
                    received_size += len(message.get("body") or b"")
                    if received_size > self.controller.config.max_request_body_bytes:
                        self.controller.record_rejection("request_body_too_large")
                        await self._respond(
                            scope,
                            receive,
                            send,
                            status_code=413,
                            request_id=request_id,
                            code="request_body_too_large",
                            detail="Request body exceeds the configured limit.",
                        )
                        return
                    if not message.get("more_body", False):
                        break
            await self.app(scope, replay_receive, add_admission_headers)
        finally:
            self.controller.leave(str(client))


_CONTROLLER: Optional[ApiTrafficController] = None
_CONTROLLER_LOCK = threading.Lock()


def get_api_traffic_controller(
    environment: Optional[Mapping[str, str]] = None,
) -> ApiTrafficController:
    global _CONTROLLER
    with _CONTROLLER_LOCK:
        if _CONTROLLER is None:
            import os

            _CONTROLLER = ApiTrafficController(
                TrafficControlConfig.from_environment(environment or os.environ)
            )
        return _CONTROLLER
