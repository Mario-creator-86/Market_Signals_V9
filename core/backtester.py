# backtester.py - backtest engine on historical data

import json
import logging
import time
from datetime import datetime
from typing import Any, Optional, Dict

import pandas as pd
import yfinance as yf
import requests
from core.http_utils import http_get

from core.config import (
    BACKTEST_RESULTS_PATH,
    ConfigManager,
    get_bucharest_time,
    normalize_ticker,
)
from core.ta import TechnicalAnalysis
from core.scoring_system import MACD_REFERENCE_PRICE, calculate_atr
from core.trading_costs import TradingCostModel, resolve_trading_cost_model

logger = logging.getLogger("MarketEngine")

BINANCE_BASE_URL = "https://api.binance.com/api/v3"


def _to_binance_symbol(ticker: str) -> str:
    t = ticker.upper().replace("-", "")
    if t.endswith("USD") and not t.endswith("USDT"):
        return t[:-3] + "USDT"
    return t


class Backtester:
    """Backtest engine for testing strategy on historical data."""

    # Indicators need a warm-up (the loop starts at bar 50) and a sample worth
    # reporting; below this the run is reported as insufficient data.
    MIN_BARS = 100

    def __init__(self, config: ConfigManager):
        self.config = config

    @staticmethod
    def _bar_time(df: pd.DataFrame, i: int) -> Optional[str]:
        """ISO timestamp of bar ``i`` (index first, then a timestamp/date column)."""
        try:
            if isinstance(df.index, pd.DatetimeIndex):
                return df.index[i].isoformat()
            for column in ("timestamp", "date", "datetime", "time"):
                if column in df.columns:
                    value = pd.Timestamp(df[column].iloc[i])
                    return None if pd.isna(value) else value.isoformat()
        except Exception:
            return None
        return None

    # ---- Paper-trading realism helpers ----

    @staticmethod
    def _simulate_execution(
        signal_type: str,
        price: float,
        cost_model: TradingCostModel,
    ) -> dict:
        """
        Simulate a transparent historical entry fill from the configured cost
        model.  OHLCV has no historical order book or account tier, so dynamic
        spread/gap penalties are intentionally not invented here.
        """
        total_slippage_pct = cost_model.entry_slippage_fraction
        commission_pct = cost_model.commission_fraction

        if signal_type == "BUY":
            fill_price = price * (1 + total_slippage_pct)
        else:
            fill_price = price * (1 - total_slippage_pct)

        return {
            "fill_price": fill_price,
            "slippage_pct": total_slippage_pct,
            "commission_pct": commission_pct,
            "gap_penalty_pct": 0.0,
        }

    # -------- Public API --------

    def run_backtest(
        self,
        ticker: str,
        days: int = 30,
        is_crypto: bool = False,
        df: Optional[pd.DataFrame] = None,
        cost_model: Optional[TradingCostModel] = None,
    ) -> Dict:
        """Run backtest on a ticker for the last N days."""
        results: Dict = {
            "wins": 0,
            "losses": 0,
            "signals": [],
            "ticker": ticker,
            "days": days,
        }

        try:
            if df is None:
                if is_crypto:
                    df = self._fetch_crypto_historical(ticker, days)
                else:
                    df = self._fetch_stock_historical(ticker, days)
            else:
                df = df.copy()

            bars = 0 if df is None else len(df)
            results["bars"] = bars
            results["min_bars_required"] = self.MIN_BARS
            if df is None or bars < self.MIN_BARS:
                # Say so explicitly: a silent "0 wins / 0 losses" is read by
                # every caller as "the strategy found nothing", which is a
                # different statement from "there was nothing to test on".
                logger.warning(f"Backtest {ticker}: insufficient data")
                results["insufficient_data"] = True
                results["warnings"] = [
                    f"Insufficient data: {bars} bars available, at least "
                    f"{self.MIN_BARS} are required. No trades were simulated."
                ]
                return results
            results["insufficient_data"] = False
            results["execution_model"] = "signal_bar_close_plus_slippage"
            data_start = self._bar_time(df, 0)
            data_end = self._bar_time(df, bars - 1)
            if data_start and data_end:
                results["data_start"] = data_start
                results["data_end"] = data_end
                try:
                    span = pd.Timestamp(data_end) - pd.Timestamp(data_start)
                    results["actual_span_days"] = round(span.total_seconds() / 86_400, 2)
                except Exception:
                    pass

            cfg = self.config.signal
            cfg_variants = self.config.signal_variants
            if cost_model is None:
                cost_model = resolve_trading_cost_model(
                    ticker,
                    strategy_v2_config=getattr(self.config, "strategy_v2", {}),
                )
            results["cost_model"] = cost_model.to_dict()

            # Calculate indicators
            df["rsi"] = TechnicalAnalysis.calc_rsi(df["close"], cfg["rsi_period"])
            macd, macd_sig, macd_hist = TechnicalAnalysis.calc_macd(
                df["close"],
                cfg["macd_fast"],
                cfg["macd_slow"],
                cfg["macd_signal"],
            )
            df["macd"], df["macd_signal"], df["macd_hist"] = macd, macd_sig, macd_hist
            df["atr"] = calculate_atr(df, period=int(cfg.get("atr_period", 14)))

            # Iterate through data looking for signals
            open_signal = None
            equity = 10000.0  # paper equity for PnL estimation
            position_size = equity * 0.1

            for i in range(50, len(df)):
                row = df.iloc[i]
                price = float(row["close"])
                high = float(row["high"])
                low = float(row["low"])
                rsi = float(row["rsi"])
                mh = float(row["macd_hist"])
                macd_val = float(row["macd"])
                macd_sig_val = float(row["macd_signal"])
                atr_val = float(df["atr"].iloc[i]) if "atr" in df.columns else 0.0
                # F2: NaN/None/0 ATR fallback. calculate_atr emits NaN during its
                # warmup (first atr_period bars) and on flat/missing data; the old
                # `if atr_val <= 0` check did NOT catch NaN (`NaN <= 0` is False),
                # so NaN propagated into _simulate_execution (vol_slippage=NaN) and
                # SL/TP sizing -> trades with NaN SL/TP never triggered an exit
                # (NaN comparisons are False) and silently fell to time_exit at the
                # last bar, understating the strategy. Fall back to a deterministic
                # volatility proxy (2% of price -- matches the default stop_loss_pct)
                # so SL/TP/exit simulation never see NaN. `not (x > 0)` catches NaN,
                # 0, and negatives (NaN > 0 is False in Python). On normal data
                # (i >= 50 >> atr_period=14) ATR is valid and this never fires.
                if not (atr_val > 0):
                    atr_val = price * 0.02

                # Check if we have an open signal
                if open_signal:
                    is_buy = open_signal["type"] == "BUY"
                    bar_open = float(row["open"])
                    # Check SL first (conservative)
                    if (is_buy and low <= open_signal["sl"]) or (not is_buy and high >= open_signal["sl"]):
                        # F1: gap-through fill at open (parity with
                        # strategy_backtester.py M2). A bar that gaps PAST the SL
                        # level fills at the (worse) open, not the level -- mirrors
                        # live gap realism (a gap-down below a long's stop fills at
                        # the lower open). Long: min(sl, open); short: max(sl, open).
                        raw_exit = open_signal["sl"]
                        exit_price = min(raw_exit, bar_open) if is_buy else max(raw_exit, bar_open)
                        # F3: exit slippage (parity with strategy_backtester.py:441-442
                        # + entry slippage symmetry). Entry pays slippage on the
                        # fill; exits must too, against the close direction -- a
                        # long close = sell-to-close -> fills lower; a short close
                        # = buy-to-cover -> fills higher. Pre-fix exits filled at
                        # the raw level while entry paid slippage -> asymmetric and
                        # overstated avg_win / shrank avg_loss magnitude.
                        exit_slip = exit_price * cost_model.exit_slippage_fraction("stop_loss")
                        exit_price = exit_price - exit_slip if is_buy else exit_price + exit_slip
                        qty = open_signal["qty"]
                        if is_buy:
                            gross_pnl = (exit_price - open_signal["entry"]) * qty
                        else:
                            gross_pnl = (open_signal["entry"] - exit_price) * qty
                        commission = (open_signal["entry"] * qty + exit_price * qty) * open_signal.get("commission_pct", 0.0)
                        net_pnl = gross_pnl - commission
                        open_signal["exit_price"] = round(exit_price, 6)
                        open_signal["gross_pnl"] = round(gross_pnl, 4)
                        open_signal["net_pnl"] = round(net_pnl, 4)
                        open_signal["commission"] = round(commission, 4)
                        open_signal["exit_time"] = self._bar_time(df, i)
                        open_signal["exit_reason"] = "stop_loss"
                        open_signal["result"] = "LOSS" if net_pnl < 0 else "WIN"
                        results["losses" if net_pnl < 0 else "wins"] += 1
                        results["signals"].append(open_signal)
                        open_signal = None
                    # Check TP
                    elif (is_buy and high >= open_signal["tp"]) or (not is_buy and low <= open_signal["tp"]):
                        # F1: gap-through fill at open (parity with M2). A bar that
                        # gaps PAST the TP level fills at the (better) open -- a
                        # gap-up above a long's TP fills at the higher open.
                        # Long: max(tp, open); short: min(tp, open).
                        raw_exit = open_signal["tp"]
                        exit_price = max(raw_exit, bar_open) if is_buy else min(raw_exit, bar_open)
                        # F3: exit slippage, same as the SL branch above.
                        exit_slip = exit_price * cost_model.exit_slippage_fraction("take_profit")
                        exit_price = exit_price - exit_slip if is_buy else exit_price + exit_slip
                        qty = open_signal["qty"]
                        if is_buy:
                            gross_pnl = (exit_price - open_signal["entry"]) * qty
                        else:
                            gross_pnl = (open_signal["entry"] - exit_price) * qty
                        commission = (open_signal["entry"] * qty + exit_price * qty) * open_signal.get("commission_pct", 0.0)
                        net_pnl = gross_pnl - commission
                        open_signal["exit_price"] = round(exit_price, 6)
                        open_signal["gross_pnl"] = round(gross_pnl, 4)
                        open_signal["net_pnl"] = round(net_pnl, 4)
                        open_signal["commission"] = round(commission, 4)
                        open_signal["exit_time"] = self._bar_time(df, i)
                        open_signal["exit_reason"] = "take_profit"
                        open_signal["result"] = "WIN" if net_pnl > 0 else "LOSS"
                        results["wins" if net_pnl > 0 else "losses"] += 1
                        results["signals"].append(open_signal)
                        open_signal = None
                    continue

                # Look for new signal
                signal_type = None
                if rsi < cfg["rsi_oversold"] and macd_val > macd_sig_val and mh > 0:
                    signal_type = "BUY"
                elif rsi > cfg["rsi_overbought"] and macd_val < macd_sig_val and mh < 0:
                    signal_type = "SHORT"

                if not signal_type:
                    continue

                variant = self._check_variant(mh, signal_type, cfg_variants, price=price)
                if not variant:
                    continue

                # Realistic execution simulation (gap measured vs previous bar's close)
                exec_info = self._simulate_execution(
                    signal_type, price, cost_model
                )
                entry_price = exec_info["fill_price"]

                exit_mode = str(cfg.get("exit_mode", "ATR")).upper()
                if exit_mode == "FIXED_PCT" or not (atr_val > 0):
                    tp_pct = float(cfg.get("profit_target_pct") or 4.0) / 100
                    sl_pct = float(cfg.get("stop_loss_pct") or 2.0) / 100
                    if signal_type == "BUY":
                        tp = entry_price * (1 + tp_pct)
                        sl = entry_price * (1 - sl_pct)
                    else:
                        tp = entry_price * (1 - tp_pct)
                        sl = entry_price * (1 + sl_pct)
                else:
                    tp_mult = float(cfg.get("atr_multiplier_tp") or 2.0)
                    sl_mult = float(cfg.get("atr_multiplier_sl") or 1.5)
                    tp_dist = atr_val * tp_mult
                    sl_dist = atr_val * sl_mult
                    if signal_type == "BUY":
                        tp = entry_price + tp_dist
                        sl = entry_price - sl_dist
                    else:
                        tp = entry_price - tp_dist
                        sl = entry_price + sl_dist

                qty = position_size / entry_price if entry_price > 0 else 0
                open_signal = {
                    "type": signal_type,
                    "variant": variant,
                    "entry": entry_price,
                    "tp": tp,
                    "sl": sl,
                    "rsi": rsi,
                    "macd_hist": mh,
                    "index": i,
                    "entry_time": self._bar_time(df, i),
                    "qty": round(qty, 6),
                    "slippage_pct": round(exec_info["slippage_pct"], 6),
                    "commission_pct": exec_info["commission_pct"],
                    "cost_model": cost_model.to_dict(),
                    "gap_penalty_pct": round(exec_info["gap_penalty_pct"], 6),
                    "gross_pnl": 0.0,
                    "net_pnl": 0.0,
                    "commission": 0.0,
                }

            # A position still open on the last bar has no outcome. It is not a
            # win or a loss, so it stays out of the statistics, but it must be
            # visible instead of vanishing.
            results["open_trades"] = 1 if open_signal else 0
            results["closed_trades"] = len(results["signals"])
            if open_signal:
                results["open_signal"] = dict(open_signal, result="OPEN")

            total = results["wins"] + results["losses"]
            results["total"] = total
            results["win_pct"] = round(results["wins"] / total * 100, 1) if total > 0 else 0.0
            results["loss_pct"] = round(results["losses"] / total * 100, 1) if total > 0 else 0.0
            results["total_gross_pnl"] = round(sum(s.get("gross_pnl", 0.0) for s in results["signals"]), 4)
            results["total_net_pnl"] = round(sum(s.get("net_pnl", 0.0) for s in results["signals"]), 4)
            results["total_commission"] = round(sum(s.get("commission", 0.0) for s in results["signals"]), 4)

        except Exception as e:
            logger.error(f"Backtest error {ticker}: {e}")
            results["error"] = str(e)

        return results

    def run_full_backtest(self, days: int = 30) -> Dict:
        """Run backtest on all tickers in the watchlist."""
        results: Dict = {
            "timestamp": get_bucharest_time().isoformat(),
            "days": days,
            "config_snapshot": {
                "signal_settings": self.config.signal,
                "signal_variants": self.config.signal_variants,
            },
            "tickers": {},
            "summary": {"total_wins": 0, "total_losses": 0, "win_pct": 0, "loss_pct": 0},
        }

        watchlist = self.config.watchlist
        # §30 survivorship limitation (documented, not pretended solved): this
        # universe is the CURRENT watchlist (current survivors). Historical
        # backtests are therefore survivorship-biased for symbols not in the
        # current watchlist and for periods before the SurvivorshipRegistry's
        # first record (core/data_model/survivorship.py). No comprehensive
        # historical delisting DB exists in V9 yet (§118). Point-in-time
        # universe filtering via SurvivorshipRegistry.universe_at(T) is wired
        # in Phase 7 (backtest consolidation). No behavior change in 1C.
        all_tickers = watchlist.get("yahoo", []) + watchlist.get("binance", [])

        # Make max_tickers configurable (default 10 for performance)
        max_tickers = self.config.backtest.get("max_tickers", 10)
        logger.info(f"🔬 Running backtest on {len(all_tickers)} tickers (max {max_tickers}) for {days} days...")

        for ticker in all_tickers[:max_tickers]:
            is_crypto = ticker.endswith("-USD") or ticker.endswith("-USDC") or ticker.endswith("USDT")
            result = self.run_backtest(ticker, days, is_crypto)
            results["tickers"][ticker] = result
            results["summary"]["total_wins"] += result.get("wins", 0)
            results["summary"]["total_losses"] += result.get("losses", 0)

        total = results["summary"]["total_wins"] + results["summary"]["total_losses"]
        if total > 0:
            results["summary"]["win_pct"] = round(
                results["summary"]["total_wins"] / total * 100, 1
            )
            results["summary"]["loss_pct"] = round(
                results["summary"]["total_losses"] / total * 100, 1
            )

        backtest_path = BACKTEST_RESULTS_PATH
        backtest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(backtest_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False, default=str)

        logger.info(
            f"✅ Backtest complete: {results['summary']['total_wins']} WIN / "
            f"{results['summary']['total_losses']} LOSS ({results['summary']['win_pct']}%)"
        )
        return results

    # -------- internal helpers --------

    def _check_variant(
        self,
        macd_hist: float,
        signal_type: str,
        cfg: dict,
        price: Optional[float] = None,
    ) -> Optional[str]:
        """
        Check which variant matches current settings.

        NOTE: This is the LEGACY rule of the non-FAITHFUL backtest modes: a
        simplified variant check that uses only the MACD histogram. It is not
        what the live engine does. Live signals take their variant (A/B/C) from
        the signal score (score bands in core.engine) and the engine's own
        histogram-threshold helper `_calculate_variant` has no caller. The
        "FAITHFUL (Crypto Live-Signal Replay)" mode does not come
        through here either: it replays the live scorer in
        core.strategy_backtester. So rescaling here cannot make a backtest
        disagree with live; it only removes the price-level bias described
        below from the legacy rule.

        The MACD histogram is in price units, so the configured thresholds
        (0.0001 .. 0.034) only mean something at the price level they were
        chosen for. Compared raw, a coin trading under ~$0.1 can never reach
        even the lowest threshold, so it could never produce a backtest trade
        (0 trades in 30 days for KAVAUSDT / PENGUUSDT), while for BTC any
        histogram at all passed the strictest one. With ``price`` given the
        histogram is rescaled to the reference price, exactly as the live
        scorer does (core.scoring_system, MACD_REFERENCE_PRICE); at that
        price the thresholds behave as before. Without ``price`` the raw
        comparison is kept for callers that have none.
        """
        try:
            px = float(price) if price is not None else 0.0
        except (TypeError, ValueError):
            px = 0.0
        if px > 0 and px == px:
            macd_hist = float(macd_hist) / px * MACD_REFERENCE_PRICE
        if signal_type == "BUY":
            if macd_hist >= cfg.get("variant_a_macd_hist_min_pos", 0.034):
                return "A"
            if macd_hist >= cfg.get("variant_b_macd_hist_min_pos", 0.032):
                return "B"
            if macd_hist >= cfg.get("variant_c_macd_hist_min_pos", 0.0001):
                return "C"
        else:
            if macd_hist <= cfg.get("variant_a_macd_hist_min_neg", -0.034):
                return "A"
            if macd_hist <= cfg.get("variant_b_macd_hist_min_neg", -0.032):
                return "B"
            if macd_hist <= cfg.get("variant_c_macd_hist_min_neg", -0.0001):
                return "C"
        return None

    def _fetch_crypto_historical(self, ticker: str, days: int) -> Optional[pd.DataFrame]:
        """Fetch historical data for crypto from Binance (1m candles)."""
        symbol = _to_binance_symbol(ticker)
        all_data = []
        limit = 1000
        total_candles = days * 24 * 60
        num_requests = (total_candles // limit) + 1

        try:
            end_time = int(datetime.now().timestamp() * 1000)
            for _ in range(min(num_requests, 30)):
                resp = http_get(
                    f"{BINANCE_BASE_URL}/klines",
                    provider="binance",
                    params={
                        "symbol": symbol,
                        "interval": "1m",
                        "limit": limit,
                        "endTime": end_time,
                    },
                    timeout=(2, 6),
                    max_retries=1,
                )
                if resp is None:
                    break
                if resp.status_code != 200:
                    break
                data = resp.json()
                if not data:
                    break
                all_data = data + all_data
                end_time = int(data[0][0]) - 1
                if len(all_data) >= total_candles:
                    break
                time.sleep(0.1)

            if not all_data:
                return None

            rows = [
                {
                    "open_time": int(k[0]),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                }
                for k in all_data
            ]
            df = pd.DataFrame(rows)
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            df = df.set_index("open_time")
            return df.sort_index()
        except Exception as e:
            logger.error(f"Backtest crypto fetch {ticker}: {e}")
            return None

    def _fetch_stock_historical(self, ticker: str, days: int) -> Optional[pd.DataFrame]:
        """Fetch historical data for stocks from Yahoo (1m/5m/1h)."""
        try:
            # Normalize ticker using global utility
            y_ticker = normalize_ticker(ticker, provider="yahoo")
            
            if days <= 7:
                interval = "1m"
            elif days <= 60:
                interval = "5m"
            else:
                interval = "1h"

            period = f"{days}d"
            data = yf.download(
                y_ticker,
                period=period,
                interval=interval,
                auto_adjust=False,
                progress=False,
            )
            if data.empty:
                if interval == "1m":
                    data = yf.download(
                        y_ticker,
                        period=period,
                        interval="5m",
                        auto_adjust=False,
                        progress=False,
                    )
                elif interval == "5m":
                    data = yf.download(
                        y_ticker,
                        period=period,
                        interval="1h",
                        auto_adjust=False,
                        progress=False,
                    )
            if data.empty:
                return None
            if isinstance(data.columns, pd.MultiIndex):
                data.columns = data.columns.get_level_values(0)
            data.columns = [col.lower() for col in data.columns]
            return data
        except Exception as e:
            logger.error(f"Backtest stock fetch {ticker}: {e}")
            return None
