from core.config import DEFAULT_CONFIG


def test_default_config_has_ai_block():
    assert isinstance(DEFAULT_CONFIG.get("ai"), dict)


def test_ai_providers_block_has_eight_providers():
    providers = DEFAULT_CONFIG["ai"]["providers"]
    expected = {"ollama", "openai", "anthropic", "gemini", "xai", "deepseek", "mistral", "openrouter"}
    assert expected <= set(providers.keys())


def test_ai_provider_entries_have_required_shape():
    for name, entry in DEFAULT_CONFIG["ai"]["providers"].items():
        assert "enabled" in entry and isinstance(entry["enabled"], bool)
        assert "type" in entry
        if name != "ollama":
            # the Claude subscription signs in through the Claude Code CLI: no API key
            if entry["type"] != "claude_code":
                assert "api_key_env" in entry and entry["api_key_env"]
            assert "default_model" in entry  # "" means discover-then-select (§35)


def test_ai_assignment_has_ten_roles_plus_all():
    a = DEFAULT_CONFIG["ai"]["assignment"]
    roles = {
        "market_analyst",
        "pre_earnings_analyst",
        "news_analyst",
        "quant_analyst",
        "portfolio_manager",
        "risk_critic",
        "research_scientist",
        "coding_agent",
        "code_reviewer",
        "security_reviewer",
    }
    assert roles <= set(a.keys())
    assert "all" in a


def test_ai_router_has_three_tiers():
    r = DEFAULT_CONFIG["ai"]["router"]
    assert {"triage", "analysis", "critic"} <= set(r.keys())


def test_ai_fallback_and_no_trade_flags():
    ai = DEFAULT_CONFIG["ai"]
    assert "fallback" in ai and "enabled" in ai["fallback"] and "chain" in ai["fallback"]
    assert ai.get("all_unavailable_no_trade") is True  # GOLD-014


def test_config_example_json_has_ai_block():
    import json
    from pathlib import Path

    p = Path(__file__).resolve().parents[2] / "config.example.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert isinstance(data.get("ai"), dict)
    assert "providers" in data["ai"] and "ollama" in data["ai"]["providers"]