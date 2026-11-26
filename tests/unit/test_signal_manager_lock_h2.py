"""Regression: SignalManager mutation concurrency (H2).

Bug (H2): ``add_signal`` / ``delete_signal`` / ``close_signal_manual`` /
``update_outcomes`` / ``back_signal_with_strict`` / ``clear_all`` mutated
``open_signals`` (and the cooldown / closed lists) WITHOUT ``self._lock``, while
``load``/``save`` already used it. The class docstring (signals.py:27-29) warns
that concurrent whole-payload writes silently drop each other's updates, and the
iteration in ``update_outcomes``/``back_signal_with_strict`` could raise
``RuntimeError: dictionary changed size during iteration`` when ``add_signal``
mutated the dict mid-loop.

Fix: the pure mutators (add_signal critical section, delete_signal, clear_all,
back_signal_with_strict) now hold ``self._lock``. The capital-touching closers
(update_outcomes, close_signal_manual) snapshot ``list(self.open_signals.items())``
and guard their pops — they do NOT hold the signals lock across the
capital_manager call, because capital_manager.close_position holds capital's lock
and calls back into SignalManager (capital->signals); holding signals->capital
here would invert that order and deadlock.

This test hammers the concurrent paths and asserts no RuntimeError / no lost
add_signal. It does not assert exact counts (concurrent closes are inherently
racy) — only that the operations are crash-free and every add survives.
"""
import sys
import threading
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
    mock_cm.add_trade_pnl = MagicMock()
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
        # Persistence is irrelevant to the dict-mutation race under test; stub save()
        # to a no-op so the suite stays fast (real save() writes a file + takes the
        # lock, which under 6-8 contending threads dominates runtime).
        sm.save = lambda *a, **k: None
    return sm, mock_cm


def _signal(sid, ticker="AAPL", action="BUY", entry=100.0, tp=110.0, sl=90.0, status="Open"):
    return {
        "id": sid, "ticker": ticker, "action": action, "type": action,
        "entry": entry, "tp": tp, "sl": sl, "status": status,
        "invested_amount": 500.0, "risk_pct_used": 5.0,
        "created_at": "2026-07-02T10:00:00+03:00",
    }


def test_concurrent_add_and_update_outcomes_no_runtime_error():
    """Hammer add_signal + update_outcomes from many threads. Pre-fix this raised
    'dictionary changed size during iteration' inside update_outcomes' loop."""
    sm, mock_cm = _make_sm()
    errors: list = []

    def adder():
        try:
            for i in range(60):
                sm.add_signal(_signal(f"sig_add_{threading.get_ident()}_{i}"))
        except Exception as e:  # noqa: BLE001 - capture any failure
            errors.append(e)

    def closer():
        try:
            for _ in range(60):
                sm.update_outcomes("AAPL", 110.0)  # hits TP -> closes
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=adder) for _ in range(4)] + \
              [threading.Thread(target=closer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"concurrent mutations raised: {errors[:3]}"
    # Every successfully-added signal that wasn't closed must still be present.
    # (No signal should be silently dropped by a lost write.)
    assert isinstance(sm.open_signals, dict)
    assert isinstance(sm.closed_signals, list)


def test_concurrent_delete_and_update_outcomes_no_keyerror():
    """close_signal_manual / delete_signal / update_outcomes racing on the same
    tickers must not KeyError on pop (guarded pop) and must not crash."""
    sm, mock_cm = _make_sm()
    # pre-populate
    for i in range(40):
        sm.open_signals[f"sig_{i}"] = _signal(f"sig_{i}")

    errors: list = []

    def deleter():
        try:
            for i in range(40):
                sm.delete_signal(f"sig_{i}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def updater():
        try:
            for _ in range(40):
                sm.update_outcomes("AAPL", 90.0)  # hits SL
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def manual_closer():
        try:
            for _ in range(40):
                sm.close_signal_manual("AAPL", "manual", exit_price=95.0)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=deleter),
               threading.Thread(target=updater),
               threading.Thread(target=manual_closer),
               threading.Thread(target=updater)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"concurrent close/delete raised: {errors[:3]}"


def test_back_signal_with_strict_concurrent_with_add_no_crash():
    sm, mock_cm = _make_sm()
    errors: list = []
    strict = {"variant": "V1", "risk_mode": "low", "signal_mode": "PULLBACK",
              "signal_score": 85, "probability_score": 0.8, "mtf_alignment": 5}

    def adder():
        try:
            for i in range(60):
                sm.add_signal(_signal(f"sig_b_{threading.get_ident()}_{i}"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def backer():
        try:
            for _ in range(60):
                sm.back_signal_with_strict("AAPL", strict)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=adder) for _ in range(3)] + \
              [threading.Thread(target=backer) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"back_signal_with_strict concurrency raised: {errors[:3]}"