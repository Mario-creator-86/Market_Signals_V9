"""The app measures its own AI reviews and shows the result to the AI.

For every candidate in the decision ledger - taken or rejected - the scorecard
replays the same exit rule on later prices, so "what I approved" and "what I
rejected" are compared on one yardstick, and the AI's stated confidence is
checked against what happened. The reviewer then receives that record with
each new candidate of the same strategy.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from core.ai.scorecard import (
    build_scorecard,
    load_scorecard,
    replay_candidate,
    track_record_for,
    write_scorecard,
)

MIN = 60_000
BAR = 5 * MIN
T0 = 1_795_000_000_000 // BAR * BAR
NOW = T0 + 10 * 24 * 60 * MIN


def _bars(start, closes, *, spike_high=None, spike_low=None):
    """5-minute bars: flat candles at the given closes; optional single extreme."""
    out, prev = [], closes[0]
    for i, close in enumerate(closes):
        high, low = max(prev, close), min(prev, close)
        if spike_high and i == spike_high[0]:
            high = spike_high[1]
        if spike_low and i == spike_low[0]:
            low = spike_low[1]
        out.append([start + i * BAR, prev, high, low, close])
        prev = close
    return out


def test_replay_exits_at_the_target_the_stop_or_after_the_time_limit():
    long_ = {"direction": "LONG", "entry": 100.0, "stop": 96.0, "target": 106.0}
    flat = [100.0] * 30

    won = replay_candidate(long_, _bars(T0, flat, spike_high=(3, 107.0)), time_stop_minutes=120, cost=0.0)
    assert won["reason"] == "target" and won["net"] == pytest.approx(0.06)

    lost = replay_candidate(long_, _bars(T0, flat, spike_low=(3, 95.0)), time_stop_minutes=120, cost=0.0)
    assert lost["reason"] == "stop" and lost["net"] == pytest.approx(-0.04)

    drift = replay_candidate(long_, _bars(T0, [100.0] * 10 + [101.0] * 30), time_stop_minutes=120, cost=0.001)
    assert drift["reason"] == "time" and drift["net"] == pytest.approx(0.01 - 0.001)

    short = {"direction": "SHORT", "entry": 100.0, "stop": 104.0, "target": 94.0}
    fell = replay_candidate(short, _bars(T0, flat, spike_low=(2, 93.0)), time_stop_minutes=120, cost=0.0)
    assert fell["reason"] == "target" and fell["net"] == pytest.approx(0.06)


def test_a_bar_that_touches_both_levels_counts_as_the_stop_and_bad_data_is_skipped():
    long_ = {"direction": "LONG", "entry": 100.0, "stop": 96.0, "target": 106.0}
    both = _bars(T0, [100.0] * 30, spike_high=(2, 107.0))
    both[2][3] = 95.0
    assert replay_candidate(long_, both, time_stop_minutes=120, cost=0.0)["reason"] == "stop"

    assert replay_candidate(long_, [], time_stop_minutes=120, cost=0.0) is None
    far = _bars(T0, [150.0] * 30)      # the bars are not this instrument's price
    assert replay_candidate(long_, far, time_stop_minutes=120, cost=0.0) is None


def _ledger(tmp_path, decisions):
    path = tmp_path / "ledger.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE decision_ledger_entries (decision_id TEXT PRIMARY KEY, created_at TEXT, ticker TEXT, action TEXT, "
               "horizon TEXT, source TEXT, status TEXT, strategy TEXT, replay_of TEXT, pit_as_of TEXT, entry_json TEXT, outcome_json TEXT)")
    from datetime import datetime, timezone

    for i, d in enumerate(decisions):
        ts = T0 + d.get("offset_min", i * 24 * 60) * MIN
        created = datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat()
        direction = d.get("direction", "LONG")
        entry = {
            "ai": {"consulted": d.get("ai_action") is not None},
            "ai_proposal": {"action": d.get("ai_action"), "confidence": d.get("confidence")},
            "critic": {"available": d.get("critic_veto") is not None, "veto": bool(d.get("critic_veto"))},
            "data_existed": {"entry": 100.0},
            "validation": {"checks": {"authoritative": {"capital_entry": {"checks": {"data": {
                "direction": direction, "entry": 100.0,
                "stop": 96.0 if direction == "LONG" else 104.0,
                "target": 106.0 if direction == "LONG" else 94.0}}}}}},
        }
        db.execute("INSERT INTO decision_ledger_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            f"dec_{i}", created, d.get("ticker", f"C{i}-USD"), d.get("action", "OPEN_LONG"), "", "SIGNAL_PATH",
            d["status"], d.get("strategy", f"SCANNER_AI_{direction}"), None, None, json.dumps(entry), None))
    db.commit()
    db.close()
    return path


def _provider(outcomes, calls=None):
    """decision order -> 'win' | 'loss' | 'none' for the bars that follow it."""
    def bars(ticker, start_ms):
        if calls is not None:
            calls.append(ticker)
        kind = outcomes.get(ticker, "none")
        if kind == "none":
            return []
        if kind == "win":
            return _bars(start_ms, [100.0] * 40, spike_high=(2, 107.0))
        return _bars(start_ms, [100.0] * 40, spike_low=(2, 95.0))
    return bars


def test_scorecard_compares_taken_and_rejected_candidates_and_checks_confidence(tmp_path):
    decisions, outcomes = [], {}
    for i in range(12):   # taken, low confidence, mostly winners
        decisions.append({"status": "accepted", "ai_action": "OPEN_LONG", "confidence": 0.5, "critic_veto": False})
        outcomes[f"C{i}-USD"] = "win" if i < 9 else "loss"
    for i in range(12, 22):  # taken, high confidence, mostly losers
        decisions.append({"status": "accepted", "ai_action": "OPEN_LONG", "confidence": 0.8, "critic_veto": False})
        outcomes[f"C{i}-USD"] = "loss" if i < 20 else "win"
    for i in range(22, 34):  # rejected by the AI, would have won
        decisions.append({"status": "rejected", "ai_action": "NO_TRADE", "action": "NO_TRADE", "confidence": 0.3})
        outcomes[f"C{i}-USD"] = "win"
    for i in range(34, 40):  # rejected by the critic
        decisions.append({"status": "rejected", "ai_action": "OPEN_LONG", "action": "NO_TRADE", "confidence": 0.6, "critic_veto": True})
        outcomes[f"C{i}-USD"] = "loss"
    ledger = _ledger(tmp_path, decisions)

    card = build_scorecard(ledger, bars_provider=_provider(outcomes), now_ms=T0 + 60 * 24 * 60 * MIN,
                           replay_cache_path=tmp_path / "replays.json", cost=0.0)

    block = card["by_strategy"]["SCANNER_AI_LONG"]
    assert block["taken"]["n"] == 22 and block["rejected_by_ai"]["n"] == 12 and block["rejected_by_critic"]["n"] == 6
    assert block["rejected_by_ai"]["mean_net_pct"] == pytest.approx(6.0)
    assert block["rejected_by_critic"]["mean_net_pct"] == pytest.approx(-4.0)
    assert block["taken"]["win_rate_pct"] == pytest.approx(50.0)
    by_range = {row["range"]: row for row in block["confidence"]}
    assert by_range["0.40-0.60"]["mean_net_pct"] > 0 > by_range["0.75-1.00"]["mean_net_pct"]
    assert card["decisions_replayed"] == 40


def test_track_record_tells_the_reviewer_what_its_past_reviews_were_worth(tmp_path):
    decisions, outcomes = [], {}
    for i in range(30):
        decisions.append({"status": "accepted", "ai_action": "OPEN_LONG", "confidence": 0.8 if i % 2 else 0.5, "critic_veto": False})
        outcomes[f"C{i}-USD"] = "loss" if i % 2 else "win"       # high confidence loses
    for i in range(30, 60):
        decisions.append({"status": "rejected", "ai_action": "NO_TRADE", "action": "NO_TRADE", "confidence": 0.3})
        outcomes[f"C{i}-USD"] = "win"
    card = build_scorecard(_ledger(tmp_path, decisions), bars_provider=_provider(outcomes),
                           now_ms=T0 + 90 * 24 * 60 * MIN, replay_cache_path=tmp_path / "r.json", cost=0.0)
    path = tmp_path / "card.json"
    write_scorecard(card, path)

    record = track_record_for({"strategy_type": "SCANNER_AI_LONG", "action": "BUY"}, load_scorecard(path, now_ms=T0 + 90 * 24 * 60 * MIN))

    assert record["taken"]["n"] == 30 and record["rejected_by_ai"]["n"] == 30
    text = " ".join(record["reading"]).lower()
    assert "rejected" in text and "better" in text          # the rejected ones did better than the taken ones
    assert "confidence" in text                              # and high confidence did not win more
    assert track_record_for({"strategy_type": "DELIST_NOTICE_SHORT"}, load_scorecard(path, now_ms=T0 + 90 * 24 * 60 * MIN)) is None


def test_small_samples_are_marked_and_repeat_reviews_of_one_coin_count_once(tmp_path):
    decisions = [{"status": "accepted", "ai_action": "OPEN_LONG", "confidence": 0.5, "critic_veto": False,
                  "ticker": "ACE-USD", "offset_min": i * 50} for i in range(6)]      # six reviews of one coin in one day
    card = build_scorecard(_ledger(tmp_path, decisions), bars_provider=_provider({"ACE-USD": "win"}),
                           now_ms=T0 + 30 * 24 * 60 * MIN, replay_cache_path=tmp_path / "r.json", cost=0.0)
    taken = card["by_strategy"]["SCANNER_AI_LONG"]["taken"]
    assert taken["n"] == 6 and taken["independent_n"] == 1 and taken["small_sample"] is True


def test_rebuild_fetches_prices_only_for_new_decisions_and_skips_too_recent_ones(tmp_path):
    decisions = [{"status": "accepted", "ai_action": "OPEN_LONG", "confidence": 0.5, "critic_veto": False} for _ in range(3)]
    ledger = _ledger(tmp_path, decisions)
    calls = []
    provider = _provider({"C0-USD": "win", "C1-USD": "loss", "C2-USD": "win"}, calls)
    now = T0 + 2 * 24 * 60 * MIN + 30 * MIN       # the third decision is 30 minutes old: no outcome yet

    first = build_scorecard(ledger, bars_provider=provider, now_ms=now, replay_cache_path=tmp_path / "r.json", cost=0.0)
    assert first["decisions_replayed"] == 2 and sorted(calls) == ["C0-USD", "C1-USD"]

    calls.clear()
    later = build_scorecard(ledger, bars_provider=provider, now_ms=now + 24 * 60 * MIN, replay_cache_path=tmp_path / "r.json", cost=0.0)
    assert later["decisions_replayed"] == 3 and calls == ["C2-USD"]


def test_an_old_or_missing_scorecard_is_not_used(tmp_path):
    path = tmp_path / "card.json"
    assert load_scorecard(path, now_ms=NOW) is None
    write_scorecard({"generated_at_ms": NOW - 10 * 24 * 60 * MIN, "by_strategy": {}}, path)
    assert load_scorecard(path, now_ms=NOW, max_age_hours=72) is None


def test_the_reviewer_prompt_carries_the_track_record_and_positioning():
    from core.ollama_decision import _compact_signal

    compact = _compact_signal({"ticker": "ACE-USD", "action": "BUY",
                               "ai_track_record": {"taken": {"n": 30}}, "positioning_evidence": {"open_interest": {}}})
    assert "ai_track_record" in compact and "positioning_evidence" in compact


def test_the_critic_sees_a_one_line_summary_of_the_track_record():
    from core.ai.critic import _compact_signal as critic_context

    text = critic_context({"ticker": "ACE-USD", "ai_track_record": {
        "taken": {"independent_n": 206, "mean_net_pct": -0.25},
        "rejected_by_ai": {"independent_n": 111, "mean_net_pct": -0.24}}})
    assert "ai_track_record(approved n=206 avg=-0.25%, rejected n=111 avg=-0.24%" in text
    assert "ai_track_record" not in critic_context({"ticker": "ACE-USD"})


def test_an_entry_the_ai_advised_against_is_not_filed_as_approved(tmp_path):
    """A rule basket is entered even when the AI says no (advisory, paper only): that is not an AI approval."""
    from core.ai.scorecard import _read_decisions

    ledger = _ledger(tmp_path, [
        {"status": "accepted", "ai_action": "NO_TRADE", "strategy": "XS_MOMENTUM_LONG"},
        {"status": "accepted", "ai_action": "OPEN_LONG", "critic_veto": True, "strategy": "LAB_ABC123_LONG"},
        {"status": "accepted", "ai_action": "OPEN_LONG", "critic_veto": False},
        {"status": "rejected", "ai_action": "NO_TRADE"},
    ])
    groups = [row["group"] for row in _read_decisions(ledger, 0)]
    assert groups == ["rejected_by_ai", "rejected_by_critic", "taken", "rejected_by_ai"]
