# core/backtest_optimizer.py
import pandas as pd
import numpy as np
from typing import Dict, List, Tuple
import json
import logging

logger = logging.getLogger("MarketEngine")


class BacktestOptimizer:
    """Optimizer for trading parameters to improve win rate to 60%+"""

    def __init__(self, config_manager=None):
        self.config = config_manager
        self.target_win_rate = 0.60
        self.min_trades = 30

    def calculate_adaptive_atr_multipliers(self, volatility: float) -> Dict[str, float]:
        """
        Calculate adaptive ATR multipliers based on volatility.

        Args:
            volatility: Current volatility measure (e.g., ATR percentage)

        Returns:
            Dict with 'tp' (take profit) and 'sl' (stop loss) multipliers

        Examples:
            - Low volatility (vol < 0.015): tp=3.0, sl=0.8
            - Normal volatility (0.015 <= vol < 0.035): tp=4.0, sl=1.0
            - High volatility (vol >= 0.035): tp=5.0, sl=1.5
        """
        if volatility < 0.015:
            return {"tp": 3.0, "sl": 0.8}  # Low volatility
        elif volatility < 0.035:
            return {"tp": 4.0, "sl": 1.0}  # Normal volatility
        else:
            return {"tp": 5.0, "sl": 1.5}  # High volatility

    def calculate_adaptive_rsi_bounds(self, avg_rsi: float) -> Dict[str, float]:
        """
        Calculate adaptive RSI bounds based on market conditions.

        Args:
            avg_rsi: Average RSI value over the period

        Returns:
            Dict with 'oversold' and 'overbought' bounds

        Examples:
            - Oversold market (avg_rsi <= 35): oversold=25, overbought=75
            - Neutral market (35 < avg_rsi < 65): oversold=30, overbought=70
            - Overbought market (avg_rsi >= 65): oversold=35, overbought=65
        """
        if avg_rsi <= 35:
            # Oversold market: tighter bounds
            return {"oversold": 25, "overbought": 75}
        elif avg_rsi >= 65:
            # Overbought market: tighter bounds
            return {"oversold": 35, "overbought": 65}
        else:
            # Neutral market: standard bounds
            return {"oversold": 30, "overbought": 70}

    def apply_volume_confirmation(self, df: pd.DataFrame, multiplier: float = 1.5) -> pd.Series:
        """
        Volume confirmation - returns mask for valid trades.

        Args:
            df: DataFrame with OHLCV data (must have 'Volume' or 'volume' column)
            multiplier: Volume multiplier relative to moving average (default 1.5)

        Returns:
            Boolean Series indicating valid trade opportunities
        """
        # Handle both column name cases
        volume_col = None
        for col in ['Volume', 'volume']:
            if col in df.columns:
                volume_col = col
                break

        if volume_col is None:
            return pd.Series([True] * len(df), index=df.index)

        avg_volume = df[volume_col].rolling(window=20, min_periods=1).mean()
        return df[volume_col] > (avg_volume * multiplier)

    def apply_time_filter(self, df: pd.DataFrame) -> pd.Series:
        """
        Time filter - avoid first/last 15 minutes of market hours.
        Market hours: 9:45 (after opening range) to 15:45 (before close)

        Args:
            df: DataFrame with datetime index

        Returns:
            Boolean Series indicating valid trade times
        """
        # Try to get datetime from index or columns
        datetime_col = None

        if isinstance(df.index, pd.DatetimeIndex):
            datetime_col = df.index
        elif "time" in df.columns:
            datetime_col = pd.to_datetime(df["time"], errors='coerce')
        elif "datetime" in df.columns:
            datetime_col = pd.to_datetime(df["datetime"], errors='coerce')
        elif "timestamp" in df.columns:
            datetime_col = pd.to_datetime(df["timestamp"], errors='coerce')

        if datetime_col is None:
            return pd.Series([True] * len(df), index=df.index)

        # Extract hour and minute - handle both Series and Index
        if isinstance(datetime_col, pd.Series):
            hours = datetime_col.dt.hour
            minutes = datetime_col.dt.minute
        else:
            hours = datetime_col.hour
            minutes = datetime_col.minute

        # Convert to minutes since midnight for easier comparison
        time_minutes = hours * 60 + minutes

        # Market hours: 9:45 (585 min) to 15:45 (945 min)
        market_start = 9 * 60 + 45   # 585
        market_end = 15 * 60 + 45    # 945

        is_valid_time = (time_minutes >= market_start) & (time_minutes <= market_end)

        # Handle numpy array case
        if hasattr(is_valid_time, 'values'):
            return pd.Series(is_valid_time.values, index=df.index)
        else:
            return pd.Series(is_valid_time, index=df.index)

    def optimize_for_ticker(self, ticker: str, results: List[Dict]) -> Dict:
        """
        Optimize parameters for a specific ticker based on backtest results.

        Args:
            ticker: Stock ticker symbol
            results: List of backtest result dictionaries

        Returns:
            Dict with optimized parameters and recommendations
        """
        if not results:
            return {
                'ticker': ticker,
                'error': 'No results provided',
                'best_params': {},
                'recommended_atr': {'tp': 4.0, 'sl': 1.0},
                'recommended_rsi': {'oversold': 30, 'overbought': 70},
                'expected_win_rate': 0.0
            }

        # Find best result based on win rate and trade count
        valid_results = [
            r for r in results
            if r.get('total_trades', 0) >= self.min_trades
        ]

        if not valid_results:
            # Fallback if not enough trades
            valid_results = results

        # Sort by win rate (descending), then by avg_r (descending)
        valid_results.sort(key=lambda x: (x.get('win_rate', 0), x.get('avg_r', 0)), reverse=True)

        best = valid_results[0]

        # Extract volatility and RSI info from results
        volatility = best.get('volatility', 0.025)  # Default to normal
        avg_rsi = best.get('avg_rsi', 50)  # Default to neutral

        # Calculate recommended parameters
        recommended_atr = self.calculate_adaptive_atr_multipliers(volatility)
        recommended_rsi = self.calculate_adaptive_rsi_bounds(avg_rsi)

        return {
            'ticker': ticker,
            'best_params': best,
            'recommended_atr': recommended_atr,
            'recommended_rsi': recommended_rsi,
            'expected_win_rate': best.get('win_rate', 0) / 100.0 if isinstance(best.get('win_rate'), (int, float)) else 0.0,
            'total_optimized_results': len(valid_results),
            'sample_size': best.get('total_trades', 0)
        }

    def get_optimized_params(self, df: pd.DataFrame) -> Dict:
        """Return optimized parameters for the DataFrame"""
        # Calculate volatility
        if "close" in df.columns and len(df) > 20:
            returns = df["close"].pct_change().dropna()
            volatility = returns.std()
        else:
            volatility = 0.02

        # Calculate average RSI
        avg_rsi = 50  # Default

        atr_mults = self.calculate_adaptive_atr_multipliers(volatility)
        rsi_bounds = self.calculate_adaptive_rsi_bounds(avg_rsi)

        return {
            "atr_multiplier_tp": atr_mults["tp"],
            "atr_multiplier_sl": atr_mults["sl"],
            "rsi_oversold": rsi_bounds["oversold"],
            "rsi_overbought": rsi_bounds["overbought"]
        }

    def analyze_loss_patterns(self, trades: List[Dict]) -> Dict:
        """Analyze patterns in losing trades"""
        if not trades:
            return {}

        losing_trades = [t for t in trades if t.get("r_multiple", 0) <= 0]

        if not losing_trades:
            return {"total_losses": 0, "patterns": []}

        patterns = {
            "total_losses": len(losing_trades),
            "avg_loss_r": np.mean([t.get("r_multiple", 0) for t in losing_trades]),
            "patterns": []
        }

        # Analyze time of day
        loss_times = []
        for t in losing_trades:
            if "entry_time" in t:
                try:
                    entry_time = pd.to_datetime(t["entry_time"])
                    loss_times.append(entry_time.hour)
                except Exception:
                    pass

        if loss_times:
            hour_counts = pd.Series(loss_times).value_counts()
            patterns["loss_hours"] = hour_counts.to_dict()

        return patterns

    def suggest_parameter_adjustments(self, current_params: Dict, win_rate: float) -> Dict:
        """Suggest parameter adjustments based on current win rate"""
        suggestions = {}

        # To RAISE win rate: bring TP closer (easier to hit) and widen SL
        # (harder to hit). Note this trades reward:risk for hit rate.
        if win_rate < 0.40:
            # Aggressive optimization needed
            suggestions["atr_multiplier_tp"] = current_params.get("atr_multiplier_tp", 2.0) * 0.8
            suggestions["atr_multiplier_sl"] = current_params.get("atr_multiplier_sl", 1.0) * 1.2
            suggestions["use_conservative_entry"] = True
        elif win_rate < 0.50:
            # Moderate optimization
            suggestions["atr_multiplier_tp"] = current_params.get("atr_multiplier_tp", 2.0) * 0.9
            suggestions["atr_multiplier_sl"] = current_params.get("atr_multiplier_sl", 1.0) * 1.1
        elif win_rate < 0.60:
            # Fine tuning
            suggestions["atr_multiplier_tp"] = current_params.get("atr_multiplier_tp", 2.0) * 0.95

        return suggestions
