"""
Faithful strategy backtester — replays the LIVE signal scoring over historical data.

The 4 legacy backtesters use a plain RSI+MACD rule that has zero relationship to live
trading (live = logistic `compute_signal_score` over MTF/regime/profile). This module
instead reconstructs the live `RowContext` per 1m bar (look-ahead-safe) and calls the
SAME `compute_signal_score` the engine uses, then simulates entries/exits with live
costs and the live trailing/TP/SL logic. So the backtest can finally validate signal
and exit changes against what actually executes.

Scope (v1): the V1 logistic path + crypto (Binance long 1m+TF history). The live engine
also has a V2 selector under BOTH_AUTO_BEST; this module now replays V2 per bar too
(see the V2 override block in ``backtest``), mirroring the live BOTH_AUTO_BEST selection.
Known fidelity limits (documented): V2 ``validation_score`` uses a COLD perf_cache in
replay (as live at startup) so the >=min_edge_score gate usually defers to V1; news
catalyst is neutral per-bar (no historical news archive). Seeding perf / news history
is a deferred follow-up.
Look-ahead safety: 1m indicators are causal; higher-TF trends use only COMPLETED TF bars
(close time <= t); V2 context is built from ``df.iloc[:i+1]`` only.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
import urllib.parse
import urllib.request
from types import SimpleNamespace
from typing import Dict, List, Optional

import pandas as pd

from core.ta import TechnicalAnalysis
from core.signal_generation import calculate_vwap
from core.scoring_system import (
    SCORE_THRESHOLDS,
    calculate_atr,
    compute_signal_score,
    detect_market_regime,
    allowed_modes_for_regime,
    prepare_1m_dataframe,
    row_to_context,
)
from core.trading_costs import resolve_trading_cost_model

logger = logging.getLogger("strategy_backtester")

BINANCE = "https://api.binance.com/api/v3"
# (binance interval, minutes-per-bar) for higher timeframes used by RowContext.
HTF = [("1d", 1440), ("4h", 240), ("1h", 60), ("30m", 30), ("15m", 15), ("5m", 5)]


def _interval_milliseconds(interval: str) -> int:
    """Return the fixed Binance interval length used by the replay.

    The faithful replay only uses minute/hour/day intervals.  Keeping this
    conversion local means pagination is derived from the requested time range
    rather than from an arbitrary number of API pages.
    """
    matched = re.fullmatch(r"(\d+)([mhd])", str(interval or "").lower())
    if not matched:
        raise ValueError(f"Unsupported fixed Binance interval: {interval!r}")
    amount, unit = matched.groups()
    multiplier = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[unit]
    return int(amount) * multiplier


def _to_binance_symbol(ticker: str) -> str:
    t = ticker.upper().replace("-", "")
    return (t[:-3] + "USDT") if t.endswith("USD") and not t.endswith("USDT") else t


def _fetch_klines(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    max_pages: Optional[int] = None,
) -> Optional[pd.DataFrame]:
    """Fetch the requested Binance range without silently shortening it.

    Binance returns at most 1,000 klines per request.  The former fixed
    60-page limit quietly changed a requested 90-day 1m replay into roughly
    42 days of data.  By default we calculate the necessary page count from
    the time range.  A caller may set ``max_pages`` only for an explicit,
    visibly-truncated diagnostic fetch; the returned dataframe is then marked
    with ``source_truncated`` instead of being presented as full history.
    """
    if end_ms <= start_ms:
        return None
    interval_ms = _interval_milliseconds(interval)
    expected_bars = max(1, math.ceil((end_ms - start_ms) / interval_ms))
    page_limit = max(1, int(max_pages)) if max_pages is not None else math.ceil(expected_bars / 1000) + 1
    rows: List = []
    cur = start_ms
    source_truncated = False
    for _ in range(page_limit):
        qs = urllib.parse.urlencode({"symbol": symbol, "interval": interval,
                                     "startTime": cur, "endTime": end_ms, "limit": 1000})
        try:
            with urllib.request.urlopen(f"{BINANCE}/klines?{qs}", timeout=15) as r:
                data = json.loads(r.read())
        except Exception:
            return None
        if not data:
            break
        rows.extend(data)
        if len(data) < 1000:
            break
        cur = int(data[-1][0]) + 1
        if cur >= end_ms:
            break
        time.sleep(0.05)
    else:
        source_truncated = cur < end_ms
    if not rows:
        return None
    df = pd.DataFrame(
        [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])) for k in rows],
        columns=["open_time", "open", "high", "low", "close", "volume"],
    )
    df["dt"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.drop_duplicates("open_time").set_index("dt").sort_index()
    df.attrs.update(
        {
            "source": "binance_public_ohlcv",
            "symbol": symbol,
            "interval": interval,
            "requested_start": pd.to_datetime(start_ms, unit="ms", utc=True).isoformat(),
            "requested_end": pd.to_datetime(end_ms, unit="ms", utc=True).isoformat(),
            "expected_bars": expected_bars,
            "received_bars": len(df),
            "source_truncated": source_truncated,
        }
    )
    if source_truncated:
        logger.warning(
            "Binance OHLCV fetch truncated for %s %s after %s pages; requested %s bars, received %s",
            symbol,
            interval,
            page_limit,
            expected_bars,
            len(df),
        )
    return df


def _htf_trend_series(df_tf: pd.DataFrame, minutes: int, cfg: dict) -> pd.DataFrame:
    """Per-TF-bar BULL/BEAR trend keyed by the bar's CLOSE time (look-ahead-safe)."""
    _, _, macd_hist = TechnicalAnalysis.calc_macd(
        df_tf["close"], cfg["macd_fast"], cfg["macd_slow"], cfg["macd_signal"]
    )
    trend = macd_hist.apply(lambda v: "BULL" if v > 0 else ("BEAR" if v < 0 else "N/A"))
    out = pd.DataFrame({"trend": trend.values})
    # Bar is only known after it closes: close_time = open_time + interval.
    out["close_time"] = df_tf.index + pd.Timedelta(minutes=minutes)
    return out.dropna().sort_values("close_time")


def build_dataframe(
    ticker: str,
    days: int = 30,
    *,
    htf_history_days: int = 60,
) -> Optional[pd.DataFrame]:
    """Fetch + assemble a 1m dataframe with all columns RowContext/scoring need."""
    from core.config import ConfigManager
    cfg = ConfigManager().signal
    sym = _to_binance_symbol(ticker)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 60 * 60 * 1000

    df = _fetch_klines(sym, "1m", start_ms, end_ms)
    if df is None or len(df) < 300:
        return None
    source_metadata = dict(df.attrs)

    # --- 1m indicators (mirror engine.py:1774-1787) ---
    df["rsi"] = TechnicalAnalysis.calc_rsi(df["close"], cfg["rsi_period"])
    macd, macd_sig, macd_hist = TechnicalAnalysis.calc_macd(
        df["close"], cfg["macd_fast"], cfg["macd_slow"], cfg["macd_signal"])
    df["macd"], df["macd_signal"], df["macd_hist"] = macd, macd_sig, macd_hist
    df["ema_20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["atr"] = calculate_atr(df, int(cfg.get("atr_period", 14)))
    df["adx"] = TechnicalAnalysis.calc_adx(df["high"], df["low"], df["close"], 14)
    df["vol_sma"] = TechnicalAnalysis.calc_sma(df["volume"], 20)
    df["vwap"] = calculate_vwap(df)
    df["trend_1m"] = df["macd_hist"].apply(lambda v: "BULL" if v > 0 else "BEAR")

    # --- prepare (adds dist_vwap_atr/dist_ema_atr/cooldown cols) ---
    df = prepare_1m_dataframe(df, atr_period=int(cfg.get("atr_period", 14)))
    # Spike-cooldown parity: live (engine.py:2454) hardcodes cooldown_active=False --
    # the live signal path never calls add_cooldown_columns, so live admits post-spike
    # (RSI>78 / dist_atr>1.8) bars. add_cooldown_columns (called inside
    # prepare_1m_dataframe) flags the next 15 bars cooldown_active=True, and
    # compute_signal_score (scoring_system.py:995) early-returns is_valid=False on
    # any cooldown-True bar -> the backtest silently dropped post-spike candidates
    # live accepts, diverging admission and biasing win-rate/expectancy/trade-count.
    # Neutralize here so the backtest mirrors live's signal admission EXACTLY. The
    # per-ticker signal_cooldown_minutes lockout (the OTHER cooldown) is still applied
    # post-close (line ~420), parity with live signals.can_open_new_signal_for_ticker.
    # dist_vwap_atr/dist_ema_atr (also added by prepare_1m_dataframe) are KEPT -- live
    # consumes them (engine.py:2452-2453). The golden parity test already applies this
    # same neutralization (test_backtest_parity.py:72-73).
    df["cooldown_active"] = False
    df["cooldown_bars_remaining"] = 0

    # --- higher-TF trends, look-ahead-safe (completed bars only) ---
    left1m = pd.DataFrame({"t": df.index.astype("datetime64[ns, UTC]")}).sort_values("t")

    def _merge_htf(series_df: pd.DataFrame, value_col: str, default):
        s = series_df.copy()
        s["close_time"] = s["close_time"].astype("datetime64[ns, UTC]")
        m = pd.merge_asof(left1m, s, left_on="t", right_on="close_time", direction="backward")
        return m[value_col].values

    df5 = None
    for interval, minutes in HTF:
        # The default preserves the research replay's 60-day warmup. Live
        # forward observation can request a smaller, explicitly configured
        # window once it still contains the indicators it consumes; this avoids
        # turning one operational candidate review into dozens of paginated
        # HTTP requests.
        tf_start = end_ms - max(days, max(1, int(htf_history_days))) * 24 * 60 * 60 * 1000
        df_tf = _fetch_klines(sym, interval, tf_start, end_ms)
        col = f"trend_{interval}"
        if df_tf is None or len(df_tf) < 35:
            df[col] = "N/A"
            continue
        if interval == "5m":
            df5 = df_tf
        ts = _htf_trend_series(df_tf, minutes, cfg)
        df[col] = pd.Series(_merge_htf(ts.rename(columns={"trend": col}), col, "N/A")).fillna("N/A").values

    # --- real 5m regime metrics (mirror engine._get_regime_metrics), look-ahead-safe ---
    if df5 is not None and len(df5) >= 30:
        atr_p = int(cfg.get("atr_period", 14))
        adx5 = TechnicalAnalysis.calc_adx(df5["high"], df5["low"], df5["close"], 14)
        bbw5 = TechnicalAnalysis.calc_bb_width(df5["close"], 20, 2.0)
        atr5 = calculate_atr(df5, atr_p)
        # percentile rank of current ATR vs expanding history (data up to that bar only)
        atr_pct5 = atr5.expanding(min_periods=20).apply(
            lambda s: float((s <= s.iloc[-1]).mean() * 100.0), raw=False)
        reg = pd.DataFrame({
            "adx_5m": adx5.values, "bb_width_5m": bbw5.values,
            "atr_percentile_5m": atr_pct5.values, "atr_5m": atr5.values,
            "close_time": df5.index + pd.Timedelta(minutes=5),
        }).dropna().sort_values("close_time")
        for c in ("adx_5m", "bb_width_5m", "atr_percentile_5m", "atr_5m"):
            df[c] = pd.Series(_merge_htf(reg[["close_time", c]].copy(), c, 0.0)).fillna(0.0).values
    else:
        df["adx_5m"], df["bb_width_5m"], df["atr_percentile_5m"] = df.get("adx", 0.0), 0.0, 0.0
        df["atr_5m"] = 0.0  # mirror live _get_atr_for_timeframe(ctx, "5m") fallback
    if not df.empty:
        actual_span = df.index[-1] - df.index[0]
        source_metadata.update(
            {
                "ticker": str(ticker).upper(),
                "requested_days": int(days),
                "actual_start": df.index[0].isoformat(),
                "actual_end": df.index[-1].isoformat(),
                "actual_span_days": round(actual_span.total_seconds() / 86_400, 6),
            }
        )
    df.attrs.update(source_metadata)
    return df


_DF_CACHE: Dict = {}


class _ColdReplayPerformanceCache:
    """Explicitly empty performance cache for historical replay.

    Historical trades must not inherit today's realized-performance cache. The
    live-equivalent cold-start assumption also avoids rereading the JSON cache
    for every replay bar.
    """

    @staticmethod
    def get_symbol_entries(_symbol: str) -> Dict:
        return {}


def _precompute_v2_ohlc_features(df: pd.DataFrame, asset_class: str) -> pd.DataFrame:
    """Vectorize causal V2 context fields used during the faithful replay.

    ``build_context`` normally derives these from ``df.iloc[:i + 1]``. Doing
    that on every 1m bar repeatedly copies and re-indicators an ever-growing
    dataframe (quadratic work). These series use the identical rolling/EMA
    definitions, each limited to values available at the current bar.
    """
    def numeric(name: str, default: float = 0.0) -> pd.Series:
        source = df[name] if name in df else pd.Series(default, index=df.index, dtype=float)
        return pd.to_numeric(source, errors="coerce")

    close = numeric("close")
    high = numeric("high")
    low = numeric("low")
    volume = numeric("volume")
    atr = numeric("atr")
    width_5m = numeric("bb_width_5m", 0.02)

    ema200 = close.ewm(span=200, adjust=False).mean()
    previous_ema200 = ema200.shift(1)
    ema_slope = (ema200 - previous_ema200) / ema200.abs()
    ema_slope = ema_slope.where(
        ema200.notna() & previous_ema200.notna() & (ema200.abs() > 1e-9),
        0.0,
    )

    bb_mid = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bb_upper = bb_mid + (bb_std * 2.0)
    bb_lower = bb_mid - (bb_std * 2.0)
    bb_width = ((bb_upper - bb_lower) / bb_mid.replace(0, pd.NA)).fillna(0.0)
    median_bb_width = bb_width.rolling(120, min_periods=1).median()
    squeeze_threshold = median_bb_width * 0.65
    squeeze_threshold = squeeze_threshold.mask(
        squeeze_threshold.notna() & (squeeze_threshold < 0.004),
        0.004,
    )

    # Levels come from the 20 bars BEFORE the current one. Including the
    # current bar makes `close > resistance` impossible (close <= high <= max),
    # so breakouts could never fire in replay.
    support = low.shift(1).rolling(20, min_periods=1).min().fillna(low)
    resistance = high.shift(1).rolling(20, min_periods=1).max().fillna(high)
    swing_low = low.rolling(12, min_periods=1).min()
    swing_high = high.rolling(12, min_periods=1).max()
    volume_avg20 = volume.rolling(20).mean()
    atr_effective = atr.where(atr > 0, close * 0.004)
    atr_pct = atr_effective / close.replace(0, pd.NA)
    impulse = (close - close.shift(1)) / close.shift(1).replace(0, pd.NA)
    prior_high_12 = high.shift(1).rolling(11, min_periods=1).max()
    prior_low_12 = low.shift(1).rolling(11, min_periods=1).min()
    asian_high = high.shift(1).rolling(47, min_periods=1).max()
    asian_low = low.shift(1).rolling(47, min_periods=1).min()

    squeeze_active = width_5m <= squeeze_threshold
    volume_expansion = (volume >= (volume_avg20 * 1.1)) & (volume >= 1.0)
    is_forex = str(asset_class or "").upper() == "FOREX_MAJORS"
    result = pd.DataFrame(index=df.index)
    result["ema200"] = ema200
    result["ema_slope"] = ema_slope
    result["bb_upper"] = bb_upper
    result["bb_mid"] = bb_mid
    result["bb_lower"] = bb_lower
    result["support"] = support
    result["resistance"] = resistance
    result["swing_low"] = swing_low
    result["swing_high"] = swing_high
    result["volume_expansion"] = volume_expansion
    result["retest_hold_long"] = low >= (resistance * 0.995)
    result["retest_hold_short"] = high <= (support * 1.005)
    result["squeeze_active"] = squeeze_active
    result["squeeze_breakout_up"] = squeeze_active & (close > resistance) & volume_expansion
    result["squeeze_breakout_down"] = squeeze_active & (close < support) & volume_expansion
    result["impulse_up"] = impulse > pd.concat([atr_pct * 0.7, pd.Series(0.004, index=df.index)], axis=1).max(axis=1)
    result["impulse_down"] = impulse < -pd.concat([atr_pct * 0.7, pd.Series(0.004, index=df.index)], axis=1).max(axis=1)
    result["consolidation_break_up"] = close > prior_high_12.fillna(resistance)
    result["consolidation_break_down"] = close < prior_low_12.fillna(support)
    result["asian_high"] = asian_high.fillna(resistance)
    result["asian_low"] = asian_low.fillna(support)
    result["session_breakout"] = is_forex & ((close > (result["asian_high"] * 1.0005)) | (close < (result["asian_low"] * 0.9995)))
    from core.market_structure import precompute_market_structure
    from core.liquidity_engine import precompute_liquidity
    from core.chart_patterns import precompute_chart_patterns
    from core.supertrend import precompute_supertrend
    from core.volume_structure import precompute_volume_structure

    structure = precompute_market_structure(df, left_bars=3, right_bars=3)
    for column in structure.columns:
        result[column] = structure[column]
    result["structural_resistance"] = result["structure_last_high"]
    result["structural_support"] = result["structure_last_low"]
    liquidity = precompute_liquidity(df)
    for column in liquidity.columns:
        result[column] = liquidity[column]
    patterns = precompute_chart_patterns(df)
    for column in patterns.columns:
        result[column] = patterns[column]
    volume_structure = precompute_volume_structure(df)
    for column in volume_structure.columns:
        target_column = "volume_structure_expansion" if column == "volume_expansion" else column
        result[target_column] = volume_structure[column]
    supertrend = precompute_supertrend(df, period=10, multiplier=3.0)
    for column in supertrend.columns:
        result[column] = supertrend[column]
    result["supertrend_period"] = 10
    result["supertrend_multiplier"] = 3.0
    return result


def _precompute_s8_mtf_features(df: pd.DataFrame) -> pd.DataFrame:
    """Map completed 30m/1h/4h S8 states onto every replay bar."""
    result = pd.DataFrame(index=df.index)
    if not isinstance(df.index, pd.DatetimeIndex) or df.empty:
        return result
    from core.market_structure import precompute_market_structure
    from core.supertrend import precompute_supertrend
    from core.volume_structure import precompute_volume_structure

    source = df.rename(columns={column: str(column).lower() for column in df.columns}).copy()
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(source.columns):
        return result
    left = pd.DataFrame({"replay_time": pd.to_datetime(df.index, utc=True)}, index=df.index)
    for timeframe, rule, minutes in (("30m", "30min", 30), ("1h", "1h", 60), ("4h", "4h", 240)):
        bars = source.resample(rule, closed="left", label="left").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
        }).dropna(subset=["open", "high", "low", "close"])
        if bars.empty:
            continue
        supertrend = precompute_supertrend(bars, period=10, multiplier=3.0)
        structure = precompute_market_structure(bars, left_bars=3, right_bars=3)
        volume = precompute_volume_structure(bars)
        states = pd.DataFrame(index=bars.index)
        for column in supertrend.columns:
            states[column] = supertrend[column]
        for column in structure.columns:
            states[column] = structure[column]
        for column in ("volume_average", "volume_ratio", "price_acceptance_direction"):
            if column in volume:
                states[column] = volume[column]
        states["close"] = bars["close"]
        states["volume"] = bars["volume"]
        states["structural_support"] = structure.get("structure_last_low")
        states["structural_resistance"] = structure.get("structure_last_high")
        states["bar_count"] = range(1, len(states) + 1)
        states["available"] = (
            (states["bar_count"] >= 20)
            & states["supertrend_direction"].notna()
        )
        close_times = pd.to_datetime(states.index, utc=True) + pd.Timedelta(minutes=minutes)
        states["as_of"] = [timestamp.isoformat() for timestamp in close_times]
        states["close_time"] = close_times
        prefixed = states.rename(columns={
            column: f"s8_{timeframe}_{column}"
            for column in states.columns
            if column != "close_time"
        }).sort_values("close_time")
        merged = pd.merge_asof(
            left.reset_index(drop=True).sort_values("replay_time"),
            prefixed.reset_index(drop=True),
            left_on="replay_time",
            right_on="close_time",
            direction="backward",
        )
        for column in prefixed.columns:
            if column != "close_time":
                result[column] = merged[column].values
    return result


def _s8_context_from_replay_row(row: pd.Series) -> Dict:
    from core.strategy_v2.context_builder import build_s8_context_from_states

    state_fields = (
        "available", "bar_count", "as_of", "close", "volume", "volume_average", "volume_ratio",
        "price_acceptance_direction", "supertrend_direction", "supertrend_last_flip_direction",
        "supertrend_flip_age_bars", "supertrend_line", "supertrend_upper_band",
        "supertrend_lower_band", "supertrend_atr", "structure_trend", "structure_last_event",
        "structure_last_event_direction", "structure_last_event_level", "structure_event_age_bars",
        "structural_support", "structural_resistance", "structure_last_high_label",
        "structure_last_low_label",
    )
    states: Dict[str, Dict] = {}
    for timeframe in ("30m", "1h", "4h"):
        state: Dict[str, object] = {"timeframe": timeframe}
        for field in state_fields:
            value = row.get(f"s8_{timeframe}_{field}")
            if pd.isna(value) if not isinstance(value, (dict, list)) else False:
                value = None
            elif field == "as_of":
                value = pd.Timestamp(value).isoformat()
            elif hasattr(value, "item"):
                value = value.item()
            state[field] = value
        state["available"] = state.get("available") is True or state.get("available") == 1
        state["supertrend_period"] = 10
        state["supertrend_multiplier"] = 3.0
        states[timeframe] = state
    return build_s8_context_from_states(states)


def backtest(ticker: str, days: int = 30, warmup: int = 60,
             tp_pct_override: Optional[float] = None,
             sl_pct_override: Optional[float] = None,
             df: Optional[pd.DataFrame] = None,
             config_manager=None,
             v2_perf_cache=None,
             v2_only: bool = False,
             allow_research_only_candidates: bool = False) -> Dict:
    """Replay live scoring + simulate trades. Returns metrics + trade list.

    tp/sl overrides (as fractions, e.g. 0.02) let a caller sweep exit calibration without
    re-fetching data. Pass `df` (from build_dataframe) to reuse fetched data across a sweep.

    ``allow_research_only_candidates`` is reserved for the training half of a
    faithful OOS replay.  It measures the same visible-but-non-executable V2
    setup used by forward research, and is never enabled by the live engine or
    by the OOS test half that determines a strategy's technical result.
    """
    from core.config import ConfigManager
    # Dependency injection is used only by the walk-forward validator.  It
    # lets an OOS test supply a temporary, training-window-only performance
    # snapshot without ever reading or writing the live cache.
    cm = config_manager or ConfigManager()
    cfg = cm.signal
    if df is None:
        df = build_dataframe(ticker, days)
    if df is None:
        return {"ticker": ticker, "error": "insufficient_data", "trades": []}

    profile_pref = str(cfg.get("risk_mode") or "MEDIUM").upper()
    if profile_pref not in SCORE_THRESHOLDS:
        profile_pref = "MEDIUM"
    if profile_pref == "PRO":
        profiles = ["PRO", "STRONG", "MEDIUM", "RELAX"]
    elif profile_pref == "STRONG":
        profiles = ["STRONG", "MEDIUM", "RELAX"]
    elif profile_pref == "RELAX":
        profiles = ["RELAX", "MEDIUM", "STRONG"]
    else:
        profiles = ["MEDIUM", "STRONG", "RELAX"]
    regime_cfg = cfg.get("regime", {}) or {}
    scoring_cfg = cfg.get("scoring")
    min_gap = int(cfg.get("min_candles_between_signals", 20))
    tp_pct = tp_pct_override if tp_pct_override is not None else float(cfg.get("profit_target_pct") or 5.0) / 100.0
    sl_pct = sl_pct_override if sl_pct_override is not None else float(cfg.get("stop_loss_pct") or 2.0) / 100.0
    # Exit-level parity: mirror live engine._calculate_exit_levels (engine.py:1806-1838)
    # + _resolve_atr_multipliers (1798-1804). Production config.json sets exit_mode=FIXED_PCT,
    # so the flat branch below is identical to pre-fix behavior (latent fix). The ATR branch
    # makes the ATR exit regime backtestable so replay matches live if exit_mode is flipped.
    exit_mode = str(cfg.get("exit_mode", "ATR")).upper()
    atr_mult_tp = float(cfg.get("atr_multiplier_tp", 2.0))
    atr_mult_sl = float(cfg.get("atr_multiplier_sl", 1.5))
    atr_mult_by_mode = cfg.get("atr_multipliers_by_mode", {}) or {}
    min_tp_pct = float(cfg.get("min_tp_pct", 0.0) or 0.0)  # PERCENT units (0.2 = 0.2%)
    bot = cm.bot or {}
    trail_on = bool(bot.get("trailing_enabled", True))
    # Parity defaults: match core/config.py DEFAULT_CONFIG["bot"] (1.0 / 0.5) and
    # core/exit_manager.py fallback (1.0 / 0.5). Pre-fix these fallbacks were
    # 2.0 / 0.8 -- divergent from live. In production deep_merge always supplies
    # 1.0/0.5 so the divergence was latent, but any path with a missing bot dict
    # would have replayed on different trailing params than live. Aligned for safety.
    act_r = float(bot.get("trailing_activation_r", 1.0))
    factor = float(bot.get("trailing_factor", 0.5))
    try:
        cost_model = resolve_trading_cost_model(ticker, strategy_v2_config=cm.strategy_v2)
    except ValueError as exc:
        return {
            "ticker": ticker,
            "days": days,
            "error": "cost_model_unavailable",
            "detail": str(exc),
            "trades": [],
        }
    comm = cost_model.commission_fraction

    # === V2 replay config (BOTH_AUTO_BEST / V2) ===
    # When signal_mode enables V2, drive StrategySelector per bar (look-ahead-safe
    # context built from df.iloc[:i+1]) and let V2 override the V1 candidate when
    # its total_score >= min_edge_score -- mirroring the live engine's BOTH_AUTO_BEST.
    # NOTE: validation_score (25% of V2 total) comes from perf_cache, which is COLD
    # in a historical replay (as it is live at startup) -> V2 total caps ~75 -> the
    # >=min_edge_score gate usually defers to V1. This is FAITHFUL to live cold-start;
    # seeding perf from walk-forward is a deferred follow-up. News catalyst is also
    # neutral per-bar (build_news_pulse returns current news, not historical), so
    # catalyst_score stays 0 in replay -- same as live before the Pasul 1 wiring.
    v2_cfg = cm.strategy_v2 or {}
    signal_mode_v2 = str(v2_cfg.get("signal_mode", "V1")).upper()
    v2_enabled = signal_mode_v2 in ("V2", "BOTH", "BOTH_AUTO_BEST", "V2_AUTO")
    min_edge_score = int((cm.bot or {}).get("min_edge_score", 75))
    v2_selector = None
    v2_build_context = None
    infer_asset_class = None
    v2_asset_class = ""
    v2_ohlc_features = None
    v2_s8_mtf_features = None
    if v2_enabled:
        try:
            from core.strategy_v2.context_builder import build_context as _v2_bc, infer_asset_class as _iac
            from core.strategy_v2.selector import StrategySelector
            v2_build_context = _v2_bc
            infer_asset_class = _iac
            v2_selector = StrategySelector(
                config=v2_cfg,
                perf_cache=v2_perf_cache or _ColdReplayPerformanceCache(),
            )
            v2_asset_class = infer_asset_class(ticker)
            v2_ohlc_features = _precompute_v2_ohlc_features(df, v2_asset_class)
            v2_s8_mtf_features = _precompute_s8_mtf_features(df)
        except Exception as exc:
            logger.debug(f"V2 selector unavailable for backtest, V1-only replay: {exc}")
            v2_enabled = False

    rows = df.reset_index(drop=False)
    n = len(rows)
    trades: List[Dict] = []
    last_signal_idx = -10 ** 9
    i = warmup
    while i < n - 1:
        row = rows.iloc[i]
        if i - last_signal_idx < min_gap:
            i += 1
            continue
        ctx = row_to_context(row)
        regime, _ = detect_market_regime(ctx, regime_cfg)
        ctx.market_regime = regime
        modes = ["PULLBACK", "BREAKOUT", "CONTRA"]
        if cfg.get("enable_regime_gating", True):
            allowed = allowed_modes_for_regime(regime, regime_cfg)
            modes = [m for m in modes if m in allowed]
        best = None
        # Iterate ALL allowed modes (PULLBACK/BREAKOUT/CONTRA) and pick the
        # global max-total_score candidate via the `> best.total_score` check.
        # Pre-fix an `if best is not None: break` here exited the outer mode
        # loop as soon as the FIRST mode (PULLBACK) yielded any valid signal,
        # so BREAKOUT/CONTRA were never scored even when they'd score higher
        # -> backtest was biased to PULLBACK-by-iteration-order and understated
        # achievable performance (contra "axează-te foarte mult pe backtest").
        if not v2_only:
            for mode in modes:
                for profile in profiles:
                    res = compute_signal_score(ctx, mode, profile, scoring_cfg)
                    if res.is_valid and (best is None or res.total_score > best.total_score):
                        best = res
        # === V2 override (BOTH_AUTO_BEST) ===
        # Drive the live StrategySelector on a look-ahead-safe context (df up to bar i)
        # and, if its best candidate clears min_edge_score, replace the V1 `best` with
        # the V2 candidate -- exactly the live BOTH_AUTO_BEST selection path.
        v2_active = False
        v2_research_only = False
        if v2_enabled and v2_selector is not None:
            try:
                close_px = float(row.get("close", 0.0))
                watch_row_v2 = {
                    "price": close_px,
                    "change_pct": 0.0,
                    "volume": float(row.get("volume", 0.0)),
                    # news is current-only (no per-bar history) -> catalyst stays 0
                    # in replay, identical to live before the Pasul 1 wiring.
                }
                v2_ctx = v2_build_context(
                    symbol=ticker,
                    asset_class=v2_asset_class,
                    watch_row=watch_row_v2,
                    row_context=ctx,
                    precomputed_ohlc_features=v2_ohlc_features.iloc[i].to_dict(),
                )
                if v2_s8_mtf_features is not None and not v2_s8_mtf_features.empty:
                    v2_ctx.update(_s8_context_from_replay_row(v2_s8_mtf_features.iloc[i]))
                v2_res = v2_selector.select(ticker, v2_asset_class, v2_ctx)
                bc = v2_res.get("best_candidate") if isinstance(v2_res, dict) else None
                if bc is None and allow_research_only_candidates and isinstance(v2_res, dict):
                    research_candidates = v2_res.get("research_candidates")
                    if isinstance(research_candidates, list):
                        valid_research = [
                            item for item in research_candidates
                            if isinstance(item, dict)
                            and str(item.get("direction") or "").upper() in ("LONG", "SHORT")
                        ]
                        if valid_research:
                            bc = max(valid_research, key=lambda item: float(item.get("total_score", 0.0) or 0.0))
                            v2_research_only = True
                if bc and float(bc.get("total_score", 0.0)) >= min_edge_score:
                    bc_dir = str(bc.get("direction", "")).upper()
                    if bc_dir in ("LONG", "SHORT"):
                        best = SimpleNamespace(
                            direction=bc_dir,
                            total_score=int(round(float(bc.get("total_score", 0.0)))),
                            mode=f"V2:{bc.get('strategy_id', '')}",
                            profile="V2",
                            is_valid=True,
                            tp=float(bc.get("tp1", 0.0) or 0.0),
                            sl=float(bc.get("sl", 0.0) or 0.0),
                            strategy_id=str(bc.get("strategy_id") or ""),
                            signal_reference_price=close_px,
                            strategy_evidence=dict((bc.get("meta") or {}).get("strategy_evidence") or {})
                            if isinstance(bc.get("meta"), dict) else {},
                        )
                        v2_active = True
            except Exception as exc:
                logger.debug(f"V2 select failed at bar {i} for {ticker}: {exc}")

        # V2 has independent setup and validation gates.  Requiring a V1
        # candidate in the same bar is not how the live engine behaves and
        # prevents a faithful V2 walk-forward replay from observing valid V2
        # entries.  We still fall back to V1 when neither layer qualifies.
        if best is None or best.direction not in ("LONG", "SHORT"):
            i += 1
            continue

        # --- enter at next bar open with live costs ---
        entry_idx = i + 1
        entry = float(rows.iloc[entry_idx]["open"])
        is_long = best.direction == "LONG"
        slip = entry * cost_model.entry_slippage_fraction
        fill = entry + slip if is_long else entry - slip
        # Exit levels: live execution converts the signal's absolute V2 levels
        # into percentages around its reference price, then Capital applies those
        # percentages to the verified execution fill. Rebase identically here.
        # Keeping the original absolute levels after a next-bar gap made replay
        # risk/reward diverge from live and could even put TP behind the fill.
        v2_tp = float(getattr(best, "tp", 0.0) or 0.0)
        v2_sl = float(getattr(best, "sl", 0.0) or 0.0)
        v2_reference = float(getattr(best, "signal_reference_price", 0.0) or 0.0)
        if v2_active and v2_tp > 0 and v2_sl > 0 and v2_reference > 0:
            v2_sl_pct = abs((v2_sl - v2_reference) / v2_reference)
            v2_tp_pct = abs((v2_tp - v2_reference) / v2_reference)
            if is_long:
                sl, tp = fill * (1 - v2_sl_pct), fill * (1 + v2_tp_pct)
            else:
                sl, tp = fill * (1 + v2_sl_pct), fill * (1 - v2_tp_pct)
        elif exit_mode == "FIXED_PCT":
            # Flat pct -- identical to live FIXED_PCT branch (production config).
            sl, tp = (fill * (1 - sl_pct), fill * (1 + tp_pct)) if is_long \
                     else (fill * (1 + sl_pct), fill * (1 - tp_pct))
        else:  # ATR (live V1 default when exit_mode != FIXED_PCT)
            atr_val = float(row.get("atr_5m") or row.get("atr") or 0.0)
            # L3: guard against NaN. `row.get("atr_5m") or row.get("atr")` returns
            # the 1m `atr` when atr_5m is 0.0 (falsy), and the 1m ATR is NaN for
            # the first atr_period bars (calculate_atr warmup). `NaN or X` returns
            # NaN (NaN is truthy in Python), so atr_val becomes NaN. The old
            # `if atr_val <= 0` check did NOT catch NaN (`NaN <= 0` is False),
            # so the ATR branch ran with NaN -> tp/sl became NaN -> no exit ever
            # triggered (NaN comparisons are False) -> every such trade fell to
            # time_exit at the last bar, understating the strategy. `not (x > 0)`
            # catches NaN (NaN > 0 is False), 0, and negatives -> flat fallback.
            if not (atr_val > 0):  # ATR unavailable/NaN -> safe flat fallback
                sl, tp = (fill * (1 - sl_pct), fill * (1 + tp_pct)) if is_long \
                         else (fill * (1 + sl_pct), fill * (1 - tp_pct))
            else:
                mc = atr_mult_by_mode.get(str(best.mode).upper(), {}) \
                    if isinstance(atr_mult_by_mode, dict) else {}
                # L4: V2 candidates (mode "V2:S1".."V2:S6") never match the V1
                # atr_multipliers_by_mode keys (PULLBACK/BREAKOUT/CONTRA), so they
                # silently fell back to the GLOBAL atr_mult_tp/sl defaults instead
                # of the PULLBACK mults (V2 S1 TrendPullback IS a pullback; the
                # conservative base). When PULLBACK overrides the globals, V2
                # trades used the wrong (non-PULLBACK) ATR distances. Map V2 ->
                # the PULLBACK base so the fallback is intentional, not an
                # accident of the global default happening to equal PULLBACK.
                # Win-rate-neutral when PULLBACK isn't separately configured.
                if not mc and str(best.mode).upper().startswith("V2:"):
                    mc = atr_mult_by_mode.get("PULLBACK", {}) \
                        if isinstance(atr_mult_by_mode, dict) else {}
                m_tp = float(mc.get("tp", atr_mult_tp))
                m_sl = float(mc.get("sl", atr_mult_sl))
                tp_dist = atr_val * m_tp
                min_tp_dist = fill * (min_tp_pct / 100.0) if fill > 0 and min_tp_pct > 0 else 0.0
                if is_long:
                    tp = fill + max(tp_dist, min_tp_dist)
                    sl = fill - atr_val * m_sl
                else:
                    tp = fill - max(tp_dist, min_tp_dist)
                    sl = fill + atr_val * m_sl
        risk = abs(fill - sl)
        trail_sl = 0.0
        exit_price, reason, exit_idx = None, "time_exit", n - 1
        for j in range(entry_idx, n):
            hi, lo = float(rows.iloc[j]["high"]), float(rows.iloc[j]["low"])
            # M2: gap-through fill at open. Pre-fix every exit filled at the
            # exact tp/sl/trail LEVEL. Live fills at the open when a bar gaps
            # PAST the level (overnight/session gap, or a wide 1m bar whose
            # open is already beyond the stop): a gap-down below the SL fills
            # at the (worse) open, a gap-up above the TP fills at the (better)
            # open. Filling at the level was optimistic for stops (smaller
            # losses -> inflated win-rate vs the 30% live vs 60% backtest gap,
            # the profitability root cause) and pessimistic for TPs. Now: if
            # the bar opens beyond the level, fill at the open; else the level.
            opn = float(rows.iloc[j]["open"])
            if is_long:
                if lo <= sl:
                    exit_price, reason, exit_idx = min(sl, opn), "stop_loss", j; break
                if hi >= tp:
                    exit_price, reason, exit_idx = max(tp, opn), "take_profit", j; break
                if trail_sl > 0 and lo <= trail_sl:
                    exit_price, reason, exit_idx = min(trail_sl, opn), "trailing_stop", j; break
            else:
                if hi >= sl:
                    exit_price, reason, exit_idx = max(sl, opn), "stop_loss", j; break
                if lo <= tp:
                    exit_price, reason, exit_idx = min(tp, opn), "take_profit", j; break
                if trail_sl > 0 and hi >= trail_sl:
                    exit_price, reason, exit_idx = max(trail_sl, opn), "trailing_stop", j; break
            if trail_on:
                fav_px = hi if is_long else lo
                fav = (fav_px - fill) if is_long else (fill - fav_px)
                if risk > 0 and fav / risk >= act_r:
                    new = fill + fav * factor if is_long else fill - fav * factor
                    trail_sl = max(trail_sl, new) if is_long else (new if trail_sl <= 0 else min(trail_sl, new))
        if exit_price is None:
            exit_price = float(rows.iloc[n - 1]["close"])
        # Historical candles do not contain order-level fills.  Apply the
        # configured, adverse exit assumption and save the exact model with the
        # report; do not describe this value as an exchange fee or actual fill.
        exit_slip = exit_price * cost_model.exit_slippage_fraction(reason)
        exit_price = exit_price - exit_slip if is_long else exit_price + exit_slip
        gross = (exit_price - fill) if is_long else (fill - exit_price)
        r_mult = gross / risk if risk > 0 else 0.0
        entry_commission_per_unit = fill * comm
        exit_commission_per_unit = exit_price * comm
        commission_per_unit = entry_commission_per_unit + exit_commission_per_unit
        net_price_pnl = gross - commission_per_unit
        net_pct = net_price_pnl / fill
        # Net R-multiple (after round-trip commission, in R units). net_pct>0
        # <=> r_net>0 when risk>0, so win/loss membership decided on net_pct is
        # consistent with R-magnitude decided on r_net — avg_win_r/avg_loss_r no
        # longer mix a net-based membership with a gross-based magnitude (which
        # put small gross-winners that net-lose after commission into loss_rs
        # with a *positive* R, corrupting avg_loss_r). "r" stays the gross
        # R-multiple for legacy/parity consumers.
        r_net = net_price_pnl / risk if risk > 0 else 0.0
        # entry_time/exit_time (ISO) so the UI trade table + parity tests can locate
        # the signal bar (signal bar = entry bar - 1, since we enter at next-bar open).
        _et = rows.iloc[entry_idx]["dt"]
        _xt = rows.iloc[exit_idx]["dt"]
        entry_time = _et.isoformat() if hasattr(_et, "isoformat") else str(_et)
        exit_time = _xt.isoformat() if hasattr(_xt, "isoformat") else str(_xt)
        trades.append({"ticker": ticker, "dir": best.direction, "entry": fill, "exit": exit_price,
                       "reason": reason, "r": round(r_mult, 3), "r_net": round(r_net, 3),
                       "net_pct": round(net_pct * 100, 3),
                       "commission_cost_pct": round((commission_per_unit / fill) * 100, 6),
                       "stop_loss_price": round(sl, 8), "take_profit_price": round(tp, 8),
                       "signal_reference_price": round(v2_reference, 8) if v2_active else None,
                       "score": round(best.total_score, 1), "mode": best.mode, "profile": best.profile,
                       "strategy_source": "V2" if v2_active else "V1",
                       "strategy_id": str(getattr(best, "strategy_id", "") or "") if v2_active else "",
                       "strategy_evidence": dict(getattr(best, "strategy_evidence", {}) or {}) if v2_active else {},
                       "research_only": bool(v2_research_only) if v2_active else False,
                       "entry_time": entry_time, "exit_time": exit_time})
        last_signal_idx = i
        # Post-close per-ticker cooldown (parity with live
        # signals.can_open_new_signal_for_ticker, default signal_cooldown_minutes=15).
        # Live blocks re-entry on a ticker for N real minutes after a close; pre-fix
        # replay re-entered on the very next bar (exit_idx + 1), overstating trade
        # count / total_net_pct with quick re-entries live's lockout filters out.
        cooldown_minutes = int(cfg.get("signal_cooldown_minutes", 15) or 0)
        if cooldown_minutes > 0:
            try:
                _bar_dt = pd.Timedelta(rows["dt"].iloc[1] - rows["dt"].iloc[0])
                bar_minutes = max(1, int(round(_bar_dt.total_seconds() / 60.0)))
            except Exception:
                bar_minutes = 1
            cooldown_bars = max(1, cooldown_minutes // bar_minutes)
            i = exit_idx + 1 + cooldown_bars
        else:
            i = exit_idx + 1  # one position at a time (mirrors per-ticker cooldown)

    summary = _summarize(
        ticker,
        days,
        trades,
        min_trades_for_validation=int(cfg.get("backtest_min_trades_for_validation", 30) or 30),
    )
    summary["cost_model"] = cost_model.to_dict()
    return summary


def _summarize(
    ticker: str,
    days: int,
    trades: List[Dict],
    *,
    min_trades_for_validation: int = 30,
) -> Dict:
    n = len(trades)
    wins = [t for t in trades if t["net_pct"] > 0]
    losses = [t for t in trades if t["net_pct"] <= 0]
    rs = [t["r"] for t in trades]
    # Use the NET R-multiple (post round-trip commission) for the per-outcome
    # R averages so R-magnitude matches the net_pct-based win/loss membership
    # above (net_pct>0 <=> r_net>0 when risk>0). rs/avg_r keep the gross R for
    # the legacy avg_R alias. Fall back to gross "r" for trade dicts that
    # predate the r_net field (backward compatible with external callers).
    win_rs = [t.get("r_net", t["r"]) for t in wins]
    loss_rs = [t.get("r_net", t["r"]) for t in losses]
    avg_w = sum(t["net_pct"] for t in wins) / len(wins) if wins else 0.0
    avg_l = sum(t["net_pct"] for t in losses) / len(losses) if losses else 0.0
    gross_w = sum(t["net_pct"] for t in wins)
    gross_l = abs(sum(t["net_pct"] for t in losses))
    avg_r = sum(rs) / n if n else 0.0
    # M3: expectancy_r is the headline R-multiple expectancy and must reflect
    # round-trip commission (NET R), consistent with avg_win_r/avg_loss_r which
    # are already net. Pre-fix expectancy_r aliased gross avg_r, so a strategy
    # with high gross expectancy but heavy commissions showed an inflated
    # headline while avg_win_r/avg_loss_r told the net story -- inconsistent
    # and overstated the edge feeding calibration decisions. avg_R stays gross
    # for legacy/parity consumers; expectancy_r is now the honest net expectancy.
    avg_r_net = sum(t.get("r_net", t["r"]) for t in trades) / n if n else 0.0
    avg_win_r = sum(win_rs) / len(win_rs) if win_rs else 0.0
    avg_loss_r = sum(loss_rs) / len(loss_rs) if loss_rs else 0.0
    # Peak-to-trough drawdown on the running cumulative net_pct curve (in pct,
    # consistent with quick_backtester.max_drawdown_pct).
    max_drawdown_pct = 0.0
    if trades:
        cumul = 0.0
        peak = 0.0
        for t in trades:
            cumul += t["net_pct"]
            peak = max(peak, cumul)
            dd = cumul - peak
            if dd < max_drawdown_pct:
                max_drawdown_pct = dd
        max_drawdown_pct = round(abs(max_drawdown_pct), 2)
    from collections import Counter
    win_rate = round(len(wins) / n * 100, 1) if n else 0.0
    rr = round(avg_w / abs(avg_l), 2) if avg_l else None
    # A finite PF cannot be calculated without a losing trade. Reporting zero
    # would falsely imply losses dominated; infinity would overstate one-sided,
    # small samples. Keep it explicitly unavailable instead.
    profit_factor = round(gross_w / gross_l, 2) if gross_l else None
    min_trades_for_validation = max(1, int(min_trades_for_validation or 30))
    evidence_sufficient = n >= min_trades_for_validation
    warnings = []
    if not evidence_sufficient:
        warnings.append(
            f"Insufficient evidence: {n} closed replay trades; need at least {min_trades_for_validation} before treating metrics as validation."
        )
    if gross_l == 0:
        warnings.append("Profit factor and payoff ratio are unavailable because this sample has no losing trades.")
    return {
        "ticker": ticker, "days": days, "trades": n,
        "wins": len(wins), "losses": len(losses),
        "win_rate": win_rate,
        "win_rate_trades": win_rate,  # trade-based alias for the UI contract
        "avg_R": round(avg_r, 3),
        "expectancy_r": round(avg_r_net, 3),  # NET R-multiple expectancy (post-commission)
        "avg_win_r": round(avg_win_r, 3),
        "avg_loss_r": round(avg_loss_r, 3),
        "expectancy_pct": round(sum(t["net_pct"] for t in trades) / n, 3) if n else 0.0,
        "rr": rr,
        "payoff_ratio": rr,  # alias of rr for the UI contract
        "profit_factor": profit_factor,
        "max_drawdown_pct": max_drawdown_pct,
        "total_net_pct": round(sum(t["net_pct"] for t in trades), 2),
        "exit_mix": dict(Counter(t["reason"] for t in trades)),
        "evidence": {
            "sample_size": n,
            "minimum_sample": min_trades_for_validation,
            "sufficient": evidence_sufficient,
            "status": "validated_sample_size" if evidence_sufficient else "insufficient_sample",
        },
        "data_limitations": [
            "No timestamped historical news archive is available for this replay; historical AI-news gating is not simulated.",
        ],
        "warnings": warnings,
        "trade_list": trades,
    }


if __name__ == "__main__":
    import sys
    tk = sys.argv[1] if len(sys.argv) > 1 else "SOL-USD"
    d = int(sys.argv[2]) if len(sys.argv) > 2 else 14
    res = backtest(tk, d)
    res.pop("trade_list", None)
    print(json.dumps(res, indent=2))
