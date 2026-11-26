from core.ai.router import TIERS, ModelRouter
from core.ai.assignment import ModelAssignment


def test_tiers_are_triage_analysis_critic():
    assert TIERS == ("triage", "analysis", "critic")


def _setup(assignment_dict, router_dict):
    return ModelRouter(router_dict, ModelAssignment(assignment_dict))


def test_tier_model_wins_when_set():
    r = _setup({"all": "cheap"}, {"triage": {"model": "fast-mini"}, "analysis": {"model": None}, "critic": {"model": None}})
    assert r.select_model("market_analyst", tier_hint="triage") == "fast-mini"


def test_falls_back_to_assignment_when_tier_unset():
    r = _setup({"all": "cheap"}, {"triage": {"model": None}, "analysis": {"model": None}, "critic": {"model": None}})
    assert r.select_model("market_analyst", tier_hint="triage") == "cheap"


def test_falls_back_to_assignment_when_no_tier_hint():
    r = _setup({"market_analyst": "ma"}, {"triage": {"model": None}, "analysis": {"model": None}, "critic": {"model": None}})
    assert r.select_model("market_analyst") == "ma"


def test_tier_hint_unknown_treated_as_no_hint():
    r = _setup({"all": "m"}, {"triage": {"model": None}, "analysis": {"model": None}, "critic": {"model": None}})
    assert r.select_model("news_analyst", tier_hint="bogus") == "m"


def test_critic_tier_uses_critic_model():
    r = _setup({"risk_critic": None}, {"triage": {"model": None}, "analysis": {"model": None}, "critic": {"model": "independent-1"}})
    assert r.select_model("risk_critic", tier_hint="critic") == "independent-1"


def test_to_dict_round_trips():
    r = _setup({}, {"triage": {"model": "t"}, "analysis": {"model": None}, "critic": {"model": "c"}})
    d = r.to_dict()
    assert d["triage"]["model"] == "t"
    assert d["critic"]["model"] == "c"


def test_from_config_reads_ai_router_block():
    config = {"ai": {"router": {"triage": {"model": "t"}, "analysis": {"model": None}, "critic": {"model": "c"}}, "assignment": {"all": "m"}}}
    r = ModelRouter.from_config(config, ModelAssignment.from_config(config))
    assert r.select_model("market_analyst", tier_hint="triage") == "t"
    assert r.select_model("market_analyst", tier_hint="critic") == "c"
    assert r.select_model("market_analyst", tier_hint="analysis") == "m"