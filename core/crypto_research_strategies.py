"""Pre-registered, price-only crypto strategy research.

This module is intentionally separate from the live signal engine.  Its small
set of fixed specifications lets us investigate alternatives without silently
changing production logic or tuning parameters on the outcome being reported.
Any result remains research-only until it has a faithful live replay, a
timestamped historical-news treatment, and the normal realized-capital gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pandas as pd
from core.config import ConfigManager
from core.trading_costs import TradingCostModel, resolve_trading_cost_model


@dataclass(frozen=True)
class ResearchStrategySpec:
    strategy_id: str
    family: str
    interval: str
    lookback: int
    fast_ema: int
    slow_ema: int
    rsi_period: int
    rsi_entry_low: float
    rsi_entry_high: float
    stop_atr: float
    target_atr: float
    description: str


# Fixed before execution.  Do not mutate these values from a report result.
PRE_REGISTERED_STRATEGIES: Dict[str, ResearchStrategySpec] = {
    "R1_DONCHIAN_TREND_20_50_200": ResearchStrategySpec(
        strategy_id="R1_DONCHIAN_TREND_20_50_200",
        family="donchian_trend",
        interval="1h",
        lookback=20,
        fast_ema=50,
        slow_ema=200,
        rsi_period=2,
        rsi_entry_low=0.0,
        rsi_entry_high=100.0,
        stop_atr=2.0,
        target_atr=3.0,
        description=(
            "1h Donchian-20 breakout in the direction of the 50/200 EMA trend; "
            "2 ATR stop and 3 ATR target."
        ),
    ),
    "R2_RSI2_TREND_REVERSION": ResearchStrategySpec(
        strategy_id="R2_RSI2_TREND_REVERSION",
        family="rsi2_trend_reversion",
        interval="1h",
        lookback=20,
        fast_ema=50,
        slow_ema=200,
        rsi_period=2,
        rsi_entry_low=10.0,
        rsi_entry_high=90.0,
        stop_atr=2.0,
        target_atr=1.0,
        description=(
            "1h RSI(2) pullback/reversion only in the 200 EMA trend direction; "
            "2 ATR stop and 1 ATR target."
        ),
    ),
    "R3_EMA20_100_PULLBACK": ResearchStrategySpec(
        strategy_id="R3_EMA20_100_PULLBACK",
        family="ema_pullback_trend",
        interval="1h",
        lookback=20,
        fast_ema=20,
        slow_ema=100,
        rsi_period=2,
        rsi_entry_low=0.0,
        rsi_entry_high=100.0,
        stop_atr=1.5,
        target_atr=3.0,
        description=(
            "1h 20/100 EMA trend pullback: close recrosses the 20 EMA after a "
            "one-bar pullback while remaining on the 100 EMA trend side; "
            "1.5 ATR stop and 3 ATR target."
        ),
    ),
    "R4_DONCHIAN_4H_20_50": ResearchStrategySpec(
        strategy_id="R4_DONCHIAN_4H_20_50",
        family="donchian_trend",
        interval="4h",
        lookback=20,
        fast_ema=20,
        slow_ema=50,
        rsi_period=2,
        rsi_entry_low=0.0,
        rsi_entry_high=100.0,
        stop_atr=2.0,
        target_atr=4.0,
        description=(
            "4h Donchian-20 breakout in the 20/50 EMA trend direction; "
            "2 ATR stop and 4 ATR target."
        ),
    ),
    "R5_DONCHIAN_4H_55_20_50": ResearchStrategySpec(
        strategy_id="R5_DONCHIAN_4H_55_20_50",
        family="donchian_trend",
        interval="4h",
        lookback=55,
        fast_ema=20,
        slow_ema=50,
        rsi_period=2,
        rsi_entry_low=0.0,
        rsi_entry_high=100.0,
        stop_atr=2.0,
        target_atr=4.0,
        description=(
            "4h Donchian-55 breakout in the 20/50 EMA trend direction; "
            "2 ATR stop and 4 ATR target. This fixed, slower breakout is "
            "research-only and cannot be promoted from this replay."
        ),
    ),
}


def get_research_strategy(strategy_id: str) -> ResearchStrategySpec:
    normalized = str(strategy_id or "").upper().strip()
    try:
        return PRE_REGISTERED_STRATEGIES[normalized]
    except KeyError as exc:
        raise ValueError(f"Unknown pre-registered research strategy: {strategy_id!r}") from exc


def fetch_research_ohlcv(ticker: str, days: int = 180, interval: str = "1h") -> Optional[pd.DataFrame]:
    """Fetch actual Binance OHLCV for research and retain coverage metadata."""
    import time

    from core.strategy_backtester import _fetch_klines, _to_binance_symbol

    safe_days = max(1, int(days))
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - safe_days * 86_400_000
    dataframe = _fetch_klines(_to_binance_symbol(ticker), interval, start_ms, end_ms)
    if dataframe is None or dataframe.empty:
        return None
    dataframe.attrs.update(
        {
            "ticker": str(ticker).upper(),
            "requested_days": safe_days,
            "actual_start": dataframe.index[0].isoformat(),
            "actual_end": dataframe.index[-1].isoformat(),
            "actual_span_days": round((dataframe.index[-1] - dataframe.index[0]).total_seconds() / 86_400, 6),
        }
    )
    return dataframe


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    relative_strength = gain / loss.replace(0.0, pd.NA)
    rsi = 100.0 - (100.0 / (1.0 + relative_strength))
    rsi = rsi.mask((loss == 0.0) & (gain > 0.0), 100.0)
    rsi = rsi.mask((gain == 0.0) & (loss > 0.0), 0.0)
    return pd.to_numeric(rsi, errors="coerce").astype(float)


def _atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _prepare(frame: pd.DataFrame, spec: ResearchStrategySpec) -> pd.DataFrame:
    required = {"open", "high", "low", "close"}
    if not required.issubset(frame.columns):
        missing = ", ".join(sorted(required - set(frame.columns)))
        raise ValueError(f"Research OHLCV is missing required columns: {missing}")
    prepared = frame.loc[:, ["open", "high", "low", "close"]].copy().sort_index()
    for column in prepared.columns:
        prepared[column] = pd.to_numeric(prepared[column], errors="coerce")
    prepared["ema_fast"] = prepared["close"].ewm(span=spec.fast_ema, adjust=False, min_periods=spec.fast_ema).mean()
    prepared["ema_slow"] = prepared["close"].ewm(span=spec.slow_ema, adjust=False, min_periods=spec.slow_ema).mean()
    prepared["prior_high"] = prepared["high"].shift(1).rolling(spec.lookback, min_periods=spec.lookback).max()
    prepared["prior_low"] = prepared["low"].shift(1).rolling(spec.lookback, min_periods=spec.lookback).min()
    prepared["previous_close"] = prepared["close"].shift(1)
    prepared["rsi"] = _rsi(prepared["close"], spec.rsi_period)
    prepared["atr"] = _atr(prepared)
    return prepared


def _direction(row: pd.Series, spec: ResearchStrategySpec) -> Optional[str]:
    values = (row.get("close"), row.get("ema_fast"), row.get("ema_slow"), row.get("atr"))
    if not all(pd.notna(value) and float(value) > 0 for value in values):
        return None
    close = float(row["close"])
    fast = float(row["ema_fast"])
    slow = float(row["ema_slow"])
    if spec.family == "donchian_trend":
        prior_high, prior_low = row.get("prior_high"), row.get("prior_low")
        if pd.notna(prior_high) and close > float(prior_high) and fast > slow:
            return "LONG"
        if pd.notna(prior_low) and close < float(prior_low) and fast < slow:
            return "SHORT"
        return None
    if spec.family == "rsi2_trend_reversion":
        rsi = row.get("rsi")
        if not pd.notna(rsi):
            return None
        if float(rsi) < spec.rsi_entry_low and close > slow:
            return "LONG"
        if float(rsi) > spec.rsi_entry_high and close < slow:
            return "SHORT"
        return None
    if spec.family == "ema_pullback_trend":
        previous_close = row.get("previous_close")
        if not pd.notna(previous_close):
            return None
        if fast > slow and close > fast and float(previous_close) <= fast:
            return "LONG"
        if fast < slow and close < fast and float(previous_close) >= fast:
            return "SHORT"
        return None
    raise ValueError(f"Unsupported research family: {spec.family}")


def _adverse_fill(
    price: float,
    direction: str,
    *,
    entering: bool,
    cost_model: TradingCostModel,
    reason: str = "",
) -> float:
    is_long = direction == "LONG"
    slippage = (
        cost_model.entry_slippage_fraction
        if entering
        else cost_model.exit_slippage_fraction(reason)
    )
    if entering:
        return price * (1.0 + slippage) if is_long else price * (1.0 - slippage)
    return price * (1.0 - slippage) if is_long else price * (1.0 + slippage)


def replay_research_strategy(
    ticker: str,
    *,
    df: pd.DataFrame,
    strategy_id: str,
    cost_model: Optional[TradingCostModel] = None,
    **_unused: Any,
) -> Dict[str, Any]:
    """Replay one fixed research strategy using only data in ``df``.

    A signal is formed at a completed bar and filled at the following bar's
    open.  If both stop and target are touched in a bar, the stop is selected
    first; this is conservative when intrabar sequencing is unknowable.
    """
    spec = get_research_strategy(strategy_id)
    if cost_model is None:
        config = ConfigManager()
        cost_model = resolve_trading_cost_model(ticker, strategy_v2_config=config.strategy_v2)
    rows = _prepare(df, spec).reset_index(drop=False)
    timestamp_column = "dt" if "dt" in rows.columns else rows.columns[0]
    trades: List[Dict[str, Any]] = []
    index = 0
    while index < len(rows) - 1:
        direction = _direction(rows.iloc[index], spec)
        if direction is None:
            index += 1
            continue
        entry_index = index + 1
        raw_entry = float(rows.iloc[entry_index]["open"])
        atr = float(rows.iloc[index]["atr"])
        if raw_entry <= 0 or not (atr > 0):
            index += 1
            continue
        entry = _adverse_fill(raw_entry, direction, entering=True, cost_model=cost_model)
        is_long = direction == "LONG"
        stop = entry - (spec.stop_atr * atr) if is_long else entry + (spec.stop_atr * atr)
        target = entry + (spec.target_atr * atr) if is_long else entry - (spec.target_atr * atr)
        risk = abs(entry - stop)
        exit_index = len(rows) - 1
        exit_price = float(rows.iloc[exit_index]["close"])
        reason = "time_exit"
        for cursor in range(entry_index, len(rows)):
            bar = rows.iloc[cursor]
            open_price, high, low = float(bar["open"]), float(bar["high"]), float(bar["low"])
            if is_long:
                if low <= stop:
                    exit_price, reason, exit_index = min(stop, open_price), "stop_loss", cursor
                    break
                if high >= target:
                    exit_price, reason, exit_index = max(target, open_price), "take_profit", cursor
                    break
            else:
                if high >= stop:
                    exit_price, reason, exit_index = max(stop, open_price), "stop_loss", cursor
                    break
                if low <= target:
                    exit_price, reason, exit_index = min(target, open_price), "take_profit", cursor
                    break
        exit_fill = _adverse_fill(
            exit_price,
            direction,
            entering=False,
            cost_model=cost_model,
            reason=reason,
        )
        gross = (exit_fill - entry) if is_long else (entry - exit_fill)
        round_trip_cost = cost_model.commission_fraction * (entry + exit_fill)
        net_pct = ((gross - round_trip_cost) / entry) * 100.0
        r_net = (gross - round_trip_cost) / risk if risk > 0 else 0.0
        entry_time = rows.iloc[entry_index][timestamp_column]
        exit_time = rows.iloc[exit_index][timestamp_column]
        trades.append(
            {
                "ticker": str(ticker).upper(),
                "dir": direction,
                "entry": round(entry, 8),
                "exit": round(exit_fill, 8),
                "reason": reason,
                "r_net": round(r_net, 6),
                "net_pct": round(net_pct, 6),
                "strategy_id": spec.strategy_id,
                "entry_time": entry_time.isoformat() if hasattr(entry_time, "isoformat") else str(entry_time),
                "exit_time": exit_time.isoformat() if hasattr(exit_time, "isoformat") else str(exit_time),
            }
        )
        index = max(entry_index + 1, exit_index + 1)
    return {
        "ticker": str(ticker).upper(),
        "strategy_id": spec.strategy_id,
        "cost_model": cost_model.to_dict(),
        "trade_list": trades,
    }
