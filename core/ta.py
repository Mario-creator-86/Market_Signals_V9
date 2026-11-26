# core/ta.py - Technical Analysis helpers

from typing import Tuple

import numpy as np
import pandas as pd


class TechnicalAnalysis:
    @staticmethod
    def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
        """
        Calculate Relative Strength Index (RSI) indicator.

        Args:
            close: Series of closing prices
            period: RSI calculation period (default 14)

        Returns:
            Series containing RSI values (0-100). Returns empty Series if input is invalid.
        """
        # Validate input
        if close is None or close.empty or len(close) < 2:
            return pd.Series(dtype=float)

        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(window=period, min_periods=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period, min_periods=period).mean()

        # Handle division by zero edge case
        # When loss is 0, RSI should be 100 (all gains, no losses)
        rs = pd.Series(index=gain.index, dtype=float)
        mask_loss_zero = (loss == 0) | loss.isna()
        mask_gain_zero = (gain == 0) | gain.isna()

        # When loss is 0 but gain > 0: RSI = 100
        rs.loc[mask_loss_zero & ~mask_gain_zero] = np.inf
        # When both are 0 or NaN: RSI = NaN (insufficient data)
        rs.loc[mask_loss_zero & mask_gain_zero] = np.nan
        # Normal case: calculate RS
        rs.loc[~mask_loss_zero] = gain.loc[~mask_loss_zero] / loss.loc[~mask_loss_zero]

        # Calculate RSI using forward fill instead of fillna(50)
        rsi = 100 - (100 / (1 + rs))
        
        # Clip to avoid alarming 0.0 or 100.0
        rsi = rsi.clip(lower=0.1, upper=99.9)

        return rsi.ffill()

    @staticmethod
    def calc_macd(
        close: pd.Series,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        Calculate MACD (Moving Average Convergence Divergence) indicator.

        Args:
            close: Series of closing prices
            fast: Fast EMA period (default 12)
            slow: Slow EMA period (default 26)
            signal: Signal line EMA period (default 9)

        Returns:
            Tuple of (macd_line, signal_line, histogram). Returns empty Series if input is invalid.
        """
        # Validate input
        if close is None or close.empty or len(close) < max(fast, slow, signal):
            empty = pd.Series(dtype=float)
            return empty, empty, empty

        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd = ema_fast - ema_slow
        macd_signal = macd.ewm(span=signal, adjust=False).mean()
        return macd, macd_signal, macd - macd_signal

    @staticmethod
    def get_trend_from_macd(macd_hist: pd.Series) -> str:
        """
        Determine market trend from MACD histogram.

        Args:
            macd_hist: Series containing MACD histogram values

        Returns:
            'BULL' if histogram is positive, 'BEAR' if negative, 'N/A' if zero or invalid.
        """
        # Validate input
        if macd_hist is None or macd_hist.empty:
            return "N/A"

        val = macd_hist.iloc[-1]

        # Handle NaN values
        if pd.isna(val):
            return "N/A"

        if val > 0:
            return "BULL"
        if val < 0:
            return "BEAR"
        return "N/A"

    @staticmethod
    def get_trend_from_zero_cross(macd_line: pd.Series) -> str:
        """
        Determine market trend from MACD line interactions with zero line.
        """
        if macd_line is None or macd_line.empty:
            return "N/A"
        val = macd_line.iloc[-1]
        if pd.isna(val):
            return "N/A"
        return "BULL" if val > 0 else "BEAR"

    @staticmethod
    def calc_sma(data: pd.Series, period: int) -> pd.Series:
        """
        Calculate Simple Moving Average.
        """
        if data is None or data.empty:
            return pd.Series(dtype=float)
        return data.rolling(window=period).mean()

    @staticmethod
    def calc_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
        """
        Calculate Average Directional Index (ADX).
        """
        if high is None or low is None or close is None or len(close) < period + 1:
            return pd.Series(dtype=float)

        # TR = max(|High - Low|, |High - PrevClose|, |Low - PrevClose|)
        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        # +DM, -DM
        delta_high = high.diff()
        delta_low = low.diff() # Note: low.shift(1) - low ? No, convention is Prev - Current? 
        # Actually standard: UpMove = Ht - Ht-1, DownMove = Lt-1 - Lt.
        # Let's use standard pandas usage if diff is used.
        # But commonly:
        up_move = high - high.shift(1)
        down_move = low.shift(1) - low
        
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        # Convert to Series
        plus_dm = pd.Series(plus_dm, index=close.index)
        minus_dm = pd.Series(minus_dm, index=close.index)

        # Smoothed TR and DM (Wilder's Smoothing)
        # First value is SUM, subsequent are (Prev * (n-1) + Curr) / n
        # Approximate using EWM
        # alpha = 1/period
        
        tr_smooth = tr.ewm(alpha=1/period, adjust=False).mean()
        plus_dm_smooth = plus_dm.ewm(alpha=1/period, adjust=False).mean()
        minus_dm_smooth = minus_dm.ewm(alpha=1/period, adjust=False).mean()

        # DI
        plus_di = 100 * (plus_dm_smooth / tr_smooth)
        minus_di = 100 * (minus_dm_smooth / tr_smooth)

        # ADX
        dx = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di))
        adx = dx.ewm(alpha=1/period, adjust=False).mean()
        
        return adx

    @staticmethod
    def calc_bollinger_bands(
        close: pd.Series, period: int = 20, stddev: float = 2.0
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        Calculate Bollinger Bands (upper, lower, mid).
        """
        if close is None or close.empty or len(close) < period:
            empty = pd.Series(dtype=float)
            return empty, empty, empty

        mid = close.rolling(window=period).mean()
        std = close.rolling(window=period).std()
        upper = mid + (stddev * std)
        lower = mid - (stddev * std)
        return upper, lower, mid

    @staticmethod
    def calc_bb_width(
        close: pd.Series, period: int = 20, stddev: float = 2.0
    ) -> pd.Series:
        """
        Calculate Bollinger Band width: (upper - lower) / mid.
        """
        upper, lower, mid = TechnicalAnalysis.calc_bollinger_bands(close, period, stddev)
        if mid is None or mid.empty:
            return pd.Series(dtype=float)
        width = (upper - lower) / mid.replace(0, np.nan)
        return width.fillna(0.0)
