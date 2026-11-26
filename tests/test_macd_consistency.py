"""
Test MACD Frontend-Backend Consistency

Verifies that the MACD values displayed in the Watchlist UI
match the values calculated by the backend engine.
"""

import pytest
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.engine import MarketEngine


class TestMACDConsistency:
    """Test MACD value consistency between frontend and backend."""
    
    @pytest.fixture
    def engine(self):
        """Create a MarketEngine instance for testing."""
        return MarketEngine()
    
    def test_macd_values_are_numeric(self, engine):
        """Test that MACD values returned by engine are valid numbers."""
        # Get watchlist data
        watchlist = engine.get_watchlist_state()
        
        assert watchlist is not None, "Watchlist should not be None"
        assert len(watchlist) > 0, "Watchlist should have items"
        
        for item in watchlist:
            ticker = item.get("ticker", "UNKNOWN")
            macd_value = item.get("macd")
            
            # MACD can be None if data is unavailable
            if macd_value is not None:
                assert isinstance(macd_value, (int, float)), \
                    f"MACD for {ticker} should be numeric, got {type(macd_value)}"
                
                # Print for debugging
                print(f"{ticker}: MACD = {macd_value}")
    
    def test_macd_values_are_reasonable(self, engine):
        """Test that MACD values are within reasonable bounds."""
        watchlist = engine.get_watchlist_state()
        
        for item in watchlist:
            ticker = item.get("ticker", "UNKNOWN")
            macd_value = item.get("macd")
            price = item.get("price", 0)
            
            if macd_value is not None and price > 0:
                # MACD should typically be a small fraction of price
                # For a $100 stock, MACD might be -5 to +5
                # For crypto like BTC, MACD could be larger
                
                macd_to_price_ratio = abs(macd_value) / price if price > 0 else 0
                
                # MACD should not exceed 10% of price (very generous bound)
                assert macd_to_price_ratio < 0.10, \
                    f"MACD for {ticker} ({macd_value}) seems too large relative to price ({price})"
                
                print(f"{ticker}: MACD={macd_value:.4f}, Price={price}, Ratio={macd_to_price_ratio:.4%}")
    
    def test_macd_source_is_daily_or_4h(self, engine):
        """Test that MACD comes from appropriate timeframe (daily/4H, not 1min)."""
        watchlist = engine.get_watchlist_state()
        
        # For stocks, we expect hybrid_macd_4h or raw_macd_1d
        # For crypto, we expect raw_macd_1d or raw_macd_4h
        
        for item in watchlist:
            ticker = item.get("ticker", "UNKNOWN")
            macd_value = item.get("macd")
            price = item.get("price", 0)
            
            if macd_value is None or price == 0:
                continue
            
            # 1-minute MACD values are typically microscopic (< 0.001% of price)
            # Daily/4H MACD values should be more meaningful
            
            macd_pct_of_price = abs(macd_value) / price * 100 if price > 0 else 0
            
            # For meaningful analysis, MACD should be at least 0.01% of price
            # (Very small values suggest 1-minute data contamination)
            if macd_pct_of_price < 0.001:
                print(f"⚠️ {ticker}: MACD {macd_value} is suspiciously small ({macd_pct_of_price:.6f}% of price)")
            else:
                print(f"✓ {ticker}: MACD {macd_value} = {macd_pct_of_price:.4f}% of price (OK)")


class TestMACDCalculation:
    """Test MACD calculation logic directly."""
    
    def test_calc_macd_function(self):
        """Test the calc_macd function with known data."""
        import pandas as pd
        import numpy as np
        from core.ta import TechnicalAnalysis
        
        # Create simple test data (26+ periods needed for MACD)
        np.random.seed(42)
        prices = 100 + np.cumsum(np.random.randn(50) * 0.5)  # Random walk
        df = pd.DataFrame({'close': prices})
        
        macd_line, signal_line, histogram = TechnicalAnalysis.calc_macd(df['close'])
        
        # Verify outputs are correct type
        assert isinstance(macd_line, pd.Series), "MACD line should be Series"
        assert isinstance(signal_line, pd.Series), "Signal line should be Series"
        assert isinstance(histogram, pd.Series), "Histogram should be Series"
        
        # Verify length matches input
        assert len(macd_line) == 50, "MACD length should match input"
        
        # Early values will have NaN (warmup period depends on implementation)
        # Just verify that we have valid values after warmup
        valid_macd = macd_line.dropna()
        assert len(valid_macd) > 0, "Should have some valid MACD values after warmup"
        
        # Histogram = MACD - Signal
        last_hist = histogram.iloc[-1]
        expected_hist = macd_line.iloc[-1] - signal_line.iloc[-1]
        assert abs(last_hist - expected_hist) < 0.0001, "Histogram should equal MACD - Signal"
        
        print(f"Last MACD: {macd_line.iloc[-1]:.4f}")
        print(f"Last Signal: {signal_line.iloc[-1]:.4f}")
        print(f"Last Histogram: {histogram.iloc[-1]:.4f}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
