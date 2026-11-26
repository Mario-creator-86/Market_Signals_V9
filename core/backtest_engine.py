#!/usr/bin/env python3
"""
Market Signals V7 - Backtest Engine
Complete backtesting functionality with performance metrics
"""
import json
import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from enum import Enum
import threading

logger = logging.getLogger("BacktestEngine")

class TradeDirection(Enum):
    LONG = "long"
    SHORT = "short"

class ExitReason(Enum):
    TP = "take_profit"
    SL = "stop_loss"
    TIME = "time_exit"
    SIGNAL = "signal_exit"
    MANUAL = "manual"

@dataclass
class Trade:
    entry_time: datetime
    exit_time: Optional[datetime] = None
    ticker: str = ""
    direction: TradeDirection = TradeDirection.LONG
    entry_price: float = 0.0
    exit_price: Optional[float] = None
    quantity: float = 0.0
    tp_price: float = 0.0
    sl_price: float = 0.0
    exit_reason: Optional[ExitReason] = None
    pnl: float = 0.0
    gross_pnl: float = 0.0
    commission: float = 0.0
    entry_commission: float = 0.0
    exit_commission: float = 0.0
    pnl_pct: float = 0.0
    holding_bars: int = 0
    max_drawdown_pct: float = 0.0
    max_profit_pct: float = 0.0
    
    def calculate_pnl(self) -> float:
        """Calculate P&L for the trade"""
        if self.exit_price is None:
            return 0.0
        
        if self.direction == TradeDirection.LONG:
            return (self.exit_price - self.entry_price) * self.quantity
        else:
            return (self.entry_price - self.exit_price) * self.quantity
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "ticker": self.ticker,
            "direction": self.direction.value,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "quantity": self.quantity,
            "tp_price": self.tp_price,
            "sl_price": self.sl_price,
            "exit_reason": self.exit_reason.value if self.exit_reason else None,
            "pnl": self.pnl,
            "gross_pnl": self.gross_pnl,
            "commission": self.commission,
            "entry_commission": self.entry_commission,
            "exit_commission": self.exit_commission,
            "pnl_pct": self.pnl_pct,
            "holding_bars": self.holding_bars,
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_profit_pct": self.max_profit_pct,
        }

@dataclass
class BacktestResult:
    """Complete backtest results"""
    strategy_name: str
    ticker: str
    start_date: datetime
    end_date: datetime
    initial_capital: float
    final_capital: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    profit_factor: Optional[float]
    total_return: float
    total_return_pct: float
    max_drawdown: float
    max_drawdown_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    avg_trade_pnl: float
    avg_winning_trade: float
    avg_losing_trade: float
    largest_win: float
    largest_loss: float
    avg_holding_bars: float
    equity_curve: List[Dict[str, Any]] = field(default_factory=list)
    trades: List[Trade] = field(default_factory=list)
    monthly_returns: Dict[str, float] = field(default_factory=dict)
    cost_model: Dict[str, Any] = field(default_factory=dict)
    validation_scope: str = "exploratory_legacy_strategy"
    autonomous_execution_eligible: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "ticker": self.ticker,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "initial_capital": self.initial_capital,
            "final_capital": self.final_capital,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "total_return": self.total_return,
            "total_return_pct": self.total_return_pct,
            "max_drawdown": self.max_drawdown,
            "max_drawdown_pct": self.max_drawdown_pct,
            "sharpe_ratio": self.sharpe_ratio,
            "sortino_ratio": self.sortino_ratio,
            "calmar_ratio": self.calmar_ratio,
            "avg_trade_pnl": self.avg_trade_pnl,
            "avg_winning_trade": self.avg_winning_trade,
            "avg_losing_trade": self.avg_losing_trade,
            "largest_win": self.largest_win,
            "largest_loss": self.largest_loss,
            "avg_holding_bars": self.avg_holding_bars,
            "equity_curve": self.equity_curve,
            "trades": [t.to_dict() for t in self.trades],
            "monthly_returns": self.monthly_returns,
            "cost_model": self.cost_model,
            "validation_scope": self.validation_scope,
            "autonomous_execution_eligible": self.autonomous_execution_eligible,
        }

class BacktestEngine:
    """
    Complete backtest engine for strategies
    """
    
    def __init__(self, cache_dir: Optional[Path] = None):
        if cache_dir is None:
            from core.config import data_path  # configured data folder (MS_DATA_DIR)
            cache_dir = data_path("backtests")
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data_cache: Dict[str, pd.DataFrame] = {}
        
    def fetch_historical_data(
        self,
        ticker: str,
        start_date: datetime,
        end_date: datetime,
        timeframe: str = "1d",
    ) -> Optional[pd.DataFrame]:
        """Fetch historical price data"""
        cache_key = f"{ticker}_{timeframe}_{start_date.date()}_{end_date.date()}"
        
        with self._lock:
            if cache_key in self._data_cache:
                return self._data_cache[cache_key]
        
        try:
            # Try to load from yfinance
            import yfinance as yf
            
            df = yf.download(
                ticker,
                start=start_date,
                end=end_date,
                interval=timeframe,
                progress=False,
            )
            
            if df.empty:
                logger.warning(f"No data returned for {ticker}")
                return None
            
            # Standardize column names (handle MultiIndex from yfinance)
            if isinstance(df.columns, pd.MultiIndex):
                # Flatten MultiIndex columns
                df.columns = [f"{col[0].lower()}" for col in df.columns]
            else:
                df.columns = [c.lower().replace(" ", "_") for c in df.columns]
            
            # Ensure required columns exist
            required = ["open", "high", "low", "close", "volume"]
            for col in required:
                if col not in df.columns:
                    logger.error(f"Missing required column {col} for {ticker}")
                    return None
            
            with self._lock:
                self._data_cache[cache_key] = df
            
            return df
            
        except Exception as e:
            logger.error(f"Failed to fetch data for {ticker}: {e}")
            return None
    
    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate technical indicators"""
        df = df.copy()
        
        # Moving averages
        df["sma_20"] = df["close"].rolling(window=20).mean()
        df["sma_50"] = df["close"].rolling(window=50).mean()
        df["ema_12"] = df["close"].ewm(span=12).mean()
        df["ema_26"] = df["close"].ewm(span=26).mean()
        
        # MACD
        df["macd"] = df["ema_12"] - df["ema_26"]
        df["macd_signal"] = df["macd"].ewm(span=9).mean()
        df["macd_hist"] = df["macd"] - df["macd_signal"]
        
        # RSI
        delta = df["close"].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df["rsi"] = 100 - (100 / (1 + rs))
        
        # Bollinger Bands
        df["bb_middle"] = df["close"].rolling(window=20).mean()
        bb_std = df["close"].rolling(window=20).std()
        df["bb_upper"] = df["bb_middle"] + (bb_std * 2)
        df["bb_lower"] = df["bb_middle"] - (bb_std * 2)
        
        # ATR
        high_low = df["high"] - df["low"]
        high_close = np.abs(df["high"] - df["close"].shift())
        low_close = np.abs(df["low"] - df["close"].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = np.max(ranges, axis=1)
        df["atr"] = true_range.rolling(window=14).mean()
        
        # Volume
        df["volume_sma"] = df["volume"].rolling(window=20).mean()
        
        return df
    
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Generate trading signals based on indicators"""
        df = df.copy()
        df["signal"] = 0  # 0 = no signal, 1 = buy, -1 = sell
        
        # Buy conditions
        buy_cond = (
            (df["macd"] > df["macd_signal"]) &  # MACD crossover
            (df["rsi"] > 30) & (df["rsi"] < 70) &  # RSI not extreme
            (df["close"] > df["sma_20"])  # Above 20 SMA
        )
        
        # Sell conditions
        sell_cond = (
            (df["macd"] < df["macd_signal"]) &  # MACD crossunder
            (df["rsi"] > 70)  # Overbought
        )
        
        df.loc[buy_cond, "signal"] = 1
        df.loc[sell_cond, "signal"] = -1
        
        return df
    
    @staticmethod
    def _bars_per_year(timeframe: str) -> float:
        """Calculate bars per year based on timeframe for proper annualization."""
        tf = timeframe.lower().strip()
        if tf in ("1h", "60m", "60min"):
            return 252 * 6.5
        elif tf in ("5m", "5min"):
            return 252 * 6.5 * 12
        elif tf in ("15m", "15min"):
            return 252 * 6.5 * 4
        elif tf in ("30m", "30min"):
            return 252 * 6.5 * 2
        # Intervals Yahoo serves that used to fall through to "daily" (252):
        # a 1-minute Sharpe was annualised as if each bar were a day,
        # understating it by sqrt(390) ~ 20x; weekly/monthly were overstated.
        elif tf in ("1m", "1min"):
            return 252 * 6.5 * 60
        elif tf in ("2m", "2min"):
            return 252 * 6.5 * 30
        elif tf in ("90m", "90min"):
            return 252 * 6.5 / 1.5
        elif tf in ("4h", "240m"):
            return 252 * 6.5 / 4
        elif tf in ("1wk", "1w"):
            return 52
        elif tf in ("1mo", "1mth"):
            return 12
        else:
            return 252  # default: daily

    def run_backtest(
        self,
        ticker: str,
        start_date: datetime,
        end_date: datetime,
        initial_capital: float = 10000.0,
        position_size_pct: float = 0.1,
        commission_pct: Optional[float] = None,
        tp_pct: float = 0.06,
        sl_pct: float = 0.03,
        max_holding_bars: int = 20,
        strategy_name: str = "MACD_RSI",
        timeframe: str = "1d",
    ) -> Optional[BacktestResult]:
        """Run complete backtest with LONG and SHORT support."""
        logger.info(f"Starting backtest for {ticker} from {start_date} to {end_date}")

        configured_cost_model = None
        if commission_pct is None:
            from core.config import ConfigManager
            from core.trading_costs import resolve_trading_cost_model

            configured_cost_model = resolve_trading_cost_model(
                ticker,
                strategy_v2_config=ConfigManager().strategy_v2,
            )
            effective_commission_pct = configured_cost_model.commission_fraction
            cost_model = configured_cost_model.to_dict()
            cost_model["legacy_engine_limit"] = (
                "This exploratory MACD/RSI engine applies only configured commission; "
                "it does not model historical bid/ask, slippage, funding, or the live signal engine."
            )
        else:
            effective_commission_pct = float(commission_pct)
            if effective_commission_pct < 0:
                raise ValueError("commission_pct must be non-negative")
            cost_model = {
                "commission_bps_per_side": effective_commission_pct * 10_000.0,
                "provenance": "explicit_request_override_estimate",
                "is_realized_exchange_fee": False,
                "reconciliation_required": "User-supplied commission only; slippage, funding and actual fills are excluded.",
                "legacy_engine_limit": "Exploratory MACD/RSI engine; not the live signal replay.",
            }

        bars_per_year = self._bars_per_year(timeframe)
        ann_factor = np.sqrt(bars_per_year)

        df = self.fetch_historical_data(ticker, start_date, end_date, timeframe=timeframe)
        if df is None or len(df) < 50:
            logger.error(f"Insufficient data for {ticker}")
            return None

        df = self.calculate_indicators(df)
        df = self.generate_signals(df)

        trades: List[Trade] = []
        equity = initial_capital
        peak_equity = initial_capital
        max_drawdown = 0.0
        max_drawdown_pct = 0.0
        equity_curve = []
        in_position = False
        current_trade: Optional[Trade] = None
        pending_direction: Optional[TradeDirection] = None

        entry_slippage_fraction = (
            configured_cost_model.entry_slippage_fraction
            if configured_cost_model is not None
            else 0.0
        )

        def close_trade(
            trade: Trade,
            *,
            raw_exit: float,
            reason: ExitReason,
            timestamp: datetime,
        ) -> tuple[float, float]:
            reason_value = reason.value
            exit_slippage_fraction = (
                configured_cost_model.exit_slippage_fraction(reason_value)
                if configured_cost_model is not None
                else 0.0
            )
            if trade.direction == TradeDirection.LONG:
                exit_fill = raw_exit * (1.0 - exit_slippage_fraction)
            else:
                exit_fill = raw_exit * (1.0 + exit_slippage_fraction)

            trade.exit_time = timestamp
            trade.exit_price = exit_fill
            trade.exit_reason = reason
            trade.gross_pnl = trade.calculate_pnl()
            trade.exit_commission = exit_fill * trade.quantity * effective_commission_pct
            trade.commission = trade.entry_commission + trade.exit_commission
            trade.pnl = trade.gross_pnl - trade.commission
            entry_notional = trade.entry_price * trade.quantity
            trade.pnl_pct = trade.pnl / entry_notional if entry_notional > 0 else 0.0
            return trade.gross_pnl, trade.exit_commission

        for i in range(50, len(df)):
            row = df.iloc[i]
            timestamp = df.index[i]
            current_price = float(row["close"])

            # A signal is only known after its bar closes. Enter at the next
            # bar's open, with the configured adverse entry slippage.
            if not in_position and pending_direction is not None:
                raw_entry = float(row["open"]) if "open" in row.index else current_price
                if pending_direction == TradeDirection.LONG:
                    entry_fill = raw_entry * (1.0 + entry_slippage_fraction)
                else:
                    entry_fill = raw_entry * (1.0 - entry_slippage_fraction)
                position_value = equity * position_size_pct
                if position_value > 0 and entry_fill > 0:
                    quantity = position_value / entry_fill
                    is_long_entry = pending_direction == TradeDirection.LONG
                    current_trade = Trade(
                        entry_time=timestamp,
                        ticker=ticker,
                        direction=pending_direction,
                        entry_price=entry_fill,
                        quantity=quantity,
                        tp_price=entry_fill * (1 + tp_pct) if is_long_entry else entry_fill * (1 - tp_pct),
                        sl_price=entry_fill * (1 - sl_pct) if is_long_entry else entry_fill * (1 + sl_pct),
                    )
                    current_trade.entry_commission = position_value * effective_commission_pct
                    equity -= current_trade.entry_commission
                    in_position = True
                pending_direction = None

            if in_position and current_trade:
                current_trade.holding_bars += 1

                if current_trade.direction == TradeDirection.LONG:
                    unrealized_pct = (current_price - current_trade.entry_price) / current_trade.entry_price
                else:
                    unrealized_pct = (current_trade.entry_price - current_price) / current_trade.entry_price

                current_trade.max_profit_pct = max(current_trade.max_profit_pct, unrealized_pct)
                current_trade.max_drawdown_pct = max(current_trade.max_drawdown_pct, -unrealized_pct)

                exit_triggered = False
                exit_reason = None
                exit_fill = current_price

                bar_open = float(row["open"]) if "open" in row.index else current_price
                bar_high = float(row["high"]) if "high" in row.index else current_price
                bar_low = float(row["low"]) if "low" in row.index else current_price

                # Direction-aware TP/SL on intrabar extremes (high/low).
                # SL is checked first (conservative) and fills at the level,
                # or at the bar open when price gapped through the level.
                if current_trade.direction == TradeDirection.LONG:
                    if bar_low <= current_trade.sl_price:
                        exit_triggered, exit_reason = True, ExitReason.SL
                        exit_fill = min(current_trade.sl_price, bar_open)
                    elif bar_high >= current_trade.tp_price:
                        exit_triggered, exit_reason = True, ExitReason.TP
                        exit_fill = max(current_trade.tp_price, bar_open)
                else:
                    if bar_high >= current_trade.sl_price:
                        exit_triggered, exit_reason = True, ExitReason.SL
                        exit_fill = max(current_trade.sl_price, bar_open)
                    elif bar_low <= current_trade.tp_price:
                        exit_triggered, exit_reason = True, ExitReason.TP
                        exit_fill = min(current_trade.tp_price, bar_open)

                if not exit_triggered and current_trade.holding_bars >= max_holding_bars:
                    exit_triggered, exit_reason = True, ExitReason.TIME

                if not exit_triggered:
                    if current_trade.direction == TradeDirection.LONG and row["signal"] == -1:
                        exit_triggered, exit_reason = True, ExitReason.SIGNAL
                    elif current_trade.direction == TradeDirection.SHORT and row["signal"] == 1:
                        exit_triggered, exit_reason = True, ExitReason.SIGNAL

                if exit_triggered:
                    gross_pnl, exit_commission = close_trade(
                        current_trade,
                        raw_exit=float(exit_fill),
                        reason=exit_reason,
                        timestamp=timestamp,
                    )
                    trades.append(current_trade)
                    # Entry commission was deducted when the position opened.
                    equity += gross_pnl - exit_commission

                    in_position = False
                    current_trade = None

            # Mark the remaining position to market net of its estimated exit
            # commission, then update the curve and drawdown.
            if in_position and current_trade:
                if current_trade.direction == TradeDirection.LONG:
                    unrealized_pnl = (current_price - current_trade.entry_price) * current_trade.quantity
                else:
                    unrealized_pnl = (current_trade.entry_price - current_price) * current_trade.quantity
                estimated_exit_commission = (
                    current_price * current_trade.quantity * effective_commission_pct
                )
                portfolio_value = equity + unrealized_pnl - estimated_exit_commission
            else:
                portfolio_value = equity

            if portfolio_value > 0:
                peak_equity = max(peak_equity, portfolio_value)
                current_dd = peak_equity - portfolio_value
                max_drawdown = max(max_drawdown, current_dd)
                current_dd_pct = (current_dd / peak_equity) * 100 if peak_equity > 0 else 0.0
                max_drawdown_pct = max(max_drawdown_pct, current_dd_pct)
            equity_curve.append({"timestamp": timestamp.isoformat(), "equity": portfolio_value})

            # The current close can schedule an entry only for the next bar.
            if not in_position:
                if row["signal"] == 1:
                    pending_direction = TradeDirection.LONG
                elif row["signal"] == -1:
                    pending_direction = TradeDirection.SHORT
                else:
                    pending_direction = None

        # Do not silently discard a position that remains open at the dataset
        # boundary. Liquidate it at the final close as a modelled time exit.
        if in_position and current_trade and len(df) > 0:
            final_row = df.iloc[-1]
            final_timestamp = df.index[-1]
            gross_pnl, exit_commission = close_trade(
                current_trade,
                raw_exit=float(final_row["close"]),
                reason=ExitReason.TIME,
                timestamp=final_timestamp,
            )
            trades.append(current_trade)
            equity += gross_pnl - exit_commission
            if equity_curve:
                equity_curve[-1]["equity"] = equity
            peak_equity = max(peak_equity, equity)
            final_dd = max(0.0, peak_equity - equity)
            max_drawdown = max(max_drawdown, final_dd)
            max_drawdown_pct = max(
                max_drawdown_pct,
                (final_dd / peak_equity) * 100 if peak_equity > 0 else 0.0,
            )

        # Metrics
        winning_trades = [t for t in trades if t.pnl > 0]
        losing_trades = [t for t in trades if t.pnl <= 0]
        total_profit = sum(t.pnl for t in winning_trades)
        total_loss = abs(sum(t.pnl for t in losing_trades))

        monthly_returns: Dict[str, float] = {}
        for trade in trades:
            if trade.exit_time:
                month_key = trade.exit_time.strftime("%Y-%m")
                if month_key not in monthly_returns:
                    monthly_returns[month_key] = 0.0
                monthly_returns[month_key] += trade.pnl

        returns = []
        if len(equity_curve) > 1:
            for i in range(1, len(equity_curve)):
                prev = equity_curve[i-1]["equity"]
                curr = equity_curve[i]["equity"]
                if prev > 0:
                    returns.append((curr - prev) / prev)

        if returns:
            avg_return = np.mean(returns)
            std_return = np.std(returns, ddof=1) if len(returns) > 1 else 0.0
            sharpe = (avg_return / std_return) * ann_factor if std_return > 0 else 0.0
        else:
            avg_return = 0.0
            sharpe = 0.0

        # Sortino (downside deviation only)
        # Correct formula: downside_dev = sqrt(mean(min(r, 0)^2))
        # Uses ALL returns, not just negative ones, and centers on 0 (not mean of negatives)
        if returns:
            downside = np.minimum(returns, 0.0)
            downside_dev = np.sqrt(np.mean(downside ** 2))
            sortino = (avg_return / downside_dev) * ann_factor if downside_dev > 0 else 0.0
        else:
            sortino = 0.0

        result = BacktestResult(
            strategy_name=strategy_name, ticker=ticker, start_date=start_date, end_date=end_date,
            initial_capital=initial_capital, final_capital=equity,
            total_trades=len(trades), winning_trades=len(winning_trades), losing_trades=len(losing_trades),
            win_rate=len(winning_trades) / len(trades) * 100 if trades else 0.0,
            profit_factor=total_profit / total_loss if total_loss > 0 else None,
            total_return=equity - initial_capital,
            total_return_pct=(equity - initial_capital) / initial_capital * 100,
            max_drawdown=max_drawdown, max_drawdown_pct=max_drawdown_pct,
            sharpe_ratio=sharpe, sortino_ratio=sortino,
            calmar_ratio=(equity - initial_capital) / max_drawdown if max_drawdown > 0 else 0.0,
            avg_trade_pnl=np.mean([t.pnl for t in trades]) if trades else 0.0,
            avg_winning_trade=np.mean([t.pnl for t in winning_trades]) if winning_trades else 0.0,
            avg_losing_trade=np.mean([t.pnl for t in losing_trades]) if losing_trades else 0.0,
            largest_win=max([t.pnl for t in trades]) if trades else 0.0,
            largest_loss=min([t.pnl for t in trades]) if trades else 0.0,
            avg_holding_bars=np.mean([t.holding_bars for t in trades]) if trades else 0.0,
            equity_curve=equity_curve, trades=trades, monthly_returns=monthly_returns,
            cost_model=cost_model,
            validation_scope="exploratory_legacy_strategy",
            autonomous_execution_eligible=False,
        )

        logger.info(f"Backtest completed: {len(trades)} trades, {result.win_rate:.1f}% win rate")
        return result
    def segment_results(self, result: BacktestResult) -> Dict[str, Any]:
        """
        Segment backtest results by market regime, asset class, setup type, and exit reason.
        Returns a structured breakdown for the paper-trading agent.
        """
        trades = result.trades
        if not trades:
            return {"summary": "No trades to segment"}

        # Asset class inference
        def _asset_class(ticker: str) -> str:
            t = ticker.upper()
            if t.endswith("USDT") or t.endswith("USD") or t in {"BTC", "ETH", "SOL", "XRP"}:
                return "crypto"
            return "stock"

        # Holding period buckets
        def _holding_bucket(bars: int) -> str:
            if bars <= 3:
                return "very_short"
            elif bars <= 7:
                return "short"
            elif bars <= 15:
                return "medium"
            else:
                return "long"

        segments: Dict[str, Any] = {
            "by_exit_reason": {},
            "by_direction": {},
            "by_asset_class": {},
            "by_holding_period": {},
            "by_month": {},
            "win_streaks": {"max_consecutive_wins": 0, "max_consecutive_losses": 0},
        }

        # Direction segmentation
        for direction in ["long", "short"]:
            dir_trades = [t for t in trades if t.direction.value == direction]
            wins = [t for t in dir_trades if t.pnl > 0]
            segments["by_direction"][direction] = {
                "count": len(dir_trades),
                "win_rate": len(wins) / len(dir_trades) * 100 if dir_trades else 0.0,
                "avg_pnl": float(np.mean([t.pnl for t in dir_trades])) if dir_trades else 0.0,
                "avg_pnl_pct": float(np.mean([t.pnl_pct for t in dir_trades])) if dir_trades else 0.0,
                "total_pnl": sum(t.pnl for t in dir_trades),
                "max_drawdown_pct": max([t.max_drawdown_pct for t in dir_trades]) if dir_trades else 0.0,
            }

        # Exit reason segmentation
        reasons: Dict[str, List[Trade]] = {}
        for t in trades:
            r = str(t.exit_reason.value if t.exit_reason else "unknown")
            reasons.setdefault(r, []).append(t)
        for r, r_trades in reasons.items():
            wins = [t for t in r_trades if t.pnl > 0]
            segments["by_exit_reason"][r] = {
                "count": len(r_trades),
                "win_rate": len(wins) / len(r_trades) * 100 if r_trades else 0.0,
                "avg_pnl": float(np.mean([t.pnl for t in r_trades])) if r_trades else 0.0,
                "total_pnl": sum(t.pnl for t in r_trades),
            }

        # Asset class segmentation
        asset_classes: Dict[str, List[Trade]] = {}
        for t in trades:
            ac = _asset_class(t.ticker)
            asset_classes.setdefault(ac, []).append(t)
        for ac, ac_trades in asset_classes.items():
            wins = [t for t in ac_trades if t.pnl > 0]
            segments["by_asset_class"][ac] = {
                "count": len(ac_trades),
                "win_rate": len(wins) / len(ac_trades) * 100 if ac_trades else 0.0,
                "avg_pnl": float(np.mean([t.pnl for t in ac_trades])) if ac_trades else 0.0,
                "avg_pnl_pct": float(np.mean([t.pnl_pct for t in ac_trades])) if ac_trades else 0.0,
                "total_pnl": sum(t.pnl for t in ac_trades),
                "tickers": list(set(t.ticker for t in ac_trades)),
            }

        # Holding period segmentation
        hp_buckets: Dict[str, List[Trade]] = {}
        for t in trades:
            b = _holding_bucket(t.holding_bars)
            hp_buckets.setdefault(b, []).append(t)
        for b, b_trades in hp_buckets.items():
            wins = [t for t in b_trades if t.pnl > 0]
            segments["by_holding_period"][b] = {
                "count": len(b_trades),
                "win_rate": len(wins) / len(b_trades) * 100 if b_trades else 0.0,
                "avg_pnl": float(np.mean([t.pnl for t in b_trades])) if b_trades else 0.0,
                "avg_pnl_pct": float(np.mean([t.pnl_pct for t in b_trades])) if b_trades else 0.0,
            }

        # Monthly segmentation
        months: Dict[str, List[Trade]] = {}
        for t in trades:
            if t.exit_time:
                m = t.exit_time.strftime("%Y-%m")
                months.setdefault(m, []).append(t)
        for m, m_trades in months.items():
            wins = [t for t in m_trades if t.pnl > 0]
            segments["by_month"][m] = {
                "count": len(m_trades),
                "win_rate": len(wins) / len(m_trades) * 100 if m_trades else 0.0,
                "total_pnl": sum(t.pnl for t in m_trades),
            }

        # Win/loss streaks
        max_wins = 0
        max_losses = 0
        current_wins = 0
        current_losses = 0
        for t in trades:
            if t.pnl > 0:
                current_wins += 1
                current_losses = 0
                max_wins = max(max_wins, current_wins)
            else:
                current_losses += 1
                current_wins = 0
                max_losses = max(max_losses, current_losses)
        segments["win_streaks"]["max_consecutive_wins"] = max_wins
        segments["win_streaks"]["max_consecutive_losses"] = max_losses

        return segments

    def save_backtest(self, result: BacktestResult, filename: Optional[str] = None) -> Path:
        """Save backtest results to file"""
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"backtest_{result.ticker}_{result.strategy_name}_{timestamp}.json"
        
        filepath = self.cache_dir / filename
        
        with open(filepath, 'w') as f:
            json.dump(result.to_dict(), f, indent=2)
        
        logger.info(f"Backtest saved to {filepath}")
        return filepath
    
    def load_backtest(self, filepath: Path) -> Optional[BacktestResult]:
        """Load backtest results from file"""
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
            
            # Reconstruct trades
            trades = []
            for t_data in data.get("trades", []):
                trade = Trade(
                    entry_time=datetime.fromisoformat(t_data["entry_time"]) if t_data["entry_time"] else None,
                    exit_time=datetime.fromisoformat(t_data["exit_time"]) if t_data["exit_time"] else None,
                    ticker=t_data["ticker"],
                    direction=TradeDirection(t_data["direction"]),
                    entry_price=t_data["entry_price"],
                    exit_price=t_data.get("exit_price"),
                    quantity=t_data["quantity"],
                    tp_price=t_data["tp_price"],
                    sl_price=t_data["sl_price"],
                    exit_reason=ExitReason(t_data["exit_reason"]) if t_data["exit_reason"] else None,
                    pnl=t_data["pnl"],
                    pnl_pct=t_data["pnl_pct"],
                    holding_bars=t_data["holding_bars"],
                    max_drawdown_pct=t_data["max_drawdown_pct"],
                    max_profit_pct=t_data["max_profit_pct"],
                )
                trades.append(trade)
            
            result = BacktestResult(
                strategy_name=data["strategy_name"],
                ticker=data["ticker"],
                start_date=datetime.fromisoformat(data["start_date"]),
                end_date=datetime.fromisoformat(data["end_date"]),
                initial_capital=data["initial_capital"],
                final_capital=data["final_capital"],
                total_trades=data["total_trades"],
                winning_trades=data["winning_trades"],
                losing_trades=data["losing_trades"],
                win_rate=data["win_rate"],
                profit_factor=data["profit_factor"],
                total_return=data["total_return"],
                total_return_pct=data["total_return_pct"],
                max_drawdown=data["max_drawdown"],
                max_drawdown_pct=data["max_drawdown_pct"],
                sharpe_ratio=data["sharpe_ratio"],
                sortino_ratio=data["sortino_ratio"],
                calmar_ratio=data["calmar_ratio"],
                avg_trade_pnl=data["avg_trade_pnl"],
                avg_winning_trade=data["avg_winning_trade"],
                avg_losing_trade=data["avg_losing_trade"],
                largest_win=data["largest_win"],
                largest_loss=data["largest_loss"],
                avg_holding_bars=data["avg_holding_bars"],
                equity_curve=data.get("equity_curve", []),
                trades=trades,
                monthly_returns=data.get("monthly_returns", {}),
            )
            
            return result
            
        except Exception as e:
            logger.error(f"Failed to load backtest: {e}")
            return None


# Singleton instance
_backtest_engine: Optional[BacktestEngine] = None

def get_backtest_engine() -> BacktestEngine:
    """Get or create singleton backtest engine"""
    global _backtest_engine
    if _backtest_engine is None:
        _backtest_engine = BacktestEngine()
    return _backtest_engine


if __name__ == "__main__":
    # Example usage
    engine = get_backtest_engine()
    
    result = engine.run_backtest(
        ticker="AAPL",
        start_date=datetime.now() - timedelta(days=365),
        end_date=datetime.now(),
        initial_capital=10000,
    )
    
    if result:
        print(f"Total Return: {result.total_return_pct:.2f}%")
        print(f"Win Rate: {result.win_rate:.1f}%")
        print(f"Total Trades: {result.total_trades}")
        print(f"Sharpe Ratio: {result.sharpe_ratio:.2f}")
        
        # Save results
        engine.save_backtest(result)
