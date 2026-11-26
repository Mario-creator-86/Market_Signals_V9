"""Learning mode (owner, 2026-11-24): Opus trades movers and news on paper, toward 10,000 measured trades.

These pin who a learning trade is, which gates stay binding, and what is set
aside for it: the pipeline's second AI opinion (Opus already chose the trade),
Capital's 100-trade experiment budget (the owner wants 10,000), the AI exit
manager (exits are stop, target or the hold time) and per-ticker streak sizing.
"""
from __future__ import annotations

import core.agent_manager as am
from agents.execution_agent import basket_ticker_adjustment
from core.capital_entry_guard import evaluate_capital_entry
from core.capital_manager import CapitalManager
from core.config import DEFAULT_CONFIG, get_bucharest_time
from core.learning_mode.settings import (
    LEARNING_SOURCE,
    MACRO_INSTRUMENTS,
    is_learning_signal,
    learning_ai_is_advisory,
    learning_config,
)

PAPER, LIVE = {"mode": "paper"}, {"mode": "live"}
NEWS = {"autonomous_source": "learning", "strategy_type": "AI_NEWS_LONG", "ticker": "GLD", "signal_mode": "AUTONOMOUS_LEARNING"}
MOVER = {"autonomous_source": "learning", "strategy_type": "AI_MOVER_SHORT", "ticker": "SMTC", "signal_mode": "AUTONOMOUS_LEARNING"}


def test_the_settings_are_the_ones_the_owner_approved():
    cfg = learning_config({})
    assert cfg["enabled"] is True and LEARNING_SOURCE == "learning"
    assert cfg["min_confidence"] == 0.55 and cfg["position_pct_of_equity"] == 1.0
    assert cfg["max_new_per_day"] == 100 and cfg["target_closed_trades"] == 10_000 and cfg["checkpoint_every"] == 1_000
    assert cfg["min_rr"] == 1.25 and cfg["max_rr"] == 4.0
    assert (cfg["min_stop_pct"], cfg["max_stop_pct"]) == (1.0, 10.0)
    assert (cfg["min_hold_hours"], cfg["max_hold_hours"]) == (1, 72)
    assert cfg["idea_ttl_minutes"] == 4 and cfg["mover_min_abs_change_pct"] == 5.0   # two cycles (review, I2)
    assert cfg["mover_ask_cooldown_minutes"] == 60 and cfg["news_interval_seconds"] == 300
    assert cfg["news_max_age_minutes"] == 30 and cfg["news_min_impact_score"] == 35
    assert cfg["news_max_headlines"] == 12 and cfg["news_max_ideas"] == 3 and cfg["opus_reserve_calls"] == 600
    assert learning_config({"learning_mode": {"max_new_per_day": 40}})["max_new_per_day"] == 40
    assert DEFAULT_CONFIG["agents"]["execution"]["learning_mode"]["enabled"] is True
    assert {"GLD", "USO", "SPY", "TLT"} <= set(MACRO_INSTRUMENTS)


def test_a_learning_trade_is_known_by_its_source_and_its_strategy_type():
    assert is_learning_signal(NEWS) and is_learning_signal(MOVER)
    assert not is_learning_signal({"autonomous_source": "scanner", "strategy_type": "AI_NEWS_LONG"})
    assert not is_learning_signal({"autonomous_source": "learning", "strategy_type": "SCANNER_AI_LONG"})
    assert not is_learning_signal(None)


def test_the_second_ai_opinion_is_advisory_on_paper_and_risk_stays_binding():
    assert learning_ai_is_advisory(NEWS, PAPER) and not learning_ai_is_advisory(NEWS, LIVE)
    for opinion in ("ai_no_trade", "ai_watch", "critic_veto", "ai_low_entry_confidence"):
        assert am._advisory_override_applies(True, opinion, MOVER, PAPER) is True
    for hard in ("ai_validation_policy", "ai_validation_authoritative_data_check"):
        assert am._advisory_override_applies(True, hard, MOVER, PAPER) is False
    assert am._advisory_override_applies(True, "ai_watch", MOVER, LIVE) is False
    assert learning_ai_is_advisory(NEWS, {"mode": "paper", "learning_mode": {"enabled": False}}) is False


def test_capital_admits_a_learning_trade_without_the_100_trade_budget():
    verified = {"_autonomous_candidate": True, "price_verified": True, "created_at": get_bucharest_time().isoformat(),
                "price_integrity": {"status": "verified", "is_verified": True}}
    losers = [{"strategy_type": "AI_NEWS_LONG", "direction": "LONG", "status": "CLOSED", "net_pnl": -1.0,
               "pnl_pct_notional": -1.0, "entry_strategy": "learning_news"} for _ in range(150)]
    result = evaluate_capital_entry({**NEWS, **verified, "action": "BUY", "entry_strategy": "learning_news",
                                     "entry": 100.0, "sl": 97.0, "tp": 104.5}, losers, PAPER)
    assert result["allowed"] is True and result.get("paper_capital_experiment") is True
    assert "paper_experiment_budget" not in result["checks"]
    live = evaluate_capital_entry({**NEWS, **verified, "action": "BUY", "entry": 100.0, "sl": 97.0, "tp": 104.5}, [], LIVE)
    assert live["allowed"] is False                                    # never with real money


def test_opus_may_close_a_learning_trade_early_and_size_ignores_ticker_streaks():
    # Owner, 2026-11-24: Opus may also close a position on a market change (reviewed once an hour).
    enabled = CapitalManager._ai_exit_management_enabled
    assert enabled({"strategy_type": "AI_NEWS_LONG", "position_type": "paper_signal"}) is True
    assert enabled({"strategy_type": "AI_MOVER_SHORT", "position_type": "paper_signal"}) is True
    applied, set_aside = basket_ticker_adjustment(MOVER, {"size_mult": 0.5})
    assert applied is None and set_aside == {"size_mult": 0.5}


def test_the_position_keeps_what_opus_decided():
    """Seen live on 2026-11-24: the first learning position (LISTA-USD) carried no learning block,
    so its result could not be counted by Opus's confidence."""
    import inspect

    from agents.execution_agent import ExecutionAgent, carry_learning_context

    context = {}
    carry_learning_context({**MOVER, "learning": {"confidence": 0.57, "source": "mover", "model": "claude-opus-5-5"}}, context)
    assert context["learning"] == {"confidence": 0.57, "source": "mover", "model": "claude-opus-5-5"}
    untouched = {}
    carry_learning_context({"autonomous_source": "scanner", "strategy_type": "SCANNER_AI_LONG", "learning": {"x": 1}}, untouched)
    assert untouched == {}
    assert "carry_learning_context(signal, decision_context)" in inspect.getsource(ExecutionAgent._process_paper_signal)
