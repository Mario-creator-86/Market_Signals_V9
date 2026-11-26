"""Safe read-only presentation for pre-registered crypto research artifacts.

Research reports are intentionally distinct from production OOS reports.  They
can document a rejected candidate, but cannot grant a signal or execution
permission.  Reports are hidden when the current strategy or validator code no
longer matches the artifact that generated them.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.oos_validation_reports import _parse_timestamp, _public_trade, _report_directory


_REPORT_GLOB = "pre_registered_crypto_oos_*.json"
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _current_hashes() -> Optional[Dict[str, str]]:
    try:
        strategy_source = _PROJECT_ROOT / "core" / "crypto_research_strategies.py"
        validator_source = _PROJECT_ROOT / "core" / "walk_forward_validation.py"
        return {
            "strategy_file_sha256": hashlib.sha256(strategy_source.read_bytes()).hexdigest(),
            "walk_forward_validation_sha256": hashlib.sha256(validator_source.read_bytes()).hexdigest(),
        }
    except OSError:
        return None


def _public_report(
    report: Dict[str, Any],
    *,
    provenance: Dict[str, Any],
    generated_at: Any,
    current_hashes: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    ticker = str(report.get("ticker") or "").upper().strip()
    strategy_id = str(report.get("strategy_id") or "").upper().strip()
    source_data = report.get("source_data") if isinstance(report.get("source_data"), dict) else {}
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    thresholds = report.get("thresholds") if isinstance(report.get("thresholds"), dict) else {}
    if not ticker or not strategy_id:
        return None
    if not {"actual_start", "actual_end", "received_bars"}.issubset(source_data):
        return None
    required_provenance = {
        "replay",
        "strategy_file_sha256",
        "walk_forward_validation_sha256",
        "config_sha256",
    }
    if not required_provenance.issubset(provenance):
        return None
    if (
        provenance.get("strategy_file_sha256") != current_hashes["strategy_file_sha256"]
        or provenance.get("walk_forward_validation_sha256") != current_hashes["walk_forward_validation_sha256"]
    ):
        return None

    trades = [item for item in (_public_trade(row) for row in report.get("oos_trades", [])) if item is not None]
    cost_model = report.get("cost_model") if isinstance(report.get("cost_model"), dict) else None
    if cost_model is None and isinstance(provenance.get("cost_model"), dict):
        cost_model = provenance["cost_model"]
    return {
        "ticker": ticker,
        "strategy_id": strategy_id,
        "status": str(report.get("status") or "unknown"),
        "reason": str(report.get("reason") or "No research verdict was recorded."),
        "method": str(report.get("method") or "unknown"),
        "as_of": report.get("as_of"),
        "generated_at": generated_at,
        "metrics": {
            key: metrics.get(key)
            for key in (
                "sample_size", "wins", "losses", "win_rate", "profit_factor",
                "expectancy_r", "expectancy_pct", "net_pct", "raw_max_drawdown_pct",
                "capital_risk_per_trade_pct", "capital_equity_start", "capital_equity_end",
                "capital_return_pct", "capital_equity_model", "max_drawdown_pct",
            )
            if key in metrics
        },
        "thresholds": {
            key: thresholds.get(key)
            for key in (
                "min_windows", "min_sample", "min_profit_factor",
                "min_expectancy_r", "max_drawdown_pct",
            )
            if key in thresholds
        },
        "source_data": {
            key: source_data.get(key)
            for key in (
                "source", "symbol", "interval", "requested_days", "requested_start",
                "requested_end", "actual_start", "actual_end", "actual_span_days",
                "expected_bars", "received_bars", "source_truncated",
            )
            if key in source_data
        },
        "capital_model": report.get("capital_model") if isinstance(report.get("capital_model"), dict) else {},
        "closed_oos_trades": trades,
        "limitations": [str(item) for item in report.get("limitations", []) if isinstance(item, str)],
        "replay_provenance": {
            key: provenance.get(key)
            for key in (
                "replay", "strategy_file_sha256", "walk_forward_validation_sha256", "config_sha256",
                "historical_news",
            )
            if key in provenance
        } | ({"cost_model": cost_model} if isinstance(cost_model, dict) else {}),
        "autonomous_execution_eligible": False,
        "autonomous_execution_reason": (
            "Pre-registered price-only research is never an execution approval. "
            "A matching production replay, timestamped news treatment, and realized Capital evidence are required."
        ),
    }


def get_latest_crypto_research_validation_reports(output_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Return the latest reproducible research artifact per ticker and strategy."""
    directory = output_dir or _report_directory()
    current_hashes = _current_hashes()
    latest: Dict[Tuple[str, str], Tuple[datetime, Dict[str, Any]]] = {}
    if current_hashes is not None and directory.is_dir():
        for path in directory.glob(_REPORT_GLOB):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict) or not payload.get("research_only"):
                    continue
                reports = payload.get("reports")
                if not isinstance(reports, list):
                    continue
                generated_at = payload.get("generated_at")
                when = _parse_timestamp(generated_at)
                if when == datetime.min.replace(tzinfo=timezone.utc):
                    when = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                    generated_at = when.isoformat()
                for raw_report in reports:
                    if not isinstance(raw_report, dict):
                        continue
                    provenance = raw_report.get("research_provenance")
                    public = _public_report(
                        raw_report,
                        provenance=provenance if isinstance(provenance, dict) else {},
                        generated_at=generated_at,
                        current_hashes=current_hashes,
                    )
                    if public is None:
                        continue
                    key = (public["ticker"], public["strategy_id"])
                    existing = latest.get(key)
                    if existing is None or when > existing[0]:
                        latest[key] = (when, public)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue

    reports = [item[1] for item in sorted(latest.values(), key=lambda item: (item[1]["ticker"], item[1]["strategy_id"]))]
    if not reports:
        return {
            "status": "no_reports",
            "report_kind": "pre_registered_crypto_research",
            "reports": [],
            "autonomous_execution_eligible": False,
            "reason": "No reproducible pre-registered crypto research report is available for this build.",
        }
    newest = max(item[0] for item in latest.values())
    return {
        "status": "available",
        "report_kind": "pre_registered_crypto_research",
        "generated_at": newest.isoformat(),
        "report_count": len(reports),
        "reports": reports,
        "autonomous_execution_eligible": False,
        "reason": (
            "Pre-registered price-only research is displayed for audit, including rejected candidates. "
            "It never authorizes autonomous execution."
        ),
    }
