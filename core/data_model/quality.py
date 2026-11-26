"""DATA-quality vs SIGNAL/ANALYSIS-quality contract (spec §33).

Every important analysis exposes: source, timestamp, freshness, coverage,
stale, conflicts, quality_flags. DATA quality is separate from SIGNAL/ANALYSIS
quality — a record may carry both side-by-side via QualityEnvelope.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


@dataclass(frozen=True)
class Conflict:
    """A cross-source disagreement on a single field (folds core.price_integrity output)."""
    field: str
    values: tuple
    sources: tuple
    deviation_pct: Optional[float] = None


@dataclass(frozen=True)
class DataQuality:
    """DATA quality (§33). Distinct from SIGNAL/ANALYSIS quality."""
    source: str
    timestamp: datetime
    freshness: Optional[timedelta] = None
    coverage: Optional[float] = None      # 0..1 fraction of requested universe served
    stale: bool = False
    conflicts: tuple = ()                 # tuple[Conflict, ...]
    quality_flags: frozenset = frozenset()


@dataclass(frozen=True)
class SignalQuality:
    """SIGNAL/ANALYSIS quality (§33). Mirrors core.signal_quality metrics."""
    win_rate: float
    profit_factor: float
    avg_R: float
    expectancy_R: float
    max_drawdown: float
    sample_size: int
    alerts: tuple = ()


@dataclass(frozen=True)
class QualityEnvelope:
    """Carries DATA quality and SIGNAL/ANALYSIS quality side-by-side (§33 separation)."""
    data: Optional[DataQuality] = None
    signal: Optional[SignalQuality] = None