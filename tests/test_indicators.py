
import pytest
import pandas as pd
import numpy as np
from core.ta import TechnicalAnalysis

class TestTechnicalAnalysis:

    def test_calc_rsi_normal(self, mock_ohlcv_data):
        close = mock_ohlcv_data["Close"]
        rsi = TechnicalAnalysis.calc_rsi(close)
        assert len(rsi) == len(close)
        assert not rsi.isna().all()
        # RSI should be between 0 and 100
        assert rsi.min() >= 0
        assert rsi.max() <= 100

    def test_calc_rsi_empty(self):
        empty_series = pd.Series(dtype=float)
        rsi = TechnicalAnalysis.calc_rsi(empty_series)
        assert rsi.empty

    def test_calc_rsi_edge_cases(self, mock_ohlcv_edge_cases):
        close = mock_ohlcv_edge_cases["Close"]
        rsi = TechnicalAnalysis.calc_rsi(close)
        # Check if it didn't crash
        assert len(rsi) == len(close)
        # NaN input can result in NaN output, but shouldn't crash
        
    def test_calc_macd(self, mock_ohlcv_data):
        close = mock_ohlcv_data["Close"]
        macd, signal, hist = TechnicalAnalysis.calc_macd(close)
        assert len(macd) == len(close)
        assert len(signal) == len(close)
        assert len(hist) == len(close)
        # Check integrity
        # hist should be macd - signal roughly
        np.testing.assert_allclose(hist.fillna(0), (macd - signal).fillna(0))

    def test_get_trend_from_macd(self):
        # Bull case
        hist = pd.Series([0.1, 0.2, 0.5])
        assert TechnicalAnalysis.get_trend_from_macd(hist) == "BULL"
        
        # Bear case
        hist = pd.Series([-0.1, -0.2, -0.5])
        assert TechnicalAnalysis.get_trend_from_macd(hist) == "BEAR"
        
        # Neutral/NA
        hist = pd.Series([np.nan])
        assert TechnicalAnalysis.get_trend_from_macd(hist) == "N/A"
        
        # Empty
        assert TechnicalAnalysis.get_trend_from_macd(pd.Series(dtype=float)) == "N/A"

    def test_calc_adx(self, mock_ohlcv_data):
        adx = TechnicalAnalysis.calc_adx(
            mock_ohlcv_data["High"],
            mock_ohlcv_data["Low"],
            mock_ohlcv_data["Close"]
        )
        assert len(adx) == len(mock_ohlcv_data)
        # ADX usually starts with NaNs due to rolling
        assert adx.notna().any()
        # Should be between 0 and 100
        valid_adx = adx.dropna()
        assert valid_adx.min() >= 0
        assert valid_adx.max() <= 100

    def test_calc_sma(self, mock_ohlcv_data):
        close = mock_ohlcv_data["Close"]
        sma = TechnicalAnalysis.calc_sma(close, period=10)
        assert len(sma) == len(close)
        assert sma.iloc[0:9].isna().all() # First 9 should be NaN
        assert not sma.iloc[10:].isna().all()

