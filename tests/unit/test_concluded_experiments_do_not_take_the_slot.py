"""A paper experiment that has used up its budget must not take the cycle's review slot.

Seen live on 2026-11-23: the stock "fade" short experiment had concluded (210
closed trades, profit factor 0.73). Its candidates still won the single review
slot of every cycle on score and were refused by the Capital gate seconds
later, so the Strategy Lab's members never got a turn.
"""
from __future__ import annotations

import core.agent_manager as am


class _Capital:
    def __init__(self, positions):
        self._positions = positions

    def get_positions(self):
        return self._positions


def _manager(monkeypatch, *, budgets, positions=()):
    import core.capital_manager as cm

    monkeypatch.setattr(cm, "get_capital_manager", lambda: _Capital(list(positions)))
    calls = []

    def budget(signal, closed, config):
        calls.append((signal["strategy_type"], signal.get("action")))
        assert signal["_autonomous_candidate"] is True
        return {"allowed": budgets.get(signal["strategy_type"], True)}

    monkeypatch.setattr(am, "evaluate_paper_experiment_budget", budget)
    monkeypatch.setattr(am, "_paper_autonomous_experiment_source",
                        lambda signal, config: "" if signal["strategy_type"] == "S1_TREND" else "scanner")
    manager = am.AgentManager.__new__(am.AgentManager)
    manager.config_manager = type("Cfg", (), {"config": {"agents": {"execution": {"mode": "paper"}}}})()
    manager.calls = calls
    return manager


def _candidate(ticker, strategy, action):
    return {"ticker": ticker, "strategy_type": strategy, "action": action, "entry_strategy": "fade", "signal_score": 95.0}


def test_candidates_of_a_concluded_experiment_are_left_out_before_the_slot_is_given(monkeypatch):
    manager = _manager(monkeypatch, budgets={"SCANNER_AI_SHORT": False})
    candidates = [_candidate("NVDA", "SCANNER_AI_SHORT", "SHORT"), _candidate("SOXL", "SCANNER_AI_SHORT", "SHORT"),
                  _candidate("KOD", "LAB_B1EA33BC8C_SHORT", "SHORT"), _candidate("WSC", "SCANNER_AI_LONG", "BUY")]

    kept, concluded = manager._drop_concluded_experiments(candidates)

    assert [c["ticker"] for c in kept] == ["KOD", "WSC"]
    assert concluded == {"SCANNER_AI_SHORT": 2}
    assert manager.calls.count(("SCANNER_AI_SHORT", "SHORT")) == 1      # one budget check per strategy, not per candidate
    assert "_autonomous_candidate" not in candidates[0]                 # the candidate itself is not changed


def test_signals_outside_paper_experiments_and_failed_checks_are_kept(monkeypatch):
    manager = _manager(monkeypatch, budgets={})
    engine_signal = _candidate("AAPL", "S1_TREND", "BUY")
    kept, concluded = manager._drop_concluded_experiments([engine_signal])
    assert kept == [engine_signal] and concluded == {} and manager.calls == []   # not a paper experiment: no budget

    def broken(signal, closed, config):
        raise RuntimeError("database locked")

    monkeypatch.setattr(am, "evaluate_paper_experiment_budget", broken)
    scanner = _candidate("NVDA", "SCANNER_AI_SHORT", "SHORT")
    kept, concluded = manager._drop_concluded_experiments([scanner])
    assert kept == [scanner] and concluded == {}                        # when unsure, the Capital gate decides as before
    assert manager._drop_concluded_experiments([]) == ([], {})


def test_without_the_capital_manager_nothing_is_left_out(monkeypatch):
    import core.capital_manager as cm

    manager = _manager(monkeypatch, budgets={"SCANNER_AI_SHORT": False})

    def unavailable():
        raise RuntimeError("capital manager not ready")

    monkeypatch.setattr(cm, "get_capital_manager", unavailable)
    scanner = _candidate("NVDA", "SCANNER_AI_SHORT", "SHORT")
    assert manager._drop_concluded_experiments([scanner]) == ([scanner], {})
