from core.api_traffic_control import (
    ApiTrafficController,
    TrafficControlConfig,
    TrafficControlConfigurationError,
)


def _config(**overrides):
    values = {
        "enabled": True,
        "window_seconds": 10,
        "read_requests": 2,
        "write_requests": 1,
        "auth_requests": 1,
        "max_buckets": 2,
        "max_request_body_bytes": 65_536,
        "max_inflight": 2,
        "max_inflight_per_client": 1,
    }
    values.update(overrides)
    return TrafficControlConfig(**values)


def test_sliding_rate_limit_uses_request_class_and_recovers():
    now = [100.0]
    controller = ApiTrafficController(_config(), clock=lambda: now[0])

    first = controller.check_rate("127.0.0.1", "GET", "/api/state")
    second = controller.check_rate("127.0.0.1", "GET", "/api/state")
    blocked = controller.check_rate("127.0.0.1", "GET", "/api/state")
    write = controller.check_rate("127.0.0.1", "POST", "/api/config")

    assert (first.allowed, first.remaining) == (True, 1)
    assert (second.allowed, second.remaining) == (True, 0)
    assert blocked.allowed is False
    assert blocked.reason == "rate_read"
    assert write.allowed is True

    now[0] += 11
    recovered = controller.check_rate("127.0.0.1", "GET", "/api/state")
    assert recovered.allowed is True


def test_auth_attempts_have_an_independent_strict_bucket():
    controller = ApiTrafficController(_config())

    accepted = controller.check_rate("127.0.0.1", "POST", "/api/auth/login")
    blocked = controller.check_rate("127.0.0.1", "POST", "/api/auth/login")

    assert accepted.category == "auth"
    assert blocked.allowed is False
    assert blocked.reason == "rate_auth"


def test_bucket_cardinality_is_bounded_and_fails_closed():
    controller = ApiTrafficController(_config(max_buckets=1))

    assert controller.check_rate("192.0.2.1", "GET", "/api/state").allowed is True
    rejected = controller.check_rate("192.0.2.2", "GET", "/api/state")

    assert rejected.allowed is False
    assert rejected.reason == "bucket_capacity"
    assert controller.snapshot()["bucket_count"] == 1


def test_global_and_per_client_concurrency_are_released():
    controller = ApiTrafficController(
        _config(max_inflight=2, max_inflight_per_client=1)
    )

    assert controller.try_enter("192.0.2.1").allowed is True
    same_client = controller.try_enter("192.0.2.1")
    assert same_client.allowed is False
    assert same_client.reason == "client_concurrency"
    assert controller.try_enter("192.0.2.2").allowed is True
    global_limit = controller.try_enter("192.0.2.3")
    assert global_limit.allowed is False
    assert global_limit.reason == "global_concurrency"

    controller.leave("192.0.2.1")
    assert controller.try_enter("192.0.2.3").allowed is True
    assert controller.snapshot()["active_requests"] == 2


def test_strict_environment_validation_rejects_unsafe_values():
    for environment, expected in (
        ({"MS_RATE_LIMIT_READS": "not-a-number"}, "must be an integer"),
        ({"MS_MAX_REQUEST_BODY_BYTES": "1"}, "between"),
        (
            {
                "MS_API_MAX_INFLIGHT": "2",
                "MS_API_MAX_INFLIGHT_PER_CLIENT": "3",
            },
            "cannot exceed",
        ),
    ):
        try:
            TrafficControlConfig.from_environment(environment, strict=True)
        except TrafficControlConfigurationError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f"unsafe traffic configuration accepted: {environment}")


def test_api_responses_expose_rate_budget_and_safe_request_id(client):
    from api import main

    main._traffic_controller.reset()
    response = client.get(
        "/api/engine/status",
        headers={"X-Request-ID": "x" * 300},
    )

    assert response.status_code == 200
    assert response.headers["ratelimit-limit"] == str(main._RATE_LIMIT_MAX_READS)
    assert int(response.headers["ratelimit-remaining"]) >= 0
    assert len(response.headers["x-request-id"]) <= 128
    assert response.headers["x-request-id"] != "x" * 300


def test_oversized_api_body_is_rejected_before_routing(client):
    from api import main

    main._traffic_controller.reset()
    body = b"x" * (main._TRAFFIC_CONFIG.max_request_body_bytes + 1)
    response = client.post(
        "/api/not-a-real-route",
        content=body,
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 413
    assert response.json()["code"] == "request_body_too_large"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-request-id"]


def test_oversized_streamed_body_without_content_length_is_rejected(client):
    from api import main

    main._traffic_controller.reset()
    chunk_size = main._TRAFFIC_CONFIG.max_request_body_bytes // 2 + 1

    def chunks():
        yield b"a" * chunk_size
        yield b"b" * chunk_size

    response = client.post(
        "/api/auth/login",
        content=chunks(),
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 413
    assert response.json()["code"] == "request_body_too_large"


def test_rate_rejection_has_retry_budget_and_security_headers(client, monkeypatch):
    from api import main

    main._traffic_controller.reset()
    monkeypatch.setattr(main, "_RATE_LIMIT_MAX_READS", 1)

    assert client.get("/api/engine/status").status_code == 200
    blocked = client.get("/api/engine/status")

    assert blocked.status_code == 429
    assert blocked.json()["code"] == "rate_limit_exceeded"
    assert blocked.headers["retry-after"]
    assert blocked.headers["ratelimit-remaining"] == "0"
    assert blocked.headers["x-frame-options"] == "DENY"


def test_cloud_ai_hosts_get_their_own_provider_bucket():
    """A 401 from an unrelated generic service must never cooldown AI calls:
    each cloud AI host maps to its own provider bucket, not "generic"."""
    from core.http_utils import _get_provider

    assert _get_provider(None, "https://api.z.ai/api/paas/v4/models") == "zai"
    assert _get_provider("zai", "https://api.z.ai/api/paas/v4/chat/completions") == "zai"
    assert _get_provider(None, "https://api.moonshot.ai/v1/models") == "kimi"
    assert _get_provider(None, "https://api.openai.com/v1/models") == "openai"
    assert _get_provider(None, "https://api.anthropic.com/v1/messages") == "anthropic"
    assert _get_provider(None, "https://openrouter.ai/api/v1/models") == "openrouter"
    # Unknown hosts still share the generic bucket.
    assert _get_provider(None, "https://example.com/x") == "generic"
    # The AI buckets are registered with concurrency/circuit/rate tables.
    from core import http_utils

    for bucket in ("zai", "kimi", "openai", "anthropic", "gemini", "xai", "deepseek", "mistral", "openrouter"):
        assert bucket in http_utils._SEMAPHORES
        assert bucket in http_utils._CIRCUIT_THRESHOLDS
