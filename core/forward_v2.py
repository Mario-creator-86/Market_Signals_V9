"""Live V2 setup discovery for isolated forward-paper evidence.

This module deliberately separates a *current V2 setup* from a validated
strategy. It may produce a bounded research candidate from current OHLCV and
the live V2 structural evaluators, but it never writes to the V2 performance
cache and never labels the candidate Capital-executable.
"""

from __future__ import annotations

import copy
import hashlib
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pandas as pd

from core.config import is_crypto_ticker, normalize_market_ticker
from core.strategy_v2.context_builder import build_context, infer_asset_class
from core.strategy_v2.selector import StrategySelector


DEFAULT_FORWARD_V2_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "asset_classes": ["CRYPTO_FUTURES", "US_STOCKS", "COMMODITIES"],
    "max_symbols_per_cycle": 4,
    "max_symbols_per_asset_class_per_cycle": 2,
    "max_fetch_workers": 6,
    "universe_size": 20,
    "rotation_interval_seconds": 120,
    "excluded_base_assets": [
        "USDT", "USDC", "FDUSD", "TUSD", "USDP", "BUSD", "DAI", "USD1", "PYUSD", "RLUSD", "USDE",
    ],
    # build_context consumes at most the latest 1,200 one-minute bars, so one
    # day retains the full causal feature window while avoiding an unused
    # third Binance pagination request per symbol.
    "history_days": 1,
    "htf_history_days": 3,
    "crypto_history_interval": "1m",
    "max_ohlcv_age_seconds": 120,
    "max_ohlcv_age_seconds_by_asset": {
        "CRYPTO_FUTURES": 120,
        "US_STOCKS": 900,
        "COMMODITIES": 900,
    },
    "yahoo_history_period": "5d",
    "yahoo_history_interval": "5m",
    "universe_symbols_by_asset_class": {
        "COMMODITIES": ["GC=F", "CL=F", "SI=F", "NG=F"],
    },
    "max_scanner_data_age_seconds": 120,
    "min_quote_volume_24h": 40_000_000,
    "min_setup_score": 64.0,
    "min_risk_score": 56.0,
    "min_forward_quality_score": 68.0,
    # News is evaluated by the independent autonomous News gate.  Keep the
    # forward setup-quality score price/risk based so a missing optional
    # crypto-news catalyst cannot silently reduce a valid technical setup.
    "forward_quality_setup_weight": 0.65,
    "forward_quality_risk_weight": 0.25,
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def forward_price_quality_score(
    setup_score: Any,
    risk_score: Any,
    config: Optional[Dict[str, Any]] = None,
) -> float:
    """Return the normalized technical quality used by forward V2 discovery.

    The historical formula assigned 10% to ``catalyst_score`` even though
    News is separately fail-closed later in the autonomous admission flow and
    is optional for non-major crypto.  A neutral/missing catalyst therefore
    made the stated technical threshold unattainable for otherwise valid
    price setups.  Preserve the original relative 65:25 setup/risk weighting
    while normalizing the available technical evidence to a 0..100 score.
    """
    cfg = config if isinstance(config, dict) else {}
    setup_weight = max(0.0, _number(cfg.get("forward_quality_setup_weight"), 0.65))
    risk_weight = max(0.0, _number(cfg.get("forward_quality_risk_weight"), 0.25))
    denominator = setup_weight + risk_weight
    if denominator <= 0:
        return 0.0
    return round(((_number(setup_score) * setup_weight) + (_number(risk_score) * risk_weight)) / denominator, 2)


def _utc_timestamp(value: Any) -> Optional[datetime]:
    try:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        return timestamp.tz_convert("UTC").to_pydatetime()
    except (TypeError, ValueError):
        return None


def _ticker_from_scanner_symbol(symbol: Any) -> str:
    raw = str(symbol or "").upper().strip()
    if raw.endswith("USDT"):
        raw = f"{raw[:-4]}-USD" if raw[:-4] else ""
    elif raw.endswith("USDC"):
        raw = f"{raw[:-4]}-USD" if raw[:-4] else ""
    return normalize_market_ticker(raw) if raw else ""


def _record_diagnostic(
    diagnostic: Optional[Dict[str, Any]],
    status: str,
    reason: str,
    **details: Any,
) -> None:
    """Populate caller-owned telemetry without changing the candidate contract."""
    if diagnostic is None:
        return
    diagnostic.clear()
    diagnostic.update({"status": status, "reason": reason, **details})


def forward_v2_config(execution_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    execution = execution_config if isinstance(execution_config, dict) else {}
    configured = execution.get("v2_forward")
    config = dict(DEFAULT_FORWARD_V2_CONFIG)
    if isinstance(configured, dict):
        config.update(configured)
    asset_classes = config.get("asset_classes")
    config["asset_classes"] = {
        str(value or "").upper().strip()
        for value in (asset_classes if isinstance(asset_classes, list) else [])
        if str(value or "").strip()
    }
    excluded_bases = config.get("excluded_base_assets")
    config["excluded_base_assets"] = {
        str(value or "").upper().strip()
        for value in (excluded_bases if isinstance(excluded_bases, list) else [])
        if str(value or "").strip()
    }
    return config


def is_forward_universe_item(item: Dict[str, Any], config: Dict[str, Any]) -> bool:
    """Return whether a scanner/watchlist item belongs to an enabled market."""
    raw = str((item or {}).get("symbol") or (item or {}).get("ticker") or "").upper().strip()
    ticker = _ticker_from_scanner_symbol(raw)
    if not ticker:
        return False
    asset_class = infer_asset_class(ticker)
    if config.get("asset_classes") and asset_class not in config.get("asset_classes", set()):
        return False
    if asset_class == "CRYPTO_FUTURES":
        if raw.endswith("USDT"):
            base = raw[:-4]
        elif raw.endswith("USDC"):
            base = raw[:-4]
        else:
            return False
        if not base or base in set(config.get("excluded_base_assets") or set()):
            return False
        quote_volume = _number((item or {}).get("quoteVolume", (item or {}).get("quote_volume", (item or {}).get("volume"))), 0.0)
        minimum = max(0.0, _number(config.get("min_quote_volume_24h"), 0.0))
        return quote_volume >= minimum
    return asset_class in {"US_STOCKS", "COMMODITIES"}


def _forward_ohlcv_dataframe(ticker: str, asset_class: str, config: Dict[str, Any]) -> Optional[pd.DataFrame]:
    if asset_class == "CRYPTO_FUTURES":
        # Forward discovery only needs causal raw OHLCV.  The historical
        # backtester builder also downloads and enriches several higher
        # timeframes, which made one autonomous cycle take minutes and could
        # expire otherwise-current scanner candidates before review.
        from core.crypto_research_strategies import fetch_research_ohlcv

        return fetch_research_ohlcv(
            ticker,
            days=max(1, int(_number(config.get("history_days"), 1))),
            interval=str(config.get("crypto_history_interval") or "1m"),
        )

    # Yahoo supplies intraday bars for US equities and listed commodity
    # futures. build_context calculates the same causal structure/liquidity/
    # volume/chart-pattern features directly from normalized raw OHLCV.
    try:
        from api.market_data_service import YahooClient

        dataframe = YahooClient.fetch_history(
            ticker,
            period=str(config.get("yahoo_history_period") or "5d"),
            interval=str(config.get("yahoo_history_interval") or "5m"),
        )
    except Exception:
        return None
    if not isinstance(dataframe, pd.DataFrame) or dataframe.empty:
        return None
    dataframe = dataframe.copy()
    dataframe.columns = [str(column).lower() for column in dataframe.columns]
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(set(dataframe.columns)):
        return None
    try:
        dataframe.index = pd.to_datetime(dataframe.index, utc=True)
    except Exception:
        return None
    dataframe = dataframe[["open", "high", "low", "close", "volume"]].sort_index()
    dataframe.attrs.update(
        {
            "source": "yahoo_intraday_ohlcv",
            "symbol": ticker,
            "interval": str(config.get("yahoo_history_interval") or "5m"),
            "source_truncated": False,
        }
    )
    return dataframe


def select_forward_universe(
    items: list[Dict[str, Any]],
    *,
    config: Dict[str, Any],
    rotate: bool,
    now_seconds: Optional[float] = None,
) -> list[Dict[str, Any]]:
    """Choose a bounded, liquid CEX universe for one forward-V2 cycle.

    Selection is deliberately based on 24h notional liquidity, not the largest
    daily move. A rotating window prevents BTC/ETH from monopolising every
    cycle and avoids the prior bias toward volatile small caps. Direction and
    entry quality remain the responsibility of the live V2 structural selector.
    """
    ranked = sorted(
        [item for item in items if isinstance(item, dict)],
        key=lambda item: (
            _number(item.get("quoteVolume", item.get("quote_volume", item.get("volume"))), 0.0),
            abs(_number(item.get("change_pct"), 0.0)),
        ),
        reverse=True,
    )
    per_cycle = max(1, int(_number(config.get("max_symbols_per_cycle"), 4)))
    universe_size = max(per_cycle, int(_number(config.get("universe_size"), per_cycle)))
    universe = ranked[:universe_size]
    if not rotate or len(universe) <= per_cycle:
        return universe[:per_cycle]

    rotation_interval = max(1.0, _number(config.get("rotation_interval_seconds"), 120.0))
    timestamp = time.time() if now_seconds is None else float(now_seconds)
    windows = max(1, math.ceil(len(universe) / per_cycle))
    slot = int(timestamp // rotation_interval) % windows
    start = slot * per_cycle
    return (universe[start:] + universe[:start])[:per_cycle]


def _forward_signal_id(ticker: str, strategy_id: str, direction: str) -> str:
    bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    digest = hashlib.sha1(f"v2-forward:{ticker}:{strategy_id}:{direction}:{bucket}".encode("utf-8")).hexdigest()[:12]
    return f"forward_v2_{ticker.replace('-', '')}_{strategy_id.lower()}_{direction.lower()}_{digest}"


def _forward_selector_config(strategy_v2_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Expose a structural setup without treating it as validated performance."""
    config = copy.deepcopy(strategy_v2_config or {})
    config.update(
        {
            "require_profitable_performance": False,
            "allow_low_sample_fallback": True,
            "allow_low_validation_fallback": True,
            "min_validation_score": 0.0,
        }
    )
    return config


def build_forward_v2_candidate(
    scanner_item: Dict[str, Any],
    *,
    strategy_v2_config: Optional[Dict[str, Any]],
    execution_config: Optional[Dict[str, Any]],
    diagnostic: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Return one strict V2 forward-research signal from a liquid scanner item.

    ``None`` means unavailable/stale/illiquid data or no qualifying V2 setup.
    No rejected input is converted into a trade.
    """
    item = scanner_item if isinstance(scanner_item, dict) else {}
    config = forward_v2_config(execution_config)
    if not bool(config.get("enabled", False)):
        _record_diagnostic(diagnostic, "disabled", "Forward V2 discovery is disabled")
        return None

    ticker = _ticker_from_scanner_symbol(item.get("symbol") or item.get("ticker"))
    if not ticker:
        _record_diagnostic(diagnostic, "invalid_symbol", "Scanner item has no supported market ticker")
        return None
    asset_class = infer_asset_class(ticker)
    if config["asset_classes"] and asset_class not in config["asset_classes"]:
        _record_diagnostic(diagnostic, "unsupported_asset_class", f"Asset class {asset_class} is not enabled")
        return None

    quote_volume = _number(item.get("quoteVolume", item.get("quote_volume", item.get("volume"))), 0.0)
    min_quote_volume = max(0.0, _number(config.get("min_quote_volume_24h"), 0.0))
    if asset_class == "CRYPTO_FUTURES" and quote_volume < min_quote_volume:
        _record_diagnostic(
            diagnostic,
            "illiquid",
            "24h quote volume is below the configured minimum",
            quote_volume_24h=round(quote_volume, 4),
            minimum_quote_volume_24h=round(min_quote_volume, 4),
        )
        return None

    dataframe = _forward_ohlcv_dataframe(ticker, asset_class, config)
    if not isinstance(dataframe, pd.DataFrame) or dataframe.empty or len(dataframe) < 220:
        _record_diagnostic(diagnostic, "insufficient_ohlcv", "Fresh OHLCV coverage is unavailable")
        return None
    if bool((getattr(dataframe, "attrs", {}) or {}).get("source_truncated")):
        _record_diagnostic(diagnostic, "truncated_ohlcv", "OHLCV source was explicitly truncated")
        return None

    last_bar_at = _utc_timestamp(dataframe.index[-1])
    if last_bar_at is None:
        _record_diagnostic(diagnostic, "invalid_ohlcv_timestamp", "Latest OHLCV bar has no valid timestamp")
        return None
    ohlcv_age = max(0.0, (datetime.now(timezone.utc) - last_bar_at).total_seconds())
    max_age_by_asset = config.get("max_ohlcv_age_seconds_by_asset") if isinstance(config.get("max_ohlcv_age_seconds_by_asset"), dict) else {}
    max_ohlcv_age = max(
        1.0,
        _number(max_age_by_asset.get(asset_class), _number(config.get("max_ohlcv_age_seconds"), 120.0)),
    )
    if ohlcv_age > max_ohlcv_age:
        _record_diagnostic(
            diagnostic,
            "stale_ohlcv",
            "Latest OHLCV bar exceeds the configured freshness limit",
            ohlcv_age_seconds=round(ohlcv_age, 3),
            max_ohlcv_age_seconds=round(max_ohlcv_age, 3),
        )
        return None

    close = _number(dataframe.iloc[-1].get("close"), 0.0)
    if close <= 0:
        _record_diagnostic(diagnostic, "invalid_price", "Latest OHLCV close is not positive")
        return None
    prior_index = max(0, len(dataframe) - 1441)
    prior_close = _number(dataframe.iloc[prior_index].get("close"), close)
    change_pct = ((close - prior_close) / prior_close * 100.0) if prior_close > 0 else 0.0
    if asset_class != "CRYPTO_FUTURES":
        # V2 quality gates compare real traded units. Scanner snapshots may not
        # carry volume for watchlist/futures symbols, so calculate it from the
        # same recent OHLCV used by the structural setup.
        quote_volume = _number(pd.to_numeric(dataframe["volume"], errors="coerce").tail(78).sum(), 0.0)
    context = build_context(
        symbol=ticker,
        asset_class=asset_class,
        watch_row={
            "price": close,
            "change_pct": change_pct,
            # QualityGates expects liquid notional, so use scanner 24h quote
            # volume rather than a single one-minute base-volume bar.
            "volume": quote_volume,
            "news_sentiment_trend": str(item.get("news_sentiment_trend") or "neutral"),
            "news_pulse_score": _number(item.get("news_pulse_score"), 0.0),
        },
        ohlc_df=dataframe,
    )
    selection = StrategySelector(config=_forward_selector_config(strategy_v2_config)).select(
        ticker, asset_class, context
    )
    candidate = selection.get("best_candidate") if isinstance(selection, dict) else None
    if not isinstance(candidate, dict):
        selector_rejections = selection.get("rejected", []) if isinstance(selection, dict) else []
        selector_regime = selection.get("regime", {}) if isinstance(selection, dict) else {}
        _record_diagnostic(
            diagnostic,
            "no_v2_setup",
            "No current V2 structural setup passed selector admission",
            ticker=ticker,
            quote_volume_24h=round(quote_volume, 4),
            ohlcv_age_seconds=round(ohlcv_age, 3),
            regime=selector_regime if isinstance(selector_regime, dict) else {},
            selector_rejections=[item for item in selector_rejections if isinstance(item, dict)][:8],
        )
        return None

    direction = str(candidate.get("direction") or "").upper()
    strategy_id = str(candidate.get("strategy_id") or "").upper()
    stop = _number(candidate.get("sl"), 0.0)
    target = _number(candidate.get("tp1", candidate.get("tp")), 0.0)
    setup_score = _number(candidate.get("setup_score"), 0.0)
    risk_score = _number(candidate.get("risk_score"), 0.0)
    catalyst_score = _number(candidate.get("catalyst_score"), 0.0)
    forward_quality_score = forward_price_quality_score(setup_score, risk_score, config)
    levels_valid = (
        (direction == "LONG" and stop < close < target)
        or (direction == "SHORT" and target < close < stop)
    )
    if (
        direction not in {"LONG", "SHORT"}
        or not strategy_id
        or not levels_valid
        or setup_score < _number(config.get("min_setup_score"), 80.0)
        or risk_score < _number(config.get("min_risk_score"), 70.0)
        or forward_quality_score < _number(config.get("min_forward_quality_score"), 85.0)
    ):
        _record_diagnostic(
            diagnostic,
            "quality_gate",
            "V2 setup did not satisfy forward structural-quality thresholds",
            ticker=ticker,
            direction=direction,
            strategy_id=strategy_id,
            levels_valid=levels_valid,
            setup_score=round(setup_score, 4),
            risk_score=round(risk_score, 4),
            catalyst_score=round(catalyst_score, 4),
            forward_quality_score=forward_quality_score,
        )
        return None

    action = "BUY" if direction == "LONG" else "SHORT"
    created_at = datetime.now(timezone.utc).isoformat()
    source_metadata = getattr(dataframe, "attrs", {}) or {}
    _record_diagnostic(
        diagnostic,
        "candidate",
        "Current V2 structural setup passed the forward research thresholds",
        ticker=ticker,
        direction=direction,
        strategy_id=strategy_id,
        forward_quality_score=forward_quality_score,
    )
    return {
        "id": _forward_signal_id(ticker, strategy_id, direction),
        "ticker": ticker,
        "asset_class": asset_class,
        "action": action,
        "type": action,
        "direction": direction,
        "status": "Open",
        "created_at": created_at,
        "source": "autonomous_monitor",
        "autonomous_source": "v2_forward",
        "strategy_source": "V2_FORWARD_RESEARCH",
        "strategy_type": strategy_id,
        "strategy_id": strategy_id,
        "entry": round(close, 8),
        "tp": round(target, 8),
        "sl": round(stop, 8),
        "signal_score": forward_quality_score,
        "score": forward_quality_score,
        "validation_score": 0.0,
        "setup_score": round(setup_score, 4),
        "risk_score": round(risk_score, 4),
        "catalyst_score": round(catalyst_score, 4),
        "risk_reward_ratio": round(_number(candidate.get("rr"), 0.0), 4),
        "performance_snapshot": {
            "validation_status": "forward_unvalidated",
            "validation_method": "live_v2_forward_observation",
            "validation_as_of": created_at,
            "sample_size": 0,
            "profit_factor": None,
            "expectancy_R": 0.0,
            "costs_used": {},
        },
        "forward_validation": {
            "status": "forward_unvalidated",
            "method": "live_v2_forward_observation",
            "reason": "Current V2 setup is collecting prospective paper outcomes; it is not live-Capital validation",
            "ohlcv_source": source_metadata.get("source"),
            "ohlcv_last_bar_at": last_bar_at.isoformat(),
            "ohlcv_age_seconds": round(ohlcv_age, 3),
            "scanner_quote_volume_24h": round(quote_volume, 4),
            "quality": {
                "setup_score": round(setup_score, 4),
                "risk_score": round(risk_score, 4),
                "catalyst_score": round(catalyst_score, 4),
                "forward_quality_score": forward_quality_score,
            },
        },
        "reasons": [
            f"Live V2 forward setup: {strategy_id} {direction}.",
            f"Structural price quality {forward_quality_score:.1f} (setup {setup_score:.1f}, risk {risk_score:.1f}); News is gated separately.",
            "Forward paper observation only; live Capital remains blocked. The configured S7 paper experiment may admit it only after price, risk, sentiment, limit, and AI checks pass.",
        ],
        "scanner_context": {
            "symbol": item.get("symbol"),
            "source": item.get("source"),
            "change_pct": _number(item.get("change_pct"), 0.0),
            "quote_volume": quote_volume,
        },
    }
