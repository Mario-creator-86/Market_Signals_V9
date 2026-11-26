"""Learning mode in the autonomous cycle: its candidates, their size, and the bookkeeping around review."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import core.agent_manager as am
from core.learning_mode.book import LearningBook

IDEA = {"side": "LONG", "confidence": 0.7, "stop_pct": 2.0, "target_pct": 4.0, "hold_hours": 48, "why": "Safe haven."}


class _Worker:
    def __init__(self, book):
        self.book = book
        self.last_result = {"status": "ran", "at": "now"}

    @staticmethod
    def latest_scanner_price(ticker):
        return None


def _manager(monkeypatch, tmp_path, equity=10_000.0):
    book = LearningBook(tmp_path / "learning_mode")
    book.add_idea(dict(IDEA), ticker="GLD", source="news", price=240.0, asset_class="US_STOCKS", headlines=[],
                  model="claude-opus-5-5", now=datetime.now(timezone.utc))
    worker = _Worker(book)
    monkeypatch.setattr(am, "get_learning_worker", lambda build=None: worker)
    monkeypatch.setattr(am.AgentManager, "_learning_equity", staticmethod(lambda: equity))
    manager = object.__new__(am.AgentManager)
    manager._autonomous_source_stats = {}
    return manager, book


def test_waiting_learning_trades_join_the_cycle_at_one_percent_of_equity(monkeypatch, tmp_path):
    manager, _ = _manager(monkeypatch, tmp_path, equity=10_125.8)
    found = manager._learning_candidates({"mode": "paper"})
    assert [s["ticker"] for s in found] == ["GLD"] and found[0]["strategy_type"] == "AI_NEWS_LONG"
    assert found[0]["learning_size_usd"] == pytest.approx(101.258)
    assert found[0]["invested_amount"] == pytest.approx(101.258)
    stats = manager._autonomous_source_stats["learning"]
    assert stats["enabled"] is True and stats["candidate_count"] == 1 and stats["worker"]["status"] == "ran"


def test_no_learning_trades_in_live_mode_or_when_switched_off(monkeypatch, tmp_path):
    manager, _ = _manager(monkeypatch, tmp_path)
    assert manager._learning_candidates({"mode": "live"}) == []
    assert manager._learning_candidates({"mode": "paper", "learning_mode": {"enabled": False}}) == []
    assert manager._autonomous_source_stats["learning"]["enabled"] is False


def test_a_reviewed_learning_trade_leaves_the_queue(monkeypatch, tmp_path):
    manager, book = _manager(monkeypatch, tmp_path)
    signal = manager._learning_candidates({"mode": "paper"})[0]
    am.AgentManager._note_autonomous_review(signal)
    assert book.pending_count() == 0


def test_losing_learning_trades_do_not_conclude_the_experiment_before_10000(monkeypatch, tmp_path):
    import core.capital_manager as cm

    losers = [{"strategy_type": "AI_NEWS_LONG", "direction": "LONG", "status": "CLOSED", "net_pnl": -1.0,
               "pnl_pct_notional": -1.0, "entry_strategy": "learning_news"} for _ in range(150)]

    class _Capital:
        def get_positions(self):
            return losers

    monkeypatch.setattr(cm, "get_capital_manager", lambda: _Capital())
    manager, _ = _manager(monkeypatch, tmp_path)
    manager.config_manager = type("C", (), {"config": {"agents": {"execution": {"mode": "paper"}}}})()
    signal = manager._learning_candidates({"mode": "paper"})[0]
    kept, concluded = manager._drop_concluded_experiments([signal])
    assert kept == [signal] and concluded == {}


def test_a_waiting_learning_trade_is_reviewed_before_the_rule_candidates():
    """Seen live on 2026-11-24: an idea waits for the cycle's single review slot and expires after 10 minutes,
    while rule candidates come back every cycle. A learning idea goes first."""
    import inspect

    learning = {"autonomous_source": "learning", "strategy_type": "AI_MOVER_SHORT", "ticker": "LISTA-USD"}
    scanner = {"autonomous_source": "scanner", "strategy_type": "SCANNER_AI_DIP_LONG", "ticker": "ZRO-USD"}
    assert am._learning_priority(learning) == 1 and am._learning_priority(scanner) == 0
    source = inspect.getsource(am.AgentManager._autonomous_monitor_once)
    assert "self._learning_turn(signal)" in source.split("def _candidate_priority", 1)[1].split("candidates.sort", 1)[0]
