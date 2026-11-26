"""Signal lifecycle type (§52) — an overlay over the dict-based
core.signals.SignalManager path. The dict path is preserved (back-compat);
consumers may opt into the typed Signal."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping, Tuple
from .horizons import SignalHorizon, map_mode_to_horizon


@dataclass(frozen=True)
class Signal:
    ticker: str
    horizon: SignalHorizon
    direction: str
    score: float
    signal_type: str
    source: str
    catalog_refs: Tuple[str, ...]
    raw: Mapping[str, Any]
    is_not_probability: bool = True

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], *, horizon: SignalHorizon | None = None) -> "Signal":
        d = dict(d or {})
        h = horizon or map_mode_to_horizon(
            d.get("mode") or d.get("strategy_id") or d.get("signal_type") or ""
        )
        return cls(
            ticker=str(d.get("symbol") or d.get("ticker") or ""),
            horizon=h,
            direction=str(d.get("direction") or d.get("side") or ""),
            score=float(d.get("signal_score") or d.get("score") or d.get("total_score") or 0.0),
            signal_type=str(d.get("signal_type") or d.get("source") or "v1_logistic"),
            source=str(d.get("source") or "core.scoring_system"),
            catalog_refs=tuple(d.get("catalog_refs") or ()),
            raw=d,
        )

    def to_dict(self) -> dict:
        out = dict(self.raw)
        out.update({
            "horizon": self.horizon.value,
            "signal_horizon": self.horizon.value,
            "direction": self.direction,
            "score": self.score,
            "signal_type": self.signal_type,
            "source": self.source,
            "catalog_refs": list(self.catalog_refs),
            "is_not_probability": self.is_not_probability,
        })
        return out