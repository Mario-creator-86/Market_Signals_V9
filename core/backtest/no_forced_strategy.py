"""Evidence hierarchy and explicit no-strategy outcome from section 61."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

from .types import BacktestMetrics, EvidenceLevel

NO_VALIDATED_TICKER_SPECIFIC_STRATEGY = "NO_VALIDATED_TICKER_SPECIFIC_STRATEGY"


@dataclass(frozen=True)
class EvidenceResolution:
    level: EvidenceLevel
    metrics: Optional[BacktestMetrics]
    is_sufficient: bool
    status: str

    def __iter__(self) -> Iterator[object]:
        """Keep the documented ``level, metrics = resolve(...)`` interface."""

        yield self.level
        yield self.metrics


def assert_min_sample(metrics: Optional[BacktestMetrics], *, min_sample: int) -> bool:
    """Require an authoritative result with enough closed OOS trades."""

    if min_sample <= 0:
        raise ValueError("min_sample must be positive")
    return bool(
        metrics is not None
        and metrics.is_authoritative
        and metrics.num_trades >= min_sample
    )


def resolve_evidence_level(
    ticker: str,
    asset_class: str,
    *,
    ticker_metrics: Optional[BacktestMetrics] = None,
    industry_metrics: Optional[BacktestMetrics] = None,
    sector_metrics: Optional[BacktestMetrics] = None,
    universe_metrics: Optional[BacktestMetrics] = None,
    min_sample: int = 24,
) -> EvidenceResolution:
    """Return the first sufficient level and label broader evidence honestly."""

    if not str(ticker or "").strip():
        raise ValueError("ticker must not be empty")
    if not str(asset_class or "").strip():
        raise ValueError("asset_class must not be empty")
    candidates = (
        (EvidenceLevel.TICKER, ticker_metrics),
        (EvidenceLevel.INDUSTRY, industry_metrics),
        (EvidenceLevel.SECTOR, sector_metrics),
        (EvidenceLevel.UNIVERSE, universe_metrics),
    )
    for level, metrics in candidates:
        if assert_min_sample(metrics, min_sample=min_sample):
            return EvidenceResolution(
                level=level,
                metrics=metrics,
                is_sufficient=True,
                status=f"VALIDATED_{level.value}_EVIDENCE",
            )
    return EvidenceResolution(
        level=EvidenceLevel.UNIVERSE,
        metrics=universe_metrics,
        is_sufficient=False,
        status=NO_VALIDATED_TICKER_SPECIFIC_STRATEGY,
    )
