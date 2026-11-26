"""Mechanical exits must not wait behind AI exit reviews.

Regression (2026-11-12): the P&L tracker reviewed each open position with the
model inline (up to 45s per call), so a stop-loss check for position N queued
behind the reviews of positions 1..N-1. Across 205 paper stop-losses the planned
3.9% stop filled at 6.1% on average and a third filled more than 1% beyond it.
"""
import pytest

import core.capital_manager as capital_mod
import core.config as config_mod
import core.ollama_decision as ollama_decision
from core.capital_manager import CapitalManager


class _CycleDone(BaseException):
    """Raised from the tracker's sleep to stop it after one cycle."""


class _RecordingExitManager:
    def __init__(self, events):
        self._events = events

    def evaluate_position(self, position, price, **kwargs):
        self._events.append(("mechanical", position["ticker"]))
        return None


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.setenv("MS_DISABLE_CAPITAL_THREADS", "1")
    monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", tmp_path / "capital.json")
    monkeypatch.setattr(capital_mod, "get_sqlite_store", lambda: None)
    CapitalManager._instance = None
    manager = CapitalManager()
    events = []
    manager.exit_manager = _RecordingExitManager(events)
    manager.positions = [
        {"position_id": f"pos_{ticker}", "ticker": ticker, "direction": "LONG", "status": "OPEN",
         "strategy_type": "SCANNER_AI_LONG", "position_type": "paper_signal",
         "ai_exit_management_enabled": True, "last_ollama_exit_review_ts": reviewed_at}
        for ticker, reviewed_at in (("AAA-USD", 300.0), ("BBB-USD", 100.0), ("CCC-USD", 200.0))
    ]
    monkeypatch.setattr(manager, "_sync_from_storage", lambda **kwargs: False)
    monkeypatch.setattr(manager, "_save_unlocked", lambda: None)
    monkeypatch.setattr(manager, "_record_closed_position_afterward", lambda: None)
    monkeypatch.setattr(manager, "_get_fresh_price_cached", lambda ticker, side: 100.0)

    class _Config:
        config = {"ollama": {"exit_decision_enabled": True, "exit_decision_cooldown_seconds": 30}}

    monkeypatch.setattr(config_mod, "ConfigManager", _Config)

    def fake_review(position, price, config=None):
        events.append(("ai", position["ticker"]))
        return {"available": True, "action": "hold", "confidence": 0.5}

    monkeypatch.setattr(ollama_decision, "evaluate_position_exit_with_ollama", fake_review)

    def stop_after_cycle(seconds):
        raise _CycleDone()

    monkeypatch.setattr("time.sleep", stop_after_cycle)
    yield manager, events
    CapitalManager._instance = None


def test_every_position_is_checked_mechanically_before_any_ai_review(tracker):
    manager, events = tracker

    with pytest.raises(_CycleDone):
        manager.start_pnl_tracker()

    kinds = [kind for kind, _ in events]
    assert kinds[:3] == ["mechanical"] * 3
    assert {ticker for kind, ticker in events if kind == "mechanical"} == {"AAA-USD", "BBB-USD", "CCC-USD"}


def test_one_ai_review_per_cycle_for_the_position_reviewed_longest_ago(tracker):
    manager, events = tracker

    with pytest.raises(_CycleDone):
        manager.start_pnl_tracker()

    assert [ticker for kind, ticker in events if kind == "ai"] == ["BBB-USD"]
