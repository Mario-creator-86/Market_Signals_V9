"""Run a look-ahead-safe technical walk-forward replay of the live V2 selector.

The resulting artifact is deliberately research-only.  It does not modify the
live strategy cache and cannot enable Capital, because a complete timestamped
historical AI-news archive is not yet available for replay.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import ConfigManager
from core.strategy_backtester import build_dataframe
from core.strategy_v2.context_builder import infer_asset_class
from core.walk_forward_validation import run_faithful_v2_technical_walk_forward


def _source_coverage(dataframe):
    attrs = getattr(dataframe, "attrs", {}) or {}
    keys = (
        "source", "symbol", "interval", "requested_days", "requested_start",
        "requested_end", "actual_start", "actual_end", "actual_span_days",
        "expected_bars", "received_bars", "source_truncated",
    )
    return {key: attrs.get(key) for key in keys if key in attrs}


def _provenance(config: ConfigManager) -> dict:
    files = {
        "strategy_backtester": ROOT / "core" / "strategy_backtester.py",
        "walk_forward_validation": ROOT / "core" / "walk_forward_validation.py",
        "strategy_selector": ROOT / "core" / "strategy_v2" / "selector.py",
    }
    return {
        "replay": "core.strategy_backtester.backtest(v2_only=True)",
        "source_sha256": {
            key: hashlib.sha256(path.read_bytes()).hexdigest()
            for key, path in files.items()
        },
        "strategy_v2_costs": (config.strategy_v2 or {}).get("costs_by_asset", {}),
        "historical_ai_news": "not_available_and_not_replayed",
        "promotion_status": "research_only",
    }


def _scoped_config(config: ConfigManager, ticker: str, strategy_ids: list[str]):
    """Return an in-memory config that evaluates only the requested V2 strategies.

    Scoping makes an S7 audit practical without changing the project's live
    configuration.  It cannot loosen a gate: strategy IDs are only removed
    from the replay, while all existing costs, thresholds, and Capital guards
    stay untouched.
    """
    requested = [str(item or "").upper().strip() for item in strategy_ids if str(item or "").strip()]
    if not requested:
        return config
    scoped_v2 = copy.deepcopy(config.strategy_v2 or {})
    enabled = scoped_v2.get("enabled_strategies_by_asset_class")
    enabled = dict(enabled) if isinstance(enabled, dict) else {}
    enabled[infer_asset_class(ticker)] = list(dict.fromkeys(requested))
    scoped_v2["enabled_strategies_by_asset_class"] = enabled
    return SimpleNamespace(
        signal=copy.deepcopy(config.signal or {}),
        bot=copy.deepcopy(config.bot or {}),
        strategy_v2=scoped_v2,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="+", help="Crypto tickers, e.g. BTC-USD ETH-USD")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--train-days", type=int, default=7)
    parser.add_argument("--test-days", type=int, default=3)
    parser.add_argument("--max-windows", type=int, default=3)
    parser.add_argument("--min-windows", type=int, default=3)
    parser.add_argument("--min-sample", type=int, default=30)
    parser.add_argument(
        "--strategy-id",
        action="append",
        default=[],
        help="Restrict the replay to one or more V2 strategy IDs without changing config.json",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    config = ConfigManager()
    risk_cfg = (config.agents or {}).get("risk", {}) if isinstance(config.agents, dict) else {}
    risk_per_trade = float(risk_cfg.get("max_risk_per_trade_pct", 0.0) or 0.0)
    if not 0.0 < risk_per_trade <= 100.0:
        raise ValueError("agents.risk.max_risk_per_trade_pct must be a finite value in (0, 100]")

    reports = []
    for ticker in args.tickers:
        replay_config = _scoped_config(config, ticker, args.strategy_id)
        v2_cfg = replay_config.strategy_v2 or {}
        dataframe = build_dataframe(ticker, days=max(1, args.days))
        if dataframe is None:
            reports.append({"ticker": ticker.upper(), "status": "insufficient_data", "reason": "OHLCV source did not provide enough prepared bars"})
            continue
        report = run_faithful_v2_technical_walk_forward(
            ticker,
            dataframe,
            config_manager=replay_config,
            train_bars=max(1, args.train_days) * 1440,
            test_bars=max(1, args.test_days) * 1440,
            max_windows=max(1, args.max_windows),
            min_windows=max(1, args.min_windows),
            min_sample=max(1, args.min_sample),
            min_profit_factor=float(v2_cfg.get("min_profit_factor", 1.2) or 1.2),
            min_expectancy_r=float(v2_cfg.get("min_expectancy_R", 0.0) or 0.0),
            max_drawdown_pct=float(v2_cfg.get("max_drawdown_pct", 25.0) or 25.0),
            capital_risk_per_trade_pct=risk_per_trade,
        )
        report["source_data"] = _source_coverage(dataframe)
        reports.append(report)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kind": "faithful_v2_technical_walk_forward",
        "promotion_status": "research_only",
        "provenance": _provenance(config),
        "strategy_scope": [str(item).upper().strip() for item in args.strategy_id if str(item or "").strip()],
        "reports": reports,
    }
    output = args.output or Path("output") / f"faithful_v2_technical_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "statuses": [{"ticker": row.get("ticker"), "status": row.get("status")} for row in reports]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
