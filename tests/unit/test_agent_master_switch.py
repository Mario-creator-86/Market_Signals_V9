"""Safety contract for the automation master switch."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from core.agent_manager import AgentManager


def _manager_with_bot(enabled: bool) -> AgentManager:
    manager = AgentManager.__new__(AgentManager)
    manager.bot_enabled = False
    manager.config_manager = SimpleNamespace(bot={"enabled": enabled})
    return manager


def test_bot_mode_reads_persisted_master_switch():
    assert _manager_with_bot(True)._bot_mode_active() is True
    assert _manager_with_bot(False)._bot_mode_active() is False


def test_on_new_signal_cannot_execute_when_master_switch_is_off(monkeypatch):
    manager = _manager_with_bot(False)
    execution = SimpleNamespace(is_running=True, process_signal=MagicMock())
    manager.agents = {"execution": execution}
    events = []
    monkeypatch.setattr("core.agent_manager.record_trade_event", lambda **event: events.append(event))
    hydration_calls = []

    def fake_hydrate(signal, _execution_cfg):
        hydration_calls.append(signal["ticker"])
        signal["news_context"] = {
            "status": "available",
            "article_count_24h": 2,
            "hydration": {"requested": True, "status": "hit", "persisted": True},
        }
        return signal

    monkeypatch.setattr("core.agent_manager._attach_cached_news_context_for_ai", fake_hydrate)

    manager.on_new_signal({"id": "sig-1", "ticker": "BTC-USD", "strategy_type": "scanner"})

    execution.process_signal.assert_not_called()
    assert hydration_calls == ["BTC-USD"]
    assert events[0]["event_type"] == "scanner_news_hydrated"
    assert events[-1]["status"] == "bot_stopped"
    assert events[-1]["summary"] == "Bot master switch is off"


def test_autonomous_monitor_does_not_collect_candidates_when_bot_is_off(monkeypatch):
    manager = _manager_with_bot(False)
    manager.agents = {"execution": SimpleNamespace(is_running=True)}
    scanner = MagicMock(side_effect=AssertionError("scanner must not run"))
    monkeypatch.setattr(manager, "_scanner_candidates", scanner)

    manager._autonomous_monitor_once()

    scanner.assert_not_called()
