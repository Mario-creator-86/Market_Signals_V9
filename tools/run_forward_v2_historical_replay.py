"""Replay the current V2-forward technical admission rules over historical OHLCV.

This is an audit tool for the forward-paper discovery path, not a Capital
backtest.  It reproduces the configured V2 structural selector, liquidity,
quality, cost, target/stop, trailing, and autonomous per-ticker lockout rules.
It cannot reconstruct timestamped AI News evidence or realised Capital edge, so
its trades are technical paper hypotheses only.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from core.config import ConfigManager
from core.forward_v2 import _forward_selector_config, forward_price_quality_score, forward_v2_config
from core.scoring_system import row_to_context
from core.strategy_backtester import _precompute_v2_ohlc_features, build_dataframe
from core.strategy_v2.context_builder import build_context, infer_asset_class
from core.strategy_v2.selector import StrategySelector
from core.trading_costs import TradingCostModel, resolve_trading_cost_model


class _EmptyPerformanceCache:
    """Prevent today's performance cache from leaking into historical replay."""

    @staticmethod
    def get_symbol_entries(_symbol: str) -> Dict[str, Dict[str, Any]]:
        return {}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _timestamp(value: Any) -> str:
    try:
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        return stamp.tz_convert("UTC").isoformat()
    except (TypeError, ValueError):
        return str(value)


def _candidate_passes(candidate: Dict[str, Any], close: float, cfg: Dict[str, Any]) -> tuple[bool, Dict[str, Any]]:
    direction = str(candidate.get("direction") or "").upper()
    strategy_id = str(candidate.get("strategy_id") or "").upper()
    stop = _number(candidate.get("sl"))
    target = _number(candidate.get("tp1", candidate.get("tp")))
    setup_score = _number(candidate.get("setup_score"))
    risk_score = _number(candidate.get("risk_score"))
    catalyst_score = _number(candidate.get("catalyst_score"))
    quality = forward_price_quality_score(setup_score, risk_score, cfg)
    levels_valid = (direction == "LONG" and stop < close < target) or (
        direction == "SHORT" and target < close < stop
    )
    detail = {
        "direction": direction,
        "strategy_id": strategy_id,
        "stop": stop,
        "target": target,
        "setup_score": round(setup_score, 4),
        "risk_score": round(risk_score, 4),
        "catalyst_score": round(catalyst_score, 4),
        "forward_quality_score": quality,
        "risk_reward_ratio": round(_number(candidate.get("rr")), 4),
        "levels_valid_at_signal": levels_valid,
    }
    failed_checks: List[str] = []
    if direction not in {"LONG", "SHORT"}:
        failed_checks.append("invalid_direction")
    if not strategy_id:
        failed_checks.append("missing_strategy_id")
    if not levels_valid:
        failed_checks.append("invalid_levels")
    if setup_score < _number(cfg.get("min_setup_score"), 80.0):
        failed_checks.append("setup_score")
    if risk_score < _number(cfg.get("min_risk_score"), 70.0):
        failed_checks.append("risk_score")
    if quality < _number(cfg.get("min_forward_quality_score"), 85.0):
        failed_checks.append("price_quality_score")
    detail["failed_checks"] = failed_checks
    return (
        not failed_checks,
        detail,
    )


def _adverse_exit_fill(price: float, direction: str, reason: str, costs: TradingCostModel) -> float:
    slippage = costs.exit_slippage_fraction(reason)
    return price * (1.0 - slippage) if direction == "LONG" else price * (1.0 + slippage)


def _simulate_exit(
    rows: pd.DataFrame,
    *,
    entry_index: int,
    entry: float,
    stop: float,
    target: float,
    direction: str,
    costs: TradingCostModel,
    bot_config: Dict[str, Any],
) -> Dict[str, Any]:
    """Mirror the conservative live-style target/stop/trailing ordering."""
    is_long = direction == "LONG"
    risk = abs(entry - stop)
    if risk <= 0:
        raise ValueError("entry and stop must define positive risk")
    trailing_enabled = bool(bot_config.get("trailing_enabled", True))
    activation_r = max(0.0, _number(bot_config.get("trailing_activation_r"), 1.0))
    trailing_factor = max(0.0, _number(bot_config.get("trailing_factor"), 0.5))
    trailing_stop = 0.0

    raw_exit = entry
    reason = "time_exit"
    exit_index = len(rows) - 1
    for cursor in range(entry_index, len(rows)):
        row = rows.iloc[cursor]
        opened, high, low = _number(row.get("open")), _number(row.get("high")), _number(row.get("low"))
        if is_long:
            if low <= stop:
                raw_exit, reason, exit_index = min(stop, opened), "stop_loss", cursor
                break
            if high >= target:
                raw_exit, reason, exit_index = max(target, opened), "take_profit", cursor
                break
            if trailing_stop > 0 and low <= trailing_stop:
                raw_exit, reason, exit_index = min(trailing_stop, opened), "trailing_stop", cursor
                break
        else:
            if high >= stop:
                raw_exit, reason, exit_index = max(stop, opened), "stop_loss", cursor
                break
            if low <= target:
                raw_exit, reason, exit_index = min(target, opened), "take_profit", cursor
                break
            if trailing_stop > 0 and high >= trailing_stop:
                raw_exit, reason, exit_index = max(trailing_stop, opened), "trailing_stop", cursor
                break

        if trailing_enabled:
            favorable_price = high if is_long else low
            favorable_move = (favorable_price - entry) if is_long else (entry - favorable_price)
            current_r = favorable_move / risk
            if current_r >= activation_r:
                if is_long:
                    updated = max(stop, entry + (favorable_move * trailing_factor))
                    trailing_stop = max(trailing_stop, updated)
                else:
                    updated = min(stop, entry - (favorable_move * trailing_factor))
                    trailing_stop = updated if trailing_stop <= 0 else min(trailing_stop, updated)
    else:
        raw_exit = _number(rows.iloc[exit_index].get("close"), entry)

    exit_price = _adverse_exit_fill(raw_exit, direction, reason, costs)
    gross_move = (exit_price - entry) if is_long else (entry - exit_price)
    net_pct = ((gross_move / entry) - (2.0 * costs.commission_fraction)) * 100.0
    net_r = (gross_move - (2.0 * costs.commission_fraction * entry)) / risk
    return {
        "exit_index": exit_index,
        "exit_time": _timestamp(rows.index[exit_index]),
        "exit_price": round(exit_price, 8),
        "exit_reason": reason,
        "net_pct": round(net_pct, 6),
        "net_r": round(net_r, 6),
    }


def _summary(trades: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(trades or [])
    wins = [row for row in rows if _number(row.get("net_pct")) > 0]
    losses = [row for row in rows if _number(row.get("net_pct")) <= 0]
    gross_wins = sum(_number(row.get("net_pct")) for row in wins)
    gross_losses = abs(sum(_number(row.get("net_pct")) for row in losses))
    cumulative = peak = 0.0
    max_drawdown = 0.0
    for row in rows:
        cumulative += _number(row.get("net_pct"))
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    return {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round((len(wins) / len(rows) * 100.0), 4) if rows else 0.0,
        "profit_factor": round(gross_wins / gross_losses, 6) if gross_losses > 0 else None,
        "expectancy_net_pct": round(sum(_number(row.get("net_pct")) for row in rows) / len(rows), 6) if rows else 0.0,
        "expectancy_net_r": round(sum(_number(row.get("net_r")) for row in rows) / len(rows), 6) if rows else 0.0,
        "sum_of_independent_trade_returns_pct": round(sum(_number(row.get("net_pct")) for row in rows), 6),
        "max_drawdown_pct": round(max_drawdown, 6),
    }


def replay_ticker(
    ticker: str,
    *,
    dataframe: pd.DataFrame,
    config: ConfigManager,
    execution_config: Dict[str, Any],
    evaluation_minutes: int,
) -> Dict[str, Any]:
    forward_cfg = forward_v2_config(execution_config)
    rows = dataframe.sort_index().copy()
    if bool((getattr(rows, "attrs", {}) or {}).get("source_truncated")):
        return {"ticker": ticker, "status": "source_truncated", "trades": [], "summary": _summary([])}
    if len(rows) < 1_442:
        return {"ticker": ticker, "status": "insufficient_data", "trades": [], "summary": _summary([])}

    asset_class = infer_asset_class(ticker)
    selector = StrategySelector(
        config=_forward_selector_config(copy.deepcopy(config.strategy_v2 or {})),
        perf_cache=_EmptyPerformanceCache(),
    )
    features = _precompute_v2_ohlc_features(rows, asset_class)
    quote_volume_24h = (pd.to_numeric(rows["close"], errors="coerce") * pd.to_numeric(rows["volume"], errors="coerce")).rolling(1_440, min_periods=1_440).sum()
    trades: List[Dict[str, Any]] = []
    stats = {
        "evaluated_bars": 0,
        "structural_candidates": 0,
        "quality_rejections": 0,
        "quality_failure_counts": {},
        "quality_rejection_examples": [],
        "invalid_fill_levels": 0,
    }
    costs = resolve_trading_cost_model(ticker, strategy_v2_config=config.strategy_v2, asset_class=asset_class)
    start = 1_440
    index = start
    stride = max(1, int(evaluation_minutes))
    lockout_bars = max(0, int(_number(execution_config.get("autonomous_ticker_lockout_hours"), 0.0) * 60.0))

    while index < len(rows) - 1:
        close = _number(rows.iloc[index].get("close"))
        prior = _number(rows.iloc[index - 1_440].get("close"))
        volume_24h = _number(quote_volume_24h.iloc[index])
        if close <= 0 or prior <= 0 or volume_24h < _number(forward_cfg.get("min_quote_volume_24h"), 0.0):
            index += stride
            continue
        stats["evaluated_bars"] += 1
        change_pct = ((close - prior) / prior) * 100.0
        context = build_context(
            symbol=ticker,
            asset_class=asset_class,
            watch_row={"price": close, "change_pct": change_pct, "volume": volume_24h, "news_sentiment_trend": "neutral", "news_pulse_score": 0.0},
            row_context=row_to_context(rows.iloc[index]),
            precomputed_ohlc_features=features.iloc[index].to_dict(),
        )
        selection = selector.select(ticker, asset_class, context)
        candidate = selection.get("best_candidate") if isinstance(selection, dict) else None
        if not isinstance(candidate, dict):
            index += stride
            continue
        stats["structural_candidates"] += 1
        passes, detail = _candidate_passes(candidate, close, forward_cfg)
        if not passes:
            stats["quality_rejections"] += 1
            for failure in detail.get("failed_checks", []):
                stats["quality_failure_counts"][failure] = int(stats["quality_failure_counts"].get(failure, 0)) + 1
            if len(stats["quality_rejection_examples"]) < 8:
                stats["quality_rejection_examples"].append({
                    "signal_time": _timestamp(rows.index[index]),
                    "signal_close": round(close, 8),
                    **detail,
                })
            index += stride
            continue
        direction = detail["direction"]
        raw_open = _number(rows.iloc[index + 1].get("open"))
        entry = raw_open * (1.0 + costs.entry_slippage_fraction) if direction == "LONG" else raw_open * (1.0 - costs.entry_slippage_fraction)
        stop, target = detail["stop"], detail["target"]
        valid_fill = (direction == "LONG" and stop < entry < target) or (direction == "SHORT" and target < entry < stop)
        if not valid_fill:
            stats["invalid_fill_levels"] += 1
            index += stride
            continue
        exit_data = _simulate_exit(
            rows,
            entry_index=index + 1,
            entry=entry,
            stop=stop,
            target=target,
            direction=direction,
            costs=costs,
            bot_config=config.bot or {},
        )
        trades.append({
            "ticker": ticker,
            "asset_class": asset_class,
            "strategy_source": "V2_FORWARD_RESEARCH_TECHNICAL_REPLAY",
            "strategy_id": detail["strategy_id"],
            "direction": direction,
            "signal_time": _timestamp(rows.index[index]),
            "entry_time": _timestamp(rows.index[index + 1]),
            "signal_close": round(close, 8),
            "entry_price": round(entry, 8),
            "stop_loss": round(stop, 8),
            "take_profit": round(target, 8),
            "quote_volume_24h": round(volume_24h, 4),
            **detail,
            **exit_data,
        })
        index = max(index + stride, int(exit_data["exit_index"]) + 1 + lockout_bars)

    return {
        "ticker": ticker,
        "asset_class": asset_class,
        "status": "completed",
        "source_data": dict(getattr(rows, "attrs", {}) or {}),
        "cost_model": costs.to_dict(),
        "evaluation": {"interval_minutes": stride, "post_close_ticker_lockout_hours": _number(execution_config.get("autonomous_ticker_lockout_hours"), 0.0), **stats},
        "summary": _summary(trades),
        "trades": trades,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="+", help="Display tickers, e.g. BTC-USD ETH-USD")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--evaluation-minutes", type=int, default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config = ConfigManager()
    execution = (config.agents or {}).get("execution", {}) if isinstance(config.agents, dict) else {}
    reports: List[Dict[str, Any]] = []
    all_trades: List[Dict[str, Any]] = []
    for raw_ticker in args.tickers:
        ticker = str(raw_ticker or "").upper().strip()
        dataframe = build_dataframe(ticker, days=max(1, int(args.days)), htf_history_days=max(3, int(args.days)))
        if dataframe is None:
            reports.append({"ticker": ticker, "status": "insufficient_data", "trades": [], "summary": _summary([])})
            continue
        report = replay_ticker(
            ticker,
            dataframe=dataframe,
            config=config,
            execution_config=execution,
            evaluation_minutes=max(1, int(args.evaluation_minutes)),
        )
        reports.append(report)
        all_trades.extend(report.get("trades", []))
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kind": "forward_v2_historical_technical_replay",
        "promotion_status": "research_only",
        "scope": {
            "days": max(1, int(args.days)),
            "tickers": [str(ticker).upper() for ticker in args.tickers],
            "evaluation_minutes": max(1, int(args.evaluation_minutes)),
            "forward_v2_config": forward_v2_config(execution),
        },
        "limitations": [
            "Historical source-backed AI News evidence is unavailable, so actual News admission is not replayed.",
            "Realised Capital performance is unavailable at historical timestamps, so Capital admission is not replayed.",
            "Tickers are supplied by the operator/current liquid universe; this is not a point-in-time historical constituent universe.",
            "Funding, account fee tier, and exchange fills are not included; configured cost estimates are included.",
            "Per-ticker lockout is modelled. Cross-ticker research-cohort capacity and priority are not, so the list is technical triggers, not realised Capital history.",
        ],
        "aggregate_summary": _summary(all_trades),
        "reports": reports,
    }
    output = args.output or ROOT / "output" / f"forward_v2_technical_90d_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"output": str(output), "aggregate_summary": payload["aggregate_summary"], "tickers": [{"ticker": report.get("ticker"), "status": report.get("status"), "trades": report.get("summary", {}).get("trades")} for report in reports]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
