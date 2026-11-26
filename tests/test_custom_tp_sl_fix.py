"""
Test module for CUST TP/SL Fix.
Verifies that custom TP/SL overrides work with ANY exit mode (ATR, HYBRID, FIXED_PCT).
"""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestCustomTpSlFix:
    """Test suite for CUST TP/SL fix — custom TP/SL must work with any exit mode."""

    @pytest.fixture
    def backtester(self):
        """Create QuickBacktester instance with minimal mock config."""
        from core.quick_backtester import QuickBacktester

        class MockSignal:
            def get(self, key, default=None):
                return default

        class MockConfig:
            signal = MockSignal()

        return QuickBacktester(config=MockConfig())

    def test_custom_tp_sl_with_atr_mode_buy(self, backtester):
        """Custom TP/SL should override ATR-calculated exits for BUY."""
        sl, tp = backtester._calculate_exits(
            entry=100.0, direction="BUY", atr=2.0,
            trade_mode="PULLBACK", mode="ATR",
            custom_tp_pct=5.0,  # 5% TP
            custom_sl_pct=2.0,  # 2% SL
        )
        # TP should be 100 + 5% = 105
        assert tp == pytest.approx(105.0, rel=1e-4), f"Expected TP ≈ 105.0, got {tp}"
        # SL should be 100 - 2% = 98
        assert sl == pytest.approx(98.0, rel=1e-4), f"Expected SL ≈ 98.0, got {sl}"

    def test_custom_tp_sl_with_atr_mode_sell(self, backtester):
        """Custom TP/SL should override ATR-calculated exits for SELL."""
        sl, tp = backtester._calculate_exits(
            entry=100.0, direction="SELL", atr=2.0,
            trade_mode="PULLBACK", mode="ATR",
            custom_tp_pct=5.0,
            custom_sl_pct=2.0,
        )
        # TP should be 100 - 5% = 95
        assert tp == pytest.approx(95.0, rel=1e-4), f"Expected TP ≈ 95.0, got {tp}"
        # SL should be 100 + 2% = 102
        assert sl == pytest.approx(102.0, rel=1e-4), f"Expected SL ≈ 102.0, got {sl}"

    def test_custom_tp_only_with_atr_mode(self, backtester):
        """Custom TP only — SL should use normal ATR calculation."""
        sl, tp = backtester._calculate_exits(
            entry=100.0, direction="BUY", atr=2.0,
            trade_mode="PULLBACK", mode="ATR",
            custom_tp_pct=3.0,
            custom_sl_pct=None,
        )
        # TP should be 100 + 3% = 103
        assert tp == pytest.approx(103.0, rel=1e-4)
        # SL should be ATR-based (entry - 1.5 * atr = 100 - 3.0 = 97.0)
        assert sl == pytest.approx(97.0, rel=1e-4)

    def test_custom_sl_only_with_atr_mode(self, backtester):
        """Custom SL only — TP should use normal ATR calculation."""
        sl, tp = backtester._calculate_exits(
            entry=100.0, direction="BUY", atr=2.0,
            trade_mode="PULLBACK", mode="ATR",
            custom_tp_pct=None,
            custom_sl_pct=1.5,
        )
        # SL should be 100 - 1.5% = 98.5
        assert sl == pytest.approx(98.5, rel=1e-4)
        # TP should be ATR-based (entry + 3.0 * atr = 100 + 6.0 = 106.0)
        assert tp == pytest.approx(106.0, rel=1e-4)

    def test_no_custom_uses_atr_defaults(self, backtester):
        """No custom TP/SL — should use normal ATR calculation."""
        sl, tp = backtester._calculate_exits(
            entry=100.0, direction="BUY", atr=2.0,
            trade_mode="PULLBACK", mode="ATR",
            custom_tp_pct=None,
            custom_sl_pct=None,
        )
        # PULLBACK defaults: sl_mult=1.5, tp_mult=3.0
        # SL = 100 - 1.5*2 = 97.0
        # TP = 100 + 3.0*2 = 106.0
        assert sl == pytest.approx(97.0, rel=1e-4)
        assert tp == pytest.approx(106.0, rel=1e-4)

    def test_custom_tp_sl_with_fixed_pct_mode(self, backtester):
        """Custom TP/SL with FIXED_PCT mode — should still work."""
        sl, tp = backtester._calculate_exits(
            entry=100.0, direction="BUY", atr=2.0,
            trade_mode="PULLBACK", mode="FIXED_PCT",
            custom_tp_pct=4.0,
            custom_sl_pct=1.0,
        )
        assert tp == pytest.approx(104.0, rel=1e-4)
        assert sl == pytest.approx(99.0, rel=1e-4)

    def test_custom_tp_sl_with_hybrid_mode(self, backtester):
        """Custom TP/SL with HYBRID mode — overrides should apply."""
        sl, tp = backtester._calculate_exits(
            entry=100.0, direction="BUY", atr=2.0,
            trade_mode="BREAKOUT", mode="HYBRID",
            custom_tp_pct=6.0,
            custom_sl_pct=3.0,
        )
        assert tp == pytest.approx(106.0, rel=1e-4)
        assert sl == pytest.approx(97.0, rel=1e-4)

    def test_custom_tp_sl_breakout_mode_sell(self, backtester):
        """Custom TP/SL for BREAKOUT SELL."""
        sl, tp = backtester._calculate_exits(
            entry=200.0, direction="SELL", atr=3.0,
            trade_mode="BREAKOUT", mode="ATR",
            custom_tp_pct=4.0,
            custom_sl_pct=2.0,
        )
        # TP = 200 - 4% = 192
        assert tp == pytest.approx(192.0, rel=1e-4)
        # SL = 200 + 2% = 204
        assert sl == pytest.approx(204.0, rel=1e-4)

    def test_custom_tp_sl_contra_mode(self, backtester):
        """Custom TP/SL for CONTRA BUY."""
        sl, tp = backtester._calculate_exits(
            entry=50.0, direction="BUY", atr=1.0,
            trade_mode="CONTRA", mode="ATR",
            custom_tp_pct=10.0,
            custom_sl_pct=5.0,
        )
        # TP = 50 + 5 = 55
        assert tp == pytest.approx(55.0, rel=1e-4)
        # SL = 50 - 2.5 = 47.5
        assert sl == pytest.approx(47.5, rel=1e-4)
