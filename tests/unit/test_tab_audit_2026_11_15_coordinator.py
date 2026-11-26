"""Regression tests for defects found while verifying the tab audit in the running app."""
from agents.news_tab_agent import NewsTabAgent


def test_ordinary_word_is_not_a_ticker_mention():
    match = NewsTabAgent._token_match_symbol
    # Seen live: a stUSDT wind-down story was filed under Atlassian (TEAM).
    assert not match("TEAM", "The stUSDT team did not disclose a reason for winding down")
    assert not match("HUT", "They built a hut near the mine")
    assert match("TEAM", "Atlassian (TEAM) beats estimates")
    assert match("TEAM", "TEAM shares jump after earnings")
    assert match("TEAM", "Buying $team ahead of the print")
    assert match("BTC", "BTCUSDT slides below support")


def test_global_model_change_moves_roles_that_followed_the_old_global():
    """Seen live: after "set global model", every analyst role stayed on the old model."""
    import re
    from pathlib import Path

    source = Path(__file__).resolve().parents[2].joinpath("api", "main.py").read_text(encoding="utf-8")
    block = source[source.index('assignment = ai_config.setdefault("assignment", {})'):]
    block = block[: block.index('providers = ai_config.setdefault("providers", {})')]
    namespace = {"ai_config": {"assignment": {
        "all": "old:cloud", "market_analyst": "old:cloud", "risk_critic": "critic:cloud", "code_reviewer": None,
    }}, "model": "new:cloud"}
    exec(re.sub(r"^        ", "", block, flags=re.M), namespace)  # the route body, run on a plain dict
    assert namespace["ai_config"]["assignment"] == {
        "all": "new:cloud", "market_analyst": "new:cloud", "risk_critic": "critic:cloud", "code_reviewer": None,
    }
