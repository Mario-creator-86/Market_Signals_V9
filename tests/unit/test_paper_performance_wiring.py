"""Paper performance evidence: ledger read path and API endpoint."""
import json
import sqlite3

import pytest

from core.decision_ledger.store import DecisionLedgerStore


def _insert(path, decision_id, strategy, pnl, evaluated_at, basis="position_close", source="SIGNAL_PATH"):
    con = sqlite3.connect(str(path))
    cols = [r[1] for r in con.execute("pragma table_info(decision_ledger_entries)")]
    values = {c: "" for c in cols}
    values.update({"decision_id": decision_id, "created_at": evaluated_at, "ticker": "T", "action": "OPEN_LONG",
                   "horizon": "SHORT_TERM", "source": source, "status": "CLOSED", "strategy": strategy,
                   "entry_json": "{}", "outcome_json": "null"})
    con.execute(
        f"INSERT OR IGNORE INTO decision_ledger_entries ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
        [values[c] for c in cols],
    )
    payload = {"basis": basis, "decision_id": decision_id, "forward_return_pct": 1.0,
               "detail": {"strategy_type": strategy, "pnl_usd": pnl, "closed_at": evaluated_at,
                          "close_reason": "take_profit", "ticker": "T", "direction": "LONG"}}
    con.execute(
        "INSERT INTO decision_ledger_outcomes (decision_id, evaluated_at, outcome, basis, outcome_payload) VALUES (?,?,?,?,?)",
        (decision_id, evaluated_at, "FAVORABLE", basis, json.dumps(payload)),
    )
    con.commit()
    con.close()


@pytest.mark.unit
def test_store_lists_latest_close_per_decision(tmp_path):
    path = tmp_path / "ledger.db"
    store = DecisionLedgerStore(path)
    _insert(path, "d1", "SCANNER_AI_LONG", 5.0, "2026-09-01T10:00:00")
    _insert(path, "d1", "SCANNER_AI_LONG", 7.5, "2026-09-02T10:00:00")
    _insert(path, "d2", "PRE_EARNINGS_AI_LONG", -3.0, "2026-09-03T10:00:00", source="PRE_EARNINGS_PATH")
    _insert(path, "d3", "SCANNER_AI_SHORT", 9.0, "2026-09-04T10:00:00", basis="forward_bars")
    trades = {t["decision_id"]: t for t in store.list_position_close_trades()}
    assert set(trades) == {"d1", "d2"}
    assert trades["d1"]["pnl_usd"] == 7.5
    assert trades["d2"]["source"] == "PRE_EARNINGS_PATH"


@pytest.mark.unit
def test_paper_performance_endpoint(monkeypatch):
    import api.main as main
    import core.decision_ledger.store as store_mod

    class FakeStore:
        def list_position_close_trades(self):
            return [{"strategy_type": "S", "pnl_usd": p, "closed_at": f"2026-09-0{1 + i % 9}"}
                    for i, p in enumerate([2.0, -1.0, 3.0])]

    monkeypatch.setattr(store_mod, "get_decision_ledger_store", lambda config=None: FakeStore())
    main._endpoint_payload_clear("evidence_paper_performance")
    body = json.loads(main.api_evidence_paper_performance().body)
    main._endpoint_payload_clear("evidence_paper_performance")
    assert body["overall"]["trades"] == 3
    assert body["overall"]["net_pnl_usd"] == 4.0
    assert body["strategies"][0]["verdict"] == "INSUFFICIENT_EVIDENCE"
