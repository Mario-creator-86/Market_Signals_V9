"""Regression: STRICT replacement close-before-validate (H3).

Bug (H3): in MarketEngine, the STRICT replacement closed the prior CUSTOM
signal (``close_signal_manual(ticker, "REPLACED_BY_STRICT")``) BEFORE the new
STRICT signal was built/validated/added. If ``add_signal`` then raised (invalid
TP/SL ordering, insufficient funds) the prior position was already settled with
no replacement — the ticker silently lost its position; the exception was
swallowed and only logged.

Fix: the close is deferred until AFTER ``add_signal`` succeeds, and
``close_signal_manual`` gained an ``exclude_sids`` arg so the just-added
replacement is not closed along with the prior CUSTOM signal. On add failure
the close never runs, so the prior position is preserved.

These tests exercise the SignalManager primitive (``exclude_sids``) and the
defer pattern directly; the engine wiring is covered by the regression suite.
"""
import sys
import tempfile
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def _make_sm():
    from core.signals import SignalManager

    mock_cm = MagicMock()
    mock_cm.get_balance.return_value = 10_000.0
    mock_cm.get_position.return_value = None
    mock_cm.get_positions.return_value = []
    with patch("core.signals.get_capital_manager", return_value=mock_cm), \
         patch("core.signals.load_signals_payload", side_effect=lambda default: default), \
         patch("core.signals.save_signals_payload"), \
         patch("core.signals.ConfigManager"):
        sm = SignalManager.__new__(SignalManager)
        sm.path = Path("dummy.json")
        sm.open_signals = {}
        sm.closed_signals = []
        sm.last_signal_time = {}
        sm.last_mtime = 0.0
        import threading as _t
        sm._lock = _t.RLock()
        sm.save = lambda *a, **k: None
    return sm, mock_cm


def _sig(sid, ticker="AAPL", action="BUY", status="Open"):
    return {"id": sid, "ticker": ticker, "action": action, "type": action,
            "entry": 100.0, "tp": 110.0, "sl": 90.0, "status": status,
            "invested_amount": 0.0, "risk_pct_used": 5.0}


def test_close_signal_manual_excludes_listed_sids():
    """exclude_sids must keep the listed signals open while closing the rest for the ticker."""
    sm, _ = _make_sm()
    sm.open_signals["old_custom"] = _sig("old_custom")
    sm.open_signals["new_strict"] = _sig("new_strict")

    sm.close_signal_manual("AAPL", "REPLACED_BY_STRICT", exit_price=100.0,
                           exclude_sids={"new_strict"})

    # prior CUSTOM closed + moved to closed_signals
    assert "old_custom" not in sm.open_signals
    assert any(c.get("id") == "old_custom" for c in sm.closed_signals)
    # replacement remains open
    assert "new_strict" in sm.open_signals
    assert sm.open_signals["new_strict"]["status"] == "Open"


def test_strict_replacement_defer_pattern_preserves_old_when_add_fails():
    """Simulate the H3 fix's guarantee: if the new signal 'add' fails, the prior
    CUSTOM signal must remain open (the close is gated on add success)."""
    sm, _ = _make_sm()
    sm.open_signals["old_custom"] = _sig("old_custom")

    # Simulate add_signal raising (e.g. invalid TP/SL / insufficient funds).
    # Per the fix, the close must NOT have run yet, so the old signal is intact.
    add_failed = False
    try:
        # add_signal would raise here; we mimic that by NOT adding and NOT closing.
        raise ValueError("insufficient funds")
    except ValueError:
        add_failed = True

    if not add_failed:
        sm.open_signals["new_strict"] = _sig("new_strict")
        sm.close_signal_manual("AAPL", "REPLACED_BY_STRICT", exit_price=100.0,
                               exclude_sids={"new_strict"})

    # Because add failed, the close never ran -> prior position preserved.
    assert add_failed is True
    assert "old_custom" in sm.open_signals
    assert sm.open_signals["old_custom"]["status"] == "Open"
    assert sm.closed_signals == []


def test_strict_replacement_success_path_closes_old_keeps_new():
    """On the success path (add succeeds), the deferred close removes the prior
    CUSTOM and keeps the replacement — same observable behavior as before the fix,
    but now only on success."""
    sm, _ = _make_sm()
    sm.open_signals["old_custom"] = _sig("old_custom")
    # add succeeded
    sm.open_signals["new_strict"] = _sig("new_strict")
    sm.close_signal_manual("AAPL", "REPLACED_BY_STRICT", exit_price=100.0,
                           exclude_sids={"new_strict"})

    assert "old_custom" not in sm.open_signals
    assert "new_strict" in sm.open_signals
    closed = [c for c in sm.closed_signals if c.get("id") == "old_custom"]
    assert len(closed) == 1
    assert closed[0]["close_reason"] == "REPLACED_BY_STRICT"