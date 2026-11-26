"""Run a reproducible fixed-configuration OOS validation using real Binance OHLCV."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.strategy_backtester import build_dataframe
from core.walk_forward_validation import run_fixed_walk_forward


def _source_coverage(dataframe):
    """Expose the actual fetched range alongside the requested backtest range."""
    attrs = getattr(dataframe, "attrs", {}) or {}
    keys = (
        "source",
        "symbol",
        "interval",
        "requested_days",
        "requested_start",
        "requested_end",
        "actual_start",
        "actual_end",
        "actual_span_days",
        "expected_bars",
        "received_bars",
        "source_truncated",
    )
    return {key: attrs.get(key) for key in keys if key in attrs}


def _redact_secrets(value):
    """Keep a reproducible replay-config snapshot without leaking credentials."""
    if isinstance(value, dict):
        redacted = {}
        for key, child in value.items():
            key_text = str(key).lower()
            if any(token in key_text for token in ("api_key", "apikey", "token", "secret", "password")):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = _redact_secrets(child)
        return redacted
    if isinstance(value, list):
        return [_redact_secrets(child) for child in value]
    return value


def _replay_provenance():
    """Capture the exact non-secret replay inputs used by this OOS run."""
    from core.config import ConfigManager

    config = ConfigManager()
    snapshot = _redact_secrets(
        {
            "signal_settings": config.signal,
            "bot": config.bot,
            "strategy_v2": config.strategy_v2,
            "agents_risk": (config.agents or {}).get("risk", {}),
        }
    )
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
    replay_source = ROOT / "core" / "strategy_backtester.py"
    validator_source = ROOT / "core" / "walk_forward_validation.py"
    return {
        "replay": "core.strategy_backtester.backtest",
        "strategy_backtester_sha256": hashlib.sha256(replay_source.read_bytes()).hexdigest(),
        "walk_forward_validation_sha256": hashlib.sha256(validator_source.read_bytes()).hexdigest(),
        "config_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "config_snapshot": snapshot,
        "cost_model": "Recorded per ticker from the replayed configured estimate.",
        "historical_news": "not_available_and_not_replayed",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="+", help="Crypto tickers, e.g. BTC-USD ETH-USD")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--train-days", type=int, default=7)
    parser.add_argument("--test-days", type=int, default=3)
    parser.add_argument("--max-windows", type=int, default=7)
    parser.add_argument("--min-windows", type=int, default=3)
    parser.add_argument("--min-sample", type=int, default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    from core.config import ConfigManager

    config = ConfigManager()
    risk_cfg = (config.agents or {}).get("risk", {}) if isinstance(config.agents, dict) else {}
    if not isinstance(risk_cfg, dict) or "max_risk_per_trade_pct" not in risk_cfg:
        raise ValueError("agents.risk.max_risk_per_trade_pct is required for Capital-aware validation")
    capital_risk_per_trade_pct = float(risk_cfg["max_risk_per_trade_pct"])

    reports = []
    for ticker in args.tickers:
        dataframe = build_dataframe(ticker, days=max(1, args.days))
        if dataframe is None:
            reports.append({"ticker": ticker.upper(), "status": "insufficient_data", "reason": "Binance did not provide enough OHLCV bars"})
            continue
        report = run_fixed_walk_forward(
            ticker,
            dataframe,
            train_bars=max(1, args.train_days) * 1440,
            test_bars=max(1, args.test_days) * 1440,
            max_windows=max(1, args.max_windows),
            min_windows=max(1, args.min_windows),
            min_sample=max(1, args.min_sample),
            capital_risk_per_trade_pct=capital_risk_per_trade_pct,
        )
        report["source_data"] = _source_coverage(dataframe)
        reports.append(report)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Binance public OHLCV via core.strategy_backtester.build_dataframe",
        "capital_risk_per_trade_pct": capital_risk_per_trade_pct,
        "replay_provenance": _replay_provenance(),
        "reports": reports,
    }
    output = args.output or Path("output") / f"oos_validation_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "statuses": [{"ticker": row.get("ticker"), "status": row.get("status")} for row in reports]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
