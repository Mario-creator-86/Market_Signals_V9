"""TDD for the Pulse accuracy loop — scoring archived Pulse picks against the
realized post-earnings move, mirroring the per-item feedback loop.

The agent is built lightweight via __new__ + _ensure_pulse_store_runtime so no
disk/network is touched. _compute_event_gap_move_pct is monkeypatched to a
deterministic stub so no yfinance fetch occurs.
"""
import threading
from datetime import timedelta

import pytest

from agents import pre_earnings_agent as pe
from agents.pre_earnings_agent import PreEarningsAgent


def _make_agent():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._pulse_store_lock = threading.RLock()
    agent._ensure_pulse_store_runtime()
    return agent


def _archived_pick(symbol, side, event_dt, archive_reason="event_reported", session="AMC"):
    return {
        "symbol": symbol,
        "side": side,
        "event_key": f"{symbol}|{pe._iso(event_dt)[:10]}",
        "event_date": pe._iso(event_dt)[:10],
        "earnings_time_utc": pe._iso(event_dt),
        "session": session,
        "reason": "test",
        "source": "llm",
        "eligible": True,
        "first_seen_at": pe._iso(event_dt - timedelta(days=2)),
        "last_seen_at": pe._iso(event_dt - timedelta(days=1)),
        "archived_at": pe._iso(event_dt + timedelta(hours=2)),
        "archive_reason": archive_reason,
    }


def _stub_move(agent, mapping):
    """Replace _compute_event_gap_move_pct with a symbol->pct lookup."""
    def _stub(symbol, event_dt, session):
        return mapping.get(str(symbol or "").upper().strip())
    agent._compute_event_gap_move_pct = _stub


CFG = {"high_gap_threshold_pct": 5.0, "refresh_seconds": 600}


@pytest.mark.unit
def test_pulse_long_pick_scored_hit_when_move_positive():
    agent = _make_agent()
    now = pe._utc_now()
    pick = _archived_pick("GCT", "top_long", now - timedelta(hours=48))
    agent._pulse_store["archive"] = [pick]
    _stub_move(agent, {"GCT": 6.0})

    changed = agent._evaluate_matured_pulse_picks_locked(CFG)

    assert changed is True
    scored = agent._pulse_store["archive"][0]
    assert scored["realized_signed_move_pct"] == 6.0
    assert scored["realized_gap_move_pct"] == 6.0
    assert scored["realized_high_gap"] is True
    assert scored["hit"] is True
    assert scored["evaluation_utc"]


@pytest.mark.unit
def test_pulse_short_pick_scored_hit_when_move_negative():
    agent = _make_agent()
    now = pe._utc_now()
    pick = _archived_pick("BEAR", "top_short", now - timedelta(hours=48))
    agent._pulse_store["archive"] = [pick]
    _stub_move(agent, {"BEAR": -4.0})

    agent._evaluate_matured_pulse_picks_locked(CFG)

    scored = agent._pulse_store["archive"][0]
    assert scored["realized_signed_move_pct"] == -4.0
    assert scored["hit"] is True  # short pick + negative move = correct


@pytest.mark.unit
def test_pulse_long_pick_miss_when_move_negative():
    agent = _make_agent()
    now = pe._utc_now()
    pick = _archived_pick("GCT", "top_long", now - timedelta(hours=48))
    agent._pulse_store["archive"] = [pick]
    _stub_move(agent, {"GCT": -3.0})

    agent._evaluate_matured_pulse_picks_locked(CFG)

    scored = agent._pulse_store["archive"][0]
    assert scored["realized_signed_move_pct"] == -3.0
    assert scored["hit"] is False  # long pick but move fell


@pytest.mark.unit
def test_pulse_pick_not_scored_before_36h_window():
    agent = _make_agent()
    now = pe._utc_now()
    pick = _archived_pick("EARLY", "top_long", now - timedelta(hours=10))
    agent._pulse_store["archive"] = [pick]
    _stub_move(agent, {"EARLY": 6.0})

    changed = agent._evaluate_matured_pulse_picks_locked(CFG)

    assert changed is False
    scored = agent._pulse_store["archive"][0]
    assert "realized_signed_move_pct" not in scored
    assert "hit" not in scored


@pytest.mark.unit
def test_pulse_unresolved_marked_after_21d():
    agent = _make_agent()
    now = pe._utc_now()
    pick = _archived_pick("NOBAR", "top_long", now - timedelta(days=22))
    agent._pulse_store["archive"] = [pick]
    _stub_move(agent, {"NOBAR": None})  # no OHLC available

    changed = agent._evaluate_matured_pulse_picks_locked(CFG)

    assert changed is True
    scored = agent._pulse_store["archive"][0]
    assert scored["unresolved"] is True
    assert "realized_signed_move_pct" not in scored
    assert "hit" not in scored


@pytest.mark.unit
def test_pulse_skip_non_reported_archive_reasons():
    agent = _make_agent()
    now = pe._utc_now()
    # active_limit / undated_event_expired archives must NOT be scored — the
    # event didn't necessarily happen, so there is no realized outcome.
    p1 = _archived_pick("LIM", "top_long", now - timedelta(hours=48), archive_reason="active_limit")
    p2 = _archived_pick("UND", "top_long", now - timedelta(hours=48), archive_reason="undated_event_expired")
    agent._pulse_store["archive"] = [p1, p2]
    _stub_move(agent, {"LIM": 6.0, "UND": 6.0})

    changed = agent._evaluate_matured_pulse_picks_locked(CFG)

    assert changed is False
    for p in agent._pulse_store["archive"]:
        assert "realized_signed_move_pct" not in p


@pytest.mark.unit
def test_pulse_metrics_per_side():
    agent = _make_agent()
    now = pe._utc_now()
    event = now - timedelta(hours=48)
    picks = [
        _archived_pick("L1", "top_long", event),
        _archived_pick("L2", "top_long", event),
        _archived_pick("S1", "top_short", event),
    ]
    # Pre-stamp realized outcomes directly (as the evaluator would).
    picks[0]["realized_signed_move_pct"] = 6.0
    picks[0]["hit"] = True
    picks[1]["realized_signed_move_pct"] = -2.0
    picks[1]["hit"] = False
    picks[2]["realized_signed_move_pct"] = -5.0
    picks[2]["hit"] = True
    agent._pulse_store["archive"] = picks

    metrics = agent._compute_pulse_track_record_metrics(CFG)

    assert metrics["overall"]["total"] == 3
    assert metrics["overall"]["hits"] == 2
    assert metrics["overall"]["hit_rate"] == round(100.0 * 2 / 3, 1)
    assert metrics["per_side"]["top_long"]["n"] == 2
    assert metrics["per_side"]["top_long"]["hits"] == 1
    assert metrics["per_side"]["top_long"]["hit_rate"] == 50.0
    assert metrics["per_side"]["top_short"]["n"] == 1
    assert metrics["per_side"]["top_short"]["hits"] == 1
    assert metrics["per_side"]["top_short"]["hit_rate"] == 100.0


@pytest.mark.unit
def test_pulse_metrics_zero_when_no_scored_picks():
    agent = _make_agent()
    metrics = agent._compute_pulse_track_record_metrics(CFG)
    assert metrics["overall"]["total"] == 0
    assert metrics["overall"]["hit_rate"] is None
    assert metrics["per_side"]["top_long"]["n"] == 0
    assert metrics["per_side"]["top_short"]["n"] == 0