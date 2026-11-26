"""
Test module for BacktestOptimizer.
Tests adaptive parameters for backtest optimization aiming for 60%+ win rate.
"""

import pytest
import pandas as pd
import numpy as np
from datetime import datetime, time
import sys
from pathlib import Path

# Ensure root directory is in python path
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestBacktestOptimizer:
    """Test suite for BacktestOptimizer class."""

    @pytest.fixture
    def optimizer(self):
        """Create BacktestOptimizer instance for testing."""
        from core.backtest_optimizer import BacktestOptimizer
        return BacktestOptimizer()

    def test_optimizer_init(self, optimizer):
        """Test optimizer initialization with default values."""
        assert optimizer.target_win_rate == 0.60
        assert optimizer.min_trades == 30

    def test_calculate_adaptive_atr_multipliers_low_volatility(self, optimizer):
        """Test ATR multipliers for low volatility (volatility < 0.015)."""
        result = optimizer.calculate_adaptive_atr_multipliers(0.01)
        assert 'tp' in result
        assert 'sl' in result
        assert result['tp'] == 3.0
        assert result['sl'] == 0.8

    def test_calculate_adaptive_atr_multipliers_normal_volatility(self, optimizer):
        """Test ATR multipliers for normal volatility (0.015 <= vol < 0.035)."""
        result = optimizer.calculate_adaptive_atr_multipliers(0.025)
        assert 'tp' in result
        assert 'sl' in result
        assert result['tp'] == 4.0
        assert result['sl'] == 1.0

    def test_calculate_adaptive_atr_multipliers_high_volatility(self, optimizer):
        """Test ATR multipliers for high volatility (volatility >= 0.035)."""
        result = optimizer.calculate_adaptive_atr_multipliers(0.05)
        assert 'tp' in result
        assert 'sl' in result
        assert result['tp'] == 5.0
        assert result['sl'] == 1.5

    def test_calculate_adaptive_rsi_bounds_oversold(self, optimizer):
        """Test RSI bounds when avg_rsi indicates oversold market."""
        result = optimizer.calculate_adaptive_rsi_bounds(30)
        assert 'oversold' in result
        assert 'overbought' in result
        assert result['oversold'] == 25
        assert result['overbought'] == 75

    def test_calculate_adaptive_rsi_bounds_neutral(self, optimizer):
        """Test RSI bounds when avg_rsi indicates neutral market."""
        result = optimizer.calculate_adaptive_rsi_bounds(50)
        assert 'oversold' in result
        assert 'overbought' in result
        assert result['oversold'] == 30
        assert result['overbought'] == 70

    def test_calculate_adaptive_rsi_bounds_overbought(self, optimizer):
        """Test RSI bounds when avg_rsi indicates overbought market."""
        result = optimizer.calculate_adaptive_rsi_bounds(70)
        assert 'oversold' in result
        assert 'overbought' in result
        assert result['oversold'] == 35
        assert result['overbought'] == 65

    def test_apply_volume_confirmation_default(self, optimizer):
        """Test volume confirmation filter with default multiplier."""
        # Create sample data with volume column
        dates = pd.date_range(start="2023-01-01", periods=20, freq="5min")
        df = pd.DataFrame({
            "Open": np.random.uniform(100, 110, 20),
            "High": np.random.uniform(110, 120, 20),
            "Low": np.random.uniform(90, 100, 20),
            "Close": np.random.uniform(100, 110, 20),
            "Volume": [1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000,
                       11000, 12000, 13000, 14000, 15000, 16000, 17000, 18000, 19000, 20000]
        }, index=dates)

        result = optimizer.apply_volume_confirmation(df)
        assert isinstance(result, pd.Series)
        assert len(result) == len(df)
        assert result.dtype == bool

    def test_apply_volume_confirmation_custom_multiplier(self, optimizer):
        """Test volume confirmation filter with custom multiplier."""
        dates = pd.date_range(start="2023-01-01", periods=10, freq="5min")
        df = pd.DataFrame({
            "Open": np.random.uniform(100, 110, 10),
            "High": np.random.uniform(110, 120, 10),
            "Low": np.random.uniform(90, 100, 10),
            "Close": np.random.uniform(100, 110, 10),
            "Volume": [1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000]
        }, index=dates)

        result = optimizer.apply_volume_confirmation(df, multiplier=2.0)
        assert isinstance(result, pd.Series)
        assert len(result) == len(df)

    def test_apply_time_filter(self, optimizer):
        """Test time filter to avoid first/last 15 minutes of market."""
        # Create sample data with timestamps
        dates = pd.date_range(start="2023-01-02", periods=100, freq="5min")
        # Set to market hours (9:30-16:00)
        base_date = pd.Timestamp("2023-01-02").tz_localize(None)
        new_dates = [base_date + pd.Timedelta(minutes=30 + i*5) for i in range(100)]
        df = pd.DataFrame({
            "Open": np.random.uniform(100, 110, 100),
            "High": np.random.uniform(110, 120, 100),
            "Low": np.random.uniform(90, 100, 100),
            "Close": np.random.uniform(100, 110, 100),
            "Volume": np.random.uniform(1000, 5000, 100)
        }, index=pd.DatetimeIndex(new_dates))

        result = optimizer.apply_time_filter(df)
        assert isinstance(result, pd.Series)
        assert len(result) == len(df)
        assert result.dtype == bool

    def test_optimize_for_ticker(self, optimizer):
        """Test ticker-specific optimization."""
        # Sample results from backtest
        sample_results = [
            {'win_rate': 55, 'total_trades': 40, 'avg_r': 1.5},
            {'win_rate': 65, 'total_trades': 35, 'avg_r': 1.8},
            {'win_rate': 45, 'total_trades': 50, 'avg_r': 1.2}
        ]

        result = optimizer.optimize_for_ticker("AAPL", sample_results)
        assert 'best_params' in result
        assert 'recommended_atr' in result
        assert 'recommended_rsi' in result
        assert 'expected_win_rate' in result

    def test_has_apply_volume_confirmation_method(self, optimizer):
        """Test that optimizer has apply_volume_confirmation method."""
        assert hasattr(optimizer, 'apply_volume_confirmation')
        assert callable(getattr(optimizer, 'apply_volume_confirmation'))

    def test_has_apply_time_filter_method(self, optimizer):
        """Test that optimizer has apply_time_filter method."""
        assert hasattr(optimizer, 'apply_time_filter')
        assert callable(getattr(optimizer, 'apply_time_filter'))

    def test_has_calculate_adaptive_atr_multipliers_method(self, optimizer):
        """Test that optimizer has calculate_adaptive_atr_multipliers method."""
        assert hasattr(optimizer, 'calculate_adaptive_atr_multipliers')
        assert callable(getattr(optimizer, 'calculate_adaptive_atr_multipliers'))

    def test_has_calculate_adaptive_rsi_bounds_method(self, optimizer):
        """Test that optimizer has calculate_adaptive_rsi_bounds method."""
        assert hasattr(optimizer, 'calculate_adaptive_rsi_bounds')
        assert callable(getattr(optimizer, 'calculate_adaptive_rsi_bounds'))

    def test_has_optimize_for_ticker_method(self, optimizer):
        """Test that optimizer has optimize_for_ticker method."""
        assert hasattr(optimizer, 'optimize_for_ticker')
        assert callable(getattr(optimizer, 'optimize_for_ticker'))


# Integration test to verify the module can be imported and used
def test_module_import():
    """Test that the module can be imported successfully."""
    from core.backtest_optimizer import BacktestOptimizer
    opt = BacktestOptimizer()
    assert opt is not None


def test_basic_functionality():
    """Test basic functionality as specified in the task."""
    from core.backtest_optimizer import BacktestOptimizer

    opt = BacktestOptimizer()
    print('ATR adaptive:', opt.calculate_adaptive_atr_multipliers(0.02))
    print('RSI bounds:', opt.calculate_adaptive_rsi_bounds(50))
    print('Volume filter exists:', hasattr(opt, 'apply_volume_confirmation'))

    # Verify expected outputs
    atr_result = opt.calculate_adaptive_atr_multipliers(0.02)
    assert atr_result['tp'] == 4.0
    assert atr_result['sl'] == 1.0

    rsi_result = opt.calculate_adaptive_rsi_bounds(50)
    assert rsi_result['oversold'] == 30
    assert rsi_result['overbought'] == 70

    assert hasattr(opt, 'apply_volume_confirmation')
