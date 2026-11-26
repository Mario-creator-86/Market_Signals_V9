"""Forward-only, cross-sectional crypto momentum research.

The model is deliberately a bounded paper-research source.  It uses a liquid
Binance universe, completed hourly candles, 7d/30d momentum, an EMA trend
filter, and ATR risk levels.  It is not a Capital strategy: historical
selection would require a point-in-time constituent history and the normal
faithful V2 / realised-Capital gates still reject every output from this module.
"""

from __future__ import annotations

import hashlib
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from core.config import is_crypto_ticker, normalize_market_ticker
from core.crypto_research_strategies import fetch_research_ohlcv


DEFAULT_CROSS_SECTIONAL_MOMENTUM_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "max_universe_symbols": 6,
    "max_candidates_per_cycle": 1,
    "max_fetch_workers": 6,
    "min_quote_volume_24h": 50_000_000,
    "excluded_base_assets": [
        "USDT", "USDC", "FDUSD", "TUSD", "USDP", "BUSD", "DAI", "USD1", "PYUSD", "RLUSD", "USDE",
    ],
    "history_days": 35,
    "short_lookback_hours": 24 * 7,
    "long_lookback_hours": 24 * 30,
    "fast_ema_hours": 20,
    "slow_ema_hours": 50,
    "atr_period_hours": 14,
    "min_short_return_pct": 2.0,
    "min_long_return_pct": 5.0,
    "min_vol_adjusted_short_return": 0.75,
    "stop_atr": 2.0,
    "target_atr": 3.5,
    "max_completed_bar_age_seconds": 7_500,
    "min_signal_score": 85.0,
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _config(execution_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    execution = execution_config if isinstance(execution_config, dict) else {}
    configured = execution.get("cross_sectional_momentum")
    cfg = dict(DEFAULT_CROSS_SECTIONAL_MOMENTUM_CONFIG)
    if isinstance(configured, dict):
        cfg.update(configured)
    excluded = cfg.get("excluded_base_assets")
    cfg["excluded_base_assets"] = {
        str(value or "").upper().strip()
        for value in (excluded if isinstance(excluded, list) else [])
        if str(value or "").strip()
    }
    return cfg


def _ticker_from_symbol(value: Any) -> Tuple[str, str]:
    raw = str(value or "").upper().strip()
    if raw.endswith("USDT"):
        base = raw[:-4]
    elif raw.endswith("USDC"):
        base = raw[:-4]
    else:
        return "", ""
    ticker = normalize_market_ticker(f"{base}-USD") if base else ""
    return ticker, base


def _eligible_item(item: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    ticker, base = _ticker_from_symbol(item.get("symbol") or item.get("ticker"))
    if not ticker or not base or base in cfg["excluded_base_assets"] or not is_crypto_ticker(ticker):
        return False
    quote_volume = _number(item.get("quoteVolume", item.get("quote_volume", item.get("volume"))), 0.0)
    return quote_volume >= max(0.0, _number(cfg.get("min_quote_volume_24h"), 0.0))


def _atr(frame: pd.DataFrame, period: int) -> pd.Series:
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1.0 / max(1, period), adjust=False, min_periods=max(1, period)).mean()


def _completed_hourly_frame(ticker: str, cfg: Dict[str, Any]) -> Optional[pd.DataFrame]:
    raw = fetch_research_ohlcv(ticker, days=max(1, int(_number(cfg.get("history_days"), 35))), interval="1h")
    if not isinstance(raw, pd.DataFrame) or raw.empty or bool((getattr(raw, "attrs", {}) or {}).get("source_truncated")):
        return None
    frame = raw.loc[:, ["open", "high", "low", "close", "volume"]].copy().sort_index()
    for column in frame.columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna()
    # Binance includes the in-progress candle.  It cannot participate in a
    # decision because its final high/low/close are unknown.
    return frame.iloc[:-1].copy() if len(frame) > 1 else None


def _observation(item: Dict[str, Any], cfg: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    symbol = str(item.get("symbol") or item.get("ticker") or "").upper().strip()
    ticker, _base = _ticker_from_symbol(symbol)
    quote_volume = _number(item.get("quoteVolume", item.get("quote_volume", item.get("volume"))), 0.0)
    base_diagnostic = {"symbol": symbol, "ticker": ticker, "quote_volume_24h": round(quote_volume, 4)}
    frame = _completed_hourly_frame(ticker, cfg)
    needed = max(
        int(_number(cfg.get("long_lookback_hours"), 720)),
        int(_number(cfg.get("slow_ema_hours"), 50)),
        int(_number(cfg.get("atr_period_hours"), 14)),
    ) + 2
    if frame is None or len(frame) < needed:
        return None, {**base_diagnostic, "status": "insufficient_ohlcv", "reason": "Completed hourly OHLCV coverage is insufficient"}
    last_open = pd.Timestamp(frame.index[-1])
    if last_open.tzinfo is None:
        last_open = last_open.tz_localize("UTC")
    else:
        last_open = last_open.tz_convert("UTC")
    completed_at = last_open + pd.Timedelta(hours=1)
    bar_age = max(0.0, (pd.Timestamp.now(tz="UTC") - completed_at).total_seconds())
    max_age = max(1.0, _number(cfg.get("max_completed_bar_age_seconds"), 7_500.0))
    if bar_age > max_age:
        return None, {
            **base_diagnostic,
            "status": "stale_hourly_ohlcv",
            "reason": "Latest completed hourly candle is too old",
            "bar_age_seconds": round(bar_age, 3),
        }

    close = frame["close"]
    short_hours = max(1, int(_number(cfg.get("short_lookback_hours"), 168)))
    long_hours = max(short_hours + 1, int(_number(cfg.get("long_lookback_hours"), 720)))
    fast_hours = max(1, int(_number(cfg.get("fast_ema_hours"), 20)))
    slow_hours = max(fast_hours + 1, int(_number(cfg.get("slow_ema_hours"), 50)))
    atr = _atr(frame, max(1, int(_number(cfg.get("atr_period_hours"), 14))))
    last_close = _number(close.iloc[-1], 0.0)
    prior_short = _number(close.iloc[-1 - short_hours], 0.0)
    prior_long = _number(close.iloc[-1 - long_hours], 0.0)
    last_atr = _number(atr.iloc[-1], 0.0)
    if min(last_close, prior_short, prior_long, last_atr) <= 0:
        return None, {**base_diagnostic, "status": "invalid_indicators", "reason": "Close, momentum, or ATR is unavailable"}

    short_return = ((last_close / prior_short) - 1.0) * 100.0
    long_return = ((last_close / prior_long) - 1.0) * 100.0
    hourly_returns = close.pct_change().iloc[-short_hours:].dropna()
    realised_vol_pct = _number(hourly_returns.std(ddof=0) * math.sqrt(short_hours) * 100.0, 0.0)
    if realised_vol_pct <= 0:
        return None, {**base_diagnostic, "status": "invalid_volatility", "reason": "Realised volatility is unavailable"}
    vol_adjusted_short_return = short_return / realised_vol_pct
    ema_fast = _number(close.ewm(span=fast_hours, adjust=False, min_periods=fast_hours).mean().iloc[-1], 0.0)
    ema_slow = _number(close.ewm(span=slow_hours, adjust=False, min_periods=slow_hours).mean().iloc[-1], 0.0)
    trend_aligned = last_close > ema_fast > ema_slow
    return {
        "symbol": symbol,
        "ticker": ticker,
        "quote_volume_24h": quote_volume,
        "close": last_close,
        "atr": last_atr,
        "short_return_pct": short_return,
        "long_return_pct": long_return,
        "realised_vol_pct": realised_vol_pct,
        "vol_adjusted_short_return": vol_adjusted_short_return,
        "trend_aligned": trend_aligned,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "completed_bar_at": completed_at.isoformat(),
        "completed_bar_age_seconds": round(bar_age, 3),
    }, {**base_diagnostic, "status": "observed", "reason": "Completed hourly observation is available"}


def _signal_id(ticker: str, completed_bar_at: str) -> str:
    digest = hashlib.sha1(f"cross-sectional-momentum:{ticker}:{completed_bar_at}".encode("utf-8")).hexdigest()[:12]
    return f"csm_forward_{ticker.replace('-', '')}_{digest}"


def build_cross_sectional_momentum_candidates(
    scanner_items: Iterable[Dict[str, Any]],
    *,
    execution_config: Optional[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build bounded forward-paper candidates and an audit-friendly diagnostic."""
    cfg = _config(execution_config)
    if not bool(cfg.get("enabled", False)):
        return [], {"enabled": False, "status": "disabled", "evaluated": 0, "candidate_count": 0}

    provided_items = [item for item in scanner_items if isinstance(item, dict)]
    eligible_items = [item for item in provided_items if _eligible_item(item, cfg)]
    configured_universe_limit = max(1, int(_number(cfg.get("max_universe_symbols"), 6)))
    ranked_items = sorted(
        eligible_items,
        key=lambda item: _number(item.get("quoteVolume", item.get("quote_volume", item.get("volume"))), 0.0),
        reverse=True,
    )[:configured_universe_limit]
    universe_diagnostic = {
        "provided_scanner_items": len(provided_items),
        "eligible_liquid_universe_size": len(eligible_items),
        "configured_universe_limit": configured_universe_limit,
        "min_quote_volume_24h": round(_number(cfg.get("min_quote_volume_24h"), 0.0), 4),
    }
    observations: List[Dict[str, Any]] = []
    diagnostics: List[Dict[str, Any]] = []
    # OHLCV requests are independent.  Bounded concurrency keeps the live
    # decision fresh when the liquid universe is widened, without changing
    # the pre-registered ranking or accepting a lower-quality observation.
    workers = min(
        len(ranked_items),
        max(1, int(_number(cfg.get("max_fetch_workers"), 6))),
    )
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="csm-ohlcv") as executor:
            observation_rows = list(executor.map(lambda item: _observation(item, cfg), ranked_items))
    else:
        observation_rows = [_observation(item, cfg) for item in ranked_items]
    for observation, diagnostic in observation_rows:
        diagnostics.append(diagnostic)
        if observation is not None:
            observations.append(observation)
    if not observations:
        return [], {
            "enabled": True,
            "status": "insufficient_market_data",
            "universe_size": len(ranked_items),
            **universe_diagnostic,
            "evaluated": len(diagnostics),
            "evaluations": diagnostics,
            "candidate_count": 0,
        }

    ranked_observations = sorted(
        observations,
        key=lambda row: (
            _number(row.get("vol_adjusted_short_return")),
            _number(row.get("long_return_pct")),
            _number(row.get("quote_volume_24h")),
        ),
        reverse=True,
    )
    candidates: List[Dict[str, Any]] = []
    minimum_short = _number(cfg.get("min_short_return_pct"), 2.0)
    minimum_long = _number(cfg.get("min_long_return_pct"), 5.0)
    minimum_vol_adjusted = _number(cfg.get("min_vol_adjusted_short_return"), 0.75)
    min_signal_score = _number(cfg.get("min_signal_score"), 85.0)
    for rank, row in enumerate(ranked_observations, start=1):
        quality = min(100.0, max(0.0, 60.0 + (20.0 * _number(row["vol_adjusted_short_return"])) + (8.0 * min(3.0, _number(row["long_return_pct"]) / 10.0))))
        trend_separation_atr = abs(_number(row["ema_fast"]) - _number(row["ema_slow"])) / max(_number(row["atr"]), 1e-12)
        trend_quality = min(100.0, 50.0 + (20.0 * trend_separation_atr))
        liquidity_quality = min(100.0, 60.0 + (10.0 * math.log10(max(1.0, _number(row["quote_volume_24h"]) / max(1.0, _number(cfg["min_quote_volume_24h"]))))))
        signal_score = round((0.55 * 100.0) + (0.20 * quality) + (0.15 * trend_quality) + (0.10 * liquidity_quality), 2)
        failed_checks: List[str] = []
        if rank != 1:
            failed_checks.append("not_top_cross_sectional_rank")
        if not bool(row["trend_aligned"]):
            failed_checks.append("ema_trend_alignment")
        if _number(row["short_return_pct"]) < minimum_short:
            failed_checks.append("short_momentum")
        if _number(row["long_return_pct"]) < minimum_long:
            failed_checks.append("long_momentum")
        if _number(row["vol_adjusted_short_return"]) < minimum_vol_adjusted:
            failed_checks.append("volatility_adjusted_momentum")
        if signal_score < min_signal_score:
            failed_checks.append("minimum_signal_score")
        eligible = not failed_checks
        diagnostics.append({
            "symbol": row["symbol"],
            "status": "eligible" if eligible else "not_selected",
            "reason": "Top cross-sectional momentum observation" if eligible else f"Failed: {', '.join(failed_checks)}",
            "failed_checks": failed_checks,
            "rank": rank,
            "short_return_pct": round(_number(row["short_return_pct"]), 4),
            "long_return_pct": round(_number(row["long_return_pct"]), 4),
            "vol_adjusted_short_return": round(_number(row["vol_adjusted_short_return"]), 4),
            "trend_aligned": bool(row["trend_aligned"]),
            "signal_score": signal_score,
        })
        if not eligible:
            continue
        entry = _number(row["close"])
        atr = _number(row["atr"])
        stop = entry - (max(0.1, _number(cfg.get("stop_atr"), 2.0)) * atr)
        target = entry + (max(0.1, _number(cfg.get("target_atr"), 3.5)) * atr)
        if not (stop > 0 and stop < entry < target):
            continue
        created_at = datetime.now(timezone.utc).isoformat()
        candidates.append({
            "id": _signal_id(str(row["ticker"]), str(row["completed_bar_at"])),
            "ticker": row["ticker"],
            "asset_class": "CRYPTO_FUTURES",
            "action": "BUY",
            "type": "BUY",
            "direction": "LONG",
            "status": "Open",
            "created_at": created_at,
            "source": "autonomous_monitor",
            "autonomous_source": "cross_sectional_momentum",
            "strategy_source": "CROSS_SECTIONAL_MOMENTUM_FORWARD_RESEARCH",
            "strategy_type": "CSM_7D_30D_LONG",
            "strategy_id": "CSM_7D_30D_LONG",
            "entry": round(entry, 8),
            "tp": round(target, 8),
            "sl": round(stop, 8),
            "signal_score": signal_score,
            "score": signal_score,
            "risk_reward_ratio": round((target - entry) / (entry - stop), 4),
            "performance_snapshot": {
                "validation_status": "forward_unvalidated",
                "validation_method": "pre_registered_cross_sectional_momentum_forward",
                "validation_as_of": created_at,
                "sample_size": 0,
                "profit_factor": None,
                "expectancy_R": 0.0,
                "costs_used": {},
            },
            "forward_validation": {
                "status": "forward_unvalidated",
                "method": "pre_registered_cross_sectional_momentum_forward",
                "reason": "Forward paper observation only; it cannot satisfy Capital validation or realised-edge gates",
                "hypothesis": "Top liquid 7d/30d volatility-adjusted momentum with hourly EMA trend alignment",
                "metrics": {
                    "cross_sectional_rank": rank,
                    "short_return_pct": round(_number(row["short_return_pct"]), 4),
                    "long_return_pct": round(_number(row["long_return_pct"]), 4),
                    "vol_adjusted_short_return": round(_number(row["vol_adjusted_short_return"]), 4),
                    "completed_bar_at": row["completed_bar_at"],
                    "completed_bar_age_seconds": row["completed_bar_age_seconds"],
                    "quote_volume_24h": round(_number(row["quote_volume_24h"]), 4),
                },
            },
            "reasons": [
                "Pre-registered forward research: top liquid cross-sectional 7d/30d momentum.",
                f"Volatility-adjusted 7d momentum {float(row['vol_adjusted_short_return']):.2f}; 30d return {float(row['long_return_pct']):.2f}%.",
                "Research cohort only; Capital remains blocked until independent V2 validation and realised-edge gates pass.",
            ],
        })
        if len(candidates) >= max(1, int(_number(cfg.get("max_candidates_per_cycle"), 1))):
            break

    return candidates, {
        "enabled": True,
        "status": "candidate_found" if candidates else "no_qualifying_cross_sectional_setup",
        "universe_size": len(ranked_items),
        **universe_diagnostic,
        "evaluated": len(observations),
        "evaluated_symbols": [str(row.get("symbol") or "") for row in observations],
        "evaluations": diagnostics,
        "candidate_count": len(candidates),
    }
