
import unittest
import shutil
import json
from pathlib import Path
from datetime import datetime

# Adjust path to import core
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
from datetime import datetime
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.signals import SignalManager

class TestPositionSizing(unittest.TestCase):
    
    @patch('core.signals.SignalManager.save') # Prevent saving to disk
    @patch('core.signals.get_capital_manager')
    @patch('core.signals.ConfigManager')
    def test_position_sizing_calculation(self, MockConfigManager, MockGetCapitalManager, MockSave):
        # 1. Setup Config Mock
        mock_config = MockConfigManager.return_value
        mock_config.agents = {"risk": {"position_size_pct": 5.0}}
        
        # 2. Setup Capital Mock
        mock_cm = MockGetCapitalManager.return_value
        mock_cm.get_balance.return_value = 10000.0
        
        # 3. Setup SignalManager
        # We can pass any path, it won't be used because we mocked save/load or we just don't load
        # SignalManager calls load() in init. We should mock that too or let it fail gently.
        # But load() reads file. simpler to mock init or mock load.
        with patch.object(SignalManager, 'load', return_value=None):
            sm = SignalManager(path=Path("dummy_signals.json"))
            
        # 4. Add Signal
        signal_data = {
            "id": "sig_test_1",
            "ticker": "AAPL",
            "action": "BUY",
            "status": "Pending",
            "entry": 150.0, "tp": 160.0, "sl": 140.0,
            "created_at": datetime.now().isoformat()
        }
        
        sm.add_signal(signal_data)
        
        # 5. Verify Logic
        saved_signal = sm.open_signals["sig_test_1"]
        self.assertEqual(saved_signal["invested_amount"], 500.0) # 5% of 10000
        self.assertEqual(saved_signal["risk_pct_used"], 5.0)

    @patch('core.signals.SignalManager.save')
    @patch('core.signals.get_capital_manager')
    @patch('core.signals.ConfigManager')
    def test_update_outcomes_pnl(self, MockConfigManager, MockGetCapitalManager, MockSave):
        # 1. Setup Capital Mock
        mock_cm = MockGetCapitalManager.return_value
        mock_cm.get_balance.return_value = 10000.0
        # Production opens a position via capital_manager before closing a signal.
        # When the signal has no paper_position_id but a matching OPEN position
        # exists, _sync_closed_signal_capital must settle via close_position
        # (releases margin + credits cash_pnl once + marks CLOSED) — NOT via
        # add_trade_pnl (which only bumps balance and leaks the OPEN reservation).
        mock_cm.get_positions.return_value = [
            {"signal_id": "sig_win", "status": "OPEN", "position_id": "pos_1", "usd_size": 500.0}
        ]
        mock_cm.history = []
        mock_cm.close_position.return_value = {
            "status": "CLOSED", "position_id": "pos_1", "net_pnl": 50.0,
            "pnl_pct_notional": 10.0, "usd_size": 500.0, "exit_price": 110.0,
            "entry_price": 100.0, "close_reason": "take_profit",
            "closed_at": "2026-07-03T10:00:00+03:00",
        }

        with patch.object(SignalManager, 'load', return_value=None):
            sm = SignalManager(path=Path("dummy.json"))

        # 2. Pre-populate open signal (no paper_position_id -> hits fallback path)
        sm.open_signals["sig_win"] = {
            "id": "sig_win",
            "ticker": "AAPL",
            "action": "BUY",
            "status": "Pending",
            "entry": 100.0, "tp": 110.0, "sl": 90.0,
            "invested_amount": 500.0, # Pre-calculated
            "risk_pct_used": 5.0
        }

        # 3. Trigger Win
        outcome = sm.update_outcomes("AAPL", 110.0)
        self.assertEqual(outcome, "WIN")

        # 4. Settled via close_position (authoritative), NOT add_trade_pnl.
        mock_cm.close_position.assert_called_once()
        _call = mock_cm.close_position.call_args
        self.assertEqual(_call.kwargs.get("position_id"), "pos_1")
        self.assertEqual(_call.kwargs.get("exit_price"), 110.0)
        self.assertFalse(_call.kwargs.get("sync_signal", True))  # avoid capital->signals lock inversion
        mock_cm.add_trade_pnl.assert_not_called()

        # 5. Signal finalized from the CLOSED position (pnl_amount = net_pnl).
        closed_sig = sm.closed_signals[0]
        self.assertEqual(closed_sig["pnl_amount"], 50.0)
        self.assertEqual(closed_sig["invested"], 500.0)
        self.assertEqual(closed_sig["paper_position_id"], "pos_1")

    @patch('core.signals.SignalManager.save')
    @patch('core.signals.get_capital_manager')
    @patch('core.signals.ConfigManager')
    def test_update_outcomes_open_status_is_active(self, MockConfigManager, MockGetCapitalManager, MockSave):
        mock_cm = MockGetCapitalManager.return_value
        mock_cm.get_balance.return_value = 10000.0
        # OPEN match (signal status "Open") must settle via close_position too.
        mock_cm.get_positions.return_value = [
            {"signal_id": "sig_open", "status": "OPEN", "position_id": "pos_2", "usd_size": 500.0}
        ]
        mock_cm.history = []
        mock_cm.close_position.return_value = {
            "status": "CLOSED", "position_id": "pos_2", "net_pnl": 50.0,
            "pnl_pct_notional": 10.0, "usd_size": 500.0, "exit_price": 110.0,
            "entry_price": 100.0, "close_reason": "take_profit",
            "closed_at": "2026-07-03T10:00:00+03:00",
        }

        with patch.object(SignalManager, 'load', return_value=None):
            sm = SignalManager(path=Path("dummy.json"))

        sm.open_signals["sig_open"] = {
            "id": "sig_open",
            "ticker": "AAPL",
            "action": "BUY",
            "status": "Open",
            "entry": 100.0, "tp": 110.0, "sl": 90.0,
            "invested_amount": 500.0,
            "risk_pct_used": 5.0
        }

        outcome = sm.update_outcomes("AAPL", 110.0)
        self.assertEqual(outcome, "WIN")
        mock_cm.close_position.assert_called_once()
        self.assertEqual(mock_cm.close_position.call_args.kwargs.get("position_id"), "pos_2")
        mock_cm.add_trade_pnl.assert_not_called()

    @patch('core.signals.SignalManager.save')
    @patch('core.signals.get_capital_manager')
    @patch('core.signals.ConfigManager')
    def test_sync_closed_no_double_credit_on_closed_match(self, MockConfigManager, MockGetCapitalManager, MockSave):
        """A CLOSED position match is already settled (close_position booked
        cash_pnl + released margin at close time). _sync_closed_signal_capital
        must NOT call add_trade_pnl (would double-credit) NOR close_position
        (already closed -> ValueError). It returns the CLOSED dict so the
        caller finalizes the signal from the authoritative net_pnl."""
        mock_cm = MockGetCapitalManager.return_value
        mock_cm.get_balance.return_value = 10000.0
        mock_cm.get_positions.return_value = [{
            "signal_id": "sig_c", "status": "CLOSED", "position_id": "pos_c",
            "net_pnl": 50.0, "pnl_pct_notional": 10.0, "usd_size": 500.0,
            "entry_price": 100.0, "exit_price": 110.0, "close_reason": "take_profit",
            "closed_at": "2026-07-03T10:00:00+03:00",
        }]
        mock_cm.close_position = MagicMock()
        mock_cm.add_trade_pnl = MagicMock()
        mock_cm.history = []

        with patch.object(SignalManager, 'load', return_value=None):
            sm = SignalManager(path=Path("dummy.json"))

        sm.open_signals["sig_c"] = {
            "id": "sig_c", "ticker": "AAPL", "action": "BUY", "status": "Pending",
            "entry": 100.0, "tp": 110.0, "sl": 90.0,
            "invested_amount": 500.0, "risk_pct_used": 5.0,
        }

        outcome = sm.update_outcomes("AAPL", 110.0)
        self.assertEqual(outcome, "WIN")
        # No re-credit, no re-close.
        mock_cm.add_trade_pnl.assert_not_called()
        mock_cm.close_position.assert_not_called()
        # Signal finalized from the CLOSED position's net_pnl.
        closed_sig = sm.closed_signals[0]
        self.assertEqual(closed_sig["pnl_amount"], 50.0)
        self.assertEqual(closed_sig["paper_position_id"], "pos_c")

    @patch('core.signals.SignalManager.save')
    @patch('core.signals.get_capital_manager')
    @patch('core.signals.ConfigManager')
    def test_sync_closed_history_only_blocks_credit(self, MockConfigManager, MockGetCapitalManager, MockSave):
        """History-only (OPEN_POSITION in history, no active position): capital
        was reserved and already settled at close time (position closed+trimmed
        or reset). Crediting now would double-book or book against a reset
        balance. Conservative: mark settled, credit nothing."""
        mock_cm = MockGetCapitalManager.return_value
        mock_cm.get_balance.return_value = 10000.0
        mock_cm.get_positions.return_value = []
        mock_cm.close_position = MagicMock()
        mock_cm.add_trade_pnl = MagicMock()
        mock_cm.history = [{"type": "OPEN_POSITION", "signal_id": "sig_h", "ticker": "AAPL"}]

        with patch.object(SignalManager, 'load', return_value=None):
            sm = SignalManager(path=Path("dummy.json"))

        sm.open_signals["sig_h"] = {
            "id": "sig_h", "ticker": "AAPL", "action": "BUY", "status": "Pending",
            "entry": 100.0, "tp": 110.0, "sl": 90.0,
            "invested_amount": 500.0, "risk_pct_used": 5.0,
        }

        sm.update_outcomes("AAPL", 110.0)
        mock_cm.add_trade_pnl.assert_not_called()
        mock_cm.close_position.assert_not_called()
        sig = sm.open_signals.get("sig_h") or sm.closed_signals[0]
        self.assertEqual(sig["pnl_amount"], 0.0)
        self.assertTrue(sig["capital_settled"])

    @patch('core.signals.SignalManager.save')
    @patch('core.signals.get_capital_manager')
    @patch('core.signals.ConfigManager')
    def test_sync_closed_phantom_guard_blocks_unreserved(self, MockConfigManager, MockGetCapitalManager, MockSave):
        """No reservation anywhere (no position, no history): the invested_amount
        on the signal was never backed by real capital -> phantom guard blocks
        crediting invented P&L."""
        mock_cm = MockGetCapitalManager.return_value
        mock_cm.get_balance.return_value = 10000.0
        mock_cm.get_positions.return_value = []
        mock_cm.close_position = MagicMock()
        mock_cm.add_trade_pnl = MagicMock()
        mock_cm.history = []

        with patch.object(SignalManager, 'load', return_value=None):
            sm = SignalManager(path=Path("dummy.json"))

        sm.open_signals["sig_p"] = {
            "id": "sig_p", "ticker": "AAPL", "action": "BUY", "status": "Pending",
            "entry": 100.0, "tp": 110.0, "sl": 90.0,
            "invested_amount": 500.0, "risk_pct_used": 5.0,
        }

        sm.update_outcomes("AAPL", 110.0)
        mock_cm.add_trade_pnl.assert_not_called()
        mock_cm.close_position.assert_not_called()
        sig = sm.open_signals.get("sig_p") or sm.closed_signals[0]
        self.assertEqual(sig["pnl_amount"], 0.0)
        self.assertFalse(sig["capital_settled"])

    def test_has_open_signal_accepts_open_status(self):
        with patch.object(SignalManager, 'load', return_value=None):
            sm = SignalManager(path=Path("dummy.json"))

        sm.open_signals["sig_open"] = {
            "id": "sig_open",
            "ticker": "AAPL",
            "action": "BUY",
            "status": "Open",
            "entry": 100.0,
            "tp": 110.0,
            "sl": 90.0,
        }

        found = sm.has_open_signal_for_ticker("AAPL")
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], "sig_open")

    @patch('core.signals.SignalManager.save')
    @patch('core.signals.get_capital_manager')
    @patch('core.signals.ConfigManager')
    def test_insufficient_funds(self, MockConfigManager, MockGetCapitalManager, MockSave):
        mock_config = MockConfigManager.return_value
        mock_config.agents = {"risk": {"position_size_pct": 5.0}}
        
        mock_cm = MockGetCapitalManager.return_value
        mock_cm.get_balance.return_value = 5.0 # Low balance
        
        with patch.object(SignalManager, 'load', return_value=None):
            sm = SignalManager(path=Path("dummy.json"))
            
        signal_data = {
            "id": "sig_fail", "ticker": "TSLA", "action": "BUY",
            "entry": 200, "tp": 210, "sl": 190
        }
        
        with self.assertRaises(ValueError):
             sm.add_signal(signal_data)

if __name__ == "__main__":
    unittest.main()
