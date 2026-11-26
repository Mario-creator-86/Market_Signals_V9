# core/signal_generation.py - Helper functions for signal generation
#
# NOTE: The old PASS/FAIL system has been replaced by the scoring system from scoring_system.py
# This file keeps only the helper functions used in other modules.

import pandas as pd
from typing import Dict
import logging

logger = logging.getLogger("MarketEngine")


# ============================================================================
# HELPER FUNCTIONS FOR TECHNICAL ANALYSIS
# ============================================================================

def calculate_vwap(df: pd.DataFrame) -> pd.Series:
    """
    Calculate Volume Weighted Average Price (VWAP).

    Args:
        df: DataFrame with columns 'high', 'low', 'close', 'volume'

    Returns:
        Series with VWAP values
    """
    if "volume" not in df.columns:
        # If no volume data, return close price as fallback
        return df["close"]

    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    vwap = (typical_price * df["volume"]).cumsum() / df["volume"].cumsum()
    return vwap


def count_mtf_alignment(mtf_trends: Dict[str, str], expected_trend: str) -> int:
    """
    Count how many MTF timeframes are aligned with the expected trend.

    Args:
        mtf_trends: Dict with keys '1h', '30m', '5m'
        expected_trend: 'BULL' or 'BEAR'

    Returns:
        Number of aligned timeframes (0-3)
    """
    aligned = 0
    for tf in ["1h", "30m", "5m"]:
        if mtf_trends.get(tf) == expected_trend:
            aligned += 1
    return aligned
