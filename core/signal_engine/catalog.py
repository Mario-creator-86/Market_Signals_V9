"""§52 Signal Engine catalog — preserve useful V7 signals; do NOT add
indicators merely because they exist. Names the EXISTING V7 indicators and
their incremental-value status. Adds none."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Mapping, Tuple


class IndicatorStatus(str, Enum):
    PRESERVED = "PRESERVED"
    EVALUATE = "EVALUATE"
    DEFERRED = "DEFERRED"


@dataclass(frozen=True)
class IndicatorSpec:
    name: str
    source_module: str
    status: IndicatorStatus
    horizon_fit: FrozenSet[str]
    incremental_value_note: str


SIGNAL_CATALOG: Tuple[IndicatorSpec, ...] = (
    IndicatorSpec("trend", "core.scoring_system", IndicatorStatus.PRESERVED,
                  frozenset({"SWING", "MEDIUM_TERM"}), "EMA slopes + MTF consensus in V1 logistic"),
    IndicatorSpec("momentum", "core.scoring_system", IndicatorStatus.PRESERVED,
                  frozenset({"SHORT_TERM", "SWING"}), "MACD hist + price_action components"),
    IndicatorSpec("rsi", "core.ta", IndicatorStatus.PRESERVED,
                  frozenset({"SHORT_TERM", "SWING"}), "calc_rsi, V1 + V2 context_builder"),
    IndicatorSpec("macd", "core.ta", IndicatorStatus.PRESERVED,
                  frozenset({"SHORT_TERM", "SWING"}), "calc_macd, V1 logistic + V2 context"),
    IndicatorSpec("moving_averages", "core.scoring_system", IndicatorStatus.PRESERVED,
                  frozenset({"SWING", "MEDIUM_TERM"}), "ema20/50/200 in context_builder"),
    IndicatorSpec("relative_strength", "core.strategy_v2.context_builder", IndicatorStatus.EVALUATE,
                  frozenset({"SWING", "MEDIUM_TERM"}), "vs benchmark/sector — partial in V2, evaluate incremental value"),
    IndicatorSpec("volume", "core.scoring_system", IndicatorStatus.PRESERVED,
                  frozenset({"SHORT_TERM", "SWING"}), "volume_avg + vwap components"),
    IndicatorSpec("rvol", "core.strategy_v2.context_builder", IndicatorStatus.EVALUATE,
                  frozenset({"SHORT_TERM"}), "volume ratio vs avg — evaluate incremental value"),
    IndicatorSpec("atr", "core.scoring_system", IndicatorStatus.PRESERVED,
                  frozenset({"SHORT_TERM", "SWING"}), "calculate_atr, V1 + V2"),
    IndicatorSpec("volatility", "core.scoring_system", IndicatorStatus.PRESERVED,
                  frozenset({"SHORT_TERM", "SWING", "MEDIUM_TERM"}), "atr_percentile + bb_width"),
    IndicatorSpec("market_regime", "core.strategy_v2.regime_engine", IndicatorStatus.PRESERVED,
                  frozenset({"SHORT_TERM", "SWING", "MEDIUM_TERM"}), "RegimeEngine V2 + detect_market_regime V1; market-wide layer in signal_engine.regime"),
    IndicatorSpec("sector", "core.sectors", IndicatorStatus.PRESERVED,
                  frozenset({"SWING", "MEDIUM_TERM"}), "get_sector/get_tickers_by_sector; sector regime rollup in §39 tool"),
    IndicatorSpec("earnings", "agents.pre_earnings_agent", IndicatorStatus.PRESERVED,
                  frozenset({"SHORT_TERM", "SWING"}), "Pre-Earnings agent (Phase 5)"),
    IndicatorSpec("news", "core.news_evidence_ledger", IndicatorStatus.DEFERRED,
                  frozenset({"SHORT_TERM", "SWING"}), "deferred to Phase 10 (News/FinBERT/SEC)"),
    IndicatorSpec("sentiment", "core.news_evidence_ledger", IndicatorStatus.DEFERRED,
                  frozenset({"SHORT_TERM", "SWING"}), "FinBERT sentiment deferred to Phase 10"),
    IndicatorSpec("ml", "core.ml.baseline", IndicatorStatus.EVALUATE,
                  frozenset({"SWING", "MEDIUM_TERM"}), "simple baseline first (§54); complex models gated by OosGate (Phase 7)"),
)

_CATALOG_INDEX: Mapping[str, IndicatorSpec] = {s.name: s for s in SIGNAL_CATALOG}


def get_catalog_index() -> Mapping[str, IndicatorSpec]:
    return _CATALOG_INDEX


def preserved_indicators() -> Tuple[IndicatorSpec, ...]:
    return tuple(s for s in SIGNAL_CATALOG if s.status is IndicatorStatus.PRESERVED)