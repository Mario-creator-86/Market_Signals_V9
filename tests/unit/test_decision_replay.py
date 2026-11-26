"""Phase 13 unit tests — §89 point-in-time AI replay + deterministic outcomes."""
import json
from datetime import datetime, timezone

import pytest

from core.ai.provider import AIResponse
from core.data_model.pit import UNAVAILABLE
from core.decision_ledger.replay import (
    bars_available_at, build_pit_snapshot, evaluate_decision_outcome, run_ai_replay,
)
from core.decision_ledger.store import DecisionLedgerStore
from core.decision_ledger.types import SOURCE_REPLAY


class StubProvider:
    name = "stub"

    def generate(self, prompt, **kwargs):
        return AIResponse(text=json.dumps({
            "recommendation": "watch", "confidence": 0.55, "size_factor": 0.3,
            "leverage": 1, "sentiment": "neutral", "supports_direction": True,
            "reason": "replay assessment", "risks": [],
        }), model="stub-replay")


class FakeHistorical:
    """Deterministic PIT source: returns the newest record at/below T."""

    def __init__(self, records):
        self.records = records  # list[(available_from_iso, payload)]

    def as_of(self, entity_type, symbol, T):
        eligible = [
            (stamp, payload) for stamp, payload in self.records
            if datetime.fromisoformat(stamp) <= T
        ]
        if not eligible:
            return UNAVAILABLE
        stamp, payload = max(eligible, key=lambda item: item[0])
        return {"pit": {"available_from": stamp}, **payload}


def _bars():
    return [
        {"available_from": "2026-08-03T20:00:00+00:00", "close": 100.0},
        {"available_from": "2026-08-04T20:00:00+00:00", "close": 101.0},
        {"available_from": "2026-08-05T20:00:00+00:00", "close": 99.0},
        {"available_from": "2026-08-06T20:00:00+00:00", "close": 97.0},
        {"available_from": "2026-08-07T20:00:00+00:00", "close": 96.0},
        {"available_from": "2026-08-10T20:00:00+00:00", "close": 95.0},
        {"available_from": "2026-08-11T20:00:00+00:00", "close": 94.0},
    ]


def test_pit_snapshot_excludes_future_records():
    historical = FakeHistorical([
        ("2026-08-03T20:00:00+00:00", {"close": 100.0}),
        ("2026-08-04T20:00:00+00:00", {"close": 101.0}),
        ("2026-08-05T20:00:00+00:00", {"close": 99.0}),  # future vs T below
    ])
    T = datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc)
    snapshot = build_pit_snapshot("AAPL", T, store=_store(), historical=historical)
    entity = snapshot["entities"]["ohlcv"]
    assert entity["available"] is True
    assert entity["available_from"] == "2026-08-04T20:00:00+00:00"
    assert entity["value"]["close"] == 101.0  # the T+1 bar (99.0) is provably absent
    assert snapshot["inputs_sha256"]


def test_pit_snapshot_unavailable_is_explicit():
    historical = FakeHistorical([])
    snapshot = build_pit_snapshot(
        "AAPL", datetime(2026, 8, 5, tzinfo=timezone.utc), store=_store(), historical=historical,
    )
    assert snapshot["entities"]["ohlcv"] == {"available": False, "value": "UNAVAILABLE"}


def _store(tmp_path=None):
    from pathlib import Path

    return DecisionLedgerStore(Path(tmp_path) / "ledger.db" if tmp_path else "data_test_replay.db")


def test_outcome_deterministic_and_inconclusive_without_future_bars():
    from core.decision_ledger.types import LedgerEntry

    entry = LedgerEntry(
        decision_id="dec_out_1", created_at="2026-08-04T21:00:00+00:00", ticker="AAPL",
        action="OPEN_LONG", horizon="SWING", source="SIGNAL_PATH", status="accepted",
        strategy="MOMENTUM", ai={}, ai_proposal={}, validation={}, critic={},
        risk={}, capital_allowed={}, executed={},
    )
    outcome = evaluate_decision_outcome(entry, _bars(), threshold_pct=1.0)
    assert outcome.outcome == "ADVERSE"  # 101 -> 94 over 5 future sessions
    assert outcome.forward_return_pct == pytest.approx(-6.930693069306931)
    assert outcome.bars_used == 5
    thin = evaluate_decision_outcome(entry, _bars()[:2], threshold_pct=1.0)
    assert thin.outcome == "INCONCLUSIVE"
    assert thin.detail["reason"] == "insufficient_post_decision_data"


def test_outcome_inverts_for_short_direction():
    from core.decision_ledger.types import LedgerEntry

    entry = LedgerEntry(
        decision_id="dec_out_2", created_at="2026-08-04T21:00:00+00:00", ticker="AAPL",
        action="OPEN_SHORT", horizon="SWING", source="SIGNAL_PATH", status="accepted",
        strategy="MOMENTUM", ai={}, ai_proposal={}, validation={}, critic={},
        risk={}, capital_allowed={}, executed={},
    )
    outcome = evaluate_decision_outcome(entry, _bars(), threshold_pct=1.0)
    assert outcome.outcome == "FAVORABLE"  # adverse for long = favorable for short


def test_run_ai_replay_records_replay_entry_and_never_executes(tmp_path):
    from core.decision_ledger.types import LedgerEntry
    from core.pre_earnings.decision import _pe_default_services

    store = _store(tmp_path)
    original = LedgerEntry(
        decision_id="dec_replay_src", created_at="2026-08-04T21:00:00+00:00", ticker="AAPL",
        action="OPEN_LONG", horizon="SWING", source="SIGNAL_PATH", status="accepted",
        strategy="MOMENTUM", ai={"provider": "stub", "model": "m1"},
        ai_proposal={}, validation={}, critic={}, risk={}, capital_allowed={}, executed={},
        data_existed={"signal_id": "sig_5", "entry": 101.0},
    )
    store.append_entry(original)

    historical = FakeHistorical([("2026-08-04T20:00:00+00:00", {"close": 101.0})])
    result = run_ai_replay(
        "dec_replay_src", store=store, config={},
        services={"primary_provider": StubProvider(), **_pe_default_services()},
        historical=historical,
    )
    assert result["original_decision_id"] == "dec_replay_src"
    replay_entry = store.get_entry(result["replay_decision_id"])
    assert replay_entry.source == SOURCE_REPLAY
    assert replay_entry.replay_of == "dec_replay_src"
    # The replay is an audit artifact — it carries explicit no-order evidence.
    assert replay_entry.executed["state"] == "NONE"
    assert replay_entry.capital_allowed["allowed"] is False
    # PIT proof: the snapshot only saw the bar available at/below T.
    assert replay_entry.data_existed["pit_snapshot"]["ohlcv"]["available_from"] == "2026-08-04T20:00:00+00:00"
    # Redelivering the replay is idempotent.
    again = run_ai_replay(
        "dec_replay_src", store=store, config={},
        services={"primary_provider": StubProvider(), **_pe_default_services()},
        historical=historical,
    )
    assert again["created"] is False and again["replay_decision_id"] == result["replay_decision_id"]


def test_run_ai_replay_unknown_decision_raises(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(KeyError):
        run_ai_replay("dec_missing", store=store, config={}, services={})


def test_bars_available_at_strictly_respects_anchor():
    T = datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc)
    available = bars_available_at(_bars(), T)
    assert [bar["close"] for bar in available] == [100.0, 101.0]
