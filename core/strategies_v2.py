# core/strategies_v2.py
import pandas as pd
import numpy as np
from typing import Dict, List, Optional
import logging

logger = logging.getLogger("MarketEngine")


class StrategyS7_MomentumBurst:
    """S7 - Momentum Burst: RSI < 30 + volume spike + price > VWAP"""

    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.rsi_oversold = self.config.get("rsi_oversold", 30)
        self.volume_multiplier = self.config.get("volume_multiplier", 2.0)
        self.tp_pct = self.config.get("tp_pct", 3.0)
        self.sl_pct = self.config.get("sl_pct", 1.5)

    def check_signal(self, df: pd.DataFrame, idx: int) -> Optional[Dict]:
        """Check for S7 signal"""
        if idx < 20:
            return None

        rsi = df.iloc[idx].get("rsi")
        if pd.isna(rsi) or rsi >= self.rsi_oversold:
            return None

        avg_volume = df["volume"].iloc[idx-20:idx].mean()
        current_volume = df.iloc[idx]["volume"]
        if current_volume < avg_volume * self.volume_multiplier:
            return None

        vwap = df.iloc[idx].get("vwap")
        if pd.isna(vwap) or df.iloc[idx]["close"] < vwap:
            return None

        return {
            "strategy": "S7_MOMENTUM_BURST",
            "type": "BUY",
            "entry_price": df.iloc[idx]["close"],
            "tp": df.iloc[idx]["close"] * (1 + self.tp_pct / 100),
            "sl": df.iloc[idx]["close"] * (1 - self.sl_pct / 100),
            "score": 75
        }


class StrategyS8_TrendContinuation:
    """S8 - Trend Continuation: EMA 9 > EMA 21 + ADX > 25"""

    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.adx_min = self.config.get("adx_min", 25)
        self.tp_pct = self.config.get("tp_pct", 5.0)
        self.sl_pct = self.config.get("sl_pct", 2.0)

    def check_signal(self, df: pd.DataFrame, idx: int) -> Optional[Dict]:
        if idx < 21:
            return None

        ema9 = df.iloc[idx].get("ema_9")
        ema21 = df.iloc[idx].get("ema_21")
        adx = df.iloc[idx].get("adx")

        if pd.isna(ema9) or pd.isna(ema21) or pd.isna(adx):
            return None

        if ema9 <= ema21 or adx < self.adx_min:
            return None

        return {
            "strategy": "S8_TREND_CONTINUATION",
            "type": "BUY",
            "entry_price": df.iloc[idx]["close"],
            "tp": df.iloc[idx]["close"] * (1 + self.tp_pct / 100),
            "sl": df.iloc[idx]["close"] * (1 - self.sl_pct / 100),
            "score": 80
        }


class StrategyS9_MeanReversionExtreme:
    """S9 - Mean Reversion Extreme: RSI < 20 + BB lower + ATR > 1.5x"""

    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.rsi_extreme = self.config.get("rsi_extreme", 20)
        self.atr_multiplier = self.config.get("atr_multiplier", 1.5)
        self.tp_pct = self.config.get("tp_pct", 4.0)
        self.sl_pct = self.config.get("sl_pct", 1.5)

    def check_signal(self, df: pd.DataFrame, idx: int) -> Optional[Dict]:
        if idx < 20:
            return None

        rsi = df.iloc[idx].get("rsi")
        if pd.isna(rsi) or rsi >= self.rsi_extreme:
            return None

        bb_lower = df.iloc[idx].get("bb_lower")
        if pd.isna(bb_lower) or df.iloc[idx]["close"] > bb_lower:
            return None

        atr = df.iloc[idx].get("atr")
        avg_atr = df["atr"].iloc[idx-20:idx].mean()
        if pd.isna(atr) or pd.isna(avg_atr) or atr < avg_atr * self.atr_multiplier:
            return None

        return {
            "strategy": "S9_MEAN_REVERSION_EXTREME",
            "type": "BUY",
            "entry_price": df.iloc[idx]["close"],
            "tp": df.iloc[idx]["close"] * (1 + self.tp_pct / 100),
            "sl": df.iloc[idx]["close"] * (1 - self.sl_pct / 100),
            "score": 85
        }


class StrategyS10_GapFill:
    """S10 - Gap Fill: Overnight gap > 2% + returns to fill"""

    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.gap_threshold = self.config.get("gap_threshold", 2.0)
        self.sl_pct = self.config.get("sl_pct", 1.0)

    def check_signal(self, df: pd.DataFrame, idx: int) -> Optional[Dict]:
        if idx < 1:
            return None

        prev_close = df.iloc[idx-1]["close"]
        current_open = df.iloc[idx]["open"]
        gap_pct = ((current_open - prev_close) / prev_close) * 100

        if abs(gap_pct) < self.gap_threshold:
            return None

        if gap_pct > 0:
            if df.iloc[idx]["close"] < current_open * 0.99:
                return {
                    "strategy": "S10_GAP_FILL",
                    "type": "SELL",
                    "entry_price": df.iloc[idx]["close"],
                    "tp": prev_close,
                    "sl": current_open * (1 + self.sl_pct / 100),
                    "score": 70
                }
        else:
            if df.iloc[idx]["close"] > current_open * 1.01:
                return {
                    "strategy": "S10_GAP_FILL",
                    "type": "BUY",
                    "entry_price": df.iloc[idx]["close"],
                    "tp": prev_close,
                    "sl": current_open * (1 - self.sl_pct / 100),
                    "score": 70
                }

        return None


class StrategyS11_VolumeProfile:
    """S11 - Volume Profile: Price breaks VWAP with volume > 150% avg"""

    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.volume_threshold = self.config.get("volume_threshold", 1.5)
        self.tp_pct = self.config.get("tp_pct", 4.0)
        self.sl_pct = self.config.get("sl_pct", 1.5)

    def check_signal(self, df: pd.DataFrame, idx: int) -> Optional[Dict]:
        if idx < 20:
            return None

        vwap = df.iloc[idx].get("vwap")
        if pd.isna(vwap):
            return None

        prev_close = df.iloc[idx-1]["close"]
        current_close = df.iloc[idx]["close"]

        if prev_close < vwap and current_close > vwap:
            avg_volume = df["volume"].iloc[idx-20:idx].mean()
            if df.iloc[idx]["volume"] > avg_volume * self.volume_threshold:
                return {
                    "strategy": "S11_VOLUME_PROFILE",
                    "type": "BUY",
                    "entry_price": current_close,
                    "tp": current_close * (1 + self.tp_pct / 100),
                    "sl": current_close * (1 - self.sl_pct / 100),
                    "score": 78
                }
        elif prev_close > vwap and current_close < vwap:
            avg_volume = df["volume"].iloc[idx-20:idx].mean()
            if df.iloc[idx]["volume"] > avg_volume * self.volume_threshold:
                return {
                    "strategy": "S11_VOLUME_PROFILE",
                    "type": "SELL",
                    "entry_price": current_close,
                    "tp": current_close * (1 - self.tp_pct / 100),
                    "sl": current_close * (1 + self.sl_pct / 100),
                    "score": 78
                }

        return None


class StrategyS12_TimeBasedExit:
    """S12 - Time-based Exit: Partial exit after 4h without TP/SL"""

    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.max_hours = self.config.get("max_hours", 4)
        self.partial_pct = self.config.get("partial_pct", 50)

    def check_exit(self, position: Dict, current_time) -> Optional[Dict]:
        """Check if partial exit is needed"""
        entry_time = position.get("entry_time")
        if not entry_time:
            return None

        hours_held = (current_time - entry_time).total_seconds() / 3600

        if hours_held >= self.max_hours:
            return {
                "action": "PARTIAL_EXIT",
                "percentage": self.partial_pct,
                "reason": "time_based"
            }

        return None


# Strategy registry
STRATEGIES = {
    "S7_MOMENTUM_BURST": StrategyS7_MomentumBurst,
    "S8_TREND_CONTINUATION": StrategyS8_TrendContinuation,
    "S9_MEAN_REVERSION_EXTREME": StrategyS9_MeanReversionExtreme,
    "S10_GAP_FILL": StrategyS10_GapFill,
    "S11_VOLUME_PROFILE": StrategyS11_VolumeProfile,
    "S12_TIME_BASED_EXIT": StrategyS12_TimeBasedExit,
}


def get_strategy(name: str, config: Dict = None):
    """Factory function for strategies"""
    strategy_class = STRATEGIES.get(name)
    if strategy_class:
        return strategy_class(config)
    return None
