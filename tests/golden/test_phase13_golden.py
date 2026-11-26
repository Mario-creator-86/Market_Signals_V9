"""Phase 13 golden tests.

GOLD-002 — Point-in-time AI replay: a replay at T provably excludes data
published after T, is recorded as an audit REPLAY entry that can never reach
the broker, and its outcome evaluation is deterministic arithmetic.

Also freezes §90 (rejection-quality partition) and §88 (identical-input
Model Lab digest) so autonomous development cannot silently weaken them.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from core.ai.provider import AIResponse
from core.data_model.pit import UNAVAILABLE
from core.decision_ledger.model_lab import ModelLab
from core.decision_ledger.no_trade_eval import evaluate_no_trade_decisions
from core.decision_ledger.replay import build_pit_snapshot, evaluate_decision_outcome, run_ai_replay
from core.decision_ledger.store import DecisionLedgerStore
from core.decision_ledger.types import SOURCE_REPLAY, SOURCE_SIGNAL_PATH, LedgerEntry

pytestmark = pytest.mark.golden


class GoldenProvider:
    name = "golden"

    def generate(self, prompt, **kwargs):
        return AIResponse(text=json.dumps({
            "action": "WATCH", "ticker": "GOLD", "horizon": "SWING",
            "confidence": 0.55, "strategy_id": "MOM", "thesis": "golden replay",
            "supporting_evidence": ["e"], "opposing_evidence": [], "risk_flags": [],
            "data_quality": 0.9, "requires_more_data": False,
        }), model="golden-1")


class GoldenHistorical:
    def __init__(self, records):
        self.records = records  # [(iso_available_from, payload)]

    def as_of(self, entity_type, symbol, T):
        eligible = [(stamp, payload) for stamp, payload in self.records
                    if datetime.fromisoformat(stamp) <= T]
        if not eligible:
            return UNAVAILABLE
        stamp, payload = max(eligible, key=lambda item: item[0])
        return {"pit": {"available_from": stamp}, **payload}


def _permissive_services():
    from core.pre_earnings.decision import _pe_default_services

    return _pe_default_services()


def _golden_entry(decision_id="dec_gold_002"):
    return LedgerEntry(
        decision_id=decision_id, created_at="2026-06-02T21:00:00+00:00", ticker="GOLD",
        action="OPEN_LONG", horizon="SWING", source=SOURCE_SIGNAL_PATH, status="accepted",
        strategy="MOMENTUM",
        ai={"provider": "golden", "model": "golden-0", "prompt_version": "trade_review_v1"},
        ai_proposal={"action": "OPEN_LONG", "confidence": 0.7},
        validation={"ok": True, "stage": "RISK_GATE"},
        critic={"available": True, "veto": False}, risk={"accepted": True},
        capital_allowed={"usd_size": 500.0}, executed={"order_intent_id": "oi-gold", "state": "FILLED"},
        data_existed={"signal_id": "sig_gold", "entry": 100.0},
    )


def _golden_bars():
    start = datetime(2026, 6, 2, 20, 0, tzinfo=timezone.utc)
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0]
    return [
        {"available_from": (start + timedelta(days=index)).isoformat(), "close": close}
        for index, close in enumerate(closes)
    ]


def test_gold_002_point_in_time_ai_replay(tmp_path):
    store = DecisionLedgerStore(tmp_path / "gold13.db")
    entry = store.append_entry(_golden_entry())[0]
    T = datetime(2026, 6, 2, 21, 0, tzinfo=timezone.utc)

    bars = _golden_bars()
    # One bar published AFTER the decision time (June 3 close=101) must be invisible.
    historical = GoldenHistorical([
        (bar["available_from"], {"close": bar["close"]}) for bar in bars[:2]
    ] + [("2026-06-03T20:00:00+00:00", {"close": 999.0})])  # poisoned future record

    snapshot = build_pit_snapshot("GOLD", T, store=store, historical=historical)
    assert snapshot["entities"]["ohlcv"]["available_from"] == "2026-06-02T20:00:00+00:00"
    assert snapshot["entities"]["ohlcv"]["value"]["close"] == 100.0  # poison excluded

    result = run_ai_replay(
        entry.decision_id, store=store, config={},
        services={"primary_provider": GoldenProvider(), **_permissive_services()},
        historical=historical,
    )
    replay_entry = store.get_entry(result["replay_decision_id"])
    assert replay_entry.source == SOURCE_REPLAY
    assert replay_entry.replay_of == entry.decision_id
    assert replay_entry.pit_as_of == T.isoformat()
    assert replay_entry.data_existed["pit_snapshot"]["ohlcv"]["value"]["close"] == 100.0
    # Replay can never carry order linkage (§40: AI prose/procedure never orders).
    assert replay_entry.executed["order_intent_id"] is None
    assert replay_entry.executed["state"] == "NONE"

    outcome = evaluate_decision_outcome(entry, bars, threshold_pct=1.0)
    assert outcome.outcome == "FAVORABLE"
    assert outcome.forward_return_pct == pytest.approx(5.0)  # baseline 100 (≤T) -> 5th future close 105
    stored, created = store.append_outcome(outcome)
    assert created
    assert store.get_entry(entry.decision_id).afterward["outcome"] == "FAVORABLE"
    # Deterministic: identical inputs → identical outcome classification.
    again = evaluate_decision_outcome(entry, bars, threshold_pct=1.0)
    assert (again.outcome, again.forward_return_pct) == (outcome.outcome, outcome.forward_return_pct)


def test_gold_090_rejection_quality_partition_frozen(tmp_path):
    store = DecisionLedgerStore(tmp_path / "gold13b.db")
    rejected_good = LedgerEntry(
        decision_id="dec_g090_good", created_at="2026-06-02T21:00:00+00:00", ticker="AAA",
        action="NO_TRADE", horizon="SWING", source=SOURCE_SIGNAL_PATH, status="rejected",
        strategy="X", ai={}, ai_proposal={}, validation={}, critic={},
        risk={}, capital_allowed={}, executed={},
    )
    rejected_bad = LedgerEntry(
        decision_id="dec_g090_bad", created_at="2026-06-02T21:00:00+00:00", ticker="BBB",
        action="NO_TRADE", horizon="SWING", source=SOURCE_SIGNAL_PATH, status="rejected",
        strategy="X", ai={}, ai_proposal={}, validation={}, critic={},
        risk={}, capital_allowed={}, executed={},
    )
    rejected_thin = LedgerEntry(
        decision_id="dec_g090_thin", created_at="2026-06-02T21:00:00+00:00", ticker="CCC",
        action="NO_TRADE", horizon="SWING", source=SOURCE_SIGNAL_PATH, status="rejected",
        strategy="X", ai={}, ai_proposal={}, validation={}, critic={},
        risk={}, capital_allowed={}, executed={},
    )
    store.append_entry(rejected_good)
    store.append_entry(rejected_bad)
    store.append_entry(rejected_thin)

    start = datetime(2026, 6, 2, 20, 0, tzinfo=timezone.utc)

    def bars(closes):
        return [{"available_from": (start + timedelta(days=i)).isoformat(), "close": close}
                for i, close in enumerate(closes)]

    report = evaluate_no_trade_decisions(
        [rejected_good, rejected_bad, rejected_thin],
        {
            "AAA": bars([100.0, 100.5, 112.0, 113.0, 114.0, 115.0, 116.0]),  # +16% missed
            "BBB": bars([100.0, 100.5, 88.0, 87.0, 86.0, 85.0, 84.0]),        # -16% dodged
            "CCC": bars([100.0, 100.5]),                                      # no horizon data
        },
        threshold_pct=1.0,
    )
    assert report["total_recorded"] == 3
    assert report["correctly_rejected"] == 1
    assert report["incorrectly_rejected_profitable"] == 1
    assert report["inconclusive"] == 1
    assert report["rejection_precision"] == 0.5


def test_gold_088_model_lab_identical_input_digest_frozen(tmp_path):
    store = DecisionLedgerStore(tmp_path / "gold13c.db")
    lab = ModelLab(store)
    signal = {"ticker": "GOLD", "entry": 100.0, "tp": 110.0, "sl": 95.0,
              "signal_score": 0.75, "horizon": "SWING", "strategy_type": "MOMENTUM"}
    run_one = lab.run(
        role="quant", signal=signal, config={},
        candidates=[{"provider": GoldenProvider(), "model": "golden-1"}],
        services=_permissive_services(),
    )
    run_two = lab.run(
        role="quant", signal=signal, config={},
        candidates=[{"provider": GoldenProvider(), "model": "golden-1"}],
        services=_permissive_services(),
    )
    # Identical point-in-time inputs must freeze to the identical digest.
    assert run_one["inputs_sha256"] == run_two["inputs_sha256"]
    changed = lab.run(
        role="quant", signal={**signal, "signal_score": 0.9}, config={},
        candidates=[{"provider": GoldenProvider(), "model": "golden-1"}],
        services=_permissive_services(),
    )
    assert changed["inputs_sha256"] != run_one["inputs_sha256"]
    # A different role with the same signal is a DIFFERENT comparison bucket.
    other_role = lab.run(
        role="risk_critic", signal=signal, config={},
        candidates=[{"provider": GoldenProvider(), "model": "golden-1"}],
        services=_permissive_services(),
    )
    summary = lab.role_summary()
    assert set(summary["roles"].keys()) >= {"quant", "risk_critic"}
    assert other_role["run_id"] != run_one["run_id"]
