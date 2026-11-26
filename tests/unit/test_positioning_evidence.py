"""Futures positioning of a coin, read from the recorder's database, for the AI reviewer."""
from __future__ import annotations

import sqlite3

import pytest

from core.crypto_derivatives_recorder import HOUR_MS, DerivativesRecorder
from core.positioning_evidence import positioning_evidence

NOW = 1_795_420_800_000


def _db(tmp_path, *, hours=24 * 20, symbol="ACEUSDT", last_age_hours=1):
    path = tmp_path / "derivs.db"
    DerivativesRecorder(path, fetch=lambda url: (0, ""), now_ms=lambda: NOW)   # creates the tables
    end = NOW - last_age_hours * HOUR_MS
    rows = []
    for i in range(hours):
        ts = end - (hours - 1 - i) * HOUR_MS
        oi_value = 1_000_000.0 + i * 1_000.0                      # open interest climbs steadily
        ratio = 1.0 + i / hours                                   # long/short ratio climbs from 1.0 to 2.0
        rows.append((symbol, "open_interest", ts, oi_value / 0.1, oi_value, None))
        rows.append((symbol, "ratio_global_account", ts, ratio, ratio / (1 + ratio), 1 / (1 + ratio)))
        rows.append((symbol, "ratio_top_position", ts, 0.8, 0.8 / 1.8, 1 / 1.8))
        rows.append((symbol, "taker", ts, 0.5 if i >= hours - 4 else 1.0, 50.0 if i >= hours - 4 else 100.0, 100.0))
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO series_1h VALUES (?, ?, ?, ?, ?, ?)", rows)
    return path


def test_evidence_summarises_open_interest_ratios_and_taker_flow(tmp_path):
    ev = positioning_evidence("ACE-USD", db_path=_db(tmp_path), now_ms=NOW)

    assert ev["symbol"] == "ACEUSDT" and ev["stale"] is False and ev["age_hours"] == pytest.approx(1.0)
    oi = ev["open_interest"]
    assert oi["value_usd"] == pytest.approx(1_000_000.0 + 479 * 1_000.0)
    assert oi["change_24h_pct"] == pytest.approx(24_000 / (1_000_000 + 455 * 1_000) * 100, rel=1e-3)
    assert oi["change_7d_pct"] > oi["change_24h_pct"] > 0
    accounts = ev["long_short_accounts"]
    assert accounts["long_share_pct"] == pytest.approx(accounts["ratio"] / (1 + accounts["ratio"]) * 100, rel=1e-3)
    assert accounts["percentile_30d"] >= 99                       # the highest reading of the window
    assert ev["top_traders_positions"]["ratio"] == pytest.approx(0.8)
    assert ev["taker_flow"]["buy_sell_ratio_4h"] == pytest.approx(0.5)      # sellers dominate the last four hours
    assert ev["taker_flow"]["buy_sell_ratio_24h"] > 0.5
    assert "context" in ev["note"].lower()


def test_a_thousand_prefixed_perpetual_is_found(tmp_path):
    ev = positioning_evidence("PEPE-USD", db_path=_db(tmp_path, symbol="1000PEPEUSDT"), now_ms=NOW)
    assert ev["symbol"] == "1000PEPEUSDT"


def test_old_data_is_marked_stale_and_unknown_coins_or_stocks_give_nothing(tmp_path):
    path = _db(tmp_path, last_age_hours=30)
    stale = positioning_evidence("ACE-USD", db_path=path, now_ms=NOW)
    assert stale["stale"] is True and stale["age_hours"] == pytest.approx(30.0)

    assert positioning_evidence("XYZ-USD", db_path=path, now_ms=NOW) is None
    assert positioning_evidence("AAPL", db_path=path, now_ms=NOW) is None
    assert positioning_evidence("ACE-USD", db_path=tmp_path / "missing.db", now_ms=NOW) is None


def test_agent_manager_attaches_both_kinds_of_evidence_and_survives_errors(monkeypatch):
    import core.agent_manager as am

    monkeypatch.setattr("core.ai.scorecard.track_record_for", lambda signal: {"taken": {"n": 30}})
    monkeypatch.setattr("core.positioning_evidence.positioning_evidence", lambda ticker: {"symbol": "ACEUSDT"})
    crypto = {"ticker": "ACE-USD", "strategy_type": "SCANNER_AI_LONG"}
    am._attach_learning_evidence(crypto)
    assert crypto["ai_track_record"] == {"taken": {"n": 30}} and crypto["positioning_evidence"] == {"symbol": "ACEUSDT"}

    stock = {"ticker": "AAPL", "strategy_type": "SCANNER_AI_LONG"}
    am._attach_learning_evidence(stock)
    assert "positioning_evidence" not in stock and "ai_track_record" in stock

    def boom(*_a, **_k):
        raise RuntimeError("database locked")

    monkeypatch.setattr("core.ai.scorecard.track_record_for", boom)
    monkeypatch.setattr("core.positioning_evidence.positioning_evidence", boom)
    safe = {"ticker": "ACE-USD"}
    am._attach_learning_evidence(safe)                             # evidence must never break a decision
    assert safe == {"ticker": "ACE-USD"}
