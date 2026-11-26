# core/scoring_system.py - Scoring system for 1m trading signals
#
# This module implements a flexible scoring system (0-100) with 3 logic modes:
# - PULLBACK: Entry in trend direction after a retracement
# - BREAKOUT: Entry on breakout from a consolidation level
# - CONTRA: Counter-trend scalp for extreme reversals
#
# All entry/exit decisions are made STRICTLY on the 1-minute timeframe.
# Higher timeframes (5m, 30m, 1h) are used only for context and filtering.

import pandas as pd
import numpy as np
from typing import Any, Dict, Optional, Tuple, List
from dataclasses import dataclass, field
import logging

logger = logging.getLogger("MarketEngine")


# ============================================================================
# SCORING CONFIGURATION PER PROFILE
# ============================================================================

# Price level at which the dollar-denominated MACD scale was chosen.
MACD_REFERENCE_PRICE = 20.0

SCORE_THRESHOLDS = {
    "PRO": 75,
    "STRONG": 92,
    "MEDIUM": 80,
    "RELAX": 70,
}

# Weights for each score component (total = 100 max points)
SCORE_WEIGHTS = {
    "mtf_alignment": 30,      # Trend alignment on higher timeframes
    "rsi_zone": 20,           # RSI in optimal zone for mode
    "vwap_distance": 12,      # Distancece from VWAP (in ATR)
    "ema_distance": 12,       # Distancece from EMA20 (in ATR)
    "macd_momentum": 20,      # MACD direction and strength
    "price_action": 6,        # Price action confirmation (current candle)
}

SCORING_DEFAULTS = {
    "mtf_alignment_min": 4,
    "min_probability": 0.6,
    "logistic_coeffs": {
        "bias": -1.0,
        "mtf": 2.2,
        "macd": 1.3,
        "rsi": 0.9,
        "vwap": 0.7,
        "ema": 0.7,
    },
    "macd_norm_scale": 0.01,
    "distance_atr_max": {
        "PULLBACK": 1.5,
        "BREAKOUT": 2.5,
        "CONTRA": 3.0,
    },
}

REGIME_DEFAULTS = {
    "adx_trend_min": 20.0,
    "adx_range_max": 15.0,
    "bb_width_max": 0.04,
    "atr_percentile_high": 80.0,
    "atr_percentile_low": 20.0,
    "mtf_alignment_min_trend": 4,
}


GATE_CONFIG = {
    "enabled": True,
    "adx_trend_min": 20,             # Was 25 - too strict for crypto
    "adx_range_max": 20,
    "contra_vwap_extreme_atr": 3.0,
    "atr_pct_min": 0.0005,           # Was 0.001 - lowered to allow BTC consolidation ranges to break out
    "atr_pct_max": 0.05,
    "cooldown_bars": 12,
    "one_signal_per_swing": True,
    "swing_retrace_atr": 0.5
}


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class ScoreResult:
    """Result of the score calculation for a signal."""
    total_score: int
    direction: str  # "LONG" or "SHORT"
    mode: str       # "PULLBACK", "BREAKOUT", "CONTRA"
    profile: str    # "STRONG", "MEDIUM", "RELAX"
    is_valid: bool  # True if score exceeds profile threshold
    # 0..1 output of a hand-set logistic. It has not been calibrated against
    # outcomes, so it ranks setups; it is not a win probability.
    probability_score: float
    raw_logit_value: float

    # Individual score components
    mtf_score: int
    rsi_score: int
    vwap_score: int
    ema_score: int
    macd_score: int
    price_action_score: int

    # Debug details
    details: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    # Cooldown status
    cooldown_active: bool = False
    cooldown_bars_remaining: int = 0
    mtf_alignment_count: int = 0
    mtf_alignment_total: int = 0

    # Module 1A - Data Metadata
    data_source: str = "unknown"
    data_age_seconds: float = 0.0

    # Signal Intelligence
    signal_thesis: str = ""          # Natural-language explanation of WHY the signal fired
    timeframe: str = "intraday"      # Recommended trading timeframe
    expected_holding_period: str = "1-4h"  # Expected holding duration


@dataclass
class RowContext:
    """Context for a 1m row with attached MTF data."""
    # 1m OHLCV data
    open: float
    high: float
    low: float
    close: float
    volume: float

    # 1m indicators
    rsi: float
    macd: float
    macd_signal: float
    macd_hist: float
    ema20: float
    vwap: float
    atr: float
    
    # New Indicators (V7 Enhanced)
    adx: float
    vol_sma: float
    adx_5m: float
    bb_width_5m: float
    atr_percentile_5m: float
    market_regime: str

    # MTF trends (attached from higher timeframes)
    trend_1d: str   # "BULL", "BEAR", "N/A"
    trend_4h: str
    trend_1h: str
    trend_30m: str
    trend_15m: str
    trend_5m: str
    trend_1m: str

    # Calculated distances
    dist_vwap_atr: float  # abs(close - vwap) / atr
    dist_ema_atr: float   # abs(close - ema20) / atr

    # Cooldown state
    cooldown_active: bool
    cooldown_bars_remaining: int


# ============================================================================
# SIGNAL INTELLIGENCE: TIMEFRAME, HOLDING PERIOD, THESIS
# ============================================================================

TIMEFRAME_MAP = {
    "PULLBACK": "1m-15m scalps",
    "BREAKOUT": "1-4h swing",
    "MOMENTUM": "intraday",
    "MEAN_REVERSION": "1-4h swing",
}

HOLDING_PERIOD_MAP = {
    "PULLBACK": "~15-60 min",
    "BREAKOUT": "2-8 hours",
    "MOMENTUM": "30min-4h",
    "MEAN_REVERSION": "1-6 hours",
}

# Mapping from internal mode names to human-readable labels
_MODE_LABEL = {
    "PULLBACK": "pullback",
    "BREAKOUT": "breakout",
    "CONTRA": "counter-trend reversal",
}

_DIRECTION_LABEL = {
    "LONG": "bullish",
    "SHORT": "bearish",
}


def derive_timeframe(mode: str) -> str:
    """Return the recommended trading timeframe for a given strategy mode."""
    return TIMEFRAME_MAP.get(mode.upper(), "intraday")


def derive_expected_holding_period(mode: str) -> str:
    """Return the expected holding period for a given strategy mode."""
    return HOLDING_PERIOD_MAP.get(mode.upper(), "1-4h")


def generate_signal_thesis(
    direction: str,
    mode: str,
    rsi: float,
    mtf_alignment: str,
    mtf_score: int,
    rsi_score: int,
    macd_score: int,
    vwap_score: int,
    ema_score: int,
    price_action_score: int,
    details: dict,
    warnings: list,
) -> str:
    """
    Build a 1-2 sentence natural-language explanation of WHY the signal fired.

    The thesis summarises the key scoring drivers in plain language.
    If scoring components are missing or zero, a fallback sentence is used.
    """
    dir_label = _DIRECTION_LABEL.get(direction, direction.lower())
    mode_label = _MODE_LABEL.get(mode, mode.lower())

    # Strength qualifier based on total score component sum
    total_component_score = mtf_score + rsi_score + macd_score + vwap_score + ema_score + price_action_score
    if total_component_score >= 80:
        strength = "Strong"
    elif total_component_score >= 50:
        strength = "Moderate"
    elif total_component_score >= 25:
        strength = "Weak"
    else:
        strength = "Marginal"

    parts = []
    # 1. Core thesis: direction + mode
    parts.append(f"{strength} {dir_label} {mode_label}")

    # 2. MTF alignment context
    if mtf_alignment and mtf_alignment not in ("", "N/A"):
        # mtf_alignment looks like "4/6"
        parts.append(f"with MTF alignment {mtf_alignment}")
    elif mtf_score > 0:
        parts.append(f"(MTF score {mtf_score})")

    # 3. Key indicator highlights
    highlights = []
    if rsi_score > 0 and rsi > 0:
        # Describe RSI context
        if mode == "CONTRA":
            if direction == "LONG" and rsi <= 30:
                highlights.append(f"RSI oversold ({rsi:.1f})")
            elif direction == "SHORT" and rsi >= 70:
                highlights.append(f"RSI overbought ({rsi:.1f})")
            else:
                highlights.append(f"RSI {rsi:.1f}")
        else:
            if rsi_score >= 15:
                highlights.append(f"RSI confirming ({rsi:.1f})")
            else:
                highlights.append(f"RSI {rsi:.1f}")

    if macd_score > 0:
        macd_detail = details.get("macd", "")
        if "cross" in macd_detail.lower():
            highlights.append("MACD cross confirmation")
        elif macd_score >= 10:
            highlights.append("MACD momentum")
        else:
            highlights.append("MACD aligned")

    if vwap_score > 0:
        dist_vwap = details.get("vwap", "")
        if "close" in dist_vwap.lower() or "very close" in dist_vwap.lower():
            highlights.append("near VWAP")
        elif "above" in dist_vwap.lower() or "below" in dist_vwap.lower():
            highlights.append("VWAP direction aligned")

    if ema_score > 0:
        dist_ema = details.get("ema", "")
        if "close" in dist_ema.lower() or "very close" in dist_ema.lower():
            highlights.append("near EMA20")
        elif "above" in dist_ema.lower() or "below" in dist_ema.lower():
            highlights.append("EMA20 direction aligned")

    if highlights:
        parts.append("with " + ", ".join(highlights[:3]))

    # 4. Warning flags (limit to 1 most relevant)
    if warnings:
        # Pick first warning, abbreviate
        warn_text = warnings[0]
        if len(warn_text) > 60:
            warn_text = warn_text[:57] + "..."
        parts.append(f"[!] {warn_text}")

    thesis = ". ".join(parts) + "."
    # Clean up: ensure first letter is capitalized, no double periods
    thesis = thesis.replace("..", ".").strip()
    if thesis and thesis[0].islower():
        thesis = thesis[0].upper() + thesis[1:]

    # Fallback if thesis is too thin
    if len(thesis) < 20:
        thesis = f"{strength} {dir_label} {mode_label} signal generated by composite scoring system."

    return thesis


# ============================================================================
# ATR CALCULATION FUNCTIONS
# ============================================================================

def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Calculate ATR (Average True Range).
    """
    high_low = df["high"] - df["low"]
    high_close = np.abs(df["high"] - df["close"].shift())
    low_close = np.abs(df["low"] - df["close"].shift())
    
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    
    return true_range.rolling(window=period).mean()

def add_atr_to_dataframe(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    Add 'atr' column to DataFrame.
    """
    df["atr"] = calculate_atr(df, period)
    return df

def calculate_distance_in_atr(current_price: float, reference_price: float, atr: float) -> float:
    """
    Calculate distance between prices measured in ATR units.
    """
    if atr <= 0:
        return 0.0
    return abs(current_price - reference_price) / atr



def _clamp(min_v: float, max_v: float, val: float) -> float:
    return max(min_v, min(max_v, val))


def _sigmoid(x: float) -> float:
    try:
        if x > 12.0:
            x = 12.0
        elif x < -12.0:
            x = -12.0
        return 1.0 / (1.0 + np.exp(-x))
    except Exception:
        return 0.0


def _merge_cfg(defaults: Dict, override: Optional[Dict]) -> Dict:
    if not override:
        return defaults
    merged = dict(defaults)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            nested = dict(merged[k])
            nested.update(v)
            merged[k] = nested
        else:
            merged[k] = v
    return merged


def calculate_percentile_rank(values: List[float], current: float) -> float:
    arr = np.array([v for v in values if v is not None and not pd.isna(v)])
    if arr.size == 0:
        return 0.0
    return float((arr <= current).sum() / arr.size * 100.0)


def calculate_mtf_consensus_count(row: 'RowContext') -> int:
    tfs = [row.trend_4h, row.trend_1h, row.trend_30m, row.trend_15m, row.trend_5m, row.trend_1m]
    bull = sum(1 for tf in tfs if tf == "BULL")
    bear = sum(1 for tf in tfs if tf == "BEAR")
    return max(bull, bear)


def calculate_mtf_alignment(row: 'RowContext', direction: str, mode: str) -> Tuple[int, int]:
    expected_trend = "BULL" if direction == "LONG" else "BEAR"
    opposite_trend = "BEAR" if direction == "LONG" else "BULL"
    tfs_all = [row.trend_4h, row.trend_1h, row.trend_30m, row.trend_15m, row.trend_5m, row.trend_1m]

    if mode == "CONTRA":
        big_tfs = [row.trend_4h, row.trend_1h, row.trend_30m, row.trend_15m]
        small_tfs = [row.trend_5m, row.trend_1m]
        aligned = sum(1 for tf in big_tfs if tf == opposite_trend) + sum(1 for tf in small_tfs if tf == expected_trend)
    else:
        aligned = sum(1 for tf in tfs_all if tf == expected_trend)

    return aligned, len(tfs_all)


def detect_market_regime(row: 'RowContext', cfg: Optional[Dict] = None) -> Tuple[str, Dict[str, str]]:
    regime_cfg = _merge_cfg(REGIME_DEFAULTS, cfg)
    adx = row.adx_5m if row.adx_5m > 0 else row.adx
    bb_width = row.bb_width_5m
    atr_pct = row.atr_percentile_5m
    mtf_consensus = calculate_mtf_consensus_count(row)

    details = {
        "adx": f"{adx:.2f}",
        "bb_width": f"{bb_width:.4f}",
        "atr_percentile": f"{atr_pct:.1f}",
        "mtf_consensus": f"{mtf_consensus}/6",
    }

    if atr_pct and atr_pct <= regime_cfg.get("atr_percentile_low", 20.0):
        return "LOW_VOL", details
    if adx >= regime_cfg.get("adx_trend_min", 20.0) and mtf_consensus >= regime_cfg.get("mtf_alignment_min_trend", 4):
        return "TREND", details
    if adx <= regime_cfg.get("adx_range_max", 15.0) and bb_width <= regime_cfg.get("bb_width_max", 0.04):
        return "RANGE", details
    if atr_pct and atr_pct >= regime_cfg.get("atr_percentile_high", 80.0):
        return "VOLATILE", details

    return "MIXED", details


def allowed_modes_for_regime(regime: str, regime_cfg: Optional[Dict] = None) -> List[str]:
    allow_volatile = True
    allow_trend_pullback = False
    allow_range_breakout = False
    allow_low_vol_breakout = False
    if regime_cfg and isinstance(regime_cfg, dict):
        allow_volatile = bool(regime_cfg.get("allow_volatile", True))
        allow_trend_pullback = bool(regime_cfg.get("allow_trend_pullback", False))
        allow_range_breakout = bool(regime_cfg.get("allow_range_breakout", False))
        allow_low_vol_breakout = bool(regime_cfg.get("allow_low_vol_breakout", False))
        
    if regime == "TREND":
        if allow_trend_pullback:
            return ["PULLBACK", "BREAKOUT", "CONTRA"]
        return ["CONTRA"]
    if regime == "RANGE":
        return ["CONTRA", "BREAKOUT"] if allow_range_breakout else ["CONTRA"]
    if regime == "LOW_VOL":
        return ["BREAKOUT"] if allow_low_vol_breakout else []
    if regime == "VOLATILE":
        return ["PULLBACK", "BREAKOUT"] if allow_volatile else []
    return ["PULLBACK", "BREAKOUT", "CONTRA"]

# ============================================================================
# SCORING HELPER FUNCTIONS
# ============================================================================

def _score_mtf_alignment(
    row: 'RowContext',
    direction: str,
    mode: str,
    profile: str
) -> Tuple[int, str, List[str]]:
    """
    Calculate MTF alignment score.
    V7 upgrade: Includes volume cross-validation and conflict detection.
    """
    # Extract trends from row for convenience
    trend_1d = row.trend_1d
    trend_4h = row.trend_4h
    trend_1h = row.trend_1h
    trend_30m = row.trend_30m
    trend_15m = row.trend_15m
    trend_5m = row.trend_5m
    trend_1m = row.trend_1m

    expected_trend = "BULL" if direction == "LONG" else "BEAR"
    opposite_trend = "BEAR" if direction == "LONG" else "BULL"

    max_points = SCORE_WEIGHTS["mtf_alignment"]
    warnings = []

    # 1. Conflict Detection (Structural divergence)
    # e.g. 5m and 1d contradict 4h
    if trend_5m != trend_4h and trend_1d != trend_4h and trend_4h != "N/A":
        warnings.append(f"CONFLICT: Trend 5m({trend_5m}) & 1d({trend_1d}) contradict pivot timeframe 4h({trend_4h})")

    if mode in ["PULLBACK", "BREAKOUT"]:
        # Count how many TFs are aligned (excluding 1d per V7 logic)
        aligned_count = 0
        total_tfs = 6
        tfs = [trend_4h, trend_1h, trend_30m, trend_15m, trend_5m, trend_1m]
        aligned_count = sum(1 for tf in tfs if tf == expected_trend)

        base_score = 0
        desc = ""

        # Points per profile
        if profile == "PRO":
            if aligned_count >= 5:
                base_score = max_points
                desc = f"(+{max_points}p) {aligned_count}/{total_tfs} TF aligned ({expected_trend})"
            elif aligned_count == 4:
                base_score = int(max_points * 0.8)
                desc = f"(+{int(max_points * 0.8)}p) {aligned_count}/{total_tfs} TF aligned (PRO Standard)"
            elif aligned_count == 3:
                base_score = int(max_points * 0.66)
                desc = f"(+{int(max_points * 0.66)}p) {aligned_count}/{total_tfs} TF aligned (PRO Min)"
            else:
                return 0, f"(+0p) Only {aligned_count}/{total_tfs} TF aligned (Minimum 3 required for PRO)", warnings

        elif profile == "STRONG":
            if aligned_count >= 5:
                base_score = max_points
                desc = f"(+{max_points}p) {aligned_count}/{total_tfs} TF aligned ({expected_trend})"
            elif aligned_count == 4:
                base_score = max_points // 2
                desc = f"(+{max_points // 2}p) {aligned_count}/{total_tfs} TF aligned (Marginal)"
            else:
                return 0, f"(+0p) Only {aligned_count}/{total_tfs} TF aligned (Minimum 5 required)", warnings
                
        elif profile == "MEDIUM":
            if aligned_count >= 4:
                base_score = max_points
                desc = f"(+{max_points}p) {aligned_count}/{total_tfs} TF aligned ({expected_trend})"
            elif aligned_count == 3:
                base_score = max_points // 2
                desc = f"(+{max_points // 2}p) {aligned_count}/{total_tfs} TF aligned (Intraday Alignment)"
            else:
                return 0, "(+0p) Insufficient MTF alignment", warnings
                
        elif profile == "RELAX":
            if trend_5m == expected_trend and trend_1m == expected_trend:
                base_score = max_points
                desc = f"(+{max_points}p) Intraday aligned (5m+1m {expected_trend})"
            elif trend_1m == expected_trend:
                support_count = sum(1 for tf in [trend_1d, trend_4h, trend_1h, trend_30m] if tf == expected_trend)
                if support_count >= 1:
                     base_score = max_points * 2 // 3
                     desc = f"(+{max_points * 2 // 3}p) 1/6 TF aligned + context ({expected_trend})"
                else:
                    return 0, "(+0p) Only 1/6 TF aligned (Minimum 2 required)", warnings
            else:
                return 0, "(+0p) 0/6 TF aligned (1m unaligned)", warnings

        # 2. Volume Cross-Validation
        # If price moves but volume is low (below 80% of SMA), penalize score
        current_vol = row.volume
        vol_sma = row.vol_sma
        if vol_sma > 0 and current_vol < vol_sma * 0.8:
            reduction = int(base_score * 0.2)
            if reduction > 0:
                base_score -= reduction
                desc += f" [Low volume: -{reduction}p]"
                warnings.append("Volume non-confirmation: current volume is < 80% of SMA")

        return base_score, desc, warnings

    else:  # CONTRA
        opposite_count = 0
        big_tfs = [trend_4h, trend_1h, trend_30m, trend_15m]
        opposite_count = sum(1 for tf in big_tfs if tf == opposite_trend)
        
        is_1m_aligned = (trend_1m == expected_trend)
        is_5m_aligned = (trend_5m == expected_trend)
        
        if trend_5m == opposite_trend:
            return 0, f"(+0p) Rejected: 5m is still {opposite_trend} (unaligned with reversal)", warnings

        if opposite_count >= 3:
             if is_1m_aligned and is_5m_aligned:
                 return max_points, f"(+{max_points}p) Strong counter-trend: {opposite_count}/4 major TFs opposite + 5m&1m confirmation", warnings
             elif is_1m_aligned:
                 return max_points * 2 // 3, f"(+{max_points * 2 // 3}p) Moderate counter-trend: {opposite_count}/4 major TFs opposite + 1m confirms (5m neutral)", warnings
             else:
                 return max_points // 3, f"(+{max_points // 3}p) Risky counter-trend: {opposite_count}/4 major TFs opposite (no full confirmation)", warnings
        elif opposite_count >= 2 and is_1m_aligned and is_5m_aligned:
             return max_points // 2, f"(+{max_points // 2}p) Mini counter-trend: {opposite_count}/4 major TFs opposite + 5m&1m confirmation", warnings
        else:
             return 0, "(+0p) Not a valid counter-trend structure", warnings


def _score_rsi_zone(
    rsi: float,
    direction: str,
    mode: str,
    profile: str
) -> Tuple[int, str]:
    """
    Calculate RSI zone score.

    PULLBACK: RSI in pullback zone (not extreme)
    BREAKOUT: RSI can be high (>70 for LONG allowed)
    CONTRA: RSI in extreme zone (for reversals)

    Returns:
        Tuple (score, description)
    """
    max_points = SCORE_WEIGHTS["rsi_zone"]

    if mode == "PULLBACK":
        # PULLBACK: RSI moderate, not extreme
        if direction == "LONG":
            # LONG pullback: ideal RSI 40-65
            if 45 <= rsi <= 60:
                return max_points, f"(+{max_points}p) RSI={rsi:.1f} optimal for LONG pullback"
            elif 40 <= rsi <= 70:
                return max_points * 2 // 3, f"(+{max_points * 2 // 3}p) RSI={rsi:.1f} acceptable for LONG pullback"
            elif 35 <= rsi <= 75:
                return max_points // 3, f"(+{max_points // 3}p) RSI={rsi:.1f} marginal for LONG pullback"
            else:
                return 0, f"(+0p) RSI={rsi:.1f} too extreme for pullback"
        else:  # SHORT
            # SHORT pullback: ideal RSI 40-55
            if 40 <= rsi <= 55:
                return max_points, f"(+{max_points}p) RSI={rsi:.1f} optimal for SHORT pullback"
            elif 30 <= rsi <= 60:
                return max_points * 2 // 3, f"(+{max_points * 2 // 3}p) RSI={rsi:.1f} acceptable for SHORT pullback"
            elif 25 <= rsi <= 65:
                return max_points // 3, f"(+{max_points // 3}p) RSI={rsi:.1f} marginal for SHORT pullback"
            else:
                return 0, f"(+0p) RSI={rsi:.1f} too extreme for pullback"

    elif mode == "BREAKOUT":
        # BREAKOUT: allows higher RSI
        if direction == "LONG":
            # LONG breakout: RSI can be >70 if strong momentum (Relaxed to 85)
            if 50 <= rsi <= 80:
                return max_points, f"(+{max_points}p) RSI={rsi:.1f} good for LONG breakout"
            elif 45 <= rsi <= 85:
                return max_points * 2 // 3, f"(+{max_points * 2 // 3}p) RSI={rsi:.1f} acceptable for breakout"
            elif rsi > 85:
                # Extreme overbought risk
                return max_points // 3, f"(+{max_points // 3}p) RSI={rsi:.1f} high - pullback risk"
            else:
                return 0, f"(+0p) RSI={rsi:.1f} too low for LONG breakout"
        else:  # SHORT breakout
            if 25 <= rsi <= 50:
                return max_points, f"(+{max_points}p) RSI={rsi:.1f} good for SHORT breakout"
            elif 20 <= rsi <= 55:
                return max_points * 2 // 3, f"(+{max_points * 2 // 3}p) RSI={rsi:.1f} acceptable for breakout"
            elif rsi < 20:
                return max_points // 3, f"(+{max_points // 3}p) RSI={rsi:.1f} low - bounce risk"
            else:
                return 0, f"(+0p) RSI={rsi:.1f} too high for SHORT breakout"

    else:  # CONTRA
        # CONTRA: want harder extremes for reversals
        if direction == "LONG":
            # LONG contra: RSI very low (oversold)
            if rsi <= 20:
                return max_points, f"(+{max_points}p) RSI={rsi:.1f} extreme oversold - potential reversal"
            elif rsi <= 25:
                return max_points * 2 // 3, f"(+{max_points * 2 // 3}p) RSI={rsi:.1f} oversold - possible reversal"
            elif rsi <= 30:
                return max_points // 3, f"(+{max_points // 3}p) RSI={rsi:.1f} near oversold"
            else:
                return 0, f"(+0p) RSI={rsi:.1f} not low enough for contra LONG"
        else:  # SHORT contra
            if rsi >= 80:
                return max_points, f"(+{max_points}p) RSI={rsi:.1f} extreme overbought - potential reversal"
            elif rsi >= 75:
                return max_points * 2 // 3, f"(+{max_points * 2 // 3}p) RSI={rsi:.1f} overbought - possible reversal"
            elif rsi >= 70:
                return max_points // 3, f"(+{max_points // 3}p) RSI={rsi:.1f} near overbought"
            else:
                return 0, f"(+0p) RSI={rsi:.1f} not high enough for contra SHORT"


def _score_vwap_distance(
    dist_vwap_atr: float,
    direction: str,
    close: float,
    vwap: float,
    mode: str,
    profile: str
) -> Tuple[int, str]:
    """
    Calculate VWAP distance score (in ATR).

    PULLBACK: want close to VWAP (0-0.5 ATR ideal)
    BREAKOUT: allow further, but not extreme
    CONTRA: want far from VWAP (signals extreme)

    Returns:
        Tuple (score, description)
    """
    max_points = SCORE_WEIGHTS["vwap_distance"]

    # Determine whether price is above or below VWAP
    above_vwap = close > vwap

    if mode == "PULLBACK":
        # PULLBACK: price close to VWAP
        if dist_vwap_atr <= 0.3:
            return max_points, f"(+{max_points}p) Very close to VWAP ({dist_vwap_atr:.2f} ATR)"
        elif dist_vwap_atr <= 0.5:
            return max_points * 3 // 4, f"(+{max_points * 3 // 4}p) Close to VWAP ({dist_vwap_atr:.2f} ATR)"
        elif dist_vwap_atr <= 1.0:
            return max_points // 2, f"(+{max_points // 2}p) Moderate distance from VWAP ({dist_vwap_atr:.2f} ATR)"
        elif dist_vwap_atr <= 1.5:
            return max_points // 4, f"(+{max_points // 4}p) Far from VWAP ({dist_vwap_atr:.2f} ATR)"
        else:
            return 0, f"(+0p) Too far from VWAP ({dist_vwap_atr:.2f} ATR)"

    elif mode == "BREAKOUT":
        # BREAKOUT: tolerate larger distances
        # But also check direction
        if direction == "LONG" and above_vwap:
            if dist_vwap_atr <= 1.0:
                return max_points, f"(+{max_points}p) LONG above VWAP ({dist_vwap_atr:.2f} ATR)"
            elif dist_vwap_atr <= 1.5:
                return max_points * 2 // 3, f"(+{max_points * 2 // 3}p) LONG above VWAP, ok distance ({dist_vwap_atr:.2f} ATR)"
            elif dist_vwap_atr <= 2.0:
                return max_points // 3, f"(+{max_points // 3}p) LONG above VWAP, large distance ({dist_vwap_atr:.2f} ATR)"
            else:
                return 0, f"(+0p) Too far from VWAP for breakout ({dist_vwap_atr:.2f} ATR)"
        elif direction == "SHORT" and not above_vwap:
            if dist_vwap_atr <= 1.0:
                return max_points, f"(+{max_points}p) SHORT below VWAP ({dist_vwap_atr:.2f} ATR)"
            elif dist_vwap_atr <= 1.5:
                return max_points * 2 // 3, f"(+{max_points * 2 // 3}p) SHORT below VWAP, ok distance ({dist_vwap_atr:.2f} ATR)"
            elif dist_vwap_atr <= 2.0:
                return max_points // 3, f"(+{max_points // 3}p) SHORT below VWAP, large distance ({dist_vwap_atr:.2f} ATR)"
            else:
                return 0, f"(+0p) Too far from VWAP for breakout ({dist_vwap_atr:.2f} ATR)"
        else:
            # Wrong direction relative to VWAP
            return max_points // 4, f"(+{max_points // 4}p) Breakout counter-VWAP ({dist_vwap_atr:.2f} ATR)"

    else:  # CONTRA
        # CONTRA: want very large distance (extreme)
        if dist_vwap_atr >= 2.5:
            return max_points, f"(+{max_points}p) Extremely far from VWAP ({dist_vwap_atr:.2f} ATR) - potential reversal"
        elif dist_vwap_atr >= 2.0:
            return max_points * 2 // 3, f"(+{max_points * 2 // 3}p) Far from VWAP ({dist_vwap_atr:.2f} ATR)"
        elif dist_vwap_atr >= 1.5:
            return max_points // 3, f"(+{max_points // 3}p) Moderately far from VWAP ({dist_vwap_atr:.2f} ATR)"
        else:
            return 0, f"(+0p) Too close to VWAP for contra ({dist_vwap_atr:.2f} ATR)"


def _score_ema_distance(
    dist_ema_atr: float,
    direction: str,
    close: float,
    ema20: float,
    mode: str,
    profile: str
) -> Tuple[int, str]:
    """
    Calculate EMA20 distance score (in ATR).
    Similar to VWAP but for EMA20.

    Returns:
        Tuple (score, description)
    """
    max_points = SCORE_WEIGHTS["ema_distance"]

    above_ema = close > ema20

    if mode == "PULLBACK":
        # PULLBACK: close to EMA (mean reversion)
        if dist_ema_atr <= 0.2:
            return max_points, f"(+{max_points}p) Very close to EMA20 ({dist_ema_atr:.2f} ATR)"
        elif dist_ema_atr <= 0.4:
            return max_points * 3 // 4, f"(+{max_points * 3 // 4}p) Close to EMA20 ({dist_ema_atr:.2f} ATR)"
        elif dist_ema_atr <= 0.8:
            return max_points // 2, f"(+{max_points // 2}p) Moderate distance from EMA20 ({dist_ema_atr:.2f} ATR)"
        elif dist_ema_atr <= 1.2:
            return max_points // 4, f"(+{max_points // 4}p) Far from EMA20 ({dist_ema_atr:.2f} ATR)"
        else:
            return 0, f"(+0p) Too far from EMA20 ({dist_ema_atr:.2f} ATR)"

    elif mode == "BREAKOUT":
        # BREAKOUT: check direction
        if direction == "LONG" and above_ema:
            if dist_ema_atr <= 0.8:
                return max_points, f"(+{max_points}p) LONG above EMA20 ({dist_ema_atr:.2f} ATR)"
            elif dist_ema_atr <= 1.2:
                return max_points * 2 // 3, f"(+{max_points * 2 // 3}p) LONG above EMA20 ({dist_ema_atr:.2f} ATR)"
            elif dist_ema_atr <= 1.5:
                return max_points // 3, f"(+{max_points // 3}p) LONG above EMA20, large distance ({dist_ema_atr:.2f} ATR)"
            else:
                return 0, f"(+0p) Too far from EMA20 ({dist_ema_atr:.2f} ATR)"
        elif direction == "SHORT" and not above_ema:
            if dist_ema_atr <= 0.8:
                return max_points, f"(+{max_points}p) SHORT below EMA20 ({dist_ema_atr:.2f} ATR)"
            elif dist_ema_atr <= 1.2:
                return max_points * 2 // 3, f"(+{max_points * 2 // 3}p) SHORT below EMA20 ({dist_ema_atr:.2f} ATR)"
            elif dist_ema_atr <= 1.5:
                return max_points // 3, f"(+{max_points // 3}p) SHORT below EMA20, large distance ({dist_ema_atr:.2f} ATR)"
            else:
                return 0, f"(+0p) Too far from EMA20 ({dist_ema_atr:.2f} ATR)"
        else:
            return max_points // 4, f"(+{max_points // 4}p) Breakout counter-EMA ({dist_ema_atr:.2f} ATR)"

    else:  # CONTRA
        # CONTRA: large distance from EMA20 = extreme
        if dist_ema_atr >= 2.0:
            return max_points, f"(+{max_points}p) Extremely far from EMA20 ({dist_ema_atr:.2f} ATR)"
        elif dist_ema_atr >= 1.4:
            return max_points * 2 // 3, f"(+{max_points * 2 // 3}p) Far from EMA20 ({dist_ema_atr:.2f} ATR)"
        elif dist_ema_atr >= 1.0:
            return max_points // 3, f"(+{max_points // 3}p) Moderately far from EMA20 ({dist_ema_atr:.2f} ATR)"
        else:
            return 0, f"(+0p) Too close to EMA20 for contra ({dist_ema_atr:.2f} ATR)"


def _score_macd_momentum(
    macd_hist: float,
    macd: float,
    macd_signal: float,
    direction: str,
    mode: str,
    profile: str
) -> Tuple[int, str]:
    """
    Calculate MACD momentum score.

    PULLBACK/BREAKOUT: MACD in trade direction
    CONTRA: Opposite MACD but with reversal signs (cross)

    Returns:
        Tuple (score, description)
    """
    max_points = SCORE_WEIGHTS["macd_momentum"]

    # Determine if we have a MACD cross
    macd_cross_up = macd > macd_signal
    macd_cross_down = macd < macd_signal

    if mode in ["PULLBACK", "BREAKOUT"]:
        if direction == "LONG":
            # LONG: want positive and rising MACD
            if macd_hist > 0 and macd_cross_up:
                if macd_hist > 0.001:
                    return max_points, f"(+{max_points}p) MACD positive and strong ({macd_hist:.6f})"
                else:
                    return max_points * 2 // 3, f"(+{max_points * 2 // 3}p) MACD positive ({macd_hist:.6f})"
            elif macd_cross_up:
                return max_points // 2, f"(+{max_points // 2}p) MACD cross up, hist={macd_hist:.6f}"
            elif macd_hist > 0:
                return max_points // 3, f"(+{max_points // 3}p) MACD hist positive but no cross ({macd_hist:.6f})"
            else:
                return 0, f"(+0p) MACD negative ({macd_hist:.6f})"
        else:  # SHORT
            if macd_hist < 0 and macd_cross_down:
                if macd_hist < -0.001:
                    return max_points, f"(+{max_points}p) MACD negative and strong ({macd_hist:.6f})"
                else:
                    return max_points * 2 // 3, f"(+{max_points * 2 // 3}p) MACD negative ({macd_hist:.6f})"
            elif macd_cross_down:
                return max_points // 2, f"(+{max_points // 2}p) MACD cross down, hist={macd_hist:.6f}"
            elif macd_hist < 0:
                return max_points // 3, f"(+{max_points // 3}p) MACD hist negative but no cross ({macd_hist:.6f})"
            else:
                return 0, f"(+0p) MACD positive ({macd_hist:.6f})"

    else:  # CONTRA
        # CONTRA: want to see reversal signs
        if direction == "LONG":
            # LONG contra: negative MACD but with reversal signs
            if macd_hist < 0 and macd_cross_up:
                return max_points, f"(+{max_points}p) MACD bullish cross from negative ({macd_hist:.6f})"
            elif macd_cross_up and macd_hist > -0.0001:
                return max_points * 2 // 3, f"(+{max_points * 2 // 3}p) MACD near bullish cross ({macd_hist:.6f})"
            elif macd_hist < -0.001:
                # Very negative - high risk
                return max_points // 3, f"(+{max_points // 3}p) MACD very negative, high risk ({macd_hist:.6f})"
            else:
                return 0, f"(+0p) MACD shows no reversal ({macd_hist:.6f})"
        else:  # SHORT contra
            if macd_hist > 0 and macd_cross_down:
                return max_points, f"(+{max_points}p) MACD bearish cross from positive ({macd_hist:.6f})"
            elif macd_cross_down and macd_hist < 0.0001:
                return max_points * 2 // 3, f"(+{max_points * 2 // 3}p) MACD near bearish cross ({macd_hist:.6f})"
            elif macd_hist > 0.001:
                return max_points // 3, f"(+{max_points // 3}p) MACD very positive, high risk ({macd_hist:.6f})"
            else:
                return 0, f"(+0p) MACD shows no reversal ({macd_hist:.6f})"


def _score_price_action(
    open_price: float,
    high: float,
    low: float,
    close: float,
    direction: str,
    mode: str,
    profile: str
) -> Tuple[int, str]:
    """
    Calculate price action score (current candle pattern).

    Returns:
        Tuple (score, description)
    """
    max_points = SCORE_WEIGHTS["price_action"]

    # Calculate body size and shadows
    body = abs(close - open_price)
    total_range = high - low

    if total_range <= 0:
        return max_points // 2, "(+0p) No price movement"

    body_ratio = body / total_range
    upper_shadow = high - max(open_price, close)
    lower_shadow = min(open_price, close) - low

    is_bullish = close > open_price
    is_bearish = close < open_price

    if mode in ["PULLBACK", "BREAKOUT"]:
        if direction == "LONG":
            if is_bullish and body_ratio > 0.6:
                return max_points, f"(+{max_points}p) Strong bullish candle ({body_ratio:.0%} body)"
            elif is_bullish and body_ratio > 0.4:
                return max_points * 2 // 3, f"(+{max_points * 2 // 3}p) Bullish candle ({body_ratio:.0%} body)"
            elif is_bullish:
                return max_points // 2, f"(+{max_points // 2}p) Weak bullish candle ({body_ratio:.0%} body)"
            elif lower_shadow > upper_shadow:
                return max_points // 3, f"(+{max_points // 3}p) Lower shadow candle (potential support)"
            else:
                return 0, "(+0p) Bearish candle"
        else:  # SHORT
            if is_bearish and body_ratio > 0.6:
                return max_points, f"(+{max_points}p) Strong bearish candle ({body_ratio:.0%} body)"
            elif is_bearish and body_ratio > 0.4:
                return max_points * 2 // 3, f"(+{max_points * 2 // 3}p) Bearish candle ({body_ratio:.0%} body)"
            elif is_bearish:
                return max_points // 2, f"(+{max_points // 2}p) Weak bearish candle ({body_ratio:.0%} body)"
            elif upper_shadow > lower_shadow:
                return max_points // 3, f"(+{max_points // 3}p) Upper shadow candle (potential resistance)"
            else:
                return 0, "(+0p) Bullish candle"

    else:  # CONTRA
        # CONTRA: look for reversal candles
        if direction == "LONG":
            # Look for hammer, bullish doji, bullish engulfing
            if is_bullish and lower_shadow > body * 2:
                return max_points, f"(+{max_points}p) Bullish hammer - potential reversal"
            elif is_bullish and body_ratio > 0.5:
                return max_points * 2 // 3, f"(+{max_points * 2 // 3}p) Bullish reversal candle"
            elif lower_shadow > upper_shadow and not is_bearish:
                return max_points // 2, f"(+{max_points // 2}p) Support on lower shadow"
            else:
                return max_points // 4, f"(+{max_points // 4}p) No reversal confirmation"
        else:  # SHORT contra
            if is_bearish and upper_shadow > body * 2:
                return max_points, f"(+{max_points}p) Shooting star - potential reversal"
            elif is_bearish and body_ratio > 0.5:
                return max_points * 2 // 3, f"(+{max_points * 2 // 3}p) Bearish reversal candle"
            elif upper_shadow > lower_shadow and not is_bullish:
                return max_points // 2, f"(+{max_points // 2}p) Resistance on upper shadow"
            else:
                return max_points // 4, f"(+{max_points // 4}p) No reversal confirmation"


# ============================================================================
# MAIN SCORING FUNCTION
# ============================================================================

def compute_signal_score(
    row: RowContext,
    mode: str,
    profile: str,
    scoring_cfg: Optional[Dict] = None,
    data_source: str = "unknown",
    data_age_seconds: float = 0.0
) -> ScoreResult:
    """
    Calculate the complete score for a 1m trading signal.

    - Hard gating on MTF alignment (if below threshold, reject immediately)
    - Logistic score (probability_score 0..1), score = probability_score * 100
    """
    # Input validation
    if mode not in ["PULLBACK", "BREAKOUT", "CONTRA"]:
        raise ValueError(f"Invalid mode: {mode}. Must be PULLBACK, BREAKOUT or CONTRA")
    if profile not in ["PRO", "STRONG", "MEDIUM", "RELAX"]:
        raise ValueError(f"Invalid profile: {profile}. Must be PRO, STRONG, MEDIUM or RELAX")

    # Check cooldown
    if row.cooldown_active:
        return ScoreResult(
            total_score=0,
            direction="NONE",
            mode=mode,
            profile=profile,
            is_valid=False,
            probability_score=0.0,
            raw_logit_value=0.0,
            mtf_score=0,
            rsi_score=0,
            vwap_score=0,
            ema_score=0,
            macd_score=0,
            price_action_score=0,
            details={"cooldown": f"Cooldown active: {row.cooldown_bars_remaining} bars remaining"},
            cooldown_active=True,
            cooldown_bars_remaining=row.cooldown_bars_remaining,
            mtf_alignment_count=0,
            mtf_alignment_total=6,
            data_source=data_source,
            data_age_seconds=data_age_seconds,
            signal_thesis="Signal blocked: cooldown active.",
            timeframe=derive_timeframe(mode),
            expected_holding_period=derive_expected_holding_period(mode),
        )

    cfg = _merge_cfg(SCORING_DEFAULTS, scoring_cfg)
    coeffs = cfg.get("logistic_coeffs", {})
    mtf_min = int(cfg.get("mtf_alignment_min", 4) or 0)
    min_prob = float(cfg.get("min_probability", 0.0) or 0.0)
    macd_scale = float(cfg.get("macd_norm_scale", 0.01) or 0.01)
    dist_max_cfg = cfg.get("distance_atr_max", {}) or {}

    best_result = None

    for direction in ["LONG", "SHORT"]:
        warnings = []
        aligned_count, total_tfs = calculate_mtf_alignment(row, direction, mode)

        if aligned_count < mtf_min:
            result = ScoreResult(
                total_score=0,
                direction=direction,
                mode=mode,
                profile=profile,
                is_valid=False,
                probability_score=0.0,
                raw_logit_value=0.0,
                mtf_score=0,
                rsi_score=0,
                vwap_score=0,
                ema_score=0,
                macd_score=0,
                price_action_score=0,
                details={
                    "mtf_gate": f"Rejected: MTF alignment {aligned_count}/{total_tfs} < {mtf_min}",
                },
                warnings=warnings,
                cooldown_active=False,
                cooldown_bars_remaining=0,
                mtf_alignment_count=aligned_count,
                mtf_alignment_total=total_tfs,
                data_source=data_source,
                data_age_seconds=data_age_seconds,
                signal_thesis=f"Rejected: MTF alignment {aligned_count}/{total_tfs} insufficient for {mode} {direction}.",
                timeframe=derive_timeframe(mode),
                expected_holding_period=derive_expected_holding_period(mode),
            )
            if best_result is None or result.total_score > best_result.total_score:
                best_result = result
            continue

        # MTF Alignment (details + visual score + warning)
        mtf_score, mtf_detail, mtf_warnings = _score_mtf_alignment(
            row, direction, mode, profile
        )
        warnings.extend(mtf_warnings)

        # RSI Zone
        rsi_score, rsi_detail = _score_rsi_zone(row.rsi, direction, mode, profile)

        # VWAP Distancece
        vwap_score, vwap_detail = _score_vwap_distance(
            row.dist_vwap_atr, direction, row.close, row.vwap, mode, profile
        )

        # EMA Distancece
        ema_score, ema_detail = _score_ema_distance(
            row.dist_ema_atr, direction, row.close, row.ema20, mode, profile
        )

        # MACD Momentum
        macd_score, macd_detail = _score_macd_momentum(
            row.macd_hist, row.macd, row.macd_signal, direction, mode, profile
        )

        # Price Action
        pa_score, pa_detail = _score_price_action(
            row.open, row.high, row.low, row.close, direction, mode, profile
        )

        # --- Logistic scoring ---
        mtf_norm = aligned_count / total_tfs if total_tfs else 0.0
        macd_signed = row.macd_hist if direction == "LONG" else -row.macd_hist
        # The MACD histogram is in price units, so it is measured against the
        # price first: otherwise BTC saturates to a sign bit and a sub-dollar
        # token never contributes. `macd_norm_scale` keeps its meaning at the
        # reference price it was chosen for.
        close_px = float(row.close or 0.0)
        if close_px > 0:
            macd_signed = macd_signed / close_px * MACD_REFERENCE_PRICE
        macd_norm = _sigmoid(macd_signed / macd_scale) if macd_scale else _sigmoid(macd_signed)
        rsi_base = (row.rsi or 0.0) / 100.0
        rsi_norm = rsi_base if direction == "LONG" else (1.0 - rsi_base)

        dist_max = float(dist_max_cfg.get(mode, 1.5) or 1.5)
        if mode == "CONTRA":
            vwap_norm = _clamp(0.0, 1.0, row.dist_vwap_atr / dist_max) if dist_max > 0 else 0.0
            ema_norm = _clamp(0.0, 1.0, row.dist_ema_atr / dist_max) if dist_max > 0 else 0.0
        else:
            vwap_norm = _clamp(0.0, 1.0, 1.0 - (row.dist_vwap_atr / dist_max)) if dist_max > 0 else 0.0
            ema_norm = _clamp(0.0, 1.0, 1.0 - (row.dist_ema_atr / dist_max)) if dist_max > 0 else 0.0

        logit = (
            float(coeffs.get("bias", -1.0))
            + float(coeffs.get("mtf", 2.2)) * mtf_norm
            + float(coeffs.get("macd", 1.3)) * macd_norm
            + float(coeffs.get("rsi", 0.9)) * rsi_norm
            + float(coeffs.get("vwap", 0.7)) * vwap_norm
            + float(coeffs.get("ema", 0.7)) * ema_norm
        )
        prob = _sigmoid(logit)
        total = int(round(prob * 100))

        threshold = SCORE_THRESHOLDS[profile]
        is_valid = (total >= threshold) and (prob >= min_prob)

        # Generate signal thesis from scoring components
        thesis = generate_signal_thesis(
            direction=direction,
            mode=mode,
            rsi=row.rsi,
            mtf_alignment=f"{aligned_count}/{total_tfs}",
            mtf_score=mtf_score,
            rsi_score=rsi_score,
            macd_score=macd_score,
            vwap_score=vwap_score,
            ema_score=ema_score,
            price_action_score=pa_score,
            details={
                "mtf": mtf_detail,
                "rsi": rsi_detail,
                "vwap": vwap_detail,
                "ema": ema_detail,
                "macd": macd_detail,
                "price_action": pa_detail,
            },
            warnings=warnings,
        )

        result = ScoreResult(
            total_score=total,
            direction=direction,
            mode=mode,
            profile=profile,
            is_valid=is_valid,
            probability_score=prob,
            raw_logit_value=float(logit),
            mtf_score=mtf_score,
            rsi_score=rsi_score,
            vwap_score=vwap_score,
            ema_score=ema_score,
            macd_score=macd_score,
            price_action_score=pa_score,
            details={
                "mtf": mtf_detail,
                "rsi": rsi_detail,
                "vwap": vwap_detail,
                "ema": ema_detail,
                "macd": macd_detail,
                "price_action": pa_detail,
                "mtf_alignment": f"{aligned_count}/{total_tfs}",
                "probability": f"{prob:.3f}",
                "probability_calibrated": False,
                "logit": f"{logit:.3f}",
                "threshold": f"{total}/{threshold} ({'VALID' if is_valid else 'INVALID'})",
            },
            warnings=warnings,
            cooldown_active=False,
            cooldown_bars_remaining=0,
            mtf_alignment_count=aligned_count,
            mtf_alignment_total=total_tfs,
            data_source=data_source,
            data_age_seconds=data_age_seconds,
            signal_thesis=thesis,
            timeframe=derive_timeframe(mode),
            expected_holding_period=derive_expected_holding_period(mode),
        )

        if prob < min_prob:
            result.is_valid = False
            # `prob` is the uncalibrated logistic setup score, not a win
            # probability: say so wherever a person reads it.
            result.details["probability_gate"] = (
                f"Rejected: setup score {prob * 100.0:.0f}/100 (not a probability) "
                f"is below the minimum {min_prob * 100.0:.0f}/100"
            )
            result.total_score = 0

        # PRO filters: Volume + Trend Strength
        if profile == "PRO" and is_valid:
            if row.volume < (row.vol_sma * 0.8):
                result.is_valid = False
                result.details["volume_filter"] = f"FAIL: Vol {row.volume:.0f} < 80% AVG {row.vol_sma:.0f}"
                result.total_score = 0
                result.probability_score = 0.0
            else:
                result.details["volume_filter"] = "PASS: Volume Confirmation (>80% Avg)"

            if mode != "CONTRA" and row.adx < 20:
                result.is_valid = False
                result.details["adx_filter"] = f"FAIL: ADX {row.adx:.1f} < 20 (Weak Trend)"
                result.total_score = 0
                result.probability_score = 0.0
            elif mode != "CONTRA":
                result.details["adx_filter"] = f"PASS: Strong Trend (ADX {row.adx:.1f} > 20)"

        if best_result is None or result.total_score > best_result.total_score:
            best_result = result

    return best_result

# ============================================================================
# COOLDOWN FUNCTIONS
# ============================================================================

def check_spike_cooldown(
    df: pd.DataFrame,
    lookback: int = 5,
    rsi_extreme_threshold: float = 78.0,
    distance_atr_threshold: float = 1.8
) -> Tuple[bool, int]:
    """
    Check if there was an extreme spike recently that requires cooldown.

    Args:
        df: DataFrame with 1m data (must have 'rsi' and 'dist_vwap_atr' columns)
        lookback: How many bars back to check
        rsi_extreme_threshold: RSI above this level = spike
        distance_atr_threshold: VWAP distance above this level = spike

    Returns:
        Tuple (cooldown_active, bars_remaining)
    """
    if len(df) < lookback:
        return False, 0

    # Check the last N bars
    recent = df.tail(lookback)

    for i, (idx, row) in enumerate(recent.iterrows()):
        rsi = row.get("rsi", 50)
        dist_atr = row.get("dist_vwap_atr", 0)

        is_spike = (rsi > rsi_extreme_threshold or rsi < (100 - rsi_extreme_threshold)) and \
                   dist_atr > distance_atr_threshold

        if is_spike:
            # Calculate how many bars have passed since the spike
            bars_since_spike = lookback - i
            cooldown_bars = lookback  # Cooldown = lookback bar
            bars_remaining = max(0, cooldown_bars - bars_since_spike)

            if bars_remaining > 0:
                return True, bars_remaining

    return False, 0


def add_cooldown_columns(
    df: pd.DataFrame,
    cooldown_bars: int = 15,
    rsi_extreme_high: float = 78.0,
    rsi_extreme_low: float = 22.0,
    dist_atr_threshold: float = 1.8
) -> pd.DataFrame:
    """
    Add cooldown columns to DataFrame.

    Args:
        df: DataFrame with 1m data
        cooldown_bars: How many bars the cooldown lasts
        rsi_extreme_high: RSI above this level activates cooldown
        rsi_extreme_low: RSI below this level activates cooldown
        dist_atr_threshold: ATR distance above which we activate cooldown

    Returns:
        DataFrame with cooldown_active and cooldown_bars_remaining columns
    """
    df = df.copy()

    # Initialize columns
    df["cooldown_active"] = False
    df["cooldown_bars_remaining"] = 0

    # Check each bar
    cooldown_counter = 0

    for i in range(len(df)):
        if cooldown_counter > 0:
            df.iloc[i, df.columns.get_loc("cooldown_active")] = True
            df.iloc[i, df.columns.get_loc("cooldown_bars_remaining")] = cooldown_counter
            cooldown_counter -= 1
        else:
            # Check if current bar is a spike
            rsi = df["rsi"].iloc[i] if "rsi" in df.columns else 50
            dist_atr = df["dist_vwap_atr"].iloc[i] if "dist_vwap_atr" in df.columns else 0

            is_spike = (rsi > rsi_extreme_high or rsi < rsi_extreme_low) and \
                       dist_atr > dist_atr_threshold

            if is_spike:
                cooldown_counter = cooldown_bars
                df.iloc[i, df.columns.get_loc("cooldown_active")] = True
                df.iloc[i, df.columns.get_loc("cooldown_bars_remaining")] = cooldown_counter

    return df


# ============================================================================
# DATAFRAME PREPARATION FUNCTIONS
# ============================================================================

def prepare_1m_dataframe(
    df_1m: pd.DataFrame,
    trend_1h: str = "N/A",
    trend_30m: str = "N/A",
    trend_15m: str = "N/A",
    trend_5m: str = "N/A",
    atr_period: int = 14
) -> pd.DataFrame:
    """
    Prepare the 1m DataFrame with all columns needed for scoring.

    This function adds:
    - ATR calculated on 1m
    - Normalized distances in ATR (from VWAP and EMA20)
    - MTF trends "attached" to each bar
    - Cooldown columns

    Args:
        df_1m: DataFrame with OHLCV data and basic indicators (rsi, macd, ema_20, vwap)
        trend_1h, trend_30m, trend_5m: Trends from higher timeframes
        atr_period: Period for ATR calculation

    Returns:
        DataFrame prepared for scoring
    """
    df = df_1m.copy()

    # Add ATR
    if "atr" not in df.columns:
        df["atr"] = calculate_atr(df, atr_period)

    # Add MTF trends (same for all bars)
    df["trend_1h"] = trend_1h
    df["trend_30m"] = trend_30m
    df["trend_15m"] = trend_15m
    df["trend_5m"] = trend_5m

    # Calculate distances in ATR
    if "vwap" in df.columns and "atr" in df.columns:
        df["dist_vwap_atr"] = df.apply(
            lambda row: calculate_distance_in_atr(row["close"], row["vwap"], row["atr"]),
            axis=1
        )
    else:
        df["dist_vwap_atr"] = 0.0

    if "ema_20" in df.columns and "atr" in df.columns:
        df["dist_ema_atr"] = df.apply(
            lambda row: calculate_distance_in_atr(row["close"], row["ema_20"], row["atr"]),
            axis=1
        )
    else:
        df["dist_ema_atr"] = 0.0

    # Add cooldown columns
    df = add_cooldown_columns(df)

    return df


def row_to_context(row: pd.Series) -> RowContext:
    """
    Convert a pandas row to a RowContext for scoring.

    Args:
        row: A row from the prepared DataFrame

    Returns:
        RowContext with all necessary data
    """
    return RowContext(
        open=float(row.get("open", 0)),
        high=float(row.get("high", 0)),
        low=float(row.get("low", 0)),
        close=float(row.get("close", 0)),
        volume=float(row.get("volume", 0)),
        rsi=float(row.get("rsi", 50)),
        macd=float(row.get("macd", 0)),
        macd_signal=float(row.get("macd_signal", 0)),
        macd_hist=float(row.get("macd_hist", 0)),
        ema20=float(row.get("ema_20", row.get("close", 0))),
        vwap=float(row.get("vwap", row.get("close", 0))),
        atr=float(row.get("atr", 0.01)),
        adx=float(row.get("adx", 0)),
        vol_sma=float(row.get("vol_sma", 0)),
        adx_5m=float(row.get("adx_5m", row.get("adx", 0))),
        bb_width_5m=float(row.get("bb_width_5m", 0)),
        atr_percentile_5m=float(row.get("atr_percentile_5m", 0)),
        market_regime=str(row.get("market_regime", "MIXED")),
        trend_1d=str(row.get("trend_1d", "N/A")),
        trend_4h=str(row.get("trend_4h", "N/A")),
        trend_1h=str(row.get("trend_1h", "N/A")),
        trend_30m=str(row.get("trend_30m", "N/A")),
        trend_15m=str(row.get("trend_15m", "N/A")),
        trend_5m=str(row.get("trend_5m", "N/A")),
        trend_1m=str(row.get("trend_1m", "N/A")),
        dist_vwap_atr=float(row.get("dist_vwap_atr", 0)),
        dist_ema_atr=float(row.get("dist_ema_atr", 0)),
        cooldown_active=bool(row.get("cooldown_active", False)),
        cooldown_bars_remaining=int(row.get("cooldown_bars_remaining", 0))
    )


def compute_all_scores(
    df: pd.DataFrame,
    modes: List[str] = None,
    profiles: List[str] = None
) -> pd.DataFrame:
    """
    Calculate scores for all mode/profile/direction combinations.

    Args:
        df: DataFrame prepared with prepare_1m_dataframe()
        modes: List of modes to test (default: all)
        profiles: List of profiles to test (default: all)

    Returns:
        DataFrame with additional score columns:
        - long_score_{mode}_{profile}
        - short_score_{mode}_{profile}
        - long_{mode}_{profile}_ok (boolean)
        - short_{mode}_{profile}_ok (boolean)
    """
    if modes is None:
        modes = ["PULLBACK", "BREAKOUT", "CONTRA"]
    if profiles is None:
        profiles = ["STRONG", "MEDIUM", "RELAX"]

    df = df.copy()

    for mode in modes:
        for profile in profiles:
            long_scores = []
            short_scores = []
            long_valid = []
            short_valid = []

            for i in range(len(df)):
                row = df.iloc[i]
                ctx = row_to_context(row)
                result = compute_signal_score(ctx, mode, profile)

                if result.direction == "LONG":
                    long_scores.append(result.total_score)
                    short_scores.append(0)
                    long_valid.append(result.is_valid)
                    short_valid.append(False)
                elif result.direction == "SHORT":
                    long_scores.append(0)
                    short_scores.append(result.total_score)
                    long_valid.append(False)
                    short_valid.append(result.is_valid)
                else:
                    long_scores.append(0)
                    short_scores.append(0)
                    long_valid.append(False)
                    short_valid.append(False)

            # Add columns
            col_suffix = f"{mode.lower()}_{profile.lower()}"
            df[f"long_score_{col_suffix}"] = long_scores
            df[f"short_score_{col_suffix}"] = short_scores
            df[f"long_{col_suffix}_ok"] = long_valid
            df[f"short_{col_suffix}_ok"] = short_valid

    return df

# ============================================================================
# AUDIT & CALIBRATION (MODULE 1A)
# ============================================================================

def run_weight_calibration_audit(lookback_days: int = 30) -> Dict[str, Any]:
    """
    Framework for scoring quality audit.
    Analyzes the historical performance of score components (RSI, VWAP, MTF etc.)
    to suggest recalibration of SCORE_WEIGHTS.
    """
    logger.info(f"Running weight calibration audit (lookback: {lookback_days} days)...")

    # Evolved (was a 'Module 5/6' skeleton). Delegates the calibration label to
    # core.ml (§55) — the V1 logistic coefficients are hand-coded and uncalibrated;
    # a real calibrated baseline + outcomes lands with the Phase 6B ML baseline.
    # The hand-coded logistic_coeffs path is NOT modified (§5/§7 — additive).
    is_statistical = False
    method = "none"
    try:
        from core.ml.calibration import CalibrationResult, ProbabilityDefinition
        from core.signal_engine import SignalHorizon
        definition = ProbabilityDefinition(
            horizon=SignalHorizon.SWING,
            condition="uncalibrated logistic score (V1 probability_score)",
        )
        res = CalibrationResult.uncalibrated(definition, sample_size=0)
        is_statistical = res.is_statistical_probability
        method = res.method
    except Exception as exc:  # never break the live engine
        logger.debug(f"calibration label deferred: {exc}")

    return {
        "status": "active",
        "available": False,
        "current_weights": SCORE_WEIGHTS,
        "suggested_changes": {},
        "confidence_score": 0.0,
        "is_statistical_probability": is_statistical,
        "calibration_method": method,
        "summary": ("V1 logistic coefficients are hand-coded and uncalibrated; "
                    "calibration lands with the Phase 6B baseline + outcomes."),
    }
