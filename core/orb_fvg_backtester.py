"""
Opening Range Breakout + Fair Value Gap (FVG) Backtester
Supports US Stocks only (market hours 9:30-16:00 ET)

Strategy:
1. Build 15-min opening range (9:30-9:45 ET)
2. Detect FVG patterns on 5-min chart breaking the range
3. Enter via limit order in FVG zone
4. Fixed 2:1 R:R with time-based exit
"""

import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, time
from typing import Dict, List, Optional, Tuple
import pytz

# Import optimizer
from core.backtest_optimizer import BacktestOptimizer

logger = logging.getLogger("MarketEngine")

# Timezone constants
ET = pytz.timezone('America/New_York')
UTC = pytz.UTC

# Market hours (Eastern Time)
MARKET_OPEN = time(9, 30)
OPENING_RANGE_END = time(9, 45)
ENTRY_WINDOW_END = time(12, 0)  # No entries after noon
EXIT_CUTOFF = time(15, 45)  # Force exit before close
MARKET_CLOSE = time(16, 0)




def resample_to_timeframe(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Resample 1-min or lower data to specified timeframe."""
    ohlc_dict = {
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }
    
    # Ensure index is datetime
    if not isinstance(df.index, pd.DatetimeIndex):
        if 'timestamp' in df.columns:
            df = df.set_index('timestamp')
        else:
            raise ValueError("DataFrame must have datetime index or 'timestamp' column")
    
    # Map timeframe string
    tf_map = {'5min': '5min', '15min': '15min', '5m': '5min', '15m': '15min'}
    tf = tf_map.get(timeframe, timeframe)
    
    return df.resample(tf).agg(ohlc_dict).dropna()


def build_opening_range(df_15m: pd.DataFrame, date: datetime.date) -> Tuple[float, float]:
    """
    Build opening range from 9:30-9:45 ET candle.
    Returns (or_high, or_low) or (None, None) if no data.
    """
    # Filter for the specific date and 9:30 candle
    day_start = pd.Timestamp(date).tz_localize(ET)
    or_start = day_start.replace(hour=9, minute=30)
    or_end = day_start.replace(hour=9, minute=45)
    
    # Find the 9:30 15-min candle
    mask = (df_15m.index >= or_start) & (df_15m.index < or_end)
    or_candle = df_15m[mask]
    
    if or_candle.empty:
        return None, None
    
    or_high = or_candle['high'].max()
    or_low = or_candle['low'].min()
    
    return or_high, or_low


def detect_bullish_fvg(c1: pd.Series, c2: pd.Series, c3: pd.Series) -> Optional[Dict]:
    """
    Detect bullish Fair Value Gap pattern.
    
    Bullish FVG conditions:
    - C2 is a strong bullish candle (close > open, pushing up)
    - Gap exists between C1.high and C3.low (C3.low > C1.high)
    
    Returns FVG zone dict or None.
    """
    # C2 must be bullish impulse (strong up move)
    if c2['close'] <= c2['open']:
        return None
    
    # FVG gap check: C3.low > C1.high (gap between wicks)
    fvg_low = c1['high']  # Top of C1 wick
    fvg_high = c3['low']  # Bottom of C3 wick
    
    if fvg_high <= fvg_low:
        return None  # No gap
    
    return {
        'type': 'BULLISH',
        'fvg_high': fvg_high,
        'fvg_low': fvg_low,
        'midpoint': (fvg_high + fvg_low) / 2,
        'c1_low': c1['low'],  # For stop loss
        'c1_time': c1.name,
        'c3_time': c3.name
    }


def detect_bearish_fvg(c1: pd.Series, c2: pd.Series, c3: pd.Series) -> Optional[Dict]:
    """
    Detect bearish Fair Value Gap pattern.
    
    Bearish FVG conditions:
    - C2 is a strong bearish candle (close < open, pushing down)
    - Gap exists between C1.low and C3.high (C3.high < C1.low)
    
    Returns FVG zone dict or None.
    """
    # C2 must be bearish impulse (strong down move)
    if c2['close'] >= c2['open']:
        return None
    
    # FVG gap check: C3.high < C1.low (gap between wicks)
    fvg_high = c1['low']  # Bottom of C1 wick
    fvg_low = c3['high']  # Top of C3 wick
    
    if fvg_high <= fvg_low:
        return None  # No gap
    
    return {
        'type': 'BEARISH',
        'fvg_high': fvg_high,
        'fvg_low': fvg_low,
        'midpoint': (fvg_high + fvg_low) / 2,
        'c1_high': c1['high'],  # For stop loss
        'c1_time': c1.name,
        'c3_time': c3.name
    }


def validate_fvg_vs_range(fvg: Dict, or_high: float, or_low: float, 
                          c1: pd.Series, c2: pd.Series, c3: pd.Series) -> bool:
    """
    Validate FVG relative to opening range.
    
    For BULLISH:
    - At least one close inside range
    - At least one close above range high
    
    For BEARISH:
    - At least one close inside range
    - At least one close below range low
    """
    closes = [c1['close'], c2['close'], c3['close']]
    
    inside_range = any(or_low <= c <= or_high for c in closes)
    
    if fvg['type'] == 'BULLISH':
        above_range = any(c > or_high for c in closes)
        return inside_range and above_range
    else:  # BEARISH
        below_range = any(c < or_low for c in closes)
        return inside_range and below_range


def scan_for_fvg_signals(df_5m: pd.DataFrame, or_high: float, or_low: float,
                         date: datetime.date) -> List[Dict]:
    """
    Scan 5-min candles for valid FVG patterns breaking opening range.
    Only considers patterns forming between 9:45 and 12:00 ET.
    """
    signals = []
    
    day_start = pd.Timestamp(date).tz_localize(ET)
    scan_start = day_start.replace(hour=9, minute=45)
    scan_end = day_start.replace(hour=12, minute=0)
    
    # Filter to entry window
    mask = (df_5m.index >= scan_start) & (df_5m.index < scan_end)
    window = df_5m[mask]
    
    if len(window) < 3:
        return signals
    
    # Scan for 3-candle patterns
    for i in range(len(window) - 2):
        c1 = window.iloc[i]
        c2 = window.iloc[i + 1]
        c3 = window.iloc[i + 2]
        
        # Try bullish FVG
        bullish = detect_bullish_fvg(c1, c2, c3)
        if bullish and validate_fvg_vs_range(bullish, or_high, or_low, c1, c2, c3):
            bullish['signal_time'] = c3.name
            signals.append(bullish)
        
        # Try bearish FVG
        bearish = detect_bearish_fvg(c1, c2, c3)
        if bearish and validate_fvg_vs_range(bearish, or_high, or_low, c1, c2, c3):
            bearish['signal_time'] = c3.name
            signals.append(bearish)
    
    return signals


def simulate_trade(fvg: Dict, df_5m: pd.DataFrame, entry_type: str,
                   risk_reward: float = 2.0, date: datetime.date = None,
                   cost_model=None) -> Optional[Dict]:
    """
    Simulate a single trade from FVG signal.
    
    Args:
        fvg: FVG signal dict
        df_5m: 5-minute data for the day
        entry_type: 'MIDPOINT' or 'CONSERVATIVE'
        risk_reward: Target R:R ratio
        date: Trading date
    
    Returns:
        Trade result dict or None if no fill
    """
    day_start = pd.Timestamp(date).tz_localize(ET)
    entry_deadline = day_start.replace(hour=12, minute=0)
    exit_cutoff = day_start.replace(hour=15, minute=45)
    
    # Determine entry price based on type
    if fvg['type'] == 'BULLISH':
        if entry_type == 'MIDPOINT':
            limit_price = fvg['midpoint']
        else:  # CONSERVATIVE - lower boundary for longs
            limit_price = fvg['fvg_low']
        
        stop_price = fvg['c1_low'] - 0.01  # 1 cent buffer
        risk = limit_price - stop_price
        target_price = limit_price + (risk * risk_reward)
        direction = 'LONG'
    else:  # BEARISH
        if entry_type == 'MIDPOINT':
            limit_price = fvg['midpoint']
        else:  # CONSERVATIVE - upper boundary for shorts
            limit_price = fvg['fvg_high']
        
        stop_price = fvg['c1_high'] + 0.01  # 1 cent buffer
        risk = stop_price - limit_price
        target_price = limit_price - (risk * risk_reward)
        direction = 'SHORT'
    
    # Filter data after signal time
    post_signal = df_5m[df_5m.index > fvg['signal_time']]
    
    if post_signal.empty:
        return None
    
    # Simulate limit order fill
    entry_time = None
    entry_price = None
    
    for idx, candle in post_signal.iterrows():
        if idx > entry_deadline:
            break  # No fill before deadline
        
        # Check if limit would fill (price touched our level)
        if direction == 'LONG':
            if candle['low'] <= limit_price <= candle['high']:
                entry_time = idx
                entry_price = limit_price
                break
        else:  # SHORT
            if candle['low'] <= limit_price <= candle['high']:
                entry_time = idx
                entry_price = limit_price
                break
    
    if entry_time is None:
        return None  # No fill

    # Simulate exit (stop, target, or time). The trade is intraday: it never
    # looks past its own session, so a day without a 15:45 bar (half day,
    # missing data) cannot roll into the next morning's gap.
    day_end = day_start + pd.Timedelta(days=1)
    post_entry = df_5m[(df_5m.index > entry_time) & (df_5m.index < day_end)]
    exit_time = None
    exit_price = None
    result = None

    # The fill bar itself can stop the trade out. The limit sits between the
    # market and the stop, so a bar that trades through both reached the limit
    # first and the stop afterwards - that order is not ambiguous. (A target
    # touch on the fill bar IS ambiguous and is deliberately not credited.)
    entry_candle = df_5m.loc[entry_time]
    if direction == 'LONG' and entry_candle['low'] <= stop_price:
        exit_time, exit_price, result = entry_time, stop_price, 'LOSS'
    elif direction == 'SHORT' and entry_candle['high'] >= stop_price:
        exit_time, exit_price, result = entry_time, stop_price, 'LOSS'

    for idx, candle in (post_entry.iterrows() if exit_time is None else ()):
        # Time exit check first
        if idx >= exit_cutoff:
            exit_time = idx
            exit_price = candle['close']
            result = 'TIME_EXIT'
            break

        # A bar that opens beyond a level fills at the open, not at the level:
        # worse than the stop, better than the target.
        bar_open = candle['open']
        if direction == 'LONG':
            # Check stop loss (first: conservative when both are in one bar)
            if candle['low'] <= stop_price:
                exit_time = idx
                exit_price = min(stop_price, bar_open)
                result = 'LOSS'
                break
            # Check target
            if candle['high'] >= target_price:
                exit_time = idx
                exit_price = max(target_price, bar_open)
                result = 'WIN'
                break
        else:  # SHORT
            # Check stop loss
            if candle['high'] >= stop_price:
                exit_time = idx
                exit_price = max(stop_price, bar_open)
                result = 'LOSS'
                break
            # Check target
            if candle['low'] <= target_price:
                exit_time = idx
                exit_price = min(target_price, bar_open)
                result = 'WIN'
                break

    # If no exit found, use last candle of the session
    if exit_time is None and not post_entry.empty:
        last = post_entry.iloc[-1]
        exit_time = last.name
        exit_price = last['close']
        result = 'TIME_EXIT'

    if exit_time is None:
        return None

    # Gross result at the modelled levels, then the configured costs: adverse
    # exit slippage plus commission on both sides. The entry is a resting limit
    # order, so it pays commission but no slippage.
    gross_pnl = (exit_price - entry_price) if direction == 'LONG' else (entry_price - exit_price)
    r_gross = gross_pnl / risk if risk > 0 else 0
    exit_fill = exit_price
    commission = 0.0
    if cost_model is not None:
        slip = exit_price * cost_model.exit_slippage_fraction(
            'stop_loss' if result == 'LOSS' else 'take_profit'
        )
        exit_fill = exit_price - slip if direction == 'LONG' else exit_price + slip
        commission = (entry_price + exit_fill) * cost_model.commission_fraction
    pnl = ((exit_fill - entry_price) if direction == 'LONG' else (entry_price - exit_fill)) - commission
    r_multiple = pnl / risk if risk > 0 else 0

    # Determine Exit Reason Description (Clear for User)
    exit_desc = ""
    exit_details = ""
    
    # Normalize direction label for UI
    dir_label = "BUY" if direction == "LONG" else "SHORT"
    
    if result == 'WIN':
        exit_desc = f"Take Profit Hit ({dir_label})"
        exit_details = f"Price hit the profit target at {exit_price:.2f}. Strategy executed successfully."
    elif result == 'LOSS':
        exit_desc = f"Stop Loss Hit ({dir_label})"
        exit_details = f"Price hit the stop level at {exit_price:.2f} to limit losses."
    elif result == 'TIME_EXIT':
        time_str = exit_cutoff.strftime('%H:%M')
        if r_multiple > 0:
            exit_desc = "Auto-Close (Profit)"
            exit_details = f"Position auto-closed at {time_str} (EOD). Positive result."
        elif r_multiple < 0:
            exit_desc = "Auto-Close (Loss)"
            exit_details = f"Position auto-closed at {time_str} (EOD). Did not reach target."
        else:
            exit_desc = "Auto-Close (Breakeven)"
            exit_details = f"Position auto-closed flat at {time_str}."

    return {
        'date': date,
        'direction': direction,
        'fvg_type': fvg['type'],
        'entry_type': entry_type,
        'entry_time': entry_time,
        'entry_price': entry_price,
        'stop_price': stop_price,
        'target_price': target_price,
        'exit_time': exit_time,
        'exit_price': exit_fill,
        'exit_level': exit_price,        # Modelled level before slippage
        'result': result,
        'exit_reason': exit_desc,        # Brief
        'exit_details': exit_details,    # Detailed for Tooltip
        'pnl': pnl,                      # Net, per share
        'gross_pnl': gross_pnl,
        'commission': commission,
        'pnl_pct': (pnl / entry_price * 100.0) if entry_price else None,
        'r_multiple': r_multiple,        # Net of costs when a cost model is set
        'r_gross': r_gross,
        'costs_applied': cost_model is not None,
        'risk': risk,
        'fvg_zone': f"{fvg['fvg_low']:.2f} - {fvg['fvg_high']:.2f}",
        'duration_min': (exit_time - entry_time).total_seconds() / 60 if exit_time and entry_time else 0
    }


def run_orb_fvg_backtest(
    ticker: str,
    days: int = 30,
    entry_type: str = "BOTH",  # MIDPOINT, CONSERVATIVE, or BOTH
    risk_reward: float = 2.0,
    max_trades_per_day: int = 1,
    df_1min: pd.DataFrame = None,
    cost_model=None,
) -> Dict:
    """
    Run Opening Range Breakout + FVG backtest.
    
    Args:
        ticker: Stock ticker symbol
        days: Number of days to backtest
        entry_type: MIDPOINT, CONSERVATIVE, or BOTH
        risk_reward: Fixed R:R ratio (default 2:1)
        max_trades_per_day: Max trades per day
        df_1min: Pre-fetched 1-min data (optional)
    
    Returns:
        Backtest results dict
    """
    results = {
        'ticker': ticker,
        'trade_mode': 'ORB_FVG',
        'days': days,
        'entry_type': entry_type,
        'risk_reward': risk_reward,
        'trades': [],
        'wins': 0,
        'losses': 0,
        'time_exits': 0,
        'win_rate': 0.0,
        'avg_r': 0.0,
        'expectancy_r': 0.0,
        'total_r': 0.0,
        'max_drawdown_r': 0.0,
        'warnings': [],
        'optimizer_applied': False,
        'optimization_suggestions': [],
        'info': {
            'entry_type_desc': get_entry_type_description(entry_type),
            'fvg_formula': 'Bullish: C3.low > C1.high, Bearish: C3.high < C1.low',
            'timezone': 'Eastern Time (ET)',
            'opening_range': '9:30-9:45 ET',
            'entry_window': '9:45-12:00 ET',
            'exit_cutoff': '15:45 ET'
        }
    }

    # Initialize optimizer for potential improvements
    optimizer = BacktestOptimizer()

    # Trading costs. Every other backtest mode reports net-of-cost results;
    # this one used to report gross R (no commission, no slippage) under the
    # same "Expectancy" label.
    if cost_model is None:
        try:
            from core.config import ConfigManager
            from core.trading_costs import resolve_trading_cost_model

            cost_model = resolve_trading_cost_model(
                ticker, strategy_v2_config=ConfigManager().strategy_v2
            )
        except Exception as e:
            logger.warning(f"ORB_FVG cost model unavailable for {ticker}: {e}")
            cost_model = None
    if cost_model is not None:
        results['cost_model'] = cost_model.to_dict()
        results['costs_applied'] = True
    else:
        results['costs_applied'] = False
        results['warnings'].append(
            "No trading-cost model is configured for this asset: R values are gross "
            "(before commission and slippage)."
        )

    # Fetch data if not provided
    if df_1min is None:
        try:
            # Auto-adjust interval based on limits
            # 1m data available for last 7 days
            # 5m data available for last 60 days
            req_interval = '1min'
            if days > 7:
                req_interval = '5min'

            # Cap days at 59 for intraday to be safe
            if days > 59:
                days = 59
                results['warnings'].append(
                    "Period limited to 59 days: Yahoo Finance does not serve older intraday data."
                )
                results['days'] = days

            # NOTE: this previously imported a non-existent `fetch_stock_bars_yf` from
            # core.quick_backtester, which raised ImportError on every call (silently
            # swallowed into results['error']), making ORB_FVG dead in production. Use the
            # shared QuickBacktester.fetch_backtest_data, which already selects 1m/5m/1h
            # by day-range (matching the logic above) and caches the result.
            from core.quick_backtester import QuickBacktester

            qb = QuickBacktester()
            df_fetched, fetch_warnings = qb.fetch_backtest_data(
                ticker, is_crypto=False, days=days, ui_interval=req_interval
            )
            results['warnings'].extend(fetch_warnings or [])

            if df_fetched is None or len(df_fetched) < 50:
                results['error'] = f"Insufficient data for {ticker} ({req_interval})."
                return results

            df_1min = df_fetched

        except Exception as e:
            results['error'] = str(e)
            return results
    
    # Ensure we have a datetime index
    if not isinstance(df_1min.index, pd.DatetimeIndex):
        if 'timestamp' in df_1min.columns:
            df_1min = df_1min.set_index('timestamp')
        else:
            results['error'] = "DataFrame must have datetime index or 'timestamp' column"
            return results
    
    # Ensure timezone-aware index in ET
    try:
        if df_1min.index.tz is None:
            df_1min.index = df_1min.index.tz_localize('UTC').tz_convert(ET)
        else:
            df_1min.index = df_1min.index.tz_convert(ET)
    except Exception as e:
         # Fallback if conversion fails
         pass
    
    # Resample to 5min and 15min
    try:
        df_5m = resample_to_timeframe(df_1min, '5min')
        df_15m = resample_to_timeframe(df_1min, '15min')
    except Exception as e:
        results['error'] = f"Could not resample the price data: {e}"
        return results

    # Apply time filter before backtesting (for improved win rate)
    # Time filter: only consider signals during market hours (9:45-15:45)
    time_mask = optimizer.apply_time_filter(df_5m)
    df_5m = df_5m[time_mask]

    if len(df_5m) < 20:
        results['warnings'].append("⚠️ Time filter removed too many bars")

    # Log filter application (volume filter applied at signal level)
    results['filters_applied'] = {
        'time_filter': True,
        'volume_filter': 'at_signal_level',
        'volume_multiplier': 1.5,
        'original_bars': len(df_5m)
    }

    # Get unique trading days
    trading_days = df_1min.index.normalize().unique()
    
    entry_types_to_test = []
    if entry_type == 'BOTH':
        entry_types_to_test = ['MIDPOINT', 'CONSERVATIVE']
    else:
        entry_types_to_test = [entry_type]
    
    all_trades = []
    
    for etype in entry_types_to_test:
        for day in trading_days:
            date = day.date()
            
            # Build opening range
            or_high, or_low = build_opening_range(df_15m, date)
            if or_high is None:
                continue
            
            # Scan for FVG signals
            signals = scan_for_fvg_signals(df_5m, or_high, or_low, date)
            
            if not signals:
                continue
            
            # Take first N signals (max_trades_per_day)
            trades_today = 0
            for fvg in signals:
                if trades_today >= max_trades_per_day:
                    break
                
                trade = simulate_trade(fvg, df_5m, etype, risk_reward, date, cost_model=cost_model)
                if trade:
                    all_trades.append(trade)
                    trades_today += 1
    
    # Calculate metrics
    results['trades'] = all_trades

    # What the data actually covers (the requested period is not evidence).
    try:
        results['data_start'] = df_1min.index[0].isoformat()
        results['data_end'] = df_1min.index[-1].isoformat()
        results['actual_span_days'] = round(
            (df_1min.index[-1] - df_1min.index[0]).total_seconds() / 86_400, 2
        )
        results['trading_days'] = int(len(trading_days))
    except Exception:
        pass

    # Sample-size honesty: a handful of trades is an anecdote, not a result.
    min_sample = optimizer.min_trades
    counted = [
        t for t in all_trades
        if entry_type != 'BOTH' or t['entry_type'] == 'MIDPOINT'
    ]
    results['evidence'] = {
        'sample_size': len(counted),
        'minimum_sample': min_sample,
        'sufficient': len(counted) >= min_sample,
        'status': 'validated_sample_size' if len(counted) >= min_sample else 'insufficient_sample',
    }
    if len(counted) < min_sample:
        results['warnings'].append(
            f"Insufficient evidence: {len(counted)} closed trades; at least {min_sample} "
            "are needed before the metrics mean anything."
        )

    if all_trades:
        # Calculate wins based on profitability (R > 0), not just 'WIN' exit reason (which excludes profitable TIME_EXITS)
        results['wins'] = sum(1 for t in all_trades if t['r_multiple'] > 0)
        results['losses'] = sum(1 for t in all_trades if t['r_multiple'] <= 0)
        results['time_exits'] = sum(1 for t in all_trades if t['result'] == 'TIME_EXIT')
        
        total = len(all_trades)
        results['win_rate'] = round((results['wins'] / total) * 100, 1) if total > 0 else 0
        
        r_values = [t['r_multiple'] for t in all_trades]
        results['avg_r'] = round(np.mean(r_values), 2) if r_values else 0
        results['total_r'] = round(sum(r_values), 2)
        results['expectancy_r'] = results['avg_r']

        # Average Win/Loss R
        wins_r = [r for r in r_values if r > 0]
        losses_r = [r for r in r_values if r <= 0]
        results['avg_win_r'] = round(np.mean(wins_r), 2) if wins_r else 0
        results['avg_loss_r'] = round(np.mean(losses_r), 2) if losses_r else 0
        
        # Calculate Profit Factor
        gross_profit_r = sum(r for r in wins_r)
        gross_loss_r = sum(abs(r) for r in losses_r)
        
        if gross_loss_r > 0:
            results['profit_factor'] = round(gross_profit_r / gross_loss_r, 2)
        else:
            results['profit_factor'] = None

        # Calculate max drawdown in R
        cumulative_r = np.cumsum(r_values)
        peak = np.maximum.accumulate(cumulative_r)
        drawdown = peak - cumulative_r
        results['max_drawdown_r'] = round(np.max(drawdown), 2) if len(drawdown) > 0 else 0
        
        # Calculate Max Drawdown % (Assuming 1% risk per 1R)
        results['max_drawdown_pct'] = round(results['max_drawdown_r'] * 1.0, 2) 
        
        # Equity curve (Base 100, 1% risk per R)
        equity = 100.0
        equity_curve = [100.0]
        for r in r_values:
            pnl_pct = r * 0.01 
            equity = equity * (1 + pnl_pct)
            equity_curve.append(equity)
            
        results['equity_curve'] = equity_curve
        results['total_pnl_pct'] = round(equity - 100.0, 2)
        
        results['win_rate_trades'] = results['win_rate']
        
        # Duration stats
        durations = [t['duration_min'] for t in all_trades]
        results['avg_duration_min'] = round(np.mean(durations), 1) if durations else 0
        
        # Breakdown by entry type if BOTH
        if entry_type == 'BOTH':
            results['by_entry_type'] = {}
            for etype in ['MIDPOINT', 'CONSERVATIVE']:
                etype_trades = [t for t in all_trades if t['entry_type'] == etype]
                if etype_trades:
                    # Fix: Win = Profitable trade (R > 0)
                    etype_wins = sum(1 for t in etype_trades if t['r_multiple'] > 0)
                    etype_r_values = [t['r_multiple'] for t in etype_trades]
                    etype_wins_r = [r for r in etype_r_values if r > 0]
                    etype_losses_r = [r for r in etype_r_values if r <= 0]
                    
                    # Calculate Profit Factor
                    etype_gross_p = sum(r for r in etype_wins_r)
                    etype_gross_l = sum(abs(r) for r in etype_losses_r)
                    if etype_gross_l > 0:
                        etype_pf = round(etype_gross_p / etype_gross_l, 2)
                    else:
                        etype_pf = None

                    # Calculate Max Drawdown R
                    etype_cum_r = np.cumsum(etype_r_values)
                    etype_peak = np.maximum.accumulate(etype_cum_r)
                    etype_dd = etype_peak - etype_cum_r
                    etype_max_dd_r = round(np.max(etype_dd), 2) if len(etype_dd) > 0 else 0.0
                    
                    results['by_entry_type'][etype] = {
                        'trades': len(etype_trades),
                        'wins': etype_wins,
                        'win_rate': round((etype_wins / len(etype_trades)) * 100, 1),
                        'win_rate_trades': round((etype_wins / len(etype_trades)) * 100, 1),
                        'avg_r': round(np.mean(etype_r_values), 2),
                        'avg_win_r': round(np.mean(etype_wins_r), 2) if etype_wins_r else 0,
                        'avg_loss_r': round(np.mean(etype_losses_r), 2) if etype_losses_r else 0,
                        'profit_factor': etype_pf,
                        'max_drawdown_r': etype_max_dd_r,
                        'max_drawdown_pct': round(etype_max_dd_r * 1.0, 2)
                    }

    # Apply optimization analysis if we have trades
    if all_trades and len(all_trades) >= optimizer.min_trades:
        win_rate_decimal = results['win_rate'] / 100.0
        current_params = {
            "atr_multiplier_tp": risk_reward,
            "atr_multiplier_sl": 1.0
        }

        # Analyze loss patterns
        loss_analysis = optimizer.analyze_loss_patterns(all_trades)

        # Get optimization suggestions
        param_suggestions = optimizer.suggest_parameter_adjustments(current_params, win_rate_decimal)

        results['optimizer_applied'] = True
        results['optimization_suggestions'] = {
            'current_win_rate': results['win_rate'],
            'target_win_rate': 60,
            'needs_optimization': win_rate_decimal < 0.60,
            'loss_patterns': loss_analysis,
            'parameter_adjustments': param_suggestions
        }

        # Apply optimized ATR multipliers if available
        if df_1min is not None and hasattr(df_1min, 'columns') and len(df_1min) > 0:
            try:
                optimized_params = optimizer.get_optimized_params(df_1min)
                results['optimization_suggestions']['optimized_params'] = optimized_params
            except Exception as e:
                logger.warning(f"Could not apply optimized params: {e}")

    return results


def get_entry_type_description(entry_type: str) -> str:
    """Return human-readable description of entry type."""
    descriptions = {
        'MIDPOINT': 'Entry at FVG zone midpoint - more fills, average entry',
        'CONSERVATIVE': 'Entry at near limit - fewer fills, better entry price',
        'BOTH': 'Compare both entry methods (MIDPOINT and CONSERVATIVE)'
    }
    return descriptions.get(entry_type, entry_type)
