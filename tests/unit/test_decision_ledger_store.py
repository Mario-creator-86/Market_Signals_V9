"""Phase 13 unit tests — DecisionLedgerStore persistence contracts."""
import sqlite3

import pytest

from core.decision_ledger.store import (
    SCHEMA_VERSION, DecisionLedgerStore, decision_ledger_store_path,
)
from core.decision_ledger.types import (
    LedgerEntry, LedgerOutcome, SOURCE_REPLAY, SOURCE_SIGNAL_PATH,
)


def _entry(decision_id="dec_1", action="OPEN_LONG", source=SOURCE_SIGNAL_PATH, **overrides):
    base = dict(
        decision_id=decision_id,
        created_at="2026-08-16T14:00:00+00:00",
        ticker="NVDA",
        action=action,
        horizon="SWING",
        source=source,
        status="accepted",
        strategy="MOMENTUM",
        ai={"available": True, "provider": "stub", "model": "m1", "prompt_version": "trade_review_v1"},
        ai_proposal={"action": action, "confidence": 0.7},
        validation={"ok": True, "stage": "RISK_GATE"},
        critic={"available": False, "reason": "critic_not_run"},
        risk={"accepted": True},
        capital_allowed={"usd_size": 500.0},
        executed={"order_intent_id": "oi-1", "state": "FILLED"},
        data_existed={"signal_id": "s1", "entry": 100.0},
    )
    base.update(overrides)
    return LedgerEntry(**base)


def test_store_migration_is_idempotent_and_preserves_entries(tmp_path):
    path = tmp_path / "ledger.db"
    store = DecisionLedgerStore(path)
    assert store.migrate() == SCHEMA_VERSION
    store.append_entry(_entry("dec_keep"))
    store.migrate()  # re-run: idempotent, no data loss
    assert store.get_entry("dec_keep") is not None
    with sqlite3.connect(str(path)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION


def test_append_entry_is_idempotent_on_decision_id(tmp_path):
    store = DecisionLedgerStore(tmp_path / "ledger.db")
    first, created = store.append_entry(_entry("dec_dup"))
    again, created_again = store.append_entry(_entry("dec_dup", action="CLOSE"))
    assert created is True and created_again is False
    assert again.action == "OPEN_LONG"  # original preserved, never rewritten


def test_invalid_source_is_rejected(tmp_path):
    store = DecisionLedgerStore(tmp_path / "ledger.db")
    with pytest.raises(ValueError):
        store.append_entry(_entry("dec_bad", source="MYSTERY"))


def test_list_entries_filters_and_bounds(tmp_path):
    store = DecisionLedgerStore(tmp_path / "ledger.db")
    for index in range(5):
        store.append_entry(_entry(f"dec_a{index}", ticker="AAPL"))
    store.append_entry(_entry("dec_r", source=SOURCE_REPLAY, replay_of="dec_a0"))
    assert store.count_entries() == 6
    assert store.count_entries(source=SOURCE_REPLAY) == 1
    assert [entry.decision_id for entry in store.list_entries(ticker="AAPL")] == [
        f"dec_a{index}" for index in range(5)
    ]
    assert all(len(batch) <= 2 for batch in [store.list_entries(limit=2)])
    assert store.list_entries(limit=0)  # bounded to >= 1, never crashes


def test_status_counts_cover_the_complete_persistent_ledger(tmp_path):
    store = DecisionLedgerStore(tmp_path / "ledger.db")
    store.append_entry(_entry("dec_ok", status="accepted"))
    store.append_entry(_entry("dec_no", status="rejected", action="NO_TRADE"))
    store.append_entry(_entry("dec_replay", status="replayed", source=SOURCE_REPLAY))

    assert store.count_entries_by_status() == {
        "accepted": 1,
        "rejected": 1,
        "replayed": 1,
    }


def test_outcomes_are_append_only_latest_wins(tmp_path):
    store = DecisionLedgerStore(tmp_path / "ledger.db")
    store.append_entry(_entry("dec_out"))
    first = LedgerOutcome(
        decision_id="dec_out", evaluated_at="2026-08-17T00:00:00+00:00",
        outcome="INCONCLUSIVE", basis="forward_return_5_sessions", bars_used=1,
    )
    second = LedgerOutcome(
        decision_id="dec_out", evaluated_at="2026-08-18T00:00:00+00:00",
        outcome="FAVORABLE", basis="forward_return_5_sessions", bars_used=5,
        forward_return_pct=4.2,
    )
    assert store.append_outcome(first)[1] is True
    assert store.append_outcome(first)[1] is False  # same evaluated_at: idempotent
    assert store.append_outcome(second)[1] is True
    assert store.get_outcome("dec_out").outcome == "FAVORABLE"
    assert len(store.list_outcomes("dec_out")) == 2  # history kept
    # Entry row mirrors the latest outcome for cheap reads.
    assert store.get_entry("dec_out").afterward["outcome"] == "FAVORABLE"


def test_outcome_for_unknown_decision_rejected(tmp_path):
    store = DecisionLedgerStore(tmp_path / "ledger.db")
    outcome = LedgerOutcome(
        decision_id="dec_missing", evaluated_at="2026-08-17T00:00:00+00:00",
        outcome="FLAT", basis="forward_return_5_sessions",
    )
    with pytest.raises(KeyError):
        store.append_outcome(outcome)


def test_invalid_outcome_value_rejected(tmp_path):
    store = DecisionLedgerStore(tmp_path / "ledger.db")
    store.append_entry(_entry("dec_bad_outcome"))
    with pytest.raises(ValueError):
        store.append_outcome(LedgerOutcome(
            decision_id="dec_bad_outcome", evaluated_at="2026-08-17T00:00:00+00:00",
            outcome="PROFITABLE", basis="x",
        ))


def test_model_lab_runs_persist_per_model_results(tmp_path):
    store = DecisionLedgerStore(tmp_path / "ledger.db")
    run = {"run_id": "lab_1", "role": "quant", "created_at": "2026-08-16T15:00:00+00:00",
           "inputs_sha256": "abc", "prompt_version": "trade_review_v1"}
    results = [
        {"provider": "stub", "model": "m1", "action": "OPEN_LONG", "ai_available": True, "valid": True},
        {"provider": "stub", "model": "m2", "action": "NO_TRADE", "ai_available": True, "valid": True},
    ]
    assert store.append_model_lab_run(run, results) is True
    assert store.append_model_lab_run(run, results) is False  # idempotent
    runs = store.list_model_lab_runs(role="quant")
    assert len(runs) == 1 and len(runs[0]["results"]) == 2
    assert {result["model"] for result in runs[0]["results"]} == {"m1", "m2"}


def test_store_path_resolution_env_overrides_config(monkeypatch):
    # Pytest restores the suite-level value after this test. Removing it by hand
    # could make later tests open the project's real data/decision_ledger.db.
    monkeypatch.setenv("MS_DECISION_LEDGER_PATH", "Z:/custom/ledger.db")
    assert str(decision_ledger_store_path({"decision_ledger": {"store_path": "data/x.db"}})) == "Z:\\custom\\ledger.db" \
        or str(decision_ledger_store_path({"decision_ledger": {"store_path": "data/x.db"}})).endswith("custom/ledger.db")
    monkeypatch.delenv("MS_DECISION_LEDGER_PATH")
    assert str(decision_ledger_store_path({"decision_ledger": {"store_path": "data/x.db"}})).endswith("x.db")
    assert str(decision_ledger_store_path(None)).endswith("decision_ledger.db")
