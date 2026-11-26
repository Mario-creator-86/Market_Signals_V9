"""Market-wide regime (§49 input) — the gap the V7 surface lacks. V7 has
per-ticker regime (RegimeEngine V2 + detect_market_regime V1) and a sector
rollup (§39 tool). This module adds the MARKET-WIDE breadth layer only; the
per-ticker and sector regime are reused unchanged (§5/§7). The VIX/macro
layer is an honest stub (§4 — macro data source is a later-phase wiring)."""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Iterable, Mapping


MARKET_RISK_ON = "RISK_ON"
MARKET_RISK_OFF = "RISK_OFF"
MARKET_MIXED = "MIXED"
MARKET_TRANSITIONING = "TRANSITIONING"

_RISK_ON_LABELS = frozenset({"TREND", "SESSION_BREAKOUT"})
_RISK_OFF_LABELS = frozenset({"RANGE", "HIGH_VOL", "EVENT_RISK"})


@dataclass(frozen=True)
class MarketRegimeResult:
    label: str
    breadth_pct: float
    distribution: Mapping[str, int]
    sample_size: int
    is_stale: bool
    is_not_probability: bool = True


class MarketRegimeEngine:
    """Aggregates per-ticker regimes (produced by the existing V2 RegimeEngine)
    into a market-wide breadth regime. Callers supply per-ticker regimes; this
    engine only aggregates (no per-ticker recompute)."""

    def __init__(self, *, universe_min: int = 10):
        self.universe_min = universe_min

    def detect_market(self, universe_regimes: Iterable[Mapping]) -> MarketRegimeResult:
        regimes = [str((r or {}).get("regime", "")).upper()
                   for r in universe_regimes if isinstance(r, Mapping)]
        n = len(regimes)
        if n < self.universe_min:
            return MarketRegimeResult(
                label=MARKET_MIXED, breadth_pct=0.0,
                distribution=dict(Counter(regimes)), sample_size=n, is_stale=True,
            )
        counts = Counter(regimes)
        risk_on = sum(counts.get(k, 0) for k in _RISK_ON_LABELS)
        risk_off = sum(counts.get(k, 0) for k in _RISK_OFF_LABELS)
        breadth = risk_on / max(n, 1)
        if breadth >= 0.60:
            label = MARKET_RISK_ON
        elif breadth <= 0.35:
            label = MARKET_RISK_OFF
        elif risk_off == 0 and breadth >= 0.45:
            label = MARKET_TRANSITIONING
        else:
            label = MARKET_MIXED
        return MarketRegimeResult(
            label=label, breadth_pct=round(breadth, 4),
            distribution=dict(counts), sample_size=n, is_stale=False,
        )

    def detect_sector(self, sector: str, per_ticker_regimes: Iterable[Mapping]) -> MarketRegimeResult:
        """Sector regime = breadth over the sector's tickers. Thin wrapper for
        symmetry with detect_market; the §39 tool's own rollup stays the source
        of truth — same aggregation shape."""
        return self.detect_market(per_ticker_regimes)


def detect_macro_regime() -> Dict[str, object]:
    """§4 honest stub: VIX/macro data source not wired (V7 feed is crypto/1m)."""
    return {"available": False, "reason": "macro_vix_data_source_deferred"}