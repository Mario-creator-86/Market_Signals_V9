"""Unit tests for the AI tighten_stop gate (exit_tighten_stop_enabled).

The user chose AI exit = close/hold only (no tighten_stop). The
`_maybe_apply_ai_tighten_stop` helper encapsulates the tighten-stop decision
and action so it is testable without running the PnL tracker daemon loop.
"""
import pytest

import core.capital_manager as capital_mod
from core.capital_manager import CapitalManager


@pytest.fixture
def isolated_manager(tmp_path, monkeypatch):
    capital_path = tmp_path / "capital_tighten.json"
    monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", capital_path)
    monkeypatch.setattr(capital_mod, "get_sqlite_store", lambda: None)
    monkeypatch.setenv("MS_MARKET_HOURS_OVERRIDE", "open")
    CapitalManager._instance = None
    manager = CapitalManager()
    yield manager
    CapitalManager._instance = None


def _make_ai_managed_long(manager, *, entry=100.0, sl=98.0, trailing=0.0):
    """Open a manual position, then relabel it as an AI-managed scanner position
    so the tighten_stop path is relevant."""
    opened = manager.open_manual_position(
        ticker="AAPL",
        direction="long",
        leverage=1,
        usd_size=100,
        entry_price=entry,
        stop_loss_price=sl,
        take_profit_price=104.0,
    )
    pid = opened["position_id"]
    with manager._lock:
        for p in manager.positions:
            if p.get("position_id") == pid:
                p["position_type"] = "paper_signal"
                p["strategy_type"] = "SCANNER_TEST"
                p["ai_exit_management_enabled"] = True
                p["automatic_trailing_enabled"] = True
                p["trailing_sl"] = trailing
                break
        manager._save_unlocked()
    return pid


def test_tighten_stop_disabled_when_flag_false(isolated_manager):
    pid = _make_ai_managed_long(isolated_manager, sl=98.0, trailing=0.0)
    pos = isolated_manager.get_position(pid)
    ai_exit = {
        "available": True,
        "action": "tighten_stop",
        "confidence": 0.95,
        "new_stop": 101.0,
    }

    applied = isolated_manager._maybe_apply_ai_tighten_stop(
        pos,
        ai_exit,
        curr_price=105.0,
        ollama_cfg={"exit_tighten_stop_enabled": False, "exit_tighten_stop_min_confidence": 0.75},
    )

    assert applied is False
    assert isolated_manager.get_position(pid)["trailing_sl"] == 0.0


def test_tighten_stop_applied_when_flag_true_and_valid(isolated_manager):
    pid = _make_ai_managed_long(isolated_manager, sl=98.0, trailing=0.0)
    pos = isolated_manager.get_position(pid)
    ai_exit = {
        "available": True,
        "action": "tighten_stop",
        "confidence": 0.95,
        "new_stop": 101.0,
    }

    applied = isolated_manager._maybe_apply_ai_tighten_stop(
        pos,
        ai_exit,
        curr_price=105.0,
        ollama_cfg={"exit_tighten_stop_enabled": True, "exit_tighten_stop_min_confidence": 0.75},
    )

    assert applied is True
    assert isolated_manager.get_position(pid)["trailing_sl"] == pytest.approx(101.0)


def test_tighten_stop_rejected_when_new_stop_outside_range(isolated_manager):
    pid = _make_ai_managed_long(isolated_manager, sl=98.0, trailing=0.0)
    pos = isolated_manager.get_position(pid)
    # new_stop above current price -> invalid for a LONG tighten
    ai_exit = {
        "available": True,
        "action": "tighten_stop",
        "confidence": 0.95,
        "new_stop": 110.0,
    }

    applied = isolated_manager._maybe_apply_ai_tighten_stop(
        pos,
        ai_exit,
        curr_price=105.0,
        ollama_cfg={"exit_tighten_stop_enabled": True, "exit_tighten_stop_min_confidence": 0.75},
    )

    assert applied is False
    assert isolated_manager.get_position(pid)["trailing_sl"] == 0.0


def test_tighten_stop_rejected_when_confidence_below_threshold(isolated_manager):
    pid = _make_ai_managed_long(isolated_manager, sl=98.0, trailing=0.0)
    pos = isolated_manager.get_position(pid)
    ai_exit = {
        "available": True,
        "action": "tighten_stop",
        "confidence": 0.5,
        "new_stop": 101.0,
    }

    applied = isolated_manager._maybe_apply_ai_tighten_stop(
        pos,
        ai_exit,
        curr_price=105.0,
        ollama_cfg={"exit_tighten_stop_enabled": True, "exit_tighten_stop_min_confidence": 0.75},
    )

    assert applied is False
    assert isolated_manager.get_position(pid)["trailing_sl"] == 0.0


def test_tighten_stop_rejected_when_action_not_tighten(isolated_manager):
    pid = _make_ai_managed_long(isolated_manager, sl=98.0, trailing=0.0)
    pos = isolated_manager.get_position(pid)
    ai_exit = {"available": True, "action": "hold", "confidence": 0.95, "new_stop": 101.0}

    applied = isolated_manager._maybe_apply_ai_tighten_stop(
        pos,
        ai_exit,
        curr_price=105.0,
        ollama_cfg={"exit_tighten_stop_enabled": True, "exit_tighten_stop_min_confidence": 0.75},
    )

    assert applied is False
    assert isolated_manager.get_position(pid)["trailing_sl"] == 0.0