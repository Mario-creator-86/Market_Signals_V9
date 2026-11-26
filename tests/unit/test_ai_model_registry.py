from __future__ import annotations

from core.ai import ModelEntry, ModelRegistry, ProviderCapabilities


def _entry(mid="gpt-x", provider="openai", **kw):
    base = dict(
        provider=provider,
        model_id=mid,
        display_name=mid,
        capabilities=("text",),
        tool_support=True,
        structured_output_support=True,
        context_metadata={},
        health="unknown",
        enabled=True,
        last_checked=None,
    )
    base.update(kw)
    return ModelEntry(**base)


def test_model_entry_has_ten_section36_fields():
    e = _entry()
    for f in (
        "provider",
        "model_id",
        "display_name",
        "capabilities",
        "tool_support",
        "structured_output_support",
        "context_metadata",
        "health",
        "enabled",
        "last_checked",
    ):
        assert hasattr(e, f)


def test_register_and_get():
    r = ModelRegistry()
    r.register(_entry("gpt-a"))
    assert r.get("gpt-a") is not None
    assert r.get("gpt-a").provider == "openai"
    assert r.get("missing") is None


def test_register_replaces_same_model_id():
    r = ModelRegistry()
    r.register(_entry("gpt-a", enabled=True))
    r.register(_entry("gpt-a", enabled=False))
    assert r.get("gpt-a").enabled is False


def test_enabled_entries_filters():
    r = ModelRegistry()
    r.register(_entry("gpt-a", enabled=True))
    r.register(_entry("gpt-b", enabled=False))
    assert {e.model_id for e in r.enabled_entries()} == {"gpt-a"}


def test_update_health():
    r = ModelRegistry()
    r.register(_entry("gpt-a", health="unknown"))
    r.update_health("gpt-a", ok=True)
    assert r.get("gpt-a").health == "healthy"
    r.update_health("gpt-a", ok=False)
    assert r.get("gpt-a").health == "unhealthy"


def test_discover_from_provider():
    class _FakeProvider:
        name = "openai"

        def list_models(self):
            return ["gpt-a", "gpt-b"]

        def capabilities(self):
            return ProviderCapabilities()

    r = ModelRegistry()
    added = r.discover(_FakeProvider())
    assert {e.model_id for e in added} == {"gpt-a", "gpt-b"}
    assert r.get("gpt-a").provider == "openai"
    assert r.get("gpt-a").health == "unknown"


def test_persistence_roundtrip():
    r = ModelRegistry()
    r.register(_entry("gpt-a"))
    r.register(_entry("claude-b", provider="anthropic"))
    snap = r.to_list()
    assert isinstance(snap, list) and len(snap) == 2
    r2 = ModelRegistry()
    r2.from_list(snap)
    assert r2.get("gpt-a") is not None and r2.get("claude-b").provider == "anthropic"


def test_from_list_loads_into_config_registry_shape():
    # the config["ai"]["registry"] is a list of dicts; from_list must accept that
    r = ModelRegistry()
    r.from_list(
        [
            {
                "provider": "openai",
                "model_id": "gpt-a",
                "display_name": "gpt-a",
                "capabilities": ["text"],
                "tool_support": True,
                "structured_output_support": True,
                "context_metadata": {},
                "health": "unknown",
                "enabled": True,
                "last_checked": None,
            },
        ]
    )
    assert r.get("gpt-a") is not None and r.get("gpt-a").enabled is True