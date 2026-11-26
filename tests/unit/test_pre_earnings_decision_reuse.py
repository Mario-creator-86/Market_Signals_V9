"""The Pre-Earnings decision is asked once per question, not once per cycle.

Seen live on 2026-11-24: the decision pass asked the model again for the same
25 tickers on every 10-minute cycle, day and night, with identical inputs. In
an 8-minute sample 25 of 34 Claude subscription calls were this pass (about
150 an hour), so the daily call limit ran out hours before the US session.
"""
from __future__ import annotations

import copy
from datetime import timedelta

import agents.pre_earnings_agent as pe
from core.ai.decision import Action, Decision
from core.ai.decision_engine import EvaluationResult
from core.ai.pipeline import ValidationResult

CONFIG = {"pre_earnings": {"decision_enabled": True, "decision_max_tickers": 25},
          "autonomy": {"mode": "PAPER_AUTO"}}


def _item(symbol="DY", **changes):
    item = {"symbol": symbol, "pre_earnings_score": 38, "pre_earnings_score_label": "LOW", "gap_risk": 44,
            "direction_bias": 0.57, "data_confidence": "HIGH", "coverage_score": 73, "coverage_label": "HIGH",
            "expected_move_pct": 5.07, "historical_median_move_pct": 7.08, "session": "BMO",
            "earnings_time_utc": "2026-11-24T00:00:00+00:00"}
    item.update(changes)
    return item


def _agent_and_asks(monkeypatch, *, available=True):
    """An agent whose model answers are counted: every entry of ``asks`` is one model call."""
    asks = []

    def _answer(item, *, config, services=None):
        asks.append(item["symbol"])
        decision = Decision(action=Action.WATCH, ticker=item["symbol"], confidence=0.6, thesis="t")
        return EvaluationResult(
            decision, ValidationResult(ok=True, decision=decision, stage="OK"), None,
            {"available": available, "recommendation": "watch", "confidence": 0.6,
             "reason": f"answer {len(asks)}", "validation_stage": "OK", "critic_veto": False},
            available,
        )

    monkeypatch.setattr(pe, "evaluate_pre_earnings_decision", _answer)
    agent = object.__new__(pe.PreEarningsAgent)
    agent._state = {"items": []}
    return agent, asks


def _cycle(agent, items, config=CONFIG):
    """One scoring cycle: the items are rebuilt from scratch, decided, and become the saved state."""
    rebuilt = [copy.deepcopy(item) for item in items]
    agent._attach_pre_earnings_decisions(config, items=rebuilt)
    agent._state = {"items": rebuilt}
    return [item.get("pre_earnings_decision") for item in rebuilt]


def test_a_ticker_whose_inputs_did_not_change_is_not_asked_again(monkeypatch):
    agent, asks = _agent_and_asks(monkeypatch)

    first = _cycle(agent, [_item()])[0]
    second = _cycle(agent, [_item()])[0]
    third = _cycle(agent, [_item(top_news=[{"title": "a field the decision never reads"}])])[0]

    assert asks == ["DY"]
    assert second["reason"] == third["reason"] == "answer 1" and second["action"] == "WATCH"
    assert second["decided_at"] == third["decided_at"] == first["decided_at"]   # the age of the answer stays visible


def test_a_changed_input_is_asked_again(monkeypatch):
    agent, asks = _agent_and_asks(monkeypatch)

    _cycle(agent, [_item()])
    after = _cycle(agent, [_item(gap_risk=61)])[0]

    assert asks == ["DY", "DY"]
    assert after["reason"] == "answer 2"


def test_another_earnings_event_of_the_same_ticker_is_asked_again(monkeypatch):
    agent, asks = _agent_and_asks(monkeypatch)

    _cycle(agent, [_item()])
    _cycle(agent, [_item(earnings_time_utc="2027-02-23T00:00:00+00:00")])

    assert asks == ["DY", "DY"]


def test_an_answer_older_than_the_reuse_window_is_asked_again(monkeypatch):
    agent, asks = _agent_and_asks(monkeypatch)

    _cycle(agent, [_item()])
    saved = agent._state["items"][0]["pre_earnings_decision"]
    saved["decided_at"] = pe._iso(pe._utc_now() - timedelta(minutes=121))
    _cycle(agent, [_item()])

    assert asks == ["DY", "DY"]


def test_the_reuse_window_is_a_setting_and_zero_asks_every_cycle(monkeypatch):
    agent, asks = _agent_and_asks(monkeypatch)
    every_cycle = {**CONFIG, "pre_earnings": {**CONFIG["pre_earnings"], "decision_reuse_minutes": 0}}
    short = {**CONFIG, "pre_earnings": {**CONFIG["pre_earnings"], "decision_reuse_minutes": 30}}

    _cycle(agent, [_item()], every_cycle)
    _cycle(agent, [_item()], every_cycle)
    assert asks == ["DY", "DY"]

    saved = agent._state["items"][0]["pre_earnings_decision"]
    saved["decided_at"] = pe._iso(pe._utc_now() - timedelta(minutes=31))
    _cycle(agent, [_item()], short)
    assert asks == ["DY", "DY", "DY"]


def test_a_cycle_in_which_the_model_did_not_answer_is_retried(monkeypatch):
    agent, asks = _agent_and_asks(monkeypatch, available=False)

    _cycle(agent, [_item()])
    _cycle(agent, [_item()])

    assert asks == ["DY", "DY"]


def test_a_kept_answer_still_counts_towards_the_tickers_of_a_cycle(monkeypatch):
    """Otherwise the calls saved on the first tickers would be spent further down the list."""
    agent, asks = _agent_and_asks(monkeypatch)
    one_per_cycle = {**CONFIG, "pre_earnings": {**CONFIG["pre_earnings"], "decision_max_tickers": 1}}

    _cycle(agent, [_item("AAA"), _item("BBB")], one_per_cycle)
    decisions = _cycle(agent, [_item("AAA"), _item("BBB")], one_per_cycle)

    assert asks == ["AAA"]
    assert decisions[0]["reason"] == "answer 1" and decisions[1] is None


def test_the_fingerprint_covers_what_the_decision_is_made_from():
    from core.pre_earnings.decision import decision_inputs_fingerprint

    base = decision_inputs_fingerprint(_item(), CONFIG)
    assert base == decision_inputs_fingerprint(_item(top_news=[{"title": "not an input"}]), CONFIG)
    assert base != decision_inputs_fingerprint(_item(pre_earnings_score=39), CONFIG)
    assert base != decision_inputs_fingerprint(_item(direction_bias=-0.57), CONFIG)
    assert base != decision_inputs_fingerprint(_item(news_only_call=True), CONFIG)
    # The autonomy mode decides whether an OPEN answer is kept or turned into WATCH.
    assert base != decision_inputs_fingerprint(_item(), {**CONFIG, "autonomy": {"mode": "RECOMMEND"}})
    # Another model assigned to the role is another question.
    other_model = {**CONFIG, "ai": {"assignment": {"pre_earnings_analyst": "claude-opus-5-5"}}}
    assert base != decision_inputs_fingerprint(_item(), other_model)
