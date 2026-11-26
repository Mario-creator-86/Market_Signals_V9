import math
from typing import Dict, Optional

import pandas as pd


DEFAULT_PAPER_BTC_CONFIG: Dict[str, object] = {
    "enabled": True,
    "tickers": ["BTC-USD"],
    "prefer": ["bollinger_rsi_short_4h", "donchian_short_1h"],
    "validation": {
        "enabled": True,
        "lookback_bars_1h": 720,
        "lookback_bars_4h": 360,
        "default": {
            "leverage": 1.0,
            "cost_bps_per_side": 4.0,
            "min_trades": 4,
            "min_win_rate": 45.0,
            "min_profit_factor": 1.2,
            "max_drawdown_pct": 35.0,
            "min_total_return_pct": 3.0,
        },
        "bollinger_rsi_short_4h": {
            "min_trades": 3,
            "min_win_rate": 55.0,
            "max_drawdown_pct": 20.0,
        },
        "donchian_short_1h": {
            "min_trades": 8,
            "min_profit_factor": 1.5,
            "max_drawdown_pct": 35.0,
            "min_total_return_pct": 5.0,
        },
    },
    "donchian": {
        "enabled": True,
        "fast": 18,
        "slow": 36,
        "entry_lookback": 24,
        "exit_lookback": 8,
        "rr_ratio": 2.2,
        "min_stop_pct": 0.006,
        "max_stop_pct": 0.04,
        "signal_score": 84.0,
        "position_size_pct": 5.0,
    },
    "bollinger": {
        "enabled": True,
        "fast": 12,
        "slow": 96,
        "bb_period": 24,
        "bb_std": 1.5,
        "entry_rsi": 55.0,
        "exit_rsi": 50.0,
        "rr_ratio": 1.8,
        "sl_buffer_pct": 0.0025,
        "signal_score": 88.0,
        "position_size_pct": 4.0,
    },
}


def _deep_merge(base: Dict[str, object], override: Optional[Dict[str, object]]) -> Dict[str, object]:
    merged = dict(base)
    if not isinstance(override, dict):
        return merged
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _normalize_ohlcv(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame()

    out = df.copy()
    out.columns = [str(col).lower() for col in out.columns]
    rename_map = {
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
    }
    out = out.rename(columns=rename_map)
    required = {"open", "high", "low", "close"}
    if not required.issubset(set(out.columns)):
        return pd.DataFrame()

    for col in required.union({"volume"}):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    out = out.dropna(subset=["open", "high", "low", "close"]).copy()
    out = out.sort_index()
    if len(out) > 1:
        out = out.iloc[:-1].copy()
    return out


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=int(span), adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)
    avg_gain = gains.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = losses.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, pd.NA)
    return (100.0 - (100.0 / (1.0 + rs))).fillna(50.0)


def _macd_hist(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    ema_fast = _ema(series, fast)
    ema_slow = _ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line - signal_line


def _bollinger(series: pd.Series, period: int, std_mult: float) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = series.rolling(int(period)).mean()
    std = series.rolling(int(period)).std()
    upper = mid + std * float(std_mult)
    lower = mid - std * float(std_mult)
    return mid, upper, lower


def _pct(value: float, base: float) -> float:
    if base <= 0:
        return 0.0
    return (value / base) * 100.0


def _bearish_4h_filter(df_4h: pd.DataFrame) -> Dict[str, float]:
    close = df_4h["close"]
    ema_fast = _ema(close, 18)
    ema_slow = _ema(close, 36)
    last_close = float(close.iloc[-1])
    last_fast = float(ema_fast.iloc[-1])
    last_slow = float(ema_slow.iloc[-1])
    spread_pct = _pct(abs(last_fast - last_slow), last_close)
    return {
        "bearish": bool(last_fast < last_slow),
        "ema_fast": last_fast,
        "ema_slow": last_slow,
        "spread_pct": spread_pct,
    }


def _build_4h_bearish_filter_for_1h(df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> pd.Series:
    close_4h = df_4h["close"]
    ema_fast_4h = _ema(close_4h, 18)
    ema_slow_4h = _ema(close_4h, 36)
    # Binance candles are indexed by opening time.  A 4h candle's close is not
    # known until the next 4h boundary, so expose only the preceding completed
    # 4h state to every 1h decision.
    bearish_4h = (ema_fast_4h < ema_slow_4h).astype(float).shift(1)
    aligned = bearish_4h.reindex(df_1h.index, method="ffill").fillna(0.0)
    return aligned > 0.5


def _build_donchian_short_positions(
    df_1h: pd.DataFrame,
    df_4h: pd.DataFrame,
    config: Dict[str, object],
) -> pd.Series:
    close = df_1h["close"]
    high = df_1h["high"]
    low = df_1h["low"]
    ema_fast = _ema(close, int(config["fast"]))
    ema_slow = _ema(close, int(config["slow"]))
    low_break = low.rolling(int(config["entry_lookback"])).min().shift(1)
    high_exit = high.rolling(int(config["exit_lookback"])).max().shift(1)
    bearish_4h = _build_4h_bearish_filter_for_1h(df_1h, df_4h)

    pos = 0.0
    out = []
    for i in range(len(df_1h)):
        close_px = float(close.iloc[i])
        if pos == 0.0:
            if (
                bool(bearish_4h.iloc[i])
                and not math.isnan(float(low_break.iloc[i]))
                and close_px < float(low_break.iloc[i])
                and float(ema_fast.iloc[i]) < float(ema_slow.iloc[i])
            ):
                pos = -1.0
        elif (
            close_px > float(high_exit.iloc[i])
            or float(ema_fast.iloc[i]) >= float(ema_slow.iloc[i])
            or not bool(bearish_4h.iloc[i])
        ):
            pos = 0.0
        out.append(pos)
    return pd.Series(out, index=df_1h.index)


def _build_bollinger_short_positions(df_4h: pd.DataFrame, config: Dict[str, object]) -> pd.Series:
    close = df_4h["close"]
    ema_fast = _ema(close, int(config["fast"]))
    ema_slow = _ema(close, int(config["slow"]))
    rsi = _rsi(close, 14)
    mid, upper, _ = _bollinger(close, int(config["bb_period"]), float(config["bb_std"]))

    pos = 0.0
    out = []
    for i in range(len(df_4h)):
        close_px = float(close.iloc[i])
        if pos == 0.0:
            if (
                float(ema_fast.iloc[i]) < float(ema_slow.iloc[i])
                and not math.isnan(float(upper.iloc[i]))
                and close_px >= float(upper.iloc[i])
                and float(rsi.iloc[i]) >= float(config["entry_rsi"])
            ):
                pos = -1.0
        elif (
            close_px <= float(mid.iloc[i])
            or float(rsi.iloc[i]) <= float(config["exit_rsi"])
            or float(ema_fast.iloc[i]) >= float(ema_slow.iloc[i])
        ):
            pos = 0.0
        out.append(pos)
    return pd.Series(out, index=df_4h.index)


def _extract_trade_returns(
    close: pd.Series,
    desired: pd.Series,
    leverage: float,
    cost_bps_per_side: float,
    *,
    include_open: bool = False,
) -> list[float]:
    return [
        float(trade["net_pct"])
        for trade in _extract_closed_trades(
            close,
            desired,
            leverage,
            cost_bps_per_side,
            include_open=include_open,
        )
    ]


def _extract_closed_trades(
    close: pd.Series,
    desired: pd.Series,
    leverage: float,
    cost_bps_per_side: float,
    *,
    include_open: bool = False,
) -> list[Dict[str, object]]:
    """Return realised trades with executable timestamps, excluding open risk."""
    if close.empty or desired.empty:
        return []

    index = desired.index
    if len(index) > 1:
        bar_duration = index[-1] - index[-2]
        if not isinstance(bar_duration, pd.Timedelta) or bar_duration <= pd.Timedelta(0):
            bar_duration = pd.Timedelta(0)
    else:
        bar_duration = pd.Timedelta(0)

    def execution_time(position: int):
        if position < len(index):
            return index[position]
        return index[-1] + bar_duration

    trades: list[Dict[str, object]] = []
    cost_per_change = (cost_bps_per_side / 10000.0) * leverage
    position = 0.0
    entry_price = 0.0
    entry_time = None

    for i in range(1, len(desired)):
        prev_signal = float(desired.iloc[i - 1])
        cur_signal = float(desired.iloc[i])

        if position == 0.0 and prev_signal != 0.0:
            position = prev_signal
            entry_price = float(close.iloc[i - 1])
            entry_time = execution_time(i)

        if position != 0.0 and cur_signal != position:
            exit_price = float(close.iloc[i])
            raw_move = ((exit_price / entry_price) - 1.0) * position
            turn_cost = 2.0 if cur_signal == 0.0 else 3.0
            trades.append(
                {
                    "entry_time": entry_time,
                    "exit_time": execution_time(i + 1),
                    "direction": "LONG" if position > 0 else "SHORT",
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "net_pct": (raw_move * leverage - (cost_per_change * turn_cost)) * 100.0,
                    "realized": True,
                }
            )
            position = 0.0
            entry_time = None
            if cur_signal != 0.0:
                position = cur_signal
                entry_price = exit_price
                entry_time = execution_time(i + 1)

    # A position which is still open at the end of the observed range has no
    # realised exit.  It must not contribute to validation performance.
    if position != 0.0 and include_open:
        exit_price = float(close.iloc[-1])
        raw_move = ((exit_price / entry_price) - 1.0) * position
        trades.append(
            {
                "entry_time": entry_time,
                "exit_time": execution_time(len(index)),
                "direction": "LONG" if position > 0 else "SHORT",
                "entry_price": entry_price,
                "exit_price": exit_price,
                "net_pct": (raw_move * leverage - (cost_per_change * 2.0)) * 100.0,
                "realized": False,
                "reason": "terminal_mark_to_market",
            }
        )
    return trades


def _backtest_position_series(
    df: pd.DataFrame,
    desired_position: pd.Series,
    leverage: float,
    cost_bps_per_side: float,
) -> Dict[str, object]:
    close = df["close"].astype(float)
    desired = desired_position.fillna(0.0).clip(-1.0, 1.0)
    trades = _extract_trade_returns(close, desired, leverage, cost_bps_per_side)

    if close.empty:
        return {
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "trade_count": 0,
            "win_rate": 0.0,
            "profit_factor": None,
        }

    # Validation is based on realised, completed trades only.  This avoids a
    # terminal mark-to-market value being presented as historical profit.
    closed_equity = [1.0]
    for trade_return in trades:
        closed_equity.append(closed_equity[-1] * (1.0 + (trade_return / 100.0)))
    closed_equity_series = pd.Series(closed_equity, dtype=float)

    gross_profit = sum(value for value in trades if value > 0)
    gross_loss = abs(sum(value for value in trades if value < 0))
    wins = sum(1 for value in trades if value > 0)
    max_dd = ((closed_equity_series / closed_equity_series.cummax()) - 1.0).min() * 100.0
    return {
        "total_return_pct": float((closed_equity_series.iloc[-1] - 1.0) * 100.0),
        "max_drawdown_pct": float(max_dd),
        "trade_count": len(trades),
        "win_rate": float((wins / len(trades)) * 100.0) if trades else 0.0,
        "profit_factor": float(gross_profit / gross_loss) if gross_loss > 0 else None,
    }


def _resolve_validation_config(merged: Dict[str, object], strategy_id: str) -> Dict[str, object]:
    validation = merged.get("validation", {}) or {}
    default_cfg = validation.get("default", {}) or {}
    strategy_cfg = validation.get(strategy_id, {}) or {}
    resolved = _deep_merge(default_cfg, strategy_cfg)
    resolved["enabled"] = bool(validation.get("enabled", True))
    return resolved


def _compute_expectancy_r(trade_returns: list[float]) -> Optional[float]:
    if not trade_returns:
        return None

    wins = [value for value in trade_returns if value > 0]
    losses = [abs(value) for value in trade_returns if value < 0]
    win_rate = len(wins) / len(trade_returns)
    loss_rate = len(losses) / len(trade_returns)
    if not losses:
        return None
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses)
    payoff = avg_win / avg_loss if avg_loss > 0 else 0.0
    return round(float((win_rate * payoff) - loss_rate), 4)


def _build_validation_metrics(
    strategy_id: str,
    bars_1h: pd.DataFrame,
    bars_4h: pd.DataFrame,
    merged: Dict[str, object],
) -> Dict[str, object]:
    validation_cfg = _resolve_validation_config(merged, strategy_id)
    if strategy_id == "donchian_short_1h":
        df = bars_1h
        positions = _build_donchian_short_positions(bars_1h, bars_4h, merged["donchian"])
    elif strategy_id == "bollinger_rsi_short_4h":
        df = bars_4h
        positions = _build_bollinger_short_positions(bars_4h, merged["bollinger"])
    else:
        return {"validation_passed": True, "validation_reason": "unsupported_strategy"}

    leverage = float(validation_cfg.get("leverage", 1.0) or 1.0)
    cost_bps = float(validation_cfg.get("cost_bps_per_side", 4.0) or 4.0)
    stats = _backtest_position_series(df, positions, leverage, cost_bps)
    trade_returns = _extract_trade_returns(df["close"].astype(float), positions, leverage, cost_bps)
    wins = sum(1 for value in trade_returns if value > 0)
    losses = sum(1 for value in trade_returns if value < 0)
    metrics = {
        "backtest_trade_count": int(stats["trade_count"]),
        "backtest_wins": int(wins),
        "backtest_losses": int(losses),
        "win_rate_30d": round(float(stats["win_rate"]), 4),
        "profit_factor_30d": round(float(stats["profit_factor"]), 4) if stats["profit_factor"] is not None else None,
        "expectancy_R_30d": _compute_expectancy_r(trade_returns),
        "validation_total_return_pct": round(float(stats["total_return_pct"]), 4),
        "validation_max_drawdown_pct": round(abs(float(stats["max_drawdown_pct"])), 4),
        "validation_leverage": leverage,
        "validation_cost_bps_per_side": cost_bps,
    }

    if not validation_cfg.get("enabled", True):
        metrics["validation_passed"] = True
        metrics["validation_reason"] = "validation_disabled"
        return metrics

    failures = []
    if metrics["backtest_trade_count"] < int(validation_cfg.get("min_trades", 1) or 1):
        failures.append("min_trades")
    if metrics["win_rate_30d"] < float(validation_cfg.get("min_win_rate", 0.0) or 0.0):
        failures.append("min_win_rate")
    if metrics["profit_factor_30d"] is None:
        failures.append("profit_factor_unavailable")
    elif metrics["profit_factor_30d"] < float(validation_cfg.get("min_profit_factor", 0.0) or 0.0):
        failures.append("min_profit_factor")
    if metrics["validation_total_return_pct"] < float(validation_cfg.get("min_total_return_pct", 0.0) or 0.0):
        failures.append("min_total_return")
    if metrics["validation_max_drawdown_pct"] > float(validation_cfg.get("max_drawdown_pct", 100.0) or 100.0):
        failures.append("max_drawdown")

    metrics["validation_passed"] = not failures
    metrics["validation_reason"] = "ok" if not failures else ",".join(failures)
    return metrics


def _validate_candidate(
    strategy_id: str,
    candidate: Optional[Dict[str, object]],
    bars_1h: pd.DataFrame,
    bars_4h: pd.DataFrame,
    merged: Dict[str, object],
) -> Optional[Dict[str, object]]:
    if not candidate:
        return None

    enriched = dict(candidate)
    enriched.update(_build_validation_metrics(strategy_id, bars_1h, bars_4h, merged))
    if not enriched.get("validation_passed", True):
        return None
    return enriched


def _build_signal(
    *,
    ticker: str,
    strategy_id: str,
    timeframe: str,
    candle_time,
    entry: float,
    tp: float,
    sl: float,
    rr_ratio: float,
    signal_score: float,
    rsi: float,
    macd_hist: float,
    position_size_pct: float,
    reasons: list[str],
    regime_strength: float,
    risk_mode: str,
    signal_mode: str,
    variant: str,
    extra: Optional[Dict[str, object]] = None,
) -> Optional[Dict[str, object]]:
    if not (entry > 0 and tp > 0 and sl > 0 and tp < entry < sl):
        return None

    candle_ts = candle_time.isoformat() if hasattr(candle_time, "isoformat") else str(candle_time)
    sl_pct = ((sl - entry) / entry) * 100.0
    tp_pct = ((entry - tp) / entry) * 100.0
    atr_proxy = abs(sl - entry)

    signal = {
        "ticker": ticker.upper(),
        "strategy_id": strategy_id,
        "timeframe": timeframe,
        "paper_strategy_key": f"{ticker.upper()}:{strategy_id}:{candle_ts}",
        "strategy_candle_time": candle_ts,
        "type": "SHORT",
        "entry": round(entry, 8),
        "tp": round(tp, 8),
        "sl": round(sl, 8),
        "tp_pct": round(tp_pct, 4),
        "sl_pct": round(sl_pct, 4),
        "rr_ratio": round(rr_ratio, 4),
        "signal_score": round(signal_score, 2),
        "probability_score": round(min(0.95, max(0.55, signal_score / 100.0)), 4),
        "mtf_alignment": "4h bearish filter",
        "market_regime": "BEAR_TREND",
        "regime_detected": "BEAR_TREND",
        "regime_strength": round(regime_strength, 4),
        "regime_details": {"strategy_id": strategy_id, "timeframe": timeframe},
        "variant": variant,
        "variant_description": "BTC paper strategy",
        "variant_mode": "STRICT",
        "mtf_score": 6,
        "quality_score": 5 if variant == "B" else 6,
        "rsi": round(rsi, 4),
        "macd": round(macd_hist, 6),
        "macd_hist": round(macd_hist, 6),
        "risk_mode": risk_mode,
        "signal_mode": signal_mode,
        "score": round(signal_score, 2),
        "raw_score": round(signal_score, 2),
        "signal_threshold": 60.0,
        "status": "Open",
        "exit_mode": "FIXED_PCT",
        "atr": round(atr_proxy, 8),
        "atr_value": round(atr_proxy, 8),
        "avg_atr_20d": round(atr_proxy, 8),
        "spread_estimate": round(entry * 0.0004, 8),
        "position_size_pct": round(position_size_pct, 4),
        "reasons": reasons,
        "trigger_info": {
            "strategy_id": strategy_id,
            "timeframe": timeframe,
            "reasons": reasons,
        },
    }
    if isinstance(extra, dict):
        signal.update(extra)
    return signal


def _evaluate_bollinger_short(
    ticker: str,
    df_4h: pd.DataFrame,
    regime: Dict[str, float],
    config: Dict[str, object],
) -> Optional[Dict[str, object]]:
    if len(df_4h) < 120 or not config.get("enabled", True) or not regime.get("bearish"):
        return None

    close = df_4h["close"]
    high = df_4h["high"]
    ema_fast = _ema(close, int(config["fast"]))
    ema_slow = _ema(close, int(config["slow"]))
    rsi = _rsi(close, 14)
    macd_hist = _macd_hist(close)
    mid, upper, _ = _bollinger(close, int(config["bb_period"]), float(config["bb_std"]))

    last = len(df_4h) - 1
    prev = last - 1
    if prev < 0 or math.isnan(float(upper.iloc[last])) or math.isnan(float(mid.iloc[last])):
        return None

    crossed = float(close.iloc[last]) >= float(upper.iloc[last])
    bearish = float(ema_fast.iloc[last]) < float(ema_slow.iloc[last])
    if not (crossed and bearish and float(rsi.iloc[last]) >= float(config["entry_rsi"])):
        return None

    entry = float(close.iloc[last])
    tp = float(mid.iloc[last])
    sl = max(float(high.iloc[last]), float(upper.iloc[last])) * (1.0 + float(config["sl_buffer_pct"]))
    risk = sl - entry
    if risk <= 0:
        return None
    min_tp = entry - (risk * float(config["rr_ratio"]))
    tp = min(tp, min_tp)

    return _build_signal(
        ticker=ticker,
        strategy_id="bollinger_rsi_short_4h",
        timeframe="4h",
        candle_time=df_4h.index[last],
        entry=entry,
        tp=tp,
        sl=sl,
        rr_ratio=float(config["rr_ratio"]),
        signal_score=float(config["signal_score"]),
        rsi=float(rsi.iloc[last]),
        macd_hist=float(macd_hist.iloc[last]),
        position_size_pct=float(config["position_size_pct"]),
        reasons=[
            "4h bearish regime active",
            "close stretched into upper Bollinger band",
            "RSI confirms overbought bounce inside downtrend",
        ],
        regime_strength=float(regime.get("spread_pct", 0.0)),
        risk_mode="MEDIUM",
        signal_mode="PULLBACK",
        variant="B",
        extra={"strategy_family": "paper_btc"},
    )


def _evaluate_donchian_short(
    ticker: str,
    df_1h: pd.DataFrame,
    regime: Dict[str, float],
    config: Dict[str, object],
) -> Optional[Dict[str, object]]:
    if len(df_1h) < 80 or not config.get("enabled", True) or not regime.get("bearish"):
        return None

    close = df_1h["close"]
    high = df_1h["high"]
    low = df_1h["low"]
    ema_fast = _ema(close, int(config["fast"]))
    ema_slow = _ema(close, int(config["slow"]))
    rsi = _rsi(close, 14)
    macd_hist = _macd_hist(close)
    low_break = low.rolling(int(config["entry_lookback"])).min().shift(1)
    high_exit = high.rolling(int(config["exit_lookback"])).max().shift(1)

    last = len(df_1h) - 1
    prev = last - 1
    if prev < 0 or math.isnan(float(low_break.iloc[last])) or math.isnan(float(high_exit.iloc[last])):
        return None

    breakout = float(close.iloc[last]) < float(low_break.iloc[last])
    bearish = float(ema_fast.iloc[last]) < float(ema_slow.iloc[last])
    if not (breakout and bearish):
        return None

    entry = float(close.iloc[last])
    sl = max(float(high_exit.iloc[last]), float(ema_slow.iloc[last]), entry * (1.0 + float(config["min_stop_pct"])))
    stop_pct = (sl - entry) / entry
    max_stop_pct = float(config["max_stop_pct"])
    if stop_pct > max_stop_pct:
        sl = entry * (1.0 + max_stop_pct)
        stop_pct = max_stop_pct
    if stop_pct <= 0:
        return None

    tp = entry * (1.0 - (stop_pct * float(config["rr_ratio"])))
    return _build_signal(
        ticker=ticker,
        strategy_id="donchian_short_1h",
        timeframe="1h",
        candle_time=df_1h.index[last],
        entry=entry,
        tp=tp,
        sl=sl,
        rr_ratio=float(config["rr_ratio"]),
        signal_score=float(config["signal_score"]),
        rsi=float(rsi.iloc[last]),
        macd_hist=float(macd_hist.iloc[last]),
        position_size_pct=float(config["position_size_pct"]),
        reasons=[
            "4h bearish regime active",
            "1h Donchian downside breakout confirmed",
            "fast EMA remains below slow EMA",
        ],
        regime_strength=float(regime.get("spread_pct", 0.0)),
        risk_mode="STRONG",
        signal_mode="BREAKOUT",
        variant="A",
        extra={"strategy_family": "paper_btc"},
    )


def generate_paper_btc_signal(
    ticker: str,
    df_1h: Optional[pd.DataFrame],
    df_4h: Optional[pd.DataFrame],
    config: Optional[Dict[str, object]] = None,
) -> Optional[Dict[str, object]]:
    merged = _deep_merge(DEFAULT_PAPER_BTC_CONFIG, config or {})
    ticker_upper = str(ticker or "").upper().strip()
    allowed = {str(item).upper().strip() for item in merged.get("tickers", []) or []}
    if allowed and ticker_upper not in allowed:
        return None

    bars_1h = _normalize_ohlcv(df_1h)
    bars_4h = _normalize_ohlcv(df_4h)
    if bars_1h.empty or bars_4h.empty:
        return None

    regime = _bearish_4h_filter(bars_4h)
    signals = {
        "bollinger_rsi_short_4h": _evaluate_bollinger_short(ticker_upper, bars_4h, regime, merged["bollinger"]),
        "donchian_short_1h": _evaluate_donchian_short(ticker_upper, bars_1h, regime, merged["donchian"]),
    }

    ordered_strategy_ids = []
    for strategy_id in merged.get("prefer", []):
        if strategy_id not in ordered_strategy_ids:
            ordered_strategy_ids.append(strategy_id)
    for strategy_id in signals:
        if strategy_id not in ordered_strategy_ids:
            ordered_strategy_ids.append(strategy_id)

    for strategy_id in ordered_strategy_ids:
        candidate = _validate_candidate(strategy_id, signals.get(strategy_id), bars_1h, bars_4h, merged)
        if candidate:
            return candidate
    return None
