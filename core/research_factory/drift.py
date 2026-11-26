"""Phase 14 §79 — concept-drift monitor: REVALIDATION, never risk changes.

Compares a recent window of closed trades against a reference window per
strategy+direction (win rate, expectancy, trade frequency, average P/L).
Verdicts: STABLE / DRIFT_SUSPECTED / INSUFFICIENT_SAMPLE. A suspected drift
emits a REVALIDATION recommendation into the experiment registry evidence —
it NEVER changes risk limits, sizing or autonomy (§79 forbids automatic
risk increase; the Risk Constitution is §48-protected anyway).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from core.research_factory.weakness import load_closed_trades

DEFAULT_DRIFT_CONFIG = {
    "min_sample": 10,            # per window, else INSUFFICIENT_SAMPLE
    "reference_trades": 30,
    "recent_trades": 10,
    "win_rate_delta": 0.15,      # absolute drop → suspect
    "expectancy_ratio": 0.5,     # recent < 50% of reference → suspect
}


@dataclass(frozen=True)
class DriftReport:
    strategy_id: str
    direction: str
    verdict: str                 # STABLE | DRIFT_SUSPECTED | INSUFFICIENT_SAMPLE
    metrics: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id, "direction": self.direction,
            "verdict": self.verdict, "metrics": dict(self.metrics),
        }


def _stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    sample = len(trades)
    pnls = [float(t.get("net_pnl") or 0.0) for t in trades]
    wins = sum(1 for pnl in pnls if pnl > 0)
    return {
        "sample": sample,
        "win_rate": round(wins / sample, 6) if sample else None,
        "avg_net_pnl": round(sum(pnls) / sample, 6) if sample else None,
    }


def _key(trade: Dict[str, Any]) -> str:
    return f"{str(trade.get('strategy_type') or 'UNKNOWN').upper()}|{str(trade.get('direction') or '').upper()}"


def detect_drift(
    trades: List[Dict[str, Any]],
    *,
    config: Optional[Dict[str, Any]] = None,
) -> List[DriftReport]:
    cfg = {**DEFAULT_DRIFT_CONFIG, **(config or {})}
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for trade in trades or []:
        grouped.setdefault(_key(trade), []).append(trade)

    reports: List[DriftReport] = []
    for key, history in sorted(grouped.items()):
        strategy_id, direction = key.split("|", 1)
        recent = history[-max(1, int(cfg["recent_trades"])):]
        reference = history[:-len(recent)][-max(1, int(cfg["reference_trades"])):] \
            if len(history) > len(recent) else []
        recent_stats = _stats(recent)
        reference_stats = _stats(reference)
        min_sample = int(cfg["min_sample"])
        if recent_stats["sample"] < min_sample or reference_stats["sample"] < min_sample:
            reports.append(DriftReport(
                strategy_id=strategy_id, direction=direction,
                verdict="INSUFFICIENT_SAMPLE",
                metrics={"recent": recent_stats, "reference": reference_stats},
            ))
            continue
        win_rate_drop = (reference_stats["win_rate"] - recent_stats["win_rate"])
        ref_avg = reference_stats["avg_net_pnl"] or 0.0
        rec_avg = recent_stats["avg_net_pnl"] or 0.0
        expectancy_ratio = (rec_avg / ref_avg) if ref_avg > 0 else None
        suspected = (
            win_rate_drop >= float(cfg["win_rate_delta"])
            or (expectancy_ratio is not None and expectancy_ratio < float(cfg["expectancy_ratio"]))
        )
        reports.append(DriftReport(
            strategy_id=strategy_id, direction=direction,
            verdict="DRIFT_SUSPECTED" if suspected else "STABLE",
            metrics={
                "recent": recent_stats, "reference": reference_stats,
                "win_rate_drop": round(win_rate_drop, 6),
                "expectancy_ratio": round(expectancy_ratio, 6) if expectancy_ratio is not None else None,
            },
        ))
    return reports


def revalidation_recommendations(reports: Iterable[DriftReport]) -> List[Dict[str, Any]]:
    """§79 — drift triggers REVALIDATION, not AUTOMATIC RISK INCREASE."""
    return [
        {
            "strategy_id": report.strategy_id,
            "direction": report.direction,
            "action": "REVALIDATION",
            "metrics": report.metrics,
        }
        for report in reports if report.verdict == "DRIFT_SUSPECTED"
    ]


__all__ = ["DriftReport", "detect_drift", "revalidation_recommendations", "DEFAULT_DRIFT_CONFIG"]
