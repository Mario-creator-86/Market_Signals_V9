"""Replay the bounded price-first stock-scanner hypothesis on real daily OHLCV.

The live scanner observes intraday movers. Yahoo's retained daily candles cannot
reconstruct its historical screener membership, intraday entry time, or historic
News/AI decisions. This is therefore an exploratory, cost-aware price replay for
the *current scanner snapshot universe* only. It is deliberately not accepted by
the Capital validation gate.

Usage:
    .\\.venv314\\Scripts\\python.exe tools\\run_scanner_price_first_stock_replay.py
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import ConfigManager
from core.trading_costs import TradingCostModel, resolve_trading_cost_model


DEFAULT_MIN_MOVE_PCT = 8.0
DEFAULT_MAX_MOVE_PCT = 18.0
DEFAULT_STOP_PCT = 0.04
DEFAULT_RISK_REWARD = 1.8
DEFAULT_MIN_DOLLAR_VOLUME = 10_000_000.0


@dataclass(frozen=True)
class ReplayTrade:
    ticker: str
    signal_date: str
    entry_date: str
    direction: str
    signal_move_pct: float
    prior_day_dollar_volume: float
    entry_price: float
    stop_price: float
    target_price: float
    exit_price: float
    exit_reason: str
    gross_return_pct: float
    net_return_pct: float
    cost_model: dict[str, Any]


def _is_stock_symbol(value: Any) -> bool:
    symbol = str(value or "").upper().strip()
    return bool(symbol and symbol.isalpha() and len(symbol) <= 5 and not symbol.endswith("USDT"))


def _snapshot_universe(path: Path) -> tuple[list[str], str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    tickers: list[str] = []
    for item in payload.get("hot_tickers", []):
        symbol = str((item or {}).get("symbol") or "").upper().strip()
        if _is_stock_symbol(symbol) and symbol not in tickers:
            tickers.append(symbol)
    return tickers, str(payload.get("published_at") or "")


def _normalise_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    data.columns = [str(column).lower().replace(" ", "_") for column in data.columns]
    required = ["open", "high", "low", "close", "volume"]
    return data.dropna(subset=required)


def _apply_entry_slippage(price: float, direction: str, cost: TradingCostModel) -> float:
    return price * (1.0 + cost.entry_slippage_fraction) if direction == "LONG" else price * (1.0 - cost.entry_slippage_fraction)


def _apply_exit_slippage(price: float, direction: str, reason: str, cost: TradingCostModel) -> float:
    fraction = cost.exit_slippage_fraction(reason)
    return price * (1.0 - fraction) if direction == "LONG" else price * (1.0 + fraction)


def _simulate_trade(
    *,
    ticker: str,
    signal_time: pd.Timestamp,
    entry_time: pd.Timestamp,
    move_pct: float,
    prior_day_dollar_volume: float,
    entry_open: float,
    high: float,
    low: float,
    close: float,
    stop_pct: float,
    risk_reward: float,
    cost: TradingCostModel,
) -> ReplayTrade:
    direction = "LONG" if move_pct > 0 else "SHORT"
    entry = _apply_entry_slippage(entry_open, direction, cost)
    target_pct = stop_pct * risk_reward
    if direction == "LONG":
        stop = entry * (1.0 - stop_pct)
        target = entry * (1.0 + target_pct)
        # A daily candle cannot establish intrabar sequence. Stop-first is the
        # conservative convention when both levels are touched.
        if low <= stop:
            raw_exit, reason = min(stop, entry_open), "stop_loss"
        elif high >= target:
            raw_exit, reason = max(target, entry_open), "take_profit"
        else:
            raw_exit, reason = close, "time_exit_one_session"
        exit_price = _apply_exit_slippage(raw_exit, direction, reason, cost)
        gross = (exit_price - entry) / entry
    else:
        stop = entry * (1.0 + stop_pct)
        target = entry * (1.0 - target_pct)
        if high >= stop:
            raw_exit, reason = max(stop, entry_open), "stop_loss"
        elif low <= target:
            raw_exit, reason = min(target, entry_open), "take_profit"
        else:
            raw_exit, reason = close, "time_exit_one_session"
        exit_price = _apply_exit_slippage(raw_exit, direction, reason, cost)
        gross = (entry - exit_price) / entry
    net = gross - (2.0 * cost.commission_fraction)
    return ReplayTrade(
        ticker=ticker,
        signal_date=str(signal_time.date()),
        entry_date=str(entry_time.date()),
        direction=direction,
        signal_move_pct=round(move_pct, 4),
        prior_day_dollar_volume=round(prior_day_dollar_volume, 2),
        entry_price=round(entry, 8),
        stop_price=round(stop, 8),
        target_price=round(target, 8),
        exit_price=round(exit_price, 8),
        exit_reason=reason,
        gross_return_pct=round(gross * 100.0, 6),
        net_return_pct=round(net * 100.0, 6),
        cost_model=cost.to_dict(),
    )


def _metrics(trades: Iterable[ReplayTrade]) -> dict[str, Any]:
    rows = list(trades)
    returns = [trade.net_return_pct / 100.0 for trade in rows]
    winners = [item for item in returns if item > 0]
    losers = [item for item in returns if item < 0]
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    if gross_loss > 0:
        profit_factor: float | None = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = None
    else:
        profit_factor = 0.0
    return {
        "trades": len(rows),
        "wins": len(winners),
        "losses": len(losers),
        "breakeven": len(rows) - len(winners) - len(losers),
        "win_rate_pct": round((len(winners) / len(rows) * 100.0), 4) if rows else None,
        "profit_factor": round(profit_factor, 6) if profit_factor is not None else None,
        "expectancy_pct_per_trade": round((sum(returns) / len(rows) * 100.0), 6) if rows else None,
        # Signals from different tickers can overlap. This sequential number is
        # diagnostic only, never a portfolio-return claim.
        "sequential_compounded_return_pct": round((equity - 1.0) * 100.0, 6) if rows else None,
        "sequential_compounding_is_not_portfolio_return": True,
        "max_drawdown_pct": round(max_drawdown * 100.0, 6) if rows else None,
        "gross_profit_pct": round(gross_profit * 100.0, 6),
        "gross_loss_pct": round(gross_loss * 100.0, 6),
    }


def _fetch_history(tickers: list[str], history_days: int) -> pd.DataFrame:
    # Fetch additional calendar time so the output can be filtered to exactly
    # `history_days` despite weekends and holidays.
    period = "6mo" if history_days >= 90 else "4mo"
    return yf.download(
        tickers,
        period=period,
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=False,
        group_by="ticker",
    )


def run_replay(args: argparse.Namespace) -> dict[str, Any]:
    snapshot_path = Path(args.scanner_state)
    tickers, snapshot_time = _snapshot_universe(snapshot_path)
    if not tickers:
        raise RuntimeError(f"No stock scanner symbols found in {snapshot_path}")
    history = _fetch_history(tickers, args.days)
    if history.empty:
        raise RuntimeError("Yahoo returned no historical stock data")
    config = ConfigManager()
    all_trades: list[ReplayTrade] = []
    skipped_symbols: dict[str, str] = {}
    effective_start: pd.Timestamp | None = None
    effective_end: pd.Timestamp | None = None
    for ticker in tickers:
        try:
            frame = history[ticker] if isinstance(history.columns, pd.MultiIndex) else history
            frame = _normalise_ohlcv(frame)
            if len(frame) < 3:
                skipped_symbols[ticker] = "insufficient_ohlcv"
                continue
            frame.index = pd.to_datetime(frame.index).tz_localize(None)
            latest = pd.Timestamp(frame.index.max())
            cutoff = latest - pd.Timedelta(days=args.days)
            frame = frame.loc[frame.index >= cutoff].copy()
            frame["change_pct"] = frame["close"].pct_change() * 100.0
            effective_start = min(effective_start, pd.Timestamp(frame.index.min())) if effective_start is not None else pd.Timestamp(frame.index.min())
            effective_end = max(effective_end, pd.Timestamp(frame.index.max())) if effective_end is not None else pd.Timestamp(frame.index.max())
            cost = resolve_trading_cost_model(
                ticker,
                strategy_v2_config=config.strategy_v2,
                asset_class="US_STOCKS",
            )
            for index in range(1, len(frame) - 1):
                signal = frame.iloc[index]
                move_pct = float(signal["change_pct"])
                dollar_volume = float(signal["close"] * signal["volume"])
                if not math.isfinite(move_pct) or not math.isfinite(dollar_volume):
                    continue
                if not args.min_move_pct <= abs(move_pct) <= args.max_move_pct:
                    continue
                if dollar_volume < args.min_dollar_volume:
                    continue
                entry = frame.iloc[index + 1]
                all_trades.append(
                    _simulate_trade(
                        ticker=ticker,
                        signal_time=pd.Timestamp(frame.index[index]),
                        entry_time=pd.Timestamp(frame.index[index + 1]),
                        move_pct=move_pct,
                        prior_day_dollar_volume=dollar_volume,
                        entry_open=float(entry["open"]),
                        high=float(entry["high"]),
                        low=float(entry["low"]),
                        close=float(entry["close"]),
                        stop_pct=args.stop_pct,
                        risk_reward=args.risk_reward,
                        cost=cost,
                    )
                )
        except Exception as exc:  # individual Yahoo symbol failures must not hide the rest
            skipped_symbols[ticker] = str(exc)

    all_trades.sort(key=lambda trade: (trade.entry_date, trade.ticker))
    end_date = max((pd.Timestamp(trade.entry_date) for trade in all_trades), default=effective_end)
    start_date = end_date - pd.Timedelta(days=args.days) if end_date is not None else None
    holdout_start = end_date - pd.Timedelta(days=30) if end_date is not None else None
    in_window = [trade for trade in all_trades if start_date is None or pd.Timestamp(trade.entry_date) >= start_date]
    development = [trade for trade in in_window if holdout_start is None or pd.Timestamp(trade.entry_date) < holdout_start]
    holdout = [trade for trade in in_window if holdout_start is not None and pd.Timestamp(trade.entry_date) >= holdout_start]
    cost = resolve_trading_cost_model(
        tickers[0], strategy_v2_config=config.strategy_v2, asset_class="US_STOCKS"
    ).to_dict()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_type": "scanner_price_first_stock_daily_replay",
        "validation_scope": "exploratory_research_only",
        "capital_execution_eligible": False,
        "universe": {
            "source": "current_scanner_snapshot",
            "snapshot_published_at": snapshot_time,
            "symbols_requested": tickers,
            "symbols_requested_count": len(tickers),
            "symbols_skipped": skipped_symbols,
        },
        "period": {
            "requested_days": args.days,
            "observed_start": str(effective_start.date()) if effective_start is not None else None,
            "observed_end": str(effective_end.date()) if effective_end is not None else None,
            "holdout_start": str(holdout_start.date()) if holdout_start is not None else None,
        },
        "rules": {
            "strategy": "price_first_scanner_momentum",
            "signal": f"previous daily move in [{args.min_move_pct:.2f}%, {args.max_move_pct:.2f}%]",
            "direction": "follow the daily move",
            "entry": "next regular-session open with adverse configured entry slippage",
            "stop_loss_pct": args.stop_pct * 100.0,
            "risk_reward_ratio": args.risk_reward,
            "take_profit_pct": args.stop_pct * args.risk_reward * 100.0,
            "minimum_previous_day_dollar_volume": args.min_dollar_volume,
            "exit": "stop/target inside next daily session; stop-first when both are touched; otherwise session close",
            "cost_model": cost,
        },
        "metrics": {
            "full_requested_window": _metrics(in_window),
            "development_before_last_30_days": _metrics(development),
            "holdout_last_30_days": _metrics(holdout),
        },
        "limitations": [
            "Current scanner membership is a look-ahead-selected universe; historical Yahoo screen membership is not available.",
            "Daily OHLCV cannot reproduce the live scanner's intraday entry timestamp, bid/ask spread, or order-book fill.",
            "Historical News/AI context is not reconstructed, so this validates price rules only.",
            "The report is not faithful_v2_walk_forward and cannot satisfy the Capital execution gate.",
        ],
        "trades": [asdict(trade) for trade in in_window],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scanner-state", default=str(ROOT / "data" / "scanner_state.json"))
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--min-move-pct", type=float, default=DEFAULT_MIN_MOVE_PCT)
    parser.add_argument("--max-move-pct", type=float, default=DEFAULT_MAX_MOVE_PCT)
    parser.add_argument("--stop-pct", type=float, default=DEFAULT_STOP_PCT)
    parser.add_argument("--risk-reward", type=float, default=DEFAULT_RISK_REWARD)
    parser.add_argument("--min-dollar-volume", type=float, default=DEFAULT_MIN_DOLLAR_VOLUME)
    parser.add_argument("--output", default=str(ROOT / "output" / "scanner_price_first_stock_90d.json"))
    args = parser.parse_args()
    if args.days < 31:
        parser.error("--days must be at least 31 to retain a 30-day holdout")
    if not 0 < args.min_move_pct <= args.max_move_pct:
        parser.error("move bounds must be positive and ordered")
    if args.stop_pct <= 0 or args.risk_reward <= 0 or args.min_dollar_volume < 0:
        parser.error("risk and liquidity values must be positive")
    return args


def main() -> None:
    args = parse_args()
    report = run_replay(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    metrics = report["metrics"]["full_requested_window"]
    print(json.dumps({"output": str(output), "metrics": metrics, "trades": len(report["trades"])}, indent=2))


if __name__ == "__main__":
    main()
