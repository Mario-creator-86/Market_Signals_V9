"""
QuickBacktester - Lightweight wrapper around core.backtester.Backtester.
Provides the interface expected by agents/backtest_agent.py without FMP dependencies.
"""

import logging
import copy
import json
import time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger("QuickBacktester")


def _as_float(value: Any, default: float) -> float:
    try:
        out = float(value)
        return out if out == out else default  # NaN guard
    except (TypeError, ValueError):
        return default

# 2026-09-03 audit P0: risk_mode/trade_mode used to be pure labels here — the
# engine ran one config-default backtest and every comparison combo / walk-
# forward "best strategy" was the same result re-labelled (12 identical rows).
# These multipliers make the modes real by scaling the config-driven TP/SL the
# Backtester actually trades (signal.profit_target_pct / signal.stop_loss_pct).
# Explicit, documented convention — not research-derived values.
_RISK_MODE_MULT = {
    "STRONG": {"tp": 1.50, "sl": 1.25},
    "PRO": {"tp": 1.25, "sl": 1.00},
    "MEDIUM": {"tp": 1.00, "sl": 1.00},
    "RELAX": {"tp": 0.75, "sl": 0.85},
}
_TRADE_MODE_MULT = {
    "PULLBACK": {"tp": 1.00, "sl": 1.00},
    "BREAKOUT": {"tp": 1.10, "sl": 0.90},  # quick invalidation, extended target
    "CONTRA": {"tp": 1.50, "sl": 1.40},    # mean-reversion needs more room
}


class _ConfigShim:
    """Backtester reads config.signal / config.signal_variants / config.strategy_v2.
    Wrap the real ConfigManager and override the signal TP/SL per combo."""

    def __init__(self, base: Any, overrides: Dict[str, Any], signal_variants: Any = None):
        self._base = base
        self._overrides = overrides or {}
        self._signal_variants = copy.deepcopy(
            getattr(base, "signal_variants", {}) if signal_variants is None else signal_variants
        )

    @property
    def signal(self) -> Dict[str, Any]:
        merged = dict(getattr(self._base, "signal", {}) or {})
        merged.update(self._overrides)
        return merged

    @property
    def signal_variants(self) -> Any:
        return self._signal_variants

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)




class QuickBacktester:
    """Fast backtest wrapper for agent use. Delegates to core.backtester.Backtester."""

    def __init__(self, config: Any = None, yfinance_lock: Any = None):
        from core.backtester import Backtester
        from core.config import ConfigManager

        self.config = config or ConfigManager()
        self._backtester = Backtester(self.config)
        self._base_result_cache: Dict[Tuple[Any, ...], Tuple[float, Dict[str, Any]]] = {}
        self._data_cache: Dict[Tuple[Any, ...], Tuple[float, pd.DataFrame]] = {}
        # yfinance_lock accepted for compatibility with MarketEngine but not used currently

    def _cache_ttl_seconds(self) -> int:
        try:
            cfg = getattr(self.config, "backtest", {}) or {}
            if isinstance(cfg, dict):
                return max(0, int(cfg.get("cache_ttl_seconds", 900) or 0))
        except Exception:
            pass
        return 900

    @staticmethod
    def _df_fingerprint(df: Optional[pd.DataFrame]) -> Tuple[Any, ...]:
        if df is None or getattr(df, "empty", True):
            return ("none", 0)
        try:
            import hashlib
            # A corrected earlier high/low/volume can change entries and exits
            # even when length, last timestamp and last close are unchanged.
            content = pd.util.hash_pandas_object(df, index=True).values.tobytes()
            schema = repr([(str(col), str(dtype)) for col, dtype in zip(df.columns, df.dtypes)])
            digest = hashlib.sha256(schema.encode("utf-8") + content).hexdigest()
            return ("df", len(df), digest)
        except Exception:
            # Unhashable custom columns must miss, not share a weak cache key.
            return ("uncacheable", object())

    def _cache_get(self, cache: Dict[Tuple[Any, ...], Tuple[float, Any]], key: Tuple[Any, ...]) -> Optional[Any]:
        ttl = self._cache_ttl_seconds()
        if ttl <= 0:
            return None
        row = cache.get(key)
        if not row:
            return None
        ts, value = row
        if time.time() - ts > ttl:
            cache.pop(key, None)
            return None
        return value

    def _cache_set(self, cache: Dict[Tuple[Any, ...], Tuple[float, Any]], key: Tuple[Any, ...], value: Any) -> None:
        ttl = self._cache_ttl_seconds()
        if ttl <= 0:
            return
        if len(cache) > 128:
            cutoff = time.time() - ttl
            for old_key, (ts, _) in list(cache.items()):
                if ts < cutoff:
                    cache.pop(old_key, None)
            while len(cache) > 128:
                cache.pop(next(iter(cache)), None)
        cache[key] = (time.time(), value)

    @staticmethod
    def _compute_trade_kpis(signals: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Compute R-multiple based KPIs (profit factor, expectancy, drawdown) from closed trades."""
        trade_count = len(signals)
        if trade_count == 0:
            return {
                "trade_count": 0,
                "avg_win_r": 0.0,
                "avg_loss_r": 0.0,
                "payoff_ratio": None,
                "profit_factor": None,
                "expectancy_r": 0.0,
                "max_drawdown_pct": 0.0,
            }

        r_values: List[float] = []
        for sig in signals:
            try:
                entry = float(sig.get("entry", 0.0) or 0.0)
                sl = float(sig.get("sl", 0.0) or 0.0)
                qty = float(sig.get("qty", 0.0) or 0.0)
                net_pnl = float(sig.get("net_pnl", 0.0) or 0.0)
                risk = abs(entry - sl) * qty
                r_values.append(net_pnl / risk if risk > 0 else 0.0)
            except (TypeError, ValueError):
                r_values.append(0.0)

        wins_r = [r for r in r_values if r > 0]
        losses_r = [r for r in r_values if r <= 0]

        avg_win_r = (sum(wins_r) / len(wins_r)) if wins_r else 0.0
        avg_loss_r = (sum(losses_r) / len(losses_r)) if losses_r else 0.0

        if avg_loss_r != 0:
            payoff_ratio = avg_win_r / abs(avg_loss_r)
        else:
            payoff_ratio = None

        gross_profit_r = sum(wins_r)
        gross_loss_r = sum(abs(r) for r in losses_r)
        if gross_loss_r > 0:
            profit_factor = gross_profit_r / gross_loss_r
        else:
            profit_factor = None

        win_rate = len(wins_r) / trade_count
        loss_rate = len(losses_r) / trade_count
        expectancy_r = (win_rate * avg_win_r) - (loss_rate * abs(avg_loss_r))

        # Max peak-to-trough drawdown of cumulative R, expressed as % (1% account risk per 1R)
        cumulative = 0.0
        peak = 0.0
        max_dd_r = 0.0
        for r in r_values:
            cumulative += r
            peak = max(peak, cumulative)
            max_dd_r = max(max_dd_r, peak - cumulative)

        return {
            "trade_count": trade_count,
            "avg_win_r": round(avg_win_r, 4),
            "avg_loss_r": round(avg_loss_r, 4),
            "payoff_ratio": round(payoff_ratio, 4) if payoff_ratio is not None else None,
            "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
            "expectancy_r": round(expectancy_r, 4),
            "max_drawdown_pct": round(max_dd_r * 1.0, 2),
            # The percentage above is not an account drawdown: it is the R
            # drawdown restated under an assumed 1% of equity risked per trade.
            "max_drawdown_r": round(max_dd_r, 2),
            "max_drawdown_basis": "cumulative_net_R_assuming_1pct_risk_per_trade",
        }

    def quick_backtest(
        self,
        ticker: str,
        risk_mode: str = "MEDIUM",
        is_crypto: bool = False,
        days: int = 7,
        trade_mode: str = "PULLBACK",
        variant_mode: Optional[str] = None,
        commission_bps: Optional[float] = None,
        entry_slippage_bps: Optional[float] = None,
        slippage_sl_bps: Optional[float] = None,
        slippage_tp_bps: Optional[float] = None,
        df: Optional[pd.DataFrame] = None,
        ui_interval: str = "5min",
        exit_mode: Optional[str] = None,
        custom_tp_pct: Optional[float] = None,
        custom_sl_pct: Optional[float] = None,
        **_: Any,
    ) -> Dict[str, Any]:
        """Run a quick backtest for a single ticker."""
        try:
            from core.backtester import Backtester
            from core.trading_costs import resolve_trading_cost_model

            norm_ticker = str(ticker or "").upper().strip()
            overrides = {
                key: value
                for key, value in {
                    "commission_bps": commission_bps,
                    "entry_slippage_bps": entry_slippage_bps,
                    "slippage_sl_bps": slippage_sl_bps,
                    "slippage_tp_bps": slippage_tp_bps,
                }.items()
                if value is not None
            }
            cost_model = resolve_trading_cost_model(
                norm_ticker,
                strategy_v2_config=getattr(self.config, "strategy_v2", {}),
                overrides=overrides or None,
            )
            # 2026-09-03 audit P0: make risk_mode/trade_mode/custom TP/SL real.
            # Resolve the effective TP/SL percentages, then run the Backtester
            # against a config shim that carries them — previously these params
            # were swallowed (`**_`) and every combo produced identical results.
            signal_cfg = getattr(self.config, "signal", {}) or {}
            base_tp_pct = _as_float(signal_cfg.get("profit_target_pct"), 4.0)
            base_sl_pct = _as_float(signal_cfg.get("stop_loss_pct"), 2.0)
            eff_tp_pct = custom_tp_pct if custom_tp_pct is not None else base_tp_pct
            eff_sl_pct = custom_sl_pct if custom_sl_pct is not None else base_sl_pct
            risk_mult = _RISK_MODE_MULT.get(str(risk_mode or "MEDIUM").upper(), _RISK_MODE_MULT["MEDIUM"])
            trade_mult = _TRADE_MODE_MULT.get(str(trade_mode or "PULLBACK").upper(), _TRADE_MODE_MULT["PULLBACK"])
            tp_pct = round(eff_tp_pct * risk_mult["tp"] * trade_mult["tp"], 4)
            sl_pct = round(eff_sl_pct * risk_mult["sl"] * trade_mult["sl"], 4)
            effective_exit_mode = str(exit_mode or signal_cfg.get("exit_mode") or "ATR").upper()
            if effective_exit_mode not in {"ATR", "FIXED_PCT"}:
                raise ValueError(f"Unsupported exit mode: {effective_exit_mode}")
            # A custom TP%/SL% can only be honoured by percentage exits. In ATR
            # mode the engine sizes exits from ATR multipliers and never reads
            # the percentages, so the user's values were silently ignored.
            # An exit mode the caller names explicitly still wins.
            has_custom_exit = custom_tp_pct is not None or custom_sl_pct is not None
            if has_custom_exit and exit_mode is None:
                effective_exit_mode = "FIXED_PCT"
            custom_exit_applied = has_custom_exit and effective_exit_mode == "FIXED_PCT"
            effective_signal = dict(signal_cfg)
            effective_signal.update({
                "exit_mode": effective_exit_mode,
                "profit_target_pct": tp_pct,
                "stop_loss_pct": sl_pct,
                "atr_multiplier_tp": _as_float(signal_cfg.get("atr_multiplier_tp"), 2.0) * risk_mult["tp"] * trade_mult["tp"],
                "atr_multiplier_sl": _as_float(signal_cfg.get("atr_multiplier_sl"), 1.5) * risk_mult["sl"] * trade_mult["sl"],
            })
            # Snapshot every engine input: a configuration edit must invalidate
            # results even when candles, percentages and fees are unchanged.
            effective_variants = copy.deepcopy(getattr(self.config, "signal_variants", {}) or {})
            engine_config = _ConfigShim(self.config, copy.deepcopy(effective_signal), effective_variants)
            engine = Backtester(engine_config)
            config_fingerprint = json.dumps(
                {"signal": effective_signal, "signal_variants": effective_variants},
                sort_keys=True, allow_nan=False,
            )
            base_key = (
                "base",
                norm_ticker,
                bool(is_crypto),
                int(days or 0),
                self._df_fingerprint(df),
                tuple(sorted(cost_model.to_dict().items(), key=str)),
                tp_pct,
                sl_pct,
                config_fingerprint,
            )
            result = self._cache_get(self._base_result_cache, base_key)
            if result is None:
                result = engine.run_backtest(
                    ticker,
                    days=days,
                    is_crypto=is_crypto,
                    df=df,
                    cost_model=cost_model,
                )
                self._cache_set(self._base_result_cache, base_key, copy.deepcopy(result))
            signals = result.get("signals", [])
            wins = result.get("wins", 0)
            losses = result.get("losses", 0)
            total = wins + losses
            kpis = self._compute_trade_kpis(signals)
            # 2026-09-03 audit P1: wins/losses already count closed trades, so
            # the trade-based win rate IS win_rate — expose it instead of the
            # fabricated constant 0 the comparison table used to show.
            trade_win_rate = round(len([s for s in signals if float(s.get("net_pnl", 0.0) or 0.0) > 0]) / len(signals) * 100, 1) if signals else 0.0
            return {
                "ticker": ticker,
                "wins": wins,
                "losses": losses,
                "total_signals": total,
                "win_rate": round(wins / total * 100, 1) if total > 0 else 0.0,
                "win_rate_trades": trade_win_rate,
                "effective_tp_pct": tp_pct,
                "effective_sl_pct": sl_pct,
                "effective_exit_mode": effective_exit_mode,
                "effective_atr_multiplier_tp": effective_signal["atr_multiplier_tp"],
                "effective_atr_multiplier_sl": effective_signal["atr_multiplier_sl"],
                "simulation_scope": "RSI_MACD_ENTRIES_WITH_EXIT_PROFILE_VARIANTS",
                "custom_exit_applied": custom_exit_applied,
                "signals": copy.deepcopy(signals),
                "days": days,
                "risk_mode": risk_mode,
                "trade_mode": trade_mode,
                "variant_mode": variant_mode,
                "cost_model": result.get("cost_model", cost_model.to_dict()),
                **kpis,
                # Data coverage and run health, straight from the engine, so a
                # caller can tell "no trades found" from "nothing to test on".
                "insufficient_data": bool(result.get("insufficient_data", False)),
                "bars": result.get("bars"),
                "min_bars_required": result.get("min_bars_required"),
                "data_start": result.get("data_start"),
                "data_end": result.get("data_end"),
                "actual_span_days": result.get("actual_span_days"),
                "closed_trades": result.get("closed_trades", len(signals)),
                "open_trades": result.get("open_trades", 0),
                "execution_model": result.get("execution_model"),
                "warnings": list(result.get("warnings") or []),
                **({"error": str(result["error"])} if result.get("error") else {}),
            }
        except Exception as e:
            logger.error(f"QuickBacktester.quick_backtest error for {ticker}: {e}")
            return {
                "ticker": ticker,
                "wins": 0,
                "losses": 0,
                "total_signals": 0,
                "win_rate": 0.0,
                "signals": [],
                "days": days,
                "risk_mode": risk_mode,
                "trade_mode": trade_mode,
                "variant_mode": variant_mode,
                "trade_count": 0,
                "avg_win_r": 0.0,
                "avg_loss_r": 0.0,
                "payoff_ratio": 0.0,
                "profit_factor": 0.0,
                "expectancy_r": 0.0,
                "max_drawdown_pct": 0.0,
                "error": str(e),
            }

    def _calculate_exits(
        self,
        entry: float,
        direction: str,
        atr: float,
        trade_mode: str,
        mode: str,
        custom_tp_pct: Optional[float] = None,
        custom_sl_pct: Optional[float] = None,
    ) -> Tuple[float, float]:
        """Calculate stop-loss and take-profit prices.

        Custom percentages override ATR-based defaults when provided.
        """
        is_buy = str(direction).upper() == "BUY"

        # Default ATR multipliers by trade_mode
        mode_upper = str(trade_mode).upper()
        if mode_upper == "PULLBACK":
            sl_mult, tp_mult = 1.5, 3.0
        elif mode_upper == "BREAKOUT":
            sl_mult, tp_mult = 1.5, 3.0
        elif mode_upper == "CONTRA":
            sl_mult, tp_mult = 2.5, 5.0
        else:
            sl_mult, tp_mult = 1.5, 3.0

        # SL: custom_pct takes precedence over ATR
        if custom_sl_pct is not None:
            if is_buy:
                sl = entry * (1.0 - custom_sl_pct / 100.0)
            else:
                sl = entry * (1.0 + custom_sl_pct / 100.0)
        else:
            if is_buy:
                sl = entry - sl_mult * atr
            else:
                sl = entry + sl_mult * atr

        # TP: custom_pct takes precedence over ATR
        if custom_tp_pct is not None:
            if is_buy:
                tp = entry * (1.0 + custom_tp_pct / 100.0)
            else:
                tp = entry * (1.0 - custom_tp_pct / 100.0)
        else:
            if is_buy:
                tp = entry + tp_mult * atr
            else:
                tp = entry - tp_mult * atr

        return sl, tp

    def fetch_backtest_data(
        self,
        ticker: str,
        is_crypto: bool = False,
        days: int = 30,
        ui_interval: str = "5min",
    ) -> Tuple[Optional[pd.DataFrame], List[str]]:
        """Fetch historical OHLC data for backtesting. Returns (DataFrame, warnings)."""
        warnings: List[str] = []
        try:
            norm_ticker = str(ticker or "").upper().strip()
            cache_key = ("data", norm_ticker, bool(is_crypto), int(days or 0), str(ui_interval or ""))
            cached = self._cache_get(self._data_cache, cache_key)
            if cached is not None:
                return cached.copy(), warnings

            if is_crypto:
                df = self._backtester._fetch_crypto_historical(ticker, days)
            else:
                df = self._backtester._fetch_stock_historical(ticker, days)

            if df is None or df.empty:
                warnings.append(f"No historical data for {ticker}")
                return None, warnings

            # Normalize column names for downstream use
            df = df.copy()
            if "date" in df.columns:
                df["timestamp"] = pd.to_datetime(df["date"])
            elif "timestamp" not in df.columns:
                df["timestamp"] = pd.to_datetime(df.index)
            self._cache_set(self._data_cache, cache_key, df.copy())
            return df, warnings
        except Exception as e:
            logger.error(f"QuickBacktester.fetch_backtest_data error for {ticker}: {e}")
            warnings.append(str(e))
            return None, warnings
