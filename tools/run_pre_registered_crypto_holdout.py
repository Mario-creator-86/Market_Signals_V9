"""Run a fixed crypto candidate against a final contiguous Binance holdout.

This does not search or tune parameters.  It reports the earlier development
segment and evaluates only the final reserved period against the fixed gates.
The output remains research-only and cannot enable autonomous execution.
"""

from __future__ import annotations

import argparse
import functools
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import ConfigManager
from core.crypto_research_strategies import (
    PRE_REGISTERED_STRATEGIES,
    fetch_research_ohlcv,
    replay_research_strategy,
)
from core.reserved_holdout_validation import run_reserved_holdout
from core.strategy_backtester import _interval_milliseconds
from core.trading_costs import resolve_trading_cost_model


def _bars(days: int, interval: str) -> int:
    return max(1, math.ceil((max(1, int(days)) * 86_400_000) / _interval_milliseconds(interval)))


def _coverage(dataframe) -> Dict[str, Any]:
    attrs = getattr(dataframe, "attrs", {}) or {}
    keys = (
        "source", "symbol", "interval", "requested_days", "requested_start", "requested_end",
        "actual_start", "actual_end", "actual_span_days", "expected_bars", "received_bars", "source_truncated",
    )
    return {key: attrs.get(key) for key in keys if key in attrs}


def _configured_capital_risk_per_trade_pct(config: ConfigManager) -> float:
    agents = config.agents if isinstance(config.agents, dict) else {}
    risk = agents.get("risk") if isinstance(agents.get("risk"), dict) else {}
    if "max_risk_per_trade_pct" not in risk:
        raise ValueError("agents.risk.max_risk_per_trade_pct is required for Capital-aware validation")
    return float(risk["max_risk_per_trade_pct"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="+", help="Crypto tickers, e.g. BTC-USD ETH-USD SOL-USD")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--holdout-days", type=int, default=90)
    parser.add_argument("--min-sample", type=int, default=30)
    parser.add_argument(
        "--strategies",
        nargs="+",
        choices=sorted(PRE_REGISTERED_STRATEGIES),
        default=sorted(PRE_REGISTERED_STRATEGIES),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    config = ConfigManager()
    capital_risk_per_trade_pct = _configured_capital_risk_per_trade_pct(config)
    reports = []
    # Multiple fixed candidates may use the same ticker/interval.  Cache only
    # the fetched source frame for this process; each strategy still receives
    # its own replay and final reserved-holdout evaluation.
    dataframes: Dict[tuple[str, str], Any] = {}
    for ticker in args.tickers:
        for strategy_id in args.strategies:
            spec = PRE_REGISTERED_STRATEGIES[strategy_id]
            try:
                cost_model = resolve_trading_cost_model(ticker, strategy_v2_config=config.strategy_v2)
            except ValueError as exc:
                reports.append({"ticker": ticker.upper(), "strategy_id": strategy_id, "status": "cost_model_unavailable", "reason": str(exc)})
                continue
            source_key = (str(ticker).upper(), spec.interval)
            if source_key not in dataframes:
                dataframes[source_key] = fetch_research_ohlcv(
                    ticker,
                    days=max(1, args.days),
                    interval=spec.interval,
                )
            dataframe = dataframes[source_key]
            if dataframe is None:
                reports.append({"ticker": ticker.upper(), "strategy_id": strategy_id, "status": "insufficient_data", "reason": "Binance did not provide OHLCV"})
                continue
            report = run_reserved_holdout(
                ticker,
                dataframe,
                holdout_bars=_bars(args.holdout_days, spec.interval),
                warmup_bars=max(spec.slow_ema, spec.fast_ema, spec.lookback, 30),
                replay_runner=functools.partial(
                    replay_research_strategy,
                    strategy_id=strategy_id,
                    cost_model=cost_model,
                ),
                min_sample=max(1, args.min_sample),
                capital_risk_per_trade_pct=capital_risk_per_trade_pct,
            )
            report["strategy_id"] = strategy_id
            report["strategy"] = {
                "family": spec.family,
                "interval": spec.interval,
                "description": spec.description,
            }
            report["source_data"] = _coverage(dataframe)
            report["research_only"] = True
            reports.append(report)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Binance public OHLCV",
        "research_only": True,
        "capital_risk_per_trade_pct": capital_risk_per_trade_pct,
        "reports": reports,
    }
    output = args.output or Path("output") / f"pre_registered_crypto_holdout_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "statuses": [
            {"ticker": item.get("ticker"), "strategy_id": item.get("strategy_id"), "status": item.get("status")}
            for item in reports
        ],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
