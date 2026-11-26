"""Regression guard for the Ollama cloud API host.

The cloud host `https://ollama.com` is the OFFICIAL Ollama cloud API endpoint
(per https://docs.ollama.com/cloud and the ollama/ollama GitHub docs/cloud.mdx).
It serves `/api/chat` and `/api/tags` directly with Bearer-token auth — it is NOT
merely a marketing site. A prior bug report incorrectly asserted the host was
wrong and proposed changing it; that would have broken the working cloud
routing. These tests pin the verified-correct value so a future change can't
silently regress it.
"""

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import os
os.environ.setdefault("TEST_MODE", "1")

from core.ollama_client import OllamaClient, OLLAMA_BASE_URL, OLLAMA_CLOUD_CATALOG_URL


CLOUD_HOST = "https://ollama.com"
_REAL_CHECK_AVAILABILITY = OllamaClient.check_availability


def test_cloud_base_url_in_config_is_the_verified_ollama_host():
    """config.json / config.example.json / core/config.py default must keep
    the verified Ollama cloud host (https://ollama.com)."""
    from core.config import ConfigManager
    cfg = ConfigManager().config.get("ollama", {}) or {}
    assert cfg.get("cloud_base_url") == CLOUD_HOST, (
        "cloud_base_url must remain https://ollama.com (verified official cloud "
        "API host per docs.ollama.com/cloud). Do NOT change to a marketing-site "
        "placeholder or api.ollama.com."
    )


def test_is_cloud_host_recognizes_ollama_com():
    """OllamaClient.is_cloud_host() must treat ollama.com as the cloud host."""
    client = OllamaClient()
    with patch.object(OllamaClient, "_resolve_base_url", return_value=CLOUD_HOST):
        client.base_url = CLOUD_HOST
    assert client.is_cloud_host() is True


def test_is_cloud_host_rejects_localhost():
    client = OllamaClient()
    with patch.object(OllamaClient, "_resolve_base_url", return_value=OLLAMA_BASE_URL):
        client.base_url = OLLAMA_BASE_URL
    assert client.is_cloud_host() is False


def test_api_url_builds_ollama_com_api_path():
    client = OllamaClient()
    client.base_url = CLOUD_HOST
    assert client._api_url("tags") == "https://ollama.com/api/tags"
    assert client._api_url("chat") == "https://ollama.com/api/chat"


def test_headers_use_bearer_token_when_api_key_present():
    client = OllamaClient()
    with patch.object(OllamaClient, "get_api_key", return_value="sk-test"):
        assert client._headers() == {"Authorization": "Bearer sk-test"}


def test_headers_empty_when_no_api_key():
    client = OllamaClient()
    with patch.object(OllamaClient, "get_api_key", return_value=""):
        assert client._headers() == {}


class _TagsResponse:
    status_code = 200

    @staticmethod
    def json():
        return {"models": [{"name": "gpt-oss:120b"}, {"name": "gemma3:12b"}]}


def test_model_catalog_uses_ttl_cache(monkeypatch):
    client = OllamaClient(base_url="http://127.0.0.1:11434")
    calls = {"count": 0}

    def fake_get(*_args, **_kwargs):
        calls["count"] += 1
        return _TagsResponse()

    monkeypatch.setattr(client, "refresh_config", lambda: None)
    monkeypatch.setattr("core.ollama_client.http_get", fake_get)

    assert client.get_models() == ["gpt-oss:120b", "gemma3:12b"]
    assert client.get_models() == ["gpt-oss:120b", "gemma3:12b"]
    assert calls["count"] == 1


def test_model_catalog_collapses_concurrent_requests(monkeypatch):
    client = OllamaClient(base_url="http://127.0.0.1:11434")
    calls = {"count": 0}

    def slow_get(*_args, **_kwargs):
        calls["count"] += 1
        time.sleep(0.04)
        return _TagsResponse()

    monkeypatch.setattr(client, "refresh_config", lambda: None)
    monkeypatch.setattr("core.ollama_client.http_get", slow_get)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: client.get_models(), range(8)))

    assert all(result == ["gpt-oss:120b", "gemma3:12b"] for result in results)
    assert calls["count"] == 1


def test_availability_probe_uses_ttl_cache(monkeypatch):
    client = OllamaClient(base_url="http://127.0.0.1:11434")
    calls = {"count": 0}

    def fake_get(*_args, **_kwargs):
        calls["count"] += 1
        return _TagsResponse()

    monkeypatch.setattr(client, "refresh_config", lambda: None)
    monkeypatch.setattr("core.ollama_client.requests.get", fake_get)

    assert _REAL_CHECK_AVAILABILITY(client) is True
    assert _REAL_CHECK_AVAILABILITY(client) is True
    assert calls["count"] == 1


def test_model_cache_is_scoped_to_runtime_host(monkeypatch):
    client = OllamaClient(base_url="http://127.0.0.1:11434")
    calls = {"count": 0}

    def fake_get(*_args, **_kwargs):
        calls["count"] += 1
        return _TagsResponse()

    monkeypatch.setattr(client, "refresh_config", lambda: None)
    monkeypatch.setattr("core.ollama_client.http_get", fake_get)

    client.get_models()
    client.base_url = "http://127.0.0.1:11435"
    client.get_models()

    assert calls["count"] == 2


def test_public_cloud_catalog_is_cached_and_converted_for_local_proxy(monkeypatch):
    client = OllamaClient(base_url="http://127.0.0.1:11434")
    calls = {"count": 0}

    class CatalogResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "models": [
                    {"name": "glm-5.3-flash"},
                    {"name": "gpt-oss:120b"},
                    {"name": "glm-5.3-flash"},
                ]
            }

    def fake_get(url, **_kwargs):
        calls["count"] += 1
        assert url == OLLAMA_CLOUD_CATALOG_URL
        return CatalogResponse()

    monkeypatch.setattr(client, "refresh_config", lambda: None)
    monkeypatch.setattr("core.ollama_client.http_get", fake_get)

    assert client.get_selectable_cloud_models() == [
        "glm-5.3-flash:cloud",
        "gpt-oss:120b-cloud",
    ]
    assert client.get_selectable_cloud_models() == [
        "glm-5.3-flash:cloud",
        "gpt-oss:120b-cloud",
    ]
    assert calls["count"] == 1


def test_direct_cloud_host_keeps_api_model_ids(monkeypatch):
    client = OllamaClient(base_url="http://127.0.0.1:11434")
    client.base_url = CLOUD_HOST
    monkeypatch.setattr(
        client, "get_cloud_models", lambda **_kwargs: ["glm-5.3-flash", "gpt-oss:120b"]
    )

    assert client.get_selectable_cloud_models() == ["glm-5.3-flash", "gpt-oss:120b"]


def test_pull_model_activates_manifest_and_invalidates_local_cache(monkeypatch):
    client = OllamaClient(base_url="http://127.0.0.1:11434")
    client._models_cache = {
        "key": ("old", False),
        "checked_at": time.monotonic(),
        "models": ["old-model"],
    }
    seen = {}

    class PullResponse:
        status_code = 200
        text = ""

    def fake_post(url, **kwargs):
        seen.update({"url": url, **kwargs})
        return PullResponse()

    monkeypatch.setattr("core.ollama_client.http_post", fake_post)

    result = client.pull_model("glm-5.3-flash:cloud")

    assert result == {"ok": True, "model": "glm-5.3-flash:cloud"}
    assert seen["url"] == "http://127.0.0.1:11434/api/pull"
    assert seen["json"] == {"model": "glm-5.3-flash:cloud", "stream": False}
    assert client._models_cache["models"] == []
