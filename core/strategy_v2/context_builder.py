from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from core.market_structure import analyze_market_structure
from core.liquidity_engine import analyze_liquidity
from core.chart_patterns import analyze_chart_patterns
from core.supertrend import analyze_supertrend
from core.volume_structure import analyze_volume_structure

from core.config import data_path


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs.fillna(0)))


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    close_prev = df["close"].shift(1)
    tr = pd.concat(
        [
            (df["high"] - df["low"]).abs(),
            (df["high"] - close_prev).abs(),
            (df["low"] - close_prev).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


def _macd_hist(close: pd.Series) -> pd.Series:
    ema_fast = _ema(close, 12)
    ema_slow = _ema(close, 26)
    macd = ema_fast - ema_slow
    signal = _ema(macd, 9)
    return macd - signal


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]

    plus_dm = (high.diff()).clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    plus_dm = plus_dm.where(plus_dm > minus_dm, 0.0)
    minus_dm = minus_dm.where(minus_dm > plus_dm, 0.0)

    tr = pd.concat(
        [
            (high - low).abs(),
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(period).mean().replace(0, np.nan)
    plus_di = (100 * (plus_dm.rolling(period).mean() / atr)).fillna(0)
    minus_di = (100 * (minus_dm.rolling(period).mean() / atr)).fillna(0)
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100).fillna(0)
    return dx.rolling(period).mean().fillna(0)


def _bollinger(close: pd.Series, period: int = 20, stdev_mult: float = 2.0):
    mid = _sma(close, period)
    std = close.rolling(period).std().fillna(0)
    upper = mid + (std * stdev_mult)
    lower = mid - (std * stdev_mult)
    width = ((upper - lower) / mid.replace(0, np.nan)).fillna(0)
    return upper, mid, lower, width


def _apply_market_structure(ctx: Dict[str, Any], frame: pd.DataFrame) -> None:
    """Attach causal structural features without replacing legacy rolling levels."""
    if frame.empty or not {"high", "low", "close"}.issubset(frame.columns):
        return
    structure = analyze_market_structure(frame, left_bars=3, right_bars=3)
    for key in (
        "structure_trend",
        "structure_last_high",
        "structure_last_low",
        "structure_last_high_label",
        "structure_last_low_label",
        "structure_event",
        "structure_event_direction",
        "structure_event_level",
        "structure_last_event",
        "structure_last_event_direction",
        "structure_last_event_level",
        "structure_event_age_bars",
        "confirmed_pivot_count",
    ):
        ctx[key] = structure.get(key)
    ctx["structural_resistance"] = structure.get("structure_last_high")
    ctx["structural_support"] = structure.get("structure_last_low")


def _apply_liquidity(ctx: Dict[str, Any], frame: pd.DataFrame) -> None:
    """Attach causal equal-high/low pools and sweep-reclaim state."""
    if frame.empty or not {"high", "low", "close"}.issubset(frame.columns):
        return
    liquidity_frame = frame.copy()
    if "atr" not in liquidity_frame:
        liquidity_frame["atr"] = _f(ctx.get("atr"), 0.0)
    liquidity = analyze_liquidity(liquidity_frame)
    for key in (
        "buy_side_liquidity",
        "sell_side_liquidity",
        "buy_side_pool_count",
        "sell_side_pool_count",
        "liquidity_event",
        "liquidity_event_direction",
        "liquidity_event_level",
        "liquidity_last_event",
        "liquidity_last_event_direction",
        "liquidity_event_age_bars",
    ):
        ctx[key] = liquidity.get(key)


def _apply_chart_patterns(ctx: Dict[str, Any], frame: pd.DataFrame) -> None:
    """Attach pattern state; only a confirmed neckline break becomes an event."""
    if frame.empty or not {"high", "low", "close"}.issubset(frame.columns):
        return
    pattern_frame = frame.copy()
    if "atr" not in pattern_frame:
        pattern_frame["atr"] = _f(ctx.get("atr"), 0.0)
    patterns = analyze_chart_patterns(pattern_frame)
    for key in (
        "chart_pattern",
        "chart_pattern_name",
        "chart_pattern_direction",
        "chart_pattern_status",
        "chart_pattern_neckline",
        "chart_pattern_invalidation",
        "chart_pattern_target",
        "chart_pattern_confidence",
        "chart_pattern_volume_ratio",
        "chart_pattern_acceptance",
        "chart_pattern_event",
    ):
        ctx[key] = patterns.get(key)


def _apply_volume_structure(ctx: Dict[str, Any], frame: pd.DataFrame) -> None:
    """Attach causal volume flow and two-close price acceptance state."""
    required = {"open", "high", "low", "close", "volume"}
    if frame.empty or not required.issubset(frame.columns):
        return
    volume = analyze_volume_structure(frame)
    for key in (
        "volume_average",
        "volume_ratio",
        "volume_structure_expansion",
        "volume_flow_score",
        "volume_flow_direction",
        "volume_event",
        "volume_event_direction",
        "volume_event_level",
        "volume_last_event",
        "volume_last_event_direction",
        "volume_event_age_bars",
        "price_acceptance",
        "price_acceptance_direction",
        "price_acceptance_level",
        "price_acceptance_age_bars",
    ):
        ctx[key] = volume.get(key)


def _apply_supertrend(ctx: Dict[str, Any], frame: pd.DataFrame) -> None:
    """Attach the standard causal SuperTrend (10, 3) state."""
    if frame.empty or not {"high", "low", "close"}.issubset(frame.columns):
        return
    supertrend = analyze_supertrend(frame, period=10, multiplier=3.0)
    for key in (
        "supertrend_line",
        "supertrend_upper_band",
        "supertrend_lower_band",
        "supertrend_direction",
        "supertrend_flip",
        "supertrend_last_flip_direction",
        "supertrend_flip_age_bars",
        "supertrend_atr",
        "supertrend_period",
        "supertrend_multiplier",
    ):
        ctx[key] = supertrend.get(key)


def _normalise_ohlcv(frame: Any) -> Optional[pd.DataFrame]:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    normalized = frame.rename(columns={column: str(column).lower() for column in frame.columns}).copy()
    required = {"open", "high", "low", "close"}
    if not required.issubset(normalized.columns):
        return None
    if "volume" not in normalized.columns:
        normalized["volume"] = 0.0
    for column in (*required, "volume"):
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    normalized = normalized.dropna(subset=list(required))
    return normalized if len(normalized) >= 20 else None


def build_s8_context_from_states(
    states: Mapping[str, Dict[str, Any]] | None,
    *,
    primary_timeframe: str = "30m",
    confirmation_timeframes: Sequence[str] = ("1h", "4h"),
) -> Dict[str, Any]:
    """Normalize already-computed causal states (used by historical replay)."""
    normalized_states = {
        str(timeframe).lower(): dict(state or {})
        for timeframe, state in (states or {}).items()
        if isinstance(state, dict)
    }
    primary_key = str(primary_timeframe).lower()
    primary = normalized_states.get(primary_key, {})
    output: Dict[str, Any] = {
        "s8_minimum_timeframe": "30m",
        "s8_primary_timeframe": primary_key,
        "s8_confirmation_timeframes": [str(tf).lower() for tf in confirmation_timeframes],
        "s8_timeframes": normalized_states,
    }
    if primary.get("available"):
        for key, value in primary.items():
            if key.startswith(("supertrend_", "structure_")) or key in {
                "structural_support", "structural_resistance", "volume_average",
                "volume_ratio", "price_acceptance_direction",
            }:
                output[key] = value
        primary_atr = _f(primary.get("supertrend_atr"), 0.0)
        if primary_atr > 0:
            output["atr"] = primary_atr
            output["atr_pct"] = primary_atr / max(_f(primary.get("close"), 0.0), 1e-9)
    return output


def build_s8_multi_timeframe_context(
    frames: Mapping[str, pd.DataFrame] | None,
    *,
    primary_timeframe: str = "30m",
    confirmation_timeframes: Sequence[str] = ("1h", "4h"),
) -> Dict[str, Any]:
    """Build causal SuperTrend/structure facts for S8 from 30m and higher.

    The primary trigger is deliberately fixed at 30 minutes. Higher frames do
    not create an entry by themselves; they are retained as independent trend
    confirmations so the evaluator and the saved entry chart can state exactly
    which horizons agreed or disagreed at decision time.
    """
    requested = (str(primary_timeframe).lower(), *(str(tf).lower() for tf in confirmation_timeframes))
    states: Dict[str, Any] = {}
    for timeframe in requested:
        frame = _normalise_ohlcv((frames or {}).get(timeframe))
        if frame is None:
            states[timeframe] = {
                "timeframe": timeframe,
                "available": False,
                "bar_count": 0,
            }
            continue
        supertrend = analyze_supertrend(frame, period=10, multiplier=3.0)
        structure = analyze_market_structure(frame, left_bars=3, right_bars=3)
        volume = analyze_volume_structure(frame)
        latest_index = frame.index[-1]
        states[timeframe] = {
            "timeframe": timeframe,
            "available": True,
            "bar_count": int(len(frame)),
            "as_of": latest_index.isoformat() if hasattr(latest_index, "isoformat") else str(latest_index),
            "close": round(_f(frame["close"].iloc[-1]), 8),
            "volume": round(_f(frame["volume"].iloc[-1]), 8),
            "volume_average": round(_f(volume.get("volume_average")), 8),
            "volume_ratio": round(_f(volume.get("volume_ratio")), 4),
            "price_acceptance_direction": str(volume.get("price_acceptance_direction") or "").upper(),
            "supertrend_direction": str(supertrend.get("supertrend_direction") or "").upper(),
            "supertrend_last_flip_direction": str(supertrend.get("supertrend_last_flip_direction") or "").upper(),
            "supertrend_flip_age_bars": int(_f(supertrend.get("supertrend_flip_age_bars"), 999.0)),
            "supertrend_line": round(_f(supertrend.get("supertrend_line")), 8),
            "supertrend_upper_band": round(_f(supertrend.get("supertrend_upper_band")), 8),
            "supertrend_lower_band": round(_f(supertrend.get("supertrend_lower_band")), 8),
            "supertrend_atr": round(_f(supertrend.get("supertrend_atr")), 8),
            "supertrend_period": int(_f(supertrend.get("supertrend_period"), 10.0)),
            "supertrend_multiplier": round(_f(supertrend.get("supertrend_multiplier"), 3.0), 4),
            "structure_trend": str(structure.get("structure_trend") or "UNKNOWN").upper(),
            "structure_last_event": str(structure.get("structure_last_event") or structure.get("structure_event") or "").upper(),
            "structure_last_event_direction": str(
                structure.get("structure_last_event_direction") or structure.get("structure_event_direction") or ""
            ).upper(),
            "structure_last_event_level": round(
                _f(structure.get("structure_last_event_level"), _f(structure.get("structure_event_level"))), 8
            ),
            "structure_event_age_bars": int(_f(structure.get("structure_event_age_bars"), 999.0)),
            "structural_support": round(_f(structure.get("structure_last_low")), 8),
            "structural_resistance": round(_f(structure.get("structure_last_high")), 8),
            "structure_last_high_label": str(structure.get("structure_last_high_label") or ""),
            "structure_last_low_label": str(structure.get("structure_last_low_label") or ""),
        }

    return build_s8_context_from_states(
        states,
        primary_timeframe=primary_timeframe,
        confirmation_timeframes=confirmation_timeframes,
    )


def load_cached_ohlc(symbol: str) -> Optional[pd.DataFrame]:
    sym = str(symbol or "").upper().strip()
    if not sym:
        return None
    cache_dir = data_path("cache_backtest")  # configured data folder (MS_DATA_DIR)
    if not cache_dir.exists():
        return None

    preferred = [f"{sym}_5min.csv", f"{sym}_1h.csv", f"{sym}_15min.csv", f"{sym}_1min.csv"]
    candidates = [cache_dir / name for name in preferred if (cache_dir / name).exists()]
    if not candidates:
        return None
    path = candidates[0]
    try:
        df = pd.read_csv(path)
        if df.empty:
            return None
        cols = {str(c).lower(): c for c in df.columns}
        rename_map = {}
        for key in ["timestamp", "open", "high", "low", "close", "volume"]:
            if key in cols:
                rename_map[cols[key]] = key
        df = df.rename(columns=rename_map)
        for req in ["timestamp", "open", "high", "low", "close"]:
            if req not in df.columns:
                return None
        if "volume" not in df.columns:
            df["volume"] = 0.0
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
        return df.tail(1200).reset_index(drop=True)
    except Exception:
        return None


def build_context(
    symbol: str,
    asset_class: str,
    watch_row: Dict[str, Any],
    ohlc_df: Optional[pd.DataFrame] = None,
    hours_to_event: float = 999.0,
    row_context: Any = None,
    precomputed_ohlc_features: Optional[Dict[str, Any]] = None,
    multi_timeframe_frames: Optional[Mapping[str, pd.DataFrame]] = None,
) -> Dict[str, Any]:
    row = watch_row if isinstance(watch_row, dict) else {}
    ctx: Dict[str, Any] = {
        "symbol": str(symbol or "").upper().strip(),
        "asset_class": str(asset_class or "").upper().strip(),
        "price": _f(row.get("price"), 0.0),
        "change_pct": _f(row.get("change_pct"), 0.0),
        "volume": _f(row.get("volume"), 0.0),
        "spread_bps": _f(row.get("spread_bps"), 0.0),
        # Normalise news sentiment to one of {bullish, bearish, neutral}.
        # build_news_pulse returns "unknown" on every error/empty path; without
        # normalisation, "unknown" falls through _news_catalyst_score's three
        # explicit branches into the counter-sentiment branch (scored below
        # neutral) — penalising V2's catalyst component when news is simply
        # unavailable, instead of treating it as no-information (neutral).
        "news_sentiment": (
            (lambda s: s if s in {"bullish", "bearish", "neutral"} else "neutral")(
                str(row.get("news_sentiment_trend") or "").lower().strip()
            )
        ),
        "news_pulse_score": _f(row.get("news_pulse_score"), 0.0),
        "hours_to_event": _f(hours_to_event, 999.0),
        "event_risk": _f(hours_to_event, 999.0) <= 6.0,
    }

    # If RowContext provided, extract indicators directly (avoids recomputation)
    if row_context is not None and hasattr(row_context, "rsi"):
        ctx["rsi"] = _f(getattr(row_context, "rsi", 50.0), 50.0)
        ctx["macd_hist"] = _f(getattr(row_context, "macd_hist", 0.0), 0.0)
        atr_val = _f(getattr(row_context, "atr", 0.0), 0.0)
        ctx["atr"] = atr_val if atr_val > 0 else ctx["price"] * 0.004
        ctx["atr_pct"] = ctx["atr"] / max(ctx["price"], 1e-9)
        ctx["adx"] = _f(getattr(row_context, "adx", 20.0), 20.0)
        ctx["ema20"] = _f(getattr(row_context, "ema20", ctx["price"]), ctx["price"])
        ctx["ema50"] = _f(row.get("ema50"), ctx["price"])
        # Compute ema200 + ema_slope from ohlc_df when available. RowContext
        # carries ema20 but NOT ema200/ema_slope, so defaulting to price/0
        # (pre-fix) understated trend strength and biased the V2 regime/trend
        # evaluators in BOTH live (engine.py) and replay (strategy_backtester.py
        # passes row_context too) -- regime_engine scored ema_slope=0 as a RANGE
        # component (never TREND), TrendPullback's `price > ema200` gate always
        # failed (ema200==price), and SessionBreakout's trend filter went no-op.
        # Both paths were buggy identically, so this fix preserves live<->replay
        # parity. Slope = 1-bar ema200 delta / abs(ema200), matching the scale of
        # the df-branch slope (line ~243) and the regime 0.0008/0.0006 thresholds.
        # Look-ahead-safe: reads only the tail of the caller-supplied df (engine
        # passes full history; backtester passes df.iloc[:i+1]).
        ema200_val: Optional[float] = None
        ema_slope_val: Optional[float] = None
        if isinstance(ohlc_df, pd.DataFrame) and not ohlc_df.empty and "close" in ohlc_df.columns:
            try:
                closes = pd.to_numeric(ohlc_df["close"], errors="coerce").dropna()
                if len(closes) >= 2:
                    ema200_series = _ema(closes, 200)
                    last_ema200 = ema200_series.iloc[-1]
                    if pd.notna(last_ema200):
                        ema200_val = float(last_ema200)
                        prev_ema200 = ema200_series.iloc[-2]
                        if pd.notna(prev_ema200) and abs(float(last_ema200)) > 1e-9:
                            ema_slope_val = float(
                                (float(last_ema200) - float(prev_ema200))
                                / abs(float(last_ema200))
                            )
            except Exception:
                ema200_val = None
                ema_slope_val = None
        ctx["ema200"] = ema200_val if ema200_val is not None else _f(row.get("ema200"), ctx["price"])
        ctx["ema_slope"] = ema_slope_val if ema_slope_val is not None else _f(row.get("ema_slope"), 0.0)
        ctx["bb_width"] = _f(getattr(row_context, "bb_width_5m", 0.02), 0.02)
        ctx["volume_avg"] = _f(getattr(row_context, "vol_sma", ctx["volume"]), max(ctx["volume"], 1.0))
        ctx["support"] = _f(getattr(row_context, "low", ctx["price"] * 0.99), ctx["price"] * 0.99)
        ctx["resistance"] = _f(getattr(row_context, "high", ctx["price"] * 1.01), ctx["price"] * 1.01)
        ctx["swing_low"] = ctx["support"]
        ctx["swing_high"] = ctx["resistance"]
        ctx["candle_dir"] = "bull" if _f(row.get("change_pct"), 0.0) >= 0 else "bear"
        ctx["market_regime"] = str(getattr(row_context, "market_regime", "MIXED")).upper()

        # Historical replay can supply these same causal OHLC-derived values in
        # bulk. It avoids recomputing a growing dataframe on every bar while
        # retaining the exact context consumed by the selector.
        if isinstance(precomputed_ohlc_features, dict):
            ctx.update(precomputed_ohlc_features)
            if multi_timeframe_frames:
                ctx.update(build_s8_multi_timeframe_context(multi_timeframe_frames))
            return ctx

        # Composite fields S2-S6 depend on (retest_hold, squeeze, impulse,
        # consolidation_break, session_breakout, bollinger bands, structural
        # support/resistance, volume_expansion). RowContext does NOT carry
        # these, so without this block they default to False / price*1.01 / 0
        # and S2 BreakoutRetest, S3 VolSqueeze, S4 MeanReversion, S5 Momentum,
        # S6 FxSessionBreakout can NEVER fire -> V2 in BOTH_AUTO_BEST only ever
        # produced S1 TrendPullback (strategy diversity = 0). Compute from
        # ohlc_df (mirrors the df-branch below) when available. Look-ahead-safe:
        # uses ohlc_df.tail(N) and excludes the current bar where the df-branch
        # does (consolidation_break, session_breakout). Live (engine.py passes
        # full history) and replay (strategy_backtester passes df.iloc[:i+1])
        # are affected identically -> live<->replay parity preserved.
        if isinstance(ohlc_df, pd.DataFrame) and not ohlc_df.empty and "close" in ohlc_df.columns:
            try:
                _cdf = ohlc_df.copy()
                for _c in ["open", "high", "low", "close", "volume"]:
                    if _c in _cdf.columns:
                        _cdf[_c] = pd.to_numeric(_cdf[_c], errors="coerce")
                _cdf = _cdf.dropna(subset=["close"]).reset_index(drop=True)
                if len(_cdf) >= 2 and "high" in _cdf.columns and "low" in _cdf.columns:
                    _close = _cdf["close"]
                    _last = _cdf.iloc[-1]
                    _prev = _cdf.iloc[-2]
                    # The 20 bars before the last one, matching the replay:
                    # a level that includes the bar being tested cannot be broken by it.
                    _recent_20 = _cdf.iloc[-21:-1] if len(_cdf) > 20 else _cdf.iloc[:-1]
                    _recent_12 = _cdf.tail(12)
                    _recent_48 = _cdf.tail(48)
                    # Bollinger bands (for S3 squeeze + S4 mean-reversion).
                    _bb_upper, _bb_mid, _bb_lower, _bb_width = _bollinger(_close, 20, 2.0)
                    ctx["bb_upper"] = _f(_bb_upper.iloc[-1], ctx["price"] * 1.01)
                    ctx["bb_mid"] = _f(_bb_mid.iloc[-1], ctx["price"])
                    ctx["bb_lower"] = _f(_bb_lower.iloc[-1], ctx["price"] * 0.99)
                    # Multi-bar structural levels (replaces the 1m-bar low/high
                    # that pre-fix used row_context.low/high -> 1m noise). M5.
                    ctx["support"] = _f(_recent_20["low"].min(), ctx["price"] * 0.99)
                    ctx["resistance"] = _f(_recent_20["high"].max(), ctx["price"] * 1.01)
                    ctx["swing_low"] = _f(_recent_12["low"].min(), ctx["price"] * 0.99)
                    ctx["swing_high"] = _f(_recent_12["high"].max(), ctx["price"] * 1.01)
                    _vol_avg20 = _f(_cdf["volume"].rolling(20).mean().iloc[-1], ctx["volume_avg"]) if "volume" in _cdf.columns else ctx["volume_avg"]
                    ctx["volume_expansion"] = ctx["volume"] >= max(_vol_avg20 * 1.1, 1.0)
                    # S2 retest hold: last bar respects the broken level.
                    ctx["retest_hold_long"] = _f(_last.get("low"), ctx["price"]) >= ctx["resistance"] * 0.995
                    ctx["retest_hold_short"] = _f(_last.get("high"), ctx["price"]) <= ctx["support"] * 1.005
                    # S3 squeeze.
                    _median_bw = _f(_bb_width.tail(120).median(), max(ctx["bb_width"], 0.001))
                    ctx["squeeze_active"] = ctx["bb_width"] <= max(_median_bw * 0.65, 0.004)
                    ctx["squeeze_breakout_up"] = bool(
                        ctx["squeeze_active"] and ctx["price"] > ctx["resistance"] and ctx["volume_expansion"]
                    )
                    ctx["squeeze_breakout_down"] = bool(
                        ctx["squeeze_active"] and ctx["price"] < ctx["support"] and ctx["volume_expansion"]
                    )
                    # S5 impulse + consolidation break (exclude current bar).
                    _impulse = (ctx["price"] - _f(_prev.get("close"), ctx["price"])) / max(
                        _f(_prev.get("close"), ctx["price"]), 1e-9
                    )
                    ctx["impulse_up"] = _impulse > max(ctx["atr_pct"] * 0.7, 0.004)
                    ctx["impulse_down"] = _impulse < -max(ctx["atr_pct"] * 0.7, 0.004)
                    ctx["consolidation_break_up"] = ctx["price"] > _f(
                        _recent_12["high"].iloc[:-1].max(), ctx["resistance"]
                    )
                    ctx["consolidation_break_down"] = ctx["price"] < _f(
                        _recent_12["low"].iloc[:-1].min(), ctx["support"]
                    )
                    # S6 FX session proxy (exclude current bar so a breakout
                    # above asian_high is achievable). H1.
                    ctx["asian_high"] = _f(_recent_48["high"].iloc[:-1].max(), ctx["resistance"])
                    ctx["asian_low"] = _f(_recent_48["low"].iloc[:-1].min(), ctx["support"])
                    ctx["session_breakout"] = bool(
                        ctx["asset_class"] == "FOREX_MAJORS"
                        and (
                            ctx["price"] > ctx["asian_high"] * 1.0005
                            or ctx["price"] < ctx["asian_low"] * 0.9995
                        )
                    )
                    _apply_market_structure(ctx, _cdf)
                    _apply_liquidity(ctx, _cdf)
                    _apply_volume_structure(ctx, _cdf)
                    _apply_chart_patterns(ctx, _cdf)
                    _apply_supertrend(ctx, _cdf)
            except Exception:
                pass
        if multi_timeframe_frames:
            ctx.update(build_s8_multi_timeframe_context(multi_timeframe_frames))
        return ctx

    df = ohlc_df.copy() if isinstance(ohlc_df, pd.DataFrame) and not ohlc_df.empty else None
    if df is None:
        # Fallback to what already exists in watchlist state.
        ctx["rsi"] = _f(row.get("rsi"), 50.0)
        ctx["macd_hist"] = _f(row.get("macd_hist"), 0.0)
        ctx["atr"] = _f(row.get("atr"), max(ctx["price"] * 0.004, 0.01))
        ctx["atr_pct"] = _f(row.get("atr"), 0.0) / max(ctx["price"], 1e-9)
        ctx["adx"] = _f(row.get("adx"), 20.0)
        ctx["ema20"] = _f(row.get("ema20"), ctx["price"])
        ctx["ema50"] = _f(row.get("ema50"), ctx["price"])
        ctx["ema200"] = _f(row.get("ema200"), ctx["price"])
        ctx["ema_slope"] = _f(row.get("ema_slope"), 0.0)
        ctx["bb_width"] = _f(row.get("bb_width"), 0.02)
        ctx["volume_avg"] = max(ctx["volume"], 1.0)
        ctx["resistance"] = ctx["price"] * 1.01
        ctx["support"] = ctx["price"] * 0.99
        ctx["swing_low"] = ctx["price"] * 0.992
        ctx["swing_high"] = ctx["price"] * 1.008
        ctx["candle_dir"] = "bull" if _f(row.get("change_pct"), 0.0) >= 0 else "bear"
        for key in (
            "supertrend_line",
            "supertrend_upper_band",
            "supertrend_lower_band",
            "supertrend_direction",
            "supertrend_flip",
            "supertrend_last_flip_direction",
            "supertrend_flip_age_bars",
        ):
            if key in row:
                ctx[key] = row.get(key)
        if multi_timeframe_frames:
            ctx.update(build_s8_multi_timeframe_context(multi_timeframe_frames))
        return ctx

    # Ensure numeric columns.
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    if df.empty:
        return ctx

    close = df["close"]
    df["ema20"] = _ema(close, 20)
    df["ema50"] = _ema(close, 50)
    df["ema200"] = _ema(close, 200)
    df["rsi"] = _rsi(close, 14)
    df["atr"] = _atr(df, 14)
    df["macd_hist"] = _macd_hist(close)
    df["adx"] = _adx(df, 14)
    bb_upper, bb_mid, bb_lower, bb_width = _bollinger(close, 20, 2.0)
    df["bb_upper"] = bb_upper
    df["bb_mid"] = bb_mid
    df["bb_lower"] = bb_lower
    df["bb_width"] = bb_width
    df["volume_avg20"] = df["volume"].rolling(20).mean()

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last
    recent_20 = df.tail(20)
    recent_12 = df.tail(12)
    recent_48 = df.tail(48)

    price = _f(last.get("close"), ctx["price"])
    ctx["price"] = price
    ctx["open"] = _f(last.get("open"), price)
    ctx["high"] = _f(last.get("high"), price)
    ctx["low"] = _f(last.get("low"), price)
    ctx["close"] = price
    ctx["volume"] = _f(last.get("volume"), ctx["volume"])
    ctx["volume_avg"] = _f(last.get("volume_avg20"), max(ctx["volume"], 1.0))
    ctx["rsi"] = _f(last.get("rsi"), 50.0)
    ctx["macd_hist"] = _f(last.get("macd_hist"), 0.0)
    ctx["atr"] = max(_f(last.get("atr"), 0.0), max(price * 0.003, 0.01))
    ctx["atr_pct"] = ctx["atr"] / max(price, 1e-9)
    ctx["adx"] = _f(last.get("adx"), 20.0)
    ctx["ema20"] = _f(last.get("ema20"), price)
    ctx["ema50"] = _f(last.get("ema50"), price)
    ctx["ema200"] = _f(last.get("ema200"), price)
    # ema_slope from ema200 (not ema20) to match the row_context branch scale
    # (ema200 delta / abs(ema200)) and the regime_engine 0.0008/0.0006
    # thresholds, which are calibrated for ema200. Using ema20 (pre-fix) made
    # the df-branch over-fire TREND / under-fire RANGE vs the live row_context
    # path -> parity divergence between the analyze endpoint (df-branch) and
    # live/backtester (row_context). M6.
    ctx["ema_slope"] = (_f(last.get("ema200"), price) - _f(prev.get("ema200"), price)) / max(abs(_f(last.get("ema200"), price)), 1e-9)
    ctx["bb_upper"] = _f(last.get("bb_upper"), price * 1.01)
    ctx["bb_mid"] = _f(last.get("bb_mid"), price)
    ctx["bb_lower"] = _f(last.get("bb_lower"), price * 0.99)
    ctx["bb_width"] = _f(last.get("bb_width"), 0.02)
    ctx["resistance"] = _f(recent_20["high"].max(), price * 1.01)
    ctx["support"] = _f(recent_20["low"].min(), price * 0.99)
    ctx["swing_low"] = _f(recent_12["low"].min(), price * 0.99)
    ctx["swing_high"] = _f(recent_12["high"].max(), price * 1.01)

    # Useful booleans for strategy evaluators.
    ctx["candle_dir"] = "bull" if ctx["close"] >= ctx["open"] else "bear"
    ctx["volume_expansion"] = ctx["volume"] >= max(ctx["volume_avg"] * 1.1, 1.0)
    ctx["retest_hold_long"] = _f(last.get("low"), price) >= ctx["resistance"] * 0.995
    ctx["retest_hold_short"] = _f(last.get("high"), price) <= ctx["support"] * 1.005

    median_bw = _f(df["bb_width"].tail(120).median(), max(ctx["bb_width"], 0.001))
    ctx["squeeze_active"] = ctx["bb_width"] <= max(median_bw * 0.65, 0.004)
    ctx["squeeze_breakout_up"] = bool(ctx["squeeze_active"] and ctx["close"] > ctx["resistance"] and ctx["volume_expansion"])
    ctx["squeeze_breakout_down"] = bool(ctx["squeeze_active"] and ctx["close"] < ctx["support"] and ctx["volume_expansion"])

    impulse = (ctx["close"] - _f(prev.get("close"), ctx["close"])) / max(_f(prev.get("close"), ctx["close"]), 1e-9)
    ctx["impulse_up"] = impulse > max(ctx["atr_pct"] * 0.7, 0.004)
    ctx["impulse_down"] = impulse < -max(ctx["atr_pct"] * 0.7, 0.004)
    ctx["consolidation_break_up"] = ctx["close"] > _f(recent_12["high"].iloc[:-1].max(), ctx["resistance"])
    ctx["consolidation_break_down"] = ctx["close"] < _f(recent_12["low"].iloc[:-1].min(), ctx["support"])

    # FX session proxy: use last 48 bars as "Asian range" approximation on 30m/1h cached bars.
    # Exclude the current bar (iloc[:-1]) so close>asian_high is achievable --
    # including the current bar makes asian_high>=last high>=close, so the
    # breakout condition was always False (S6 FxSessionBreakout dead in the
    # df-branch too). H1.
    ctx["asian_high"] = _f(recent_48["high"].iloc[:-1].max(), ctx["resistance"])
    ctx["asian_low"] = _f(recent_48["low"].iloc[:-1].min(), ctx["support"])
    ctx["session_breakout"] = bool(
        (ctx["asset_class"] == "FOREX_MAJORS")
        and (ctx["close"] > ctx["asian_high"] * 1.0005 or ctx["close"] < ctx["asian_low"] * 0.9995)
    )
    _apply_market_structure(ctx, df)
    _apply_liquidity(ctx, df)
    _apply_volume_structure(ctx, df)
    _apply_chart_patterns(ctx, df)
    _apply_supertrend(ctx, df)
    if multi_timeframe_frames:
        ctx.update(build_s8_multi_timeframe_context(multi_timeframe_frames))
    return ctx


def infer_asset_class(symbol: str) -> str:
    s = str(symbol or "").upper().strip()
    if not s:
        return "US_STOCKS"

    forex_majors = {"EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X", "AUDUSD=X", "USDCAD=X", "NZDUSD=X"}
    commodities = {"GC=F", "SI=F", "CL=F", "NG=F", "HG=F", "PL=F", "PA=F", "BZ=F"}

    if s in forex_majors:
        return "FOREX_MAJORS"
    if s in commodities:
        return "COMMODITIES"
    if s.endswith(("-USD", "-USDT", "-USDC")) or s.endswith(("USDT", "USDC")):
        return "CRYPTO_FUTURES"
    return "US_STOCKS"

