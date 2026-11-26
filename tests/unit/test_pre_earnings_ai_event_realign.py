"""TDD: when a saved LLM verdict is reused for the same earnings event whose
calendar date has shifted (within the 14-day same-event window), the attached
``ai_decision.event_utc`` must be realigned to the item's current
``earnings_time_utc`` so the analysis is consistent with the event date the UI
shows. The original event the verdict was generated for is preserved in
``reused_from_event_utc`` for transparency.

This closes the live-audit finding (2026-08-04): 6 items had
``ai_decision.event_utc`` = Aug 3 while the item's ``earnings_time_utc`` was
Aug 4-10 because the calendar shifted the date after the AI was cached.
"""
import copy
import threading
from datetime import timedelta

import pytest

from agents import pre_earnings_agent as pe
from agents.pre_earnings_agent import PreEarningsAgent


def _item(symbol, event_utc, with_ai=True):
    """A structurally-complete item fixture (mirrors test_pre_earnings_ai_persistence._pltr_item)."""
    it = {
        "symbol": symbol,
        "earnings_time_utc": pe._iso(event_utc),
        "session": "AMC",
        "gap_risk": 32,
        "coverage_score": 82,
        "structured_direction_bias_score": 50,
        "direction_bias_score": 50,
        "direction_label": "LONG",
        "badges": ["LONG_BIAS"],
        "bias_reasons": ["Structured direction = LONG +50."],
        "top_news": [{"title": f"{symbol} earnings preview", "url": "https://example.test/" + symbol}],
    }
    if with_ai:
        it["ai_decision"] = {
            "used": True,
            "verdict": "LONG",
            "confidence": 0.8,
            "sentiment": 0.5,
            "rationale": "Constructive setup.",
            "catalyst_label": "GUIDANCE",
            "model": "glm-5.2",
            "event_utc": pe._iso(event_utc),
            "generated_at": pe._iso(pe._utc_now()),
            "source": "forced",
        }
        it["ollama"] = {
            "decision": "LONG",
            "decision_confidence": 0.8,
            "sentiment": 0.5,
            "decision_rationale": "Constructive setup.",
        }
    else:
        it["ollama"] = None
    return it


CFG = {"ollama_decision_weight": 0.35, "high_gap_threshold_pct": 5.0}


# --- helper-level tests ---

@pytest.mark.unit
def test_realign_noop_when_event_dates_match():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    ev = pe._utc_now() + timedelta(days=3)
    it = _item("RXRX", ev)
    ai = copy.deepcopy(it["ai_decision"])
    agent._realign_reused_ai_event_to_item(ai, it)
    assert ai["event_utc"] == pe._iso(ev)
    assert "reused_from_event_utc" not in ai


@pytest.mark.unit
def test_realign_realigns_shifted_same_window_date():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    cached_ev = pe._utc_now() + timedelta(days=2)   # e.g. Aug 3
    current_ev = pe._utc_now() + timedelta(days=4)   # e.g. Aug 5 (same quarter)
    it = _item("RXRX", current_ev)
    ai = copy.deepcopy(it["ai_decision"])
    ai["event_utc"] = pe._iso(cached_ev)             # stale: AI was for the Aug-3 date
    agent._realign_reused_ai_event_to_item(ai, it)
    assert ai["event_utc"] == pe._iso(current_ev)    # realigned to the item's date
    assert ai["reused_from_event_utc"] == pe._iso(cached_ev)


@pytest.mark.unit
def test_realign_noop_outside_14d_window():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    cached_ev = pe._utc_now() + timedelta(days=2)
    current_ev = pe._utc_now() + timedelta(days=25)  # >14d -> different quarter
    it = _item("RXRX", current_ev)
    ai = copy.deepcopy(it["ai_decision"])
    ai["event_utc"] = pe._iso(cached_ev)
    agent._realign_reused_ai_event_to_item(ai, it)
    # Not the same event window -> left untouched (caller must not reuse across quarters)
    assert ai["event_utc"] == pe._iso(cached_ev)
    assert "reused_from_event_utc" not in ai


@pytest.mark.unit
def test_realign_noop_when_item_missing_event():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    ev_iso = pe._iso(pe._utc_now() + timedelta(days=2))
    ai = {"used": True, "event_utc": ev_iso, "verdict": "LONG"}
    agent._realign_reused_ai_event_to_item(ai, {"earnings_time_utc": None})
    assert ai["event_utc"] == ev_iso
    assert "reused_from_event_utc" not in ai


@pytest.mark.unit
def test_realign_noop_when_ai_not_used():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    ev_iso = pe._iso(pe._utc_now() + timedelta(days=2))
    it = _item("RXRX", pe._utc_now() + timedelta(days=4))
    ai = {"used": False, "event_utc": ev_iso}
    agent._realign_reused_ai_event_to_item(ai, it)
    assert ai["event_utc"] == ev_iso


# --- end-to-end carry-forward (cache-lookup path = the live-audit scenario) ---

@pytest.mark.unit
def test_carry_forward_realigns_event_utc_to_current_item():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    agent._ai_cache = {}
    cached_ev = pe._utc_now() + timedelta(days=2)    # AI generated for Aug 3
    current_ev = pe._utc_now() + timedelta(days=4)   # calendar shifted to Aug 5
    cached_item = _item("RXRX", cached_ev)
    with agent._lock:
        agent._cache_ai_from_item_locked(cached_item)

    current = _item("RXRX", current_ev, with_ai=False)
    assert agent._carry_forward_ai_decision(current, previous_item=None, cfg=CFG) is True

    assert current["ai_decision"]["used"] is True
    assert current["ai_decision"]["source"] == "persisted_cache"
    assert current["ai_decision"]["event_utc"] == pe._iso(current_ev)          # realigned
    assert current["ai_decision"]["reused_from_event_utc"] == pe._iso(cached_ev)


@pytest.mark.unit
def test_carry_forward_no_realign_tag_when_same_date():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    agent._ai_cache = {}
    ev = pe._utc_now() + timedelta(days=3)
    cached_item = _item("PLTR", ev)
    with agent._lock:
        agent._cache_ai_from_item_locked(cached_item)

    current = _item("PLTR", ev, with_ai=False)
    assert agent._carry_forward_ai_decision(current, previous_item=None, cfg=CFG) is True
    assert current["ai_decision"]["event_utc"] == pe._iso(ev)
    assert "reused_from_event_utc" not in current["ai_decision"]


@pytest.mark.unit
def test_carry_forward_from_previous_item_realigns_to_current():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    agent._ai_cache = {}
    prev_ev = pe._utc_now() + timedelta(days=2)
    current_ev = pe._utc_now() + timedelta(days=5)   # 3-day shift, same window
    previous = _item("HIMS", prev_ev)
    current = _item("HIMS", current_ev, with_ai=False)

    assert agent._carry_forward_ai_decision(current, previous_item=previous, cfg=CFG) is True
    assert current["ai_decision"]["event_utc"] == pe._iso(current_ev)
    assert current["ai_decision"]["reused_from_event_utc"] == pe._iso(prev_ev)
    assert current["ai_decision"]["source"] == "persisted"