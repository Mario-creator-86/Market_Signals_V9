"""Run fixed, pre-registered crypto research strategies on real Binance OHLCV.

Outputs are deliberately research-only.  They cannot approve live or paper
autonomous execution because they do not replay timestamped historical news
and are not yet the production signal implementation.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.crypto_research_strategies import (
    PRE_REGISTERED_STRATEGIES,
    fetch_research_ohlcv,
    replay_research_strategy,
)
from core.config import ConfigManager
from core.trading_costs import TradingCostModel, resolve_trading_cost_model
from core.walk_forward_validation import run_fixed_walk_forward


def _interval_bars(days: int, interval: str) -> int:
    """Translate calendar windows to exact bars for the fixed Binance interval."""
    from core.strategy_backtester import _interval_milliseconds

    return max(1, math.ceil((max(1, int(days)) * 86_400_000) / _interval_milliseconds(interval)))


def _source_coverage(dataframe) -> Dict[str, Any]:
    attrs = getattr(dataframe, "attrs", {}) or {}
    keys = (
        "source", "symbol", "interval", "requested_days", "requested_start", "requested_end",
        "actual_start", "actual_end", "actual_span_days", "expected_bars", "received_bars",
        "source_truncated",
    )
    return {key: attrs.get(key) for key in keys if key in attrs}


def _replay_provenance(
    strategy_id: str,
    cost_model: TradingCostModel,
    config: ConfigManager,
) -> Dict[str, Any]:
    strategy = PRE_REGISTERED_STRATEGIES[strategy_id]
    strategy_source = ROOT / "core" / "crypto_research_strategies.py"
    validator_source = ROOT / "core" / "walk_forward_validation.py"
    agents = config.agents if isinstance(config.agents, dict) else {}
    snapshot = {
        "strategy_v2": config.strategy_v2,
        "agents_risk": agents.get("risk") if isinstance(agents.get("risk"), dict) else {},
    }
    canonical_snapshot = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "replay": "core.crypto_research_strategies.replay_research_strategy",
        "strategy_file_sha256": hashlib.sha256(strategy_source.read_bytes()).hexdigest(),
        "walk_forward_validation_sha256": hashlib.sha256(validator_source.read_bytes()).hexdigest(),
        "config_sha256": hashlib.sha256(canonical_snapshot.encode("utf-8")).hexdigest(),
        "config_snapshot": snapshot,
        "strategy": {
            "strategy_id": strategy.strategy_id,
            "family": strategy.family,
            "interval": strategy.interval,
            "lookback": strategy.lookback,
            "fast_ema": strategy.fast_ema,
            "slow_ema": strategy.slow_ema,
            "rsi_period": strategy.rsi_period,
            "rsi_entry_low": strategy.rsi_entry_low,
            "rsi_entry_high": strategy.rsi_entry_high,
            "stop_atr": strategy.stop_atr,
            "target_atr": strategy.target_atr,
            "description": strategy.description,
        },
        "cost_model": cost_model.to_dict(),
        "research_only": True,
        "promotion_eligible": False,
        "promotion_blockers": [
            "Strategy is not the production live signal implementation.",
            "No timestamped historical news archive was replayed.",
            "The realized-capital gate has not accumulated matching live trades.",
        ],
    }


def _configured_capital_risk_per_trade_pct(config: ConfigManager) -> float:
    agents = config.agents if isinstance(config.agents, dict) else {}
    risk = agents.get("risk") if isinstance(agents.get("risk"), dict) else {}
    if "max_risk_per_trade_pct" not in risk:
        raise ValueError("agents.risk.max_risk_per_trade_pct is required for Capital-aware validation")
    return float(risk["max_risk_per_trade_pct"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="+", help="Crypto tickers, e.g. BTC-USD ETH-USD SOL-USD")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--train-days", type=int, default=30)
    parser.add_argument("--test-days", type=int, default=7)
    parser.add_argument("--max-windows", type=int, default=20)
    parser.add_argument("--min-windows", type=int, default=3)
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
    # R1-R3 share the same 1h source series.  Reusing the exact in-memory
    # frame avoids duplicate public-API requests without reusing results or
    # changing any strategy's independent replay.
    dataframes: Dict[tuple[str, str], Any] = {}
    for ticker in args.tickers:
        for strategy_id in args.strategies:
            strategy = PRE_REGISTERED_STRATEGIES[strategy_id]
            try:
                cost_model = resolve_trading_cost_model(
                    ticker,
                    strategy_v2_config=config.strategy_v2,
                )
            except ValueError as exc:
                reports.append({
                    "ticker": ticker.upper(),
                    "strategy_id": strategy_id,
                    "status": "cost_model_unavailable",
                    "reason": str(exc),
                })
                continue
            source_key = (str(ticker).upper(), strategy.interval)
            if source_key not in dataframes:
                dataframes[source_key] = fetch_research_ohlcv(
                    ticker,
                    days=max(1, args.days),
                    interval=strategy.interval,
                )
            dataframe = dataframes[source_key]
            if dataframe is None:
                reports.append({
                    "ticker": ticker.upper(),
                    "strategy_id": strategy_id,
                    "status": "insufficient_data",
                    "reason": f"Binance did not provide {strategy.interval} OHLCV",
                })
                continue
            train_bars = _interval_bars(args.train_days, strategy.interval)
            test_bars = _interval_bars(args.test_days, strategy.interval)
            warmup_bars = max(strategy.slow_ema, strategy.fast_ema, strategy.lookback, 30)
            report = run_fixed_walk_forward(
                ticker,
                dataframe,
                train_bars=train_bars,
                test_bars=test_bars,
                max_windows=max(1, args.max_windows),
                min_windows=max(1, args.min_windows),
                min_sample=max(1, args.min_sample),
                capital_risk_per_trade_pct=capital_risk_per_trade_pct,
                warmup_bars=warmup_bars,
                replay_runner=functools.partial(
                    replay_research_strategy,
                    strategy_id=strategy_id,
                    cost_model=cost_model,
                ),
            )
            report["method"] = "fixed_configuration_pre_registered_crypto_research_ohlcv"
            report["strategy_id"] = strategy_id
            report["source_data"] = _source_coverage(dataframe)
            report["research_provenance"] = _replay_provenance(strategy_id, cost_model, config)
            report["promotion_eligible"] = False
            report["limitations"] = list(report.get("limitations", [])) + [
                "This strategy is research-only and is not wired to live signal generation or execution.",
                "A price-only pass would still not authorize autonomous execution.",
            ]
            reports.append(report)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Binance public OHLCV",
        "research_only": True,
        "capital_risk_per_trade_pct": capital_risk_per_trade_pct,
        "reports": reports,
    }
    output = args.output or Path("output") / f"pre_registered_crypto_oos_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "research_only": True,
                "statuses": [
                    {"ticker": row.get("ticker"), "strategy_id": row.get("strategy_id"), "status": row.get("status")}
                    for row in reports
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
