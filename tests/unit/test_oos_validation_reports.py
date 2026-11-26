import json
import hashlib
from pathlib import Path

from core.oos_validation_reports import get_latest_oos_validation_reports


def _payload(generated_at, ticker, status, *, net_pct=-1.0):
    strategy_source = Path(__file__).resolve().parents[2] / "core" / "strategy_backtester.py"
    validator_source = Path(__file__).resolve().parents[2] / "core" / "walk_forward_validation.py"
    return {
        "generated_at": generated_at,
        "replay_provenance": {
            "replay": "core.strategy_backtester.backtest",
            "strategy_backtester_sha256": hashlib.sha256(strategy_source.read_bytes()).hexdigest(),
            "walk_forward_validation_sha256": hashlib.sha256(validator_source.read_bytes()).hexdigest(),
            "config_sha256": "config-hash",
            "config_snapshot": {"must_not": "be exposed"},
            "cost_model": {"commission_per_side_fraction": 0.0025},
            "historical_news": "not_available_and_not_replayed",
        },
        "reports": [
            {
                "ticker": ticker,
                "status": status,
                "reason": "Recorded test result",
                "method": "fixed_configuration_walk_forward_ohlcv",
                "metrics": {
                    "sample_size": 30,
                    "net_pct": net_pct,
                    "profit_factor": 0.8,
                    "capital_risk_per_trade_pct": 0.5,
                    "capital_return_pct": 1.2,
                    "max_drawdown_pct": 0.8,
                },
                "capital_model": {
                    "risk_per_trade_pct": 0.5,
                    "equity_model": "sequential_net_r_compounded_at_configured_risk",
                    "is_realized_capital": False,
                },
                "thresholds": {"min_sample": 30, "min_profit_factor": 1.2},
                "source_data": {
                    "actual_start": "2026-01-01T00:00:00+00:00",
                    "actual_end": "2026-04-01T00:00:00+00:00",
                    "actual_span_days": 90,
                    "received_bars": 129_600,
                    "source_truncated": False,
                },
                "limitations": ["No timestamped historical news archive is available; news gating was not replayed."],
                "oos_trades": [{"ticker": ticker, "entry": 10, "exit": 9, "net_pct": -1, "internal": "hidden"}],
            }
        ],
    }


def test_oos_report_reader_returns_explicit_no_reports_for_empty_directory(tmp_path):
    result = get_latest_oos_validation_reports(tmp_path)

    assert result["status"] == "no_reports"
    assert result["reports"] == []
    assert result["autonomous_execution_eligible"] is False


def test_oos_report_reader_uses_latest_per_ticker_and_redacts_config_snapshot(tmp_path):
    (tmp_path / "oos_validation_old.json").write_text(
        json.dumps(_payload("2026-01-01T00:00:00+00:00", "BTC-USD", "negative_edge", net_pct=-12)),
        encoding="utf-8",
    )
    (tmp_path / "oos_validation_new.json").write_text(
        json.dumps(_payload("2026-02-01T00:00:00+00:00", "BTC-USD", "validated", net_pct=4)),
        encoding="utf-8",
    )
    (tmp_path / "oos_validation_eth.json").write_text(
        json.dumps(_payload("2026-01-15T00:00:00+00:00", "ETH-USD", "negative_edge", net_pct=-4)),
        encoding="utf-8",
    )

    result = get_latest_oos_validation_reports(tmp_path)

    assert result["status"] == "available"
    assert [row["ticker"] for row in result["reports"]] == ["BTC-USD", "ETH-USD"]
    btc = result["reports"][0]
    assert btc["status"] == "validated"
    assert btc["metrics"]["net_pct"] == 4
    assert btc["metrics"]["capital_return_pct"] == 1.2
    assert btc["capital_model"]["risk_per_trade_pct"] == 0.5
    assert btc["closed_oos_trades"] == [{"ticker": "BTC-USD", "entry": 10, "exit": 9, "net_pct": -1}]
    assert "config_snapshot" not in json.dumps(result)
    assert btc["autonomous_execution_eligible"] is False


def test_oos_report_reader_hides_artifacts_from_a_different_replay_build(tmp_path):
    payload = _payload("2026-02-01T00:00:00+00:00", "BTC-USD", "validated", net_pct=4)
    payload["replay_provenance"]["strategy_backtester_sha256"] = "stale-build"
    (tmp_path / "oos_validation_stale.json").write_text(json.dumps(payload), encoding="utf-8")

    result = get_latest_oos_validation_reports(tmp_path)

    assert result["status"] == "no_reports"
