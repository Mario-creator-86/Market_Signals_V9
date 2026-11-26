"""Phase 14 §111 — deterministic weakness detection.

Structured observations over closed-trade history (per strategy+direction):
rolling expectancy, win rate and adverse streak vs explicit thresholds.
An observation is a FACT record (metric, value, threshold, sample, window)
— the only sanctioned seed for a §76 hypothesis. No vague prose, no
invented numbers; insufficient sample yields INSUFFICIENT_SAMPLE, not a
guess.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import DATA_DIR

DEFAULT_JOURNAL_PATHS = (
    DATA_DIR / "trade_journal.json",
    DATA_DIR / "research_cohort_journal.json",
)

DEFAULT_THRESHOLDS = {
    "min_sample": 10,             # below this nothing is claimed
    "min_expectancy_pct": 0.0,    # negative expectancy over the window = weakness
    "min_win_rate": 0.40,
    "window_trades": 20,
}


def load_closed_trades(paths=None) -> List[Dict[str, Any]]:
    trades: List[Dict[str, Any]] = []
    for path in paths or DEFAULT_JOURNAL_PATHS:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(payload, list):
            trades.extend(t for t in payload if isinstance(t, dict))
    trades.sort(key=lambda t: str(t.get("closed_at") or ""))
    return trades


@dataclass(frozen=True)
class WeaknessObservation:
    strategy_id: str
    direction: str
    metric: str               # expectancy_pct | win_rate
    value: float
    threshold: float
    sample: int
    verdict: str              # WEAKNESS | INSUFFICIENT_SAMPLE
    window_trades: int
    detail: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id, "direction": self.direction,
            "metric": self.metric, "value": self.value, "threshold": self.threshold,
            "sample": self.sample, "verdict": self.verdict,
            "window_trades": self.window_trades, "detail": dict(self.detail),
        }


def _key(trade: Dict[str, Any]) -> str:
    return f"{str(trade.get('strategy_type') or 'UNKNOWN').upper()}|{str(trade.get('direction') or '').upper()}"


def detect_weaknesses(
    trades: List[Dict[str, Any]],
    *,
    thresholds: Optional[Dict[str, Any]] = None,
) -> List[WeaknessObservation]:
    cfg = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    window = max(1, int(cfg["window_trades"]))
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for trade in trades or []:
        grouped.setdefault(_key(trade), []).append(trade)

    observations: List[WeaknessObservation] = []
    for key, history in sorted(grouped.items()):
        strategy_id, direction = key.split("|", 1)
        recent = history[-window:]
        sample = len(recent)
        if sample < int(cfg["min_sample"]):
            observations.append(WeaknessObservation(
                strategy_id=strategy_id, direction=direction, metric="sample",
                value=float(sample), threshold=float(cfg["min_sample"]), sample=sample,
                verdict="INSUFFICIENT_SAMPLE", window_trades=window,
                detail={"note": "no weakness claim below the minimum sample"},
            ))
            continue
        pnls = [float(t.get("net_pnl") or 0.0) for t in recent]
        wins = sum(1 for pnl in pnls if pnl > 0)
        win_rate = wins / sample
        expectancy = sum(pnls) / sample
        # Normalize expectancy per closed trade against average exposure when
        # present; fall back to raw per-trade P/L (still a deterministic fact).
        exposures = [float(t.get("invested_amount") or 0.0) or 0.0 for t in recent]
        avg_exposure = (sum(exposures) / len(exposures)) if exposures else 0.0
        expectancy_pct = (expectancy / avg_exposure * 100.0) if avg_exposure > 0 else expectancy
        observations.append(WeaknessObservation(
            strategy_id=strategy_id, direction=direction, metric="expectancy_pct",
            value=round(expectancy_pct, 6), threshold=float(cfg["min_expectancy_pct"]),
            sample=sample, verdict="WEAKNESS" if expectancy_pct < float(cfg["min_expectancy_pct"]) else "OK",
            window_trades=window,
            detail={"wins": wins, "losses": sample - wins, "avg_net_pnl": round(expectancy, 6)},
        ))
        observations.append(WeaknessObservation(
            strategy_id=strategy_id, direction=direction, metric="win_rate",
            value=round(win_rate, 6), threshold=float(cfg["min_win_rate"]),
            sample=sample, verdict="WEAKNESS" if win_rate < float(cfg["min_win_rate"]) else "OK",
            window_trades=window,
            detail={"wins": wins, "losses": sample - wins},
        ))
    return observations


def actionable_weaknesses(observations: List[WeaknessObservation]) -> List[WeaknessObservation]:
    return [o for o in observations if o.verdict == "WEAKNESS"]


def hypothesis_seed_from_observation(observation: WeaknessObservation) -> Dict[str, Any]:
    """Map one structured weakness to §76 draft inputs (caller completes them)."""
    return {
        "problem": (
            f"{observation.strategy_id} {observation.direction}: {observation.metric}="
            f"{observation.value} is below the {observation.threshold} threshold "
            f"over the last {observation.sample} closed trades."
        ),
        "observation": json.dumps(observation.to_dict(), sort_keys=True),
        "source_observation": observation.to_dict(),
    }


__all__ = [
    "WeaknessObservation", "detect_weaknesses", "actionable_weaknesses",
    "hypothesis_seed_from_observation", "load_closed_trades", "DEFAULT_THRESHOLDS",
]
