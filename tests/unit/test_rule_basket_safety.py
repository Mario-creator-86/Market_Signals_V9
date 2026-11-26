"""Safety properties of rule baskets (weekly momentum basket, Strategy Lab rules).

Found by the independent review on 2026-11-23: the advisory treatment of the AI
must cover the AI's opinion only, exits must not undo what entries allow, and a
two-legged basket must not be stopped one leg at a time.
"""
from __future__ import annotations

import core.agent_manager as am
from core.capital_entry_guard import evaluate_capital_entry
from core.capital_manager import CapitalManager

LAB = {"autonomous_source": "strategy_lab", "strategy_type": "LAB_ABC123_LONG", "ticker": "AAPL", "id": "auto_lab_abc123_AAPL_long_20779"}
XS = {"autonomous_source": "xs_momentum_basket", "strategy_type": "XS_MOMENTUM_SHORT", "ticker": "ZEC-USD"}
SCANNER = {"autonomous_source": "scanner", "strategy_type": "SCANNER_AI_SHORT", "ticker": "NVDA"}
PAPER, LIVE = {"mode": "paper"}, {"mode": "live"}


class _Capital:
    def __init__(self, positions):
        self._positions = positions

    def get_positions(self):
        return self._positions


def _capital(monkeypatch, positions):
    import core.capital_manager as cm

    monkeypatch.setattr(cm, "get_capital_manager", lambda: _Capital(list(positions)))


def test_only_an_ai_opinion_is_advisory_risk_and_capital_blocks_stay_binding():
    for opinion in ("ai_no_trade", "ai_watch", "critic_veto", "ai_low_entry_confidence", "legacy_ai_veto", "ai_validation_semantic"):
        assert am._advisory_override_applies(True, opinion, LAB, PAPER) is True
        assert am._advisory_override_applies(True, opinion, XS, PAPER) is True
    for hard in ("ai_validation_policy", "ai_validation_authoritative_data_check"):
        assert am._advisory_override_applies(True, hard, LAB, PAPER) is False     # the Risk Agent / the Capital gate said no
        assert am._advisory_override_applies(True, hard, XS, PAPER) is False
    assert am._advisory_override_applies(True, "ai_no_trade", SCANNER, PAPER) is False   # every other candidate keeps the veto
    assert am._advisory_override_applies(True, "ai_no_trade", LAB, LIVE) is False        # never with real money
    assert am._advisory_override_applies(False, "", LAB, PAPER) is False


def test_a_crafted_label_on_another_source_does_not_buy_the_advisory_treatment_in_live_mode():
    forged = {"autonomous_source": "scanner", "strategy_type": "LAB_FAKE_LONG", "signal_mode": "AUTONOMOUS_FACTOR"}
    assert am._advisory_override_applies(True, "ai_no_trade", forged, LIVE) is False
    assert evaluate_capital_entry({**forged, "_autonomous_candidate": True, "price_verified": True,
                                   "price_integrity": {"status": "verified", "is_verified": True}}, [], LIVE)["allowed"] is False


def test_opus_may_close_every_automated_position_including_rule_baskets():
    """Owner, 2026-11-24: "Da, pornește la toate, dar la 1h" - Opus reviews every open position once an
    hour and may close it on a market change; rule baskets and learning trades are no longer exempt."""
    enabled = CapitalManager._ai_exit_management_enabled
    for strategy in ("LAB_ABC123_LONG", "XS_MOMENTUM_SHORT", "AI_NEWS_LONG", "AI_MOVER_SHORT",
                     "SCANNER_AI_SHORT", "SCANNER_AI_DIP_LONG", "PRE_EARNINGS_AI_LONG"):
        assert enabled({"strategy_type": strategy, "position_type": "paper_signal"}) is True, strategy
    assert enabled({"strategy_type": "LAB_ABC123_LONG", "ai_exit_management_enabled": False}) is False  # an explicit choice wins
    assert enabled({"strategy_type": "MANUAL_CHART", "position_type": "manual"}) is False            # manual stays opt-in


def test_each_position_is_reviewed_at_most_once_an_hour():
    from core.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["ollama"]["exit_decision_cooldown_seconds"] == 3600


def test_a_basket_has_no_per_leg_budget_and_other_experiments_keep_theirs():
    from core.config import get_bucharest_time

    verified = {"_autonomous_candidate": True, "price_verified": True, "created_at": get_bucharest_time().isoformat(),
                "price_integrity": {"status": "verified", "is_verified": True}}
    losing_leg = [{"strategy_type": "LAB_ABC123_LONG", "direction": "LONG", "status": "CLOSED", "net_pnl": -1.0,
                   "pnl_pct_notional": -1.0, "entry_strategy": "lab_abc123"} for _ in range(150)]
    lab = evaluate_capital_entry({**LAB, **verified, "action": "BUY", "entry_strategy": "lab_abc123",
                                  "entry": 100.0, "sl": 75.0, "tp": 175.0}, losing_leg, PAPER)
    assert lab["allowed"] is True and lab["check"] == "paper_autonomous_experiment"
    assert "paper_experiment_budget" not in lab["checks"]             # 150 losing closes of one leg do not stop the basket
    scanner = evaluate_capital_entry({**SCANNER, **verified, "action": "SHORT", "entry": 100.0, "sl": 104.0, "tp": 94.0},
                                     [], PAPER)
    assert scanner["allowed"] is True and "paper_experiment_budget" in scanner["checks"]


def test_the_weekly_basket_stops_itself_on_both_legs_together(monkeypatch):
    winners = [{"strategy_type": "XS_MOMENTUM_LONG", "status": "CLOSED", "net_pnl": 2.0} for _ in range(60)]
    losers = [{"strategy_type": "XS_MOMENTUM_SHORT", "status": "CLOSED", "net_pnl": -3.0} for _ in range(60)]
    _capital(monkeypatch, winners + losers + [{"strategy_type": "XS_MOMENTUM_LONG", "status": "OPEN", "net_pnl": -999.0}])
    assert "concluded after 120 closed trades" in am.AgentManager._xs_basket_concluded({})

    _capital(monkeypatch, winners + losers[:30])                         # 90 closed: not enough to judge
    assert am.AgentManager._xs_basket_concluded({}) == ""
    _capital(monkeypatch, winners + [{"strategy_type": "XS_MOMENTUM_SHORT", "status": "CLOSED", "net_pnl": -1.0}] * 60)
    assert am.AgentManager._xs_basket_concluded({}) == ""               # 120 closed and a net profit: it goes on
    _capital(monkeypatch, winners[:10] + losers)                         # 70 closed, net loss: over a lower limit
    assert am.AgentManager._xs_basket_concluded({}) == ""
    assert "concluded after 70 closed trades" in am.AgentManager._xs_basket_concluded(
        {"xs_momentum_basket": {"max_closed_without_profit": 50}})


def test_a_basket_member_is_not_kept_out_by_the_post_close_lockout_in_paper_mode():
    assert am._is_rule_basket_in_paper(LAB, PAPER) and am._is_rule_basket_in_paper(XS, {})
    assert not am._is_rule_basket_in_paper(LAB, LIVE)
    assert not am._is_rule_basket_in_paper(SCANNER, PAPER)              # the lockout still protects everything else


def test_a_lab_member_blocked_by_another_strategys_position_is_recorded(monkeypatch):
    class Baskets:
        blocked = []

        def note_blocked(self, signal_id, reason):
            self.blocked.append((signal_id, reason))

    baskets = Baskets()
    monkeypatch.setattr(am, "get_lab_baskets", lambda: baskets)
    _capital(monkeypatch, [{"ticker": "AAPL", "status": "OPEN", "strategy_type": "SCANNER_AI_LONG"}])
    am.AgentManager._note_basket_member_blocked(LAB)
    assert baskets.blocked == [("auto_lab_abc123_AAPL_long_20779", "held by SCANNER_AI_LONG")]

    _capital(monkeypatch, [{"ticker": "AAPL", "status": "OPEN", "strategy_type": "LAB_ABC123_LONG"}])
    am.AgentManager._note_basket_member_blocked(LAB)                     # its own position: nothing to report
    am.AgentManager._note_basket_member_blocked(SCANNER)                 # not a lab member
    assert len(baskets.blocked) == 1


def test_blocked_members_are_shown_with_the_rule(tmp_path):
    import json

    from core.strategy_lab.live import LabBaskets

    live = {"as_of_day": 20_779, "enter_from_ms": 0, "enter_until_ms": 10, "exit_at_ms": 20, "long": ["AAPL"], "short": ["TSLA"]}
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"strategies": {"abc123": {"id": "abc123", "market": "stocks", "status": "paper", "live": live}}}),
                    encoding="utf-8")
    baskets = LabBaskets(path, tmp_path / "state.json", now_ms=lambda: 15)
    baskets.note_blocked("auto_lab_abc123_AAPL_long_20779", "held by SCANNER_AI_LONG")
    baskets.note_blocked("auto_lab_abc123_OLD_long_20700", "held by X")        # an earlier ranking
    baskets.note_blocked("auto_xsmom_ETHUSDT_long_20261123", "held by X")      # not a lab member
    assert baskets.status()["trading_rules"][0]["not_entered"] == {"AAPL": "held by SCANNER_AI_LONG"}
