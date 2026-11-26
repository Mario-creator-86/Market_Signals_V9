import hashlib
import json
from pathlib import Path

from core.research_validation_reports import get_latest_crypto_research_validation_reports


def _payload(generated_at: str, *, status: str = "negative_edge"):
    root = Path(__file__).resolve().parents[2]
    strategy_source = root / "core" / "crypto_research_strategies.py"
    validator_source = root / "core" / "walk_forward_validation.py"
    return {
        "generated_at": generated_at,
        "research_only": True,
        "reports": [{
            "ticker": "BTC-USD",
            "strategy_id": "R5_DONCHIAN_4H_55_20_50",
            "status": status,
            "reason": "Recorded research result",
            "method": "fixed_configuration_pre_registered_crypto_research_ohlcv",
            "metrics": {"sample_size": 72, "profit_factor": 0.95, "capital_return_pct": -0.42},
            "thresholds": {"min_windows": 10, "min_sample": 30, "min_profit_factor": 1.2},
            "source_data": {
                "actual_start": "2022-01-01T00:00:00+00:00",
                "actual_end": "2026-01-01T00:00:00+00:00",
                "received_bars": 8760,
                "source_truncated": False,
            },
            "research_provenance": {
                "replay": "core.crypto_research_strategies.replay_research_strategy",
                "strategy_file_sha256": hashlib.sha256(strategy_source.read_bytes()).hexdigest(),
                "walk_forward_validation_sha256": hashlib.sha256(validator_source.read_bytes()).hexdigest(),
                "config_sha256": "config-hash",
                "config_snapshot": {"must_not": "be exposed"},
                "historical_news": "not_available_and_not_replayed",
            },
            "oos_trades": [{"ticker": "BTC-USD", "entry": 10, "exit": 9, "net_pct": -1, "internal": "hidden"}],
        }],
    }


def test_research_reader_hides_missing_or_stale_artifacts(tmp_path):
    result = get_latest_crypto_research_validation_reports(tmp_path)
    assert result["status"] == "no_reports"

    stale = _payload("2026-01-01T00:00:00+00:00")
    stale["reports"][0]["research_provenance"]["strategy_file_sha256"] = "stale"
    (tmp_path / "pre_registered_crypto_oos_stale.json").write_text(json.dumps(stale), encoding="utf-8")
    assert get_latest_crypto_research_validation_reports(tmp_path)["status"] == "no_reports"


def test_research_reader_keeps_latest_candidate_and_redacts_snapshot(tmp_path):
    old = _payload("2026-01-01T00:00:00+00:00")
    new = _payload("2026-02-01T00:00:00+00:00", status="validated")
    (tmp_path / "pre_registered_crypto_oos_old.json").write_text(json.dumps(old), encoding="utf-8")
    (tmp_path / "pre_registered_crypto_oos_new.json").write_text(json.dumps(new), encoding="utf-8")

    result = get_latest_crypto_research_validation_reports(tmp_path)

    assert result["status"] == "available"
    assert result["report_kind"] == "pre_registered_crypto_research"
    report = result["reports"][0]
    assert report["strategy_id"] == "R5_DONCHIAN_4H_55_20_50"
    assert report["status"] == "validated"
    assert report["closed_oos_trades"] == [{"ticker": "BTC-USD", "entry": 10, "exit": 9, "net_pct": -1}]
    assert "config_snapshot" not in json.dumps(result)
    assert report["autonomous_execution_eligible"] is False
