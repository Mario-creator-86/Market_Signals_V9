from core.ai.assignment import ROLES, ModelAssignment
from core.ai.registry import ModelEntry, ModelRegistry


def test_roles_are_the_ten_spec_roles():
    assert ROLES == (
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
    )
    assert len(ROLES) == 10


def test_resolve_returns_role_model_when_set():
    a = ModelAssignment({"market_analyst": "gpt-4o", "all": None})
    assert a.resolve("market_analyst") == "gpt-4o"


def test_resolve_falls_back_to_all_alias():
    a = ModelAssignment({"all": "claude-sonnet", "market_analyst": None})
    assert a.resolve("market_analyst") == "claude-sonnet"
    assert a.resolve("news_analyst") == "claude-sonnet"


def test_resolve_explicit_role_overrides_all():
    a = ModelAssignment({"all": "claude-sonnet", "risk_critic": "gpt-4o"})
    assert a.resolve("risk_critic") == "gpt-4o"
    assert a.resolve("news_analyst") == "claude-sonnet"


def test_resolve_unknown_role_returns_none():
    a = ModelAssignment({"all": "m"})
    assert a.resolve("nonexistent_role") is None


def test_set_and_set_all_mutate_in_memory():
    a = ModelAssignment({})
    a.set("quant_analyst", "q")
    assert a.resolve("quant_analyst") == "q"
    a.set_all("m")
    assert a.resolve("quant_analyst") == "q"  # explicit role wins
    assert a.resolve("news_analyst") == "m"   # all fills the rest


def test_to_dict_round_trips_all_roles():
    a = ModelAssignment({"all": "m", "risk_critic": "r"})
    d = a.to_dict()
    assert d["all"] == "m"
    assert d["risk_critic"] == "r"
    assert set(ROLES).issubset(set(d.keys()))


def test_from_config_reads_ai_assignment_block():
    config = {"ai": {"assignment": {"all": "m", "market_analyst": "ma"}}}
    a = ModelAssignment.from_config(config)
    assert a.resolve("market_analyst") == "ma"
    assert a.resolve("news_analyst") == "m"


def test_from_config_uses_legacy_global_ollama_selection_as_all_fallback():
    config = {"ai": {"assignment": {"all": None}}, "ollama": {"model": "owner-model"}}
    assignment = ModelAssignment.from_config(config)
    assert assignment.resolve("pre_earnings_analyst") == "owner-model"
    assert assignment.resolve("news_analyst") == "owner-model"


def test_validate_checks_registry_membership():
    reg = ModelRegistry()
    reg.register(ModelEntry(provider="openai", model_id="gpt-4o", display_name="gpt-4o"))
    a = ModelAssignment({})
    assert a.validate("gpt-4o", reg) is True
    assert a.validate("not-a-model", reg) is False
