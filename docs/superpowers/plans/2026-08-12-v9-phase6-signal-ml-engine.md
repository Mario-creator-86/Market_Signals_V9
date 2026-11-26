# Phase 6 — Signal & ML Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the V7 signal surface into the V9 Signal & ML Engine (§52 catalog, §53 horizons, §54 ML registry + simple baseline + OosGate seam, §55 calibration, §56 meta-labeling) + a market-wide regime layer, all additive and composing the existing V1/V2 pipelines.

**Architecture:** New `core/signal_engine/` (horizons/catalog/regime/types) + `core/ml/` (registry/baseline/calibration/meta_label/qlib_adapter) packages, composed into the existing `pre_earnings` decision context and the §39 regime tool. No existing signal/indicator code is rewritten; later-phase inputs (§57 backtest, §63 strategy, §10 news) stay honest stubs.

**Tech Stack:** Python 3.14 stdlib + numpy/pandas already in V7 (no new deps, §118). Qlib import-guarded optional ADAPTER. pytest golden tests.

## Global Constraints

- §5/§7 evolve-not-rewrite — additive diffs only; V1/V2 pipelines, `SignalManager`, `RegimeEngine`, `detect_market_regime`, `context_builder`, the §39 regime tools, and agents are composed, not rewritten.
- §4 — no forbidden success language; later-phase inputs are honest stubs (`available:False`+reason), never faked numbers.
- §55 — LLM confidence is never a statistical probability (enforced in code: `from_llm_confidence` raises).
- §54 — complex models prove OOS improvement; in Phase 6 only the gate+seam exist (real OOS is Phase 7).
- §118 — zero new production deps. Qlib import-guarded and optional; baseline/calibration/meta-label are stdlib/numpy.
- §115 — tests not weakened; deterministic synthetic data for golden/unit ML tests.
- §116 — no remote push; commit to master only. §106 — no secrets.
- V7 at `Market Signals V7 - Premium` (HEAD f912a33) is READ-ONLY — never modified.

---

## File Structure

- Create: `core/signal_engine/__init__.py`, `horizons.py`, `catalog.py`, `regime.py`, `types.py`
- Create: `core/ml/__init__.py`, `registry.py`, `baseline.py`, `calibration.py`, `meta_label.py`, `qlib_adapter.py`
- Create: `tests/golden/test_phase6a_golden.py` (GOLD-052, GOLD-053), `tests/golden/test_phase6b_golden.py` (GOLD-054, GOLD-055, GOLD-056)
- Create: `tests/unit/test_signal_engine.py`, `tests/unit/test_ml_registry_baseline.py`, `tests/unit/test_ml_calibration_meta.py`
- Modify: `core/pre_earnings/decision.py` (§51 `signal` input → real delegation)
- Modify: `core/ai/tools/market_data.py` (additive market-wide regime consultation)
- Modify: `core/config.py` + `config.example.json` (additive `ml` + `signal_engine` config)
- Modify: `core/scoring_system.py` (evolve `run_weight_calibration_audit` stub to delegate to `core.ml`)
- Modify: control docs + SDD ledger + memory

---

### Task 1: `core/signal_engine/horizons.py` — §53 horizons

**Files:** Create `core/signal_engine/horizons.py`; Test `tests/unit/test_signal_engine.py` (append).

**Interfaces:** Produces `SignalHorizon`, `HorizonDefinition`, `HORIZON_DEFINITIONS`, `map_mode_to_horizon`, `align_horizon`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_signal_engine.py
from core.signal_engine.horizons import (
    SignalHorizon, HORIZON_DEFINITIONS, map_mode_to_horizon, align_horizon,
)


def test_horizons_enum():
    assert {h.value for h in SignalHorizon} == {"SHORT_TERM", "SWING", "MEDIUM_TERM"}


def test_map_mode_to_horizon_fail_safe():
    assert map_mode_to_horizon("MOMENTUM") is SignalHorizon.SWING
    assert map_mode_to_horizon("PULLBACK") is SignalHorizon.SHORT_TERM
    assert map_mode_to_horizon("nope") is SignalHorizon.SWING
    assert map_mode_to_horizon(None) is SignalHorizon.SWING


def test_align_horizon():
    assert align_horizon(SignalHorizon.SHORT_TERM, "1m") is True
    assert align_horizon(SignalHorizon.SHORT_TERM, "1d") is False
    assert align_horizon(SignalHorizon.MEDIUM_TERM, "1d") is True
```

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/unit/test_signal_engine.py -v` → FAIL (no module).

- [ ] **Step 3: Write minimal implementation**

```python
# core/signal_engine/horizons.py
"""§53 Signal Horizons — SHORT_TERM / SWING / MEDIUM_TERM.

Targets/features must align with horizon (§53). The existing V7 per-mode
strings (core/scoring_system.TIMEFRAME_MAP / HOLDING_PERIOD_MAP) are evolved
into this enum via map_mode_to_horizon; the strings are not removed."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Mapping


class SignalHorizon(str, Enum):
    SHORT_TERM = "SHORT_TERM"
    SWING = "SWING"
    MEDIUM_TERM = "MEDIUM_TERM"


@dataclass(frozen=True)
class HorizonDefinition:
    horizon: SignalHorizon
    sessions_min: int
    sessions_max: int
    target_condition_template: str  # §55 seed
    feature_timeframes: FrozenSet[str]

    def target_condition(self) -> str:
        return self.target_condition_template.format(
            n=f"{self.sessions_min}-{self.sessions_max}"
        )


HORIZON_DEFINITIONS: Mapping[SignalHorizon, HorizonDefinition] = {
    SignalHorizon.SHORT_TERM: HorizonDefinition(
        horizon=SignalHorizon.SHORT_TERM, sessions_min=1, sessions_max=5,
        target_condition_template="P(return > 0 over next {n} sessions)",
        feature_timeframes=frozenset({"1m", "5m", "15m"}),
    ),
    SignalHorizon.SWING: HorizonDefinition(
        horizon=SignalHorizon.SWING, sessions_min=5, sessions_max=20,
        target_condition_template="P(return > 0 over next {n} sessions)",
        feature_timeframes=frozenset({"15m", "1h", "4h"}),
    ),
    SignalHorizon.MEDIUM_TERM: HorizonDefinition(
        horizon=SignalHorizon.MEDIUM_TERM, sessions_min=20, sessions_max=60,
        target_condition_template="P(return > 0 over next {n} sessions)",
        feature_timeframes=frozenset({"1h", "4h", "1d"}),
    ),
}

_MODE_TO_HORIZON: Mapping[str, SignalHorizon] = {
    "PULLBACK": SignalHorizon.SHORT_TERM,
    "BREAKOUT": SignalHorizon.SHORT_TERM,
    "MOMENTUM": SignalHorizon.SWING,
    "MEAN_REVERSION": SignalHorizon.SWING,
    "TrendPullback": SignalHorizon.SWING,
    "BreakoutRetest": SignalHorizon.SHORT_TERM,
    "VolSqueeze": SignalHorizon.SWING,
    "MeanReversion": SignalHorizon.SWING,
    "MomentumContinuation": SignalHorizon.SHORT_TERM,
    "FxSessionBreakout": SignalHorizon.SHORT_TERM,
    "LiquiditySweepReversal": SignalHorizon.SHORT_TERM,
    "SuperTrendBos": SignalHorizon.SWING,
}


def map_mode_to_horizon(mode: str) -> SignalHorizon:
    """Map a V7 mode/strategy name to a §53 horizon. Unknown → SWING (fail-safe)."""
    if not isinstance(mode, str):
        return SignalHorizon.SWING
    key = mode.strip()
    if key.upper() in _MODE_TO_HORIZON:
        return _MODE_TO_HORIZON[key.upper()]
    if key in _MODE_TO_HORIZON:
        return _MODE_TO_HORIZON[key]
    return SignalHorizon.SWING


def align_horizon(horizon: SignalHorizon, feature_timeframe: str) -> bool:
    """§53 alignment guard: a feature timeframe must be in the horizon's set."""
    if not isinstance(horizon, SignalHorizon) or not isinstance(feature_timeframe, str):
        return False
    return feature_timeframe.strip().lower() in HORIZON_DEFINITIONS[horizon].feature_timeframes
```

- [ ] **Step 4: Run test to verify it passes** — `pytest tests/unit/test_signal_engine.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat(phase6a): §53 Signal horizons enum + alignment (§39/§52)`

---

### Task 2: `core/signal_engine/catalog.py` — §52 catalog

**Files:** Create `core/signal_engine/catalog.py`; Test `tests/unit/test_signal_engine.py` (append).

**Interfaces:** Produces `IndicatorStatus`, `IndicatorSpec`, `SIGNAL_CATALOG`, `get_catalog_index`, `preserved_indicators`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_signal_engine.py
from core.signal_engine.catalog import (
    IndicatorStatus, SIGNAL_CATALOG, get_catalog_index, preserved_indicators,
)

SECTION_52_INDICATORS = [
    "trend", "momentum", "rsi", "macd", "moving_averages", "relative_strength",
    "volume", "rvol", "atr", "volatility", "market_regime", "sector",
    "earnings", "news", "sentiment", "ml",
]


def test_catalog_lists_section_52_exactly():
    assert sorted(s.name for s in SIGNAL_CATALOG) == sorted(SECTION_52_INDICATORS)
    assert len(SIGNAL_CATALOG) == 16


def test_preserved_have_source_modules():
    for spec in preserved_indicators():
        assert spec.source_module
        assert spec.status is IndicatorStatus.PRESERVED


def test_news_sentiment_deferred_ml_evaluate():
    idx = get_catalog_index()
    assert idx["news"].status is IndicatorStatus.DEFERRED
    assert idx["sentiment"].status is IndicatorStatus.DEFERRED
    assert idx["ml"].status is IndicatorStatus.EVALUATE
```

- [ ] **Step 2: Run test to verify it fails** — FAIL.
- [ ] **Step 3: Write minimal implementation**

```python
# core/signal_engine/catalog.py
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
```

- [ ] **Step 4: Run test to verify it passes** — PASS.
- [ ] **Step 5: Commit** — `feat(phase6a): §52 Signal catalog (preserve V7, add none)`

---

### Task 3: `core/signal_engine/regime.py` — market-wide regime

**Files:** Create `core/signal_engine/regime.py`; Test `tests/unit/test_signal_engine.py` (append).

**Interfaces:** Produces `MarketRegimeEngine`, `MarketRegimeResult`, `detect_macro_regime`, and the `MARKET_*` labels.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_signal_engine.py
from core.signal_engine.regime import (
    MarketRegimeEngine, detect_macro_regime, MARKET_RISK_ON, MARKET_RISK_OFF, MARKET_MIXED,
)


def test_market_regime_risk_on():
    engine = MarketRegimeEngine(universe_min=5)
    regimes = [{"regime": "TREND"}] * 7 + [{"regime": "RANGE"}] * 3
    res = engine.detect_market(regimes)
    assert res.label == MARKET_RISK_ON
    assert res.is_not_probability is True
    assert res.sample_size == 10


def test_market_regime_insufficient_is_stale():
    engine = MarketRegimeEngine(universe_min=10)
    res = engine.detect_market([{"regime": "TREND"}])
    assert res.is_stale is True
    assert res.label == MARKET_MIXED


def test_macro_regime_is_honest_stub():
    m = detect_macro_regime()
    assert m["available"] is False
    assert "deferred" in m["reason"]
```

- [ ] **Step 2: Run test to verify it fails** — FAIL.
- [ ] **Step 3: Write minimal implementation**

```python
# core/signal_engine/regime.py
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
```

- [ ] **Step 4: Run test to verify it passes** — PASS.
- [ ] **Step 5: Commit** — `feat(phase6a): market-wide regime engine + macro honest stub (§49)`

---

### Task 4: `core/signal_engine/types.py` + `__init__.py` — Signal overlay

**Files:** Create `core/signal_engine/types.py`, `core/signal_engine/__init__.py`; Test `tests/unit/test_signal_engine.py` (append).

**Interfaces:** Produces `Signal` with `from_dict`/`to_dict`. Re-exports all of `signal_engine`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_signal_engine.py
from core.signal_engine import Signal, SignalHorizon


def test_signal_from_dict_roundtrip():
    raw = {"symbol": "NVDA", "direction": "LONG", "signal_score": 72.0,
           "mode": "MOMENTUM", "signal_type": "v1_logistic", "source": "core.scoring_system"}
    sig = Signal.from_dict(raw)
    assert sig.ticker == "NVDA"
    assert sig.horizon is SignalHorizon.SWING
    assert sig.is_not_probability is True
    out = sig.to_dict()
    assert out["horizon"] == "SWING"
    assert out["signal_horizon"] == "SWING"
    assert out["symbol"] == "NVDA"  # raw preserved
```

- [ ] **Step 2: Run test to verify it fails** — FAIL.
- [ ] **Step 3: Write minimal implementation**

```python
# core/signal_engine/types.py
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
```

```python
# core/signal_engine/__init__.py
"""core.signal_engine — §52/§53 Signal Engine surface + market-wide regime."""
from .horizons import (SignalHorizon, HorizonDefinition, HORIZON_DEFINITIONS,
                        map_mode_to_horizon, align_horizon)
from .catalog import (IndicatorStatus, IndicatorSpec, SIGNAL_CATALOG,
                      get_catalog_index, preserved_indicators)
from .regime import (MarketRegimeEngine, MarketRegimeResult, detect_macro_regime,
                     MARKET_RISK_ON, MARKET_RISK_OFF, MARKET_MIXED, MARKET_TRANSITIONING)
from .types import Signal

__all__ = [
    "SignalHorizon", "HorizonDefinition", "HORIZON_DEFINITIONS",
    "map_mode_to_horizon", "align_horizon",
    "IndicatorStatus", "IndicatorSpec", "SIGNAL_CATALOG",
    "get_catalog_index", "preserved_indicators",
    "MarketRegimeEngine", "MarketRegimeResult", "detect_macro_regime",
    "MARKET_RISK_ON", "MARKET_RISK_OFF", "MARKET_MIXED", "MARKET_TRANSITIONING",
    "Signal",
]
```

- [ ] **Step 4: Run test to verify it passes** — PASS.
- [ ] **Step 5: Commit** — `feat(phase6a): Signal lifecycle overlay + signal_engine package (§52)`

---

### Task 5: GOLD-052 + GOLD-053 golden (5A gate)

**Files:** Create `tests/golden/test_phase6a_golden.py`.

- [ ] **Step 1: Write the golden tests**

```python
# tests/golden/test_phase6a_golden.py
"""GOLD-052 — §52 catalog names the existing V7 indicators; no indicator is
added (catalog length == 16). GOLD-053 — §53 horizons + target/feature
alignment, deterministic map_mode_to_horizon + align_horizon."""
import pytest
from core.signal_engine import (SignalHorizon, HORIZON_DEFINITIONS, map_mode_to_horizon,
                                 align_horizon, SIGNAL_CATALOG, IndicatorStatus,
                                 get_catalog_index, preserved_indicators)

pytestmark = pytest.mark.golden

SECTION_52_INDICATORS = [
    "trend", "momentum", "rsi", "macd", "moving_averages", "relative_strength",
    "volume", "rvol", "atr", "volatility", "market_regime", "sector",
    "earnings", "news", "sentiment", "ml",
]


def test_gold052_catalog_lists_exactly_section_52():
    assert sorted(s.name for s in SIGNAL_CATALOG) == sorted(SECTION_52_INDICATORS)
    assert len(SIGNAL_CATALOG) == 16  # do not add indicators merely because they exist


def test_gold052_preserved_have_real_sources_deferred_are_honest():
    for spec in preserved_indicators():
        assert spec.source_module and spec.status is IndicatorStatus.PRESERVED
    idx = get_catalog_index()
    assert idx["news"].status is IndicatorStatus.DEFERRED
    assert idx["sentiment"].status is IndicatorStatus.DEFERRED
    assert idx["ml"].status is IndicatorStatus.EVALUATE


def test_gold053_three_horizons_each_with_target_and_features():
    assert {h.value for h in SignalHorizon} == {"SHORT_TERM", "SWING", "MEDIUM_TERM"}
    for h in SignalHorizon:
        d = HORIZON_DEFINITIONS[h]
        assert d.sessions_min < d.sessions_max
        assert "P(" in d.target_condition()
        assert len(d.feature_timeframes) >= 2


def test_gold053_map_and_align_deterministic():
    assert map_mode_to_horizon("MOMENTUM") is SignalHorizon.SWING
    assert map_mode_to_horizon("PULLBACK") is SignalHorizon.SHORT_TERM
    assert map_mode_to_horizon("unknown") is SignalHorizon.SWING
    assert align_horizon(SignalHorizon.SHORT_TERM, "1m") is True
    assert align_horizon(SignalHorizon.SHORT_TERM, "1d") is False
    assert align_horizon(SignalHorizon.MEDIUM_TERM, "1d") is True
```

- [ ] **Step 2: Run** — `pytest tests/golden/test_phase6a_golden.py -v` → PASS.
- [ ] **Step 3: Commit** — `test(phase6a): GOLD-052/053 golden — catalog + horizons`

---

### Task 6: `core/ml/registry.py` — §54 model registry + OosGate

**Files:** Create `core/ml/registry.py`; Test `tests/unit/test_ml_registry_baseline.py` (create).

**Interfaces:** Produces `TargetSpec`, `ModelRecord`, `ModelRegistry`, `OosGate`, `OosVerdict`, `OosVerdictLabel`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_ml_registry_baseline.py
import pytest
from core.signal_engine import SignalHorizon
from core.ml.registry import (ModelRecord, ModelRegistry, TargetSpec, OosGate,
                              OosVerdict, OosVerdictLabel)


def _mk(is_simple=True, verdict=None):
    return ModelRecord(
        model="m", version="v1", features=("rsi",), feature_version="v1",
        target=TargetSpec(horizon=SignalHorizon.SWING, condition="c"),
        horizon=SignalHorizon.SWING, training_range=("a", "b"),
        validation_range=("c", "d"), test_range=("e", "f"), trained_at="t",
        is_simple_baseline=is_simple,
        oos_verdict=verdict or OosVerdictLabel.PENDING.value,
    )


def test_model_record_has_ten_section_54_fields():
    rec = _mk()
    for f in ("model", "version", "features", "feature_version", "target",
              "horizon", "training_range", "validation_range", "test_range", "trained_at"):
        assert hasattr(rec, f)


def test_complex_model_cannot_register_with_promoting_verdict():
    reg = ModelRegistry()
    with pytest.raises(ValueError):
        reg.register(_mk(is_simple=False, verdict=OosVerdictLabel.ROBUST.value))


def test_promote_with_real_verdict_works():
    reg = ModelRegistry()
    pending = _mk(is_simple=False)
    reg.register(pending)
    promoted = reg.promote(pending, OosVerdict(available=True, verdict=OosVerdictLabel.ROBUST.value))
    assert promoted.oos_verdict == OosVerdictLabel.ROBUST.value


def test_oos_gate_deferred_without_runner():
    gate = OosGate()
    v = gate.evaluate_oos_improvement(_mk(), _mk(is_simple=False))
    assert v.available is False
    assert v.verdict == OosVerdictLabel.DEFERRED_PHASE7.value
```

- [ ] **Step 2: Run test to verify it fails** — FAIL.
- [ ] **Step 3: Write minimal implementation**

```python
# core/ml/registry.py
"""§54 ML Engine — model registry tracking the 10 §54 fields + the OOS-
improvement gate (interface seam; real OOS backtest is Phase 7 §57)."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Any, List, Mapping, Optional, Tuple

from core.signal_engine import SignalHorizon


@dataclass(frozen=True)
class TargetSpec:
    horizon: SignalHorizon
    condition: str  # precise §55 definition

    def to_dict(self) -> dict:
        return {"horizon": self.horizon.value, "condition": self.condition}


class OosVerdictLabel(str, Enum):
    PENDING = "PENDING"
    DEFERRED_PHASE7 = "DEFERRED_PHASE7"
    ROBUST = "ROBUST"
    MODERATE = "MODERATE"
    FRAGILE = "FRAGILE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


_PHASE6_SETTABLE = frozenset({OosVerdictLabel.PENDING.value, OosVerdictLabel.DEFERRED_PHASE7.value})
_PROMOTING = frozenset({OosVerdictLabel.ROBUST.value, OosVerdictLabel.MODERATE.value})
_ALL_VERDICTS = _PHASE6_SETTABLE | _PROMOTING | frozenset(
    {OosVerdictLabel.FRAGILE.value, OosVerdictLabel.INSUFFICIENT_EVIDENCE.value}
)


@dataclass(frozen=True)
class ModelRecord:
    model: str
    version: str
    features: Tuple[str, ...]
    feature_version: str
    target: TargetSpec
    horizon: SignalHorizon
    training_range: Tuple[str, str]
    validation_range: Tuple[str, str]
    test_range: Tuple[str, str]
    trained_at: str
    is_simple_baseline: bool = False
    oos_verdict: str = OosVerdictLabel.PENDING.value

    def __post_init__(self):
        if self.oos_verdict not in _ALL_VERDICTS:
            raise ValueError(f"unknown oos_verdict: {self.oos_verdict!r}")

    def to_dict(self) -> dict:
        return {
            "model": self.model, "version": self.version,
            "features": list(self.features), "feature_version": self.feature_version,
            "target": self.target.to_dict(), "horizon": self.horizon.value,
            "training_range": list(self.training_range),
            "validation_range": list(self.validation_range),
            "test_range": list(self.test_range), "trained_at": self.trained_at,
            "is_simple_baseline": self.is_simple_baseline, "oos_verdict": self.oos_verdict,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ModelRecord":
        d = dict(d or {})
        tgt = d.get("target") or {}
        return cls(
            model=str(d.get("model", "")), version=str(d.get("version", "")),
            features=tuple(d.get("features") or ()), feature_version=str(d.get("feature_version", "")),
            target=TargetSpec(horizon=SignalHorizon(tgt.get("horizon")),
                              condition=str(tgt.get("condition", ""))),
            horizon=SignalHorizon(d.get("horizon")),
            training_range=tuple(d.get("training_range") or ("", "")),
            validation_range=tuple(d.get("validation_range") or ("", "")),
            test_range=tuple(d.get("test_range") or ("", "")),
            trained_at=str(d.get("trained_at", "")),
            is_simple_baseline=bool(d.get("is_simple_baseline", False)),
            oos_verdict=str(d.get("oos_verdict", OosVerdictLabel.PENDING.value)),
        )


@dataclass(frozen=True)
class OosVerdict:
    available: bool
    verdict: str
    reason: str = ""


class OosGate:
    """§54 'complex models must prove OOS improvement'. Real OOS backtest is
    Phase 7 (§57). With no runner, the gate is an honest DEFERRED stub."""

    def evaluate_oos_improvement(self, baseline: ModelRecord, challenger: ModelRecord,
                                 backtest_runner=None) -> OosVerdict:
        if backtest_runner is None:
            return OosVerdict(available=False,
                              verdict=OosVerdictLabel.DEFERRED_PHASE7.value,
                              reason="phase7_backtest_engine")
        return OosVerdict(available=True,
                          verdict=str(backtest_runner(baseline, challenger)),
                          reason="phase7_runner")


class ModelRegistry:
    def __init__(self):
        self._records: List[ModelRecord] = []

    def register(self, record: ModelRecord) -> ModelRecord:
        if not isinstance(record, ModelRecord):
            raise TypeError("register expects a ModelRecord")
        # §54 gate: a complex model may not be REGISTERED with a promoting verdict;
        # promotion requires promote() with a real available OosVerdict.
        if not record.is_simple_baseline and record.oos_verdict in _PROMOTING:
            raise ValueError(
                "complex model cannot be registered with a promoting oos_verdict; use promote() with an OosVerdict"
            )
        self._records.append(record)
        return record

    def get(self, model: str, version: str) -> Optional[ModelRecord]:
        for r in self._records:
            if r.model == model and r.version == version:
                return r
        return None

    def list_by_horizon(self, horizon: SignalHorizon) -> List[ModelRecord]:
        return [r for r in self._records if r.horizon is horizon]

    def list_all(self) -> List[ModelRecord]:
        return list(self._records)

    def promote(self, record: ModelRecord, verdict: OosVerdict) -> ModelRecord:
        if not verdict.available:
            raise ValueError("cannot promote with an unavailable OosVerdict")
        if verdict.verdict not in _PROMOTING:
            raise ValueError(f"{verdict.verdict!r} is not a promoting verdict")
        promoted = ModelRecord(
            model=record.model, version=record.version, features=record.features,
            feature_version=record.feature_version, target=record.target, horizon=record.horizon,
            training_range=record.training_range, validation_range=record.validation_range,
            test_range=record.test_range, trained_at=record.trained_at,
            is_simple_baseline=record.is_simple_baseline, oos_verdict=verdict.verdict,
        )
        for i, r in enumerate(self._records):
            if r is record:
                self._records[i] = promoted
                break
        return promoted

    def to_list(self) -> List[dict]:
        return [r.to_dict() for r in self._records]

    def from_list(self, records: List[Mapping[str, Any]]) -> None:
        self._records = [ModelRecord.from_dict(r) for r in records]
```

- [ ] **Step 4: Run test to verify it passes** — PASS.
- [ ] **Step 5: Commit** — `feat(phase6b): §54 model registry + OosGate seam (§39/§54)`

---

### Task 7: `core/ml/baseline.py` — §54 simple baseline + evolve `run_weight_calibration_audit`

**Files:** Create `core/ml/baseline.py`; Modify `core/scoring_system.py` (`run_weight_calibration_audit`); Test `tests/unit/test_ml_registry_baseline.py` (append).

**Interfaces:** Produces `SimpleBaselineModel`, `TrainedBaseline`, `train_baseline`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_ml_registry_baseline.py
from core.ml.baseline import SimpleBaselineModel, train_baseline


def test_simple_baseline_trains_and_registers_pending():
    feats = [{"rsi": 50.0}, {"rsi": 30.0}, {"rsi": 70.0}, {"rsi": 60.0}]
    outs = [0.0, 1.0, 0.0, 1.0]
    trained, rec = train_baseline(
        feats, outs, feature_names=("rsi",), horizon=SignalHorizon.SWING,
        target_condition="P(return>0 over next 5-20 sessions)",
        training_range=("2023-01-01", "2023-06-01"),
        validation_range=("2023-06-01", "2023-09-01"),
        test_range=("2023-09-01", "2023-12-01"), trained_at="2026-08-12T00:00:00Z",
    )
    assert rec.is_simple_baseline is True
    assert rec.oos_verdict == OosVerdictLabel.PENDING.value
    assert len(trained.weights) == 2  # bias + 1 feature
    # predictable: lower rsi (30) should map toward outcome 1 here
    model = SimpleBaselineModel()
    p_low = model.predict_proba(trained, [30.0])
    p_high = model.predict_proba(trained, [70.0])
    assert p_low > p_high  # rsi=30 had outcome 1, rsi=70 had outcome 0


def test_run_weight_calibration_audit_delegates_to_ml():
    from core.scoring_system import run_weight_calibration_audit
    result = run_weight_calibration_audit()
    assert isinstance(result, dict)
    assert result.get("available") in (True, False)
    assert "summary" in result
```

- [ ] **Step 2: Run test to verify it fails** — FAIL.
- [ ] **Step 3: Write minimal implementation**

```python
# core/ml/baseline.py
"""§54 ML Engine — simple baseline first. A stdlib logistic baseline trained
on the existing context_builder feature surface + SignalManager closed-signal
outcomes. No sklearn, no qlib (§118). Records itself in the registry."""
from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Sequence, Tuple

from core.signal_engine import SignalHorizon
from .registry import ModelRecord, TargetSpec, OosVerdictLabel


@dataclass(frozen=True)
class TrainedBaseline:
    weights: Tuple[float, ...]  # index 0 = bias, then one per feature
    feature_names: Tuple[str, ...]


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    return math.exp(z) / (1.0 + math.exp(z))


class SimpleBaselineModel:
    """§54 'simple baseline models come first'. Stdlib logistic regression via
    gradient descent. Features are the existing context_builder keys."""
    def __init__(self, *, learning_rate: float = 0.1, epochs: int = 300):
        self.learning_rate = learning_rate
        self.epochs = epochs

    def fit(self, X: Sequence[Sequence[float]], y: Sequence[float]) -> TrainedBaseline:
        if len(X) == 0 or len(y) == 0:
            raise ValueError("fit requires non-empty X and y")
        if len(X) != len(y):
            raise ValueError("X and y length mismatch")
        n_features = len(X[0])
        w = [0.0] * (n_features + 1)  # index 0 = bias
        for _ in range(self.epochs):
            grads = [0.0] * len(w)
            for xi, yi in zip(X, y):
                z = w[0] + sum(w[j + 1] * xi[j] for j in range(n_features))
                p = _sigmoid(z)
                err = p - float(yi)
                grads[0] += err
                for j in range(n_features):
                    grads[j + 1] += err * xi[j]
            m = len(X)
            for k in range(len(w)):
                w[k] -= self.learning_rate * grads[k] / m
        return TrainedBaseline(weights=tuple(w), feature_names=tuple())

    def predict_proba(self, baseline: TrainedBaseline, x: Sequence[float]) -> float:
        n = min(len(x), len(baseline.weights) - 1)
        z = baseline.weights[0] + sum(baseline.weights[j + 1] * x[j] for j in range(n))
        return _sigmoid(z)


def train_baseline(features: Sequence, outcomes: Sequence[float], *,
                   feature_names: Sequence[str], horizon: SignalHorizon,
                   target_condition: str, training_range: Tuple[str, str],
                   validation_range: Tuple[str, str], test_range: Tuple[str, str],
                   trained_at: str, model_name: str = "simple_logistic",
                   version: str = "v1", feature_version: str = "v1") -> Tuple[TrainedBaseline, ModelRecord]:
    """Train a simple baseline and produce a ModelRecord (caller registers it)."""
    if len(features) != len(outcomes):
        raise ValueError("features/outcomes length mismatch")
    X = [[float(f.get(name, 0.0)) for name in feature_names] for f in features]
    model = SimpleBaselineModel()
    trained = model.fit(X, list(outcomes))
    record = ModelRecord(
        model=model_name, version=version, features=tuple(feature_names),
        feature_version=feature_version,
        target=TargetSpec(horizon=horizon, condition=target_condition),
        horizon=horizon, training_range=tuple(training_range),
        validation_range=tuple(validation_range), test_range=tuple(test_range),
        trained_at=trained_at, is_simple_baseline=True,
        oos_verdict=OosVerdictLabel.PENDING.value,
    )
    return trained, record
```

Then evolve the `run_weight_calibration_audit` stub in `core/scoring_system.py` (read the existing function first, then replace its body to delegate):

```python
# In core/scoring_system.py — replace the run_weight_calibration_audit body:
def run_weight_calibration_audit():
    """§55/§54 calibration audit — evolved (was a 'Module 5/6' stub).
    Delegates to core.ml. Returns a structured summary; does NOT modify the
    hand-coded logistic_coeffs path (§5/§7 — additive)."""
    try:
        from core.ml.calibration import CalibrationResult, ProbabilityDefinition
        from core.signal_engine import SignalHorizon
        definition = ProbabilityDefinition(
            horizon=SignalHorizon.SWING,
            condition="uncalibrated logistic score (V1 probability_score)",
        )
        res = CalibrationResult.uncalibrated(definition, sample_size=0)
        return {
            "available": False,
            "summary": "V1 logistic coefficients are hand-coded and uncalibrated; "
                       "calibration lands with Phase 6B baseline + outcomes.",
            "is_statistical_probability": res.is_statistical_probability,
            "method": res.method,
        }
    except Exception as exc:  # never break the live engine
        return {"available": False, "summary": f"calibration audit deferred: {exc}"}
```

(The exact existing signature/return is preserved as a dict; the body is evolved from the stub comment to a real delegation. Read the current function before editing to keep its name/signature.)

- [ ] **Step 4: Run test to verify it passes** — PASS.
- [ ] **Step 5: Commit** — `feat(phase6b): §54 simple logistic baseline + evolve calibration-audit stub`

---

### Task 8: `core/ml/calibration.py` + `meta_label.py` + `qlib_adapter.py` + `__init__.py`

**Files:** Create `core/ml/calibration.py`, `core/ml/meta_label.py`, `core/ml/qlib_adapter.py`, `core/ml/__init__.py`; Test `tests/unit/test_ml_calibration_meta.py` (create).

**Interfaces:** Produces `CalibrationResult`, `CalibrationError`, `ProbabilityDefinition`, `calibrate`, `apply_calibration`, `MetaLabel`, `MetaLabeler`, `MetaLabelEvaluation`, `evaluate_meta_labeling`, `qlib_available`, `qlib_adapt`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_ml_calibration_meta.py
import pytest
from core.signal_engine import SignalHorizon
from core.ml.calibration import (CalibrationResult, CalibrationError, ProbabilityDefinition,
                                  calibrate, apply_calibration)
from core.ml.meta_label import MetaLabeler, evaluate_meta_labeling
from core.ml import qlib_available, qlib_adapt


def test_calibrate_isotonic_statistical():
    d = ProbabilityDefinition(horizon=SignalHorizon.SWING,
                              condition="P(return > 0 over next 5-20 sessions)")
    res = calibrate([0.1, 0.4, 0.6, 0.9], [0, 0, 1, 1], definition=d, method="isotonic")
    assert res.is_statistical_probability is True
    assert res.sample_size == 4
    assert res.mapping  # PAV blocks


def test_llm_confidence_raises():
    with pytest.raises(CalibrationError):
        CalibrationResult.from_llm_confidence(confidence=0.78)


def test_uncalibrated_not_statistical():
    d = ProbabilityDefinition(horizon=SignalHorizon.SHORT_TERM, condition="uncalibrated")
    res = CalibrationResult.uncalibrated(d, sample_size=100)
    assert res.is_statistical_probability is False
    assert apply_calibration(0.5, res) == 0.5  # pass-through


def test_meta_label_section_56_example():
    assert MetaLabeler.combine("LONG", "DO_NOT_EXECUTE") == "NO_TRADE"
    assert MetaLabeler.combine("LONG", "EXECUTE") == "LONG"
    assert MetaLabeler.combine("LONG", "INSUFFICIENT") == "NO_TRADE"


def test_evaluate_meta_labeling_improves_precision():
    primary = ["LONG", "LONG", "LONG", "LONG"]
    meta = ["EXECUTE", "EXECUTE", "DO_NOT_EXECUTE", "EXECUTE"]
    outcomes = ["LONG", "LONG", "NO_TRADE", "NO_TRADE"]
    ev = evaluate_meta_labeling(primary, meta, outcomes, positive_label="LONG")
    assert ev.sample_size == 4
    assert ev.meta_labeling_improves_precision is True


def test_qlib_adapter_honest_when_unavailable():
    res = qlib_adapt()
    assert res["available"] in (True, False)
    if not qlib_available():
        assert res["available"] is False
        assert "qlib" in res["reason"]
```

- [ ] **Step 2: Run test to verify it fails** — FAIL.
- [ ] **Step 3: Write minimal implementation**

```python
# core/ml/calibration.py
"""§55 Calibration — define precisely what probability means; do NOT display
LLM confidence as statistical probability. stdlib isotonic (PAV) + Platt — no
sklearn (§118)."""
from __future__ import annotations
import math
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from core.signal_engine import SignalHorizon


class CalibrationError(Exception):
    pass


@dataclass(frozen=True)
class ProbabilityDefinition:
    horizon: SignalHorizon
    condition: str
    calibration_method: str = "none"


@dataclass(frozen=True)
class CalibrationResult:
    method: str
    raw_range: Tuple[float, float]
    calibrated_range: Tuple[float, float]
    sample_size: int
    is_statistical_probability: bool
    definition: ProbabilityDefinition
    mapping: Tuple[Tuple[float, float], ...] = ()

    @classmethod
    def from_llm_confidence(cls, *args: object, **kwargs: object) -> "CalibrationResult":
        """§55 — LLM confidence is NEVER a statistical probability."""
        raise CalibrationError("LLM confidence is not a statistical probability (§55)")

    @classmethod
    def uncalibrated(cls, definition: ProbabilityDefinition, *, sample_size: int = 0,
                     raw_range: Tuple[float, float] = (0.0, 1.0)) -> "CalibrationResult":
        return cls(method="none", raw_range=raw_range, calibrated_range=raw_range,
                   sample_size=sample_size, is_statistical_probability=False,
                   definition=definition, mapping=())


def _isotonic_pav(scores: Sequence[float], outcomes: Sequence[float]) -> List[Tuple[float, float]]:
    """Pool-Adjacent-Violators isotonic regression — stdlib only."""
    if len(scores) != len(outcomes):
        raise ValueError("scores/outcomes length mismatch")
    pts = sorted(zip([float(s) for s in scores], [float(o) for o in outcomes]), key=lambda t: t[0])
    blocks = [[p[0], p[1], 1] for p in pts]  # [value, mean, weight]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][1] > blocks[i + 1][1]:
            w1, w2 = blocks[i][2], blocks[i + 1][2]
            pooled = (blocks[i][1] * w1 + blocks[i + 1][1] * w2) / (w1 + w2)
            blocks[i][1] = pooled
            blocks[i][2] = w1 + w2
            del blocks[i + 1]
            if i > 0:
                i -= 1
        else:
            i += 1
    return [(b[0], b[1]) for b in blocks]


def calibrate(raw_scores: Sequence[float], outcomes: Sequence[float], *,
              definition: ProbabilityDefinition, method: str = "isotonic") -> CalibrationResult:
    if method not in ("isotonic", "platt", "none"):
        raise ValueError(f"unknown calibration method: {method!r}")
    n = len(raw_scores)
    if method == "none" or n == 0:
        r = (float(min(raw_scores)) if n else 0.0, float(max(raw_scores)) if n else 1.0)
        return CalibrationResult.uncalibrated(definition, sample_size=n, raw_range=r)
    if method == "isotonic":
        mapping = tuple(_isotonic_pav(raw_scores, outcomes))
    else:
        mapping = ()
    return CalibrationResult(
        method=method,
        raw_range=(float(min(raw_scores)), float(max(raw_scores))),
        calibrated_range=(0.0, 1.0),
        sample_size=n,
        is_statistical_probability=True,
        definition=ProbabilityDefinition(horizon=definition.horizon,
                                        condition=definition.condition,
                                        calibration_method=method),
        mapping=mapping,
    )


def apply_calibration(raw_score: float, calibration: CalibrationResult) -> float:
    if not calibration.is_statistical_probability or not calibration.mapping:
        return float(raw_score)
    s = float(raw_score)
    best = calibration.mapping[0][1]
    for raw_k, cal_v in calibration.mapping:
        if raw_k <= s:
            best = cal_v
        else:
            break
    return float(best)
```

```python
# core/ml/meta_label.py
"""§56 Meta-labeling — evaluate. Primary model + meta filter → final label.
Spec example (§56): primary LONG, meta DO_NOT_EXECUTE → final NO_TRADE is valid."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Sequence, Tuple


_PRIMARY_LONG = "LONG"
_PRIMARY_SHORT = "SHORT"
_META_EXECUTE = "EXECUTE"
_META_DO_NOT_EXECUTE = "DO_NOT_EXECUTE"
_META_INSUFFICIENT = "INSUFFICIENT"
_FINAL_NO_TRADE = "NO_TRADE"


@dataclass(frozen=True)
class MetaLabel:
    primary: str
    meta: str
    final: str


class MetaLabeler:
    @staticmethod
    def combine(primary: str, meta: str) -> str:
        p = str(primary or "").upper()
        m = str(meta or "").upper()
        if m in (_META_DO_NOT_EXECUTE, _META_INSUFFICIENT):
            return _FINAL_NO_TRADE
        if m == _META_EXECUTE:
            return p if p in (_PRIMARY_LONG, _PRIMARY_SHORT) else _FINAL_NO_TRADE
        return _FINAL_NO_TRADE  # unknown meta → no-trade (fail-safe)


@dataclass(frozen=True)
class MetaLabelEvaluation:
    primary_precision: float
    primary_recall: float
    meta_precision: float
    meta_recall: float
    sample_size: int
    meta_labeling_improves_precision: bool


def evaluate_meta_labeling(primary_predictions: Sequence[str], meta_predictions: Sequence[str],
                           outcomes: Sequence[str], *, positive_label: str = "LONG") -> MetaLabelEvaluation:
    """§56 'evaluate meta-labeling' — does the meta filter improve precision
    vs primary-only? outcomes are realized labels (LONG/SHORT/NO_TRADE)."""
    if not (len(primary_predictions) == len(meta_predictions) == len(outcomes)):
        raise ValueError("length mismatch")
    n = len(outcomes)

    def metrics(preds: Sequence[str]) -> Tuple[float, float]:
        tp = fp = fn = 0
        for p, o in zip(preds, outcomes):
            o_pos = str(o).upper() == positive_label
            p_pos = str(p).upper() == positive_label
            if p_pos and o_pos:
                tp += 1
            elif p_pos and not o_pos:
                fp += 1
            elif (not p_pos) and o_pos:
                fn += 1
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        return prec, rec

    pp, pr = metrics(primary_predictions)
    combined = [MetaLabeler.combine(p, m)
                for p, m in zip(primary_predictions, meta_predictions)]
    mp, mr = metrics(combined)
    return MetaLabelEvaluation(
        primary_precision=round(pp, 4), primary_recall=round(pr, 4),
        meta_precision=round(mp, 4), meta_recall=round(mr, 4), sample_size=n,
        meta_labeling_improves_precision=mp > pp,
    )
```

```python
# core/ml/qlib_adapter.py
"""§54 'use Qlib where appropriate' — import-guarded ADAPTER (Phase 2 manifest:
Qlib is ADAPTER, V9 data model authoritative). Optional; zero new deps (§118).
Owner installs qlib to activate; until then this is an honest stub (§4)."""
from __future__ import annotations
from typing import Any, Dict


def is_available() -> bool:
    try:
        import qlib  # noqa: F401
    except Exception:
        return False
    return True


def adapt(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Adapt Qlib to the V9 data model. If qlib is not installed, return an
    honest unavailable result (§4) — never fake."""
    if not is_available():
        return {"available": False, "reason": "qlib_not_installed_owner_opt_in"}
    return {"available": True, "reason": "qlib_adapter_pending_owner_activation",
            "note": "V9 data model authoritative; Qlib used as ADAPTER only"}
```

```python
# core/ml/__init__.py
"""core.ml — §54 ML Engine + §55 Calibration + §56 Meta-labeling."""
from .registry import (ModelRecord, ModelRegistry, TargetSpec, OosGate, OosVerdict,
                       OosVerdictLabel)
from .baseline import SimpleBaselineModel, TrainedBaseline, train_baseline
from .calibration import (CalibrationResult, CalibrationError, ProbabilityDefinition,
                          calibrate, apply_calibration)
from .meta_label import (MetaLabel, MetaLabeler, MetaLabelEvaluation,
                         evaluate_meta_labeling)
from .qlib_adapter import is_available as qlib_available, adapt as qlib_adapt

__all__ = [
    "ModelRecord", "ModelRegistry", "TargetSpec", "OosGate", "OosVerdict", "OosVerdictLabel",
    "SimpleBaselineModel", "TrainedBaseline", "train_baseline",
    "CalibrationResult", "CalibrationError", "ProbabilityDefinition", "calibrate", "apply_calibration",
    "MetaLabel", "MetaLabeler", "MetaLabelEvaluation", "evaluate_meta_labeling",
    "qlib_available", "qlib_adapt",
]
```

- [ ] **Step 4: Run test to verify it passes** — PASS.
- [ ] **Step 5: Commit** — `feat(phase6b): §55 calibration + §56 meta-label + qlib adapter seam`

---

### Task 9: GOLD-054 + GOLD-055 + GOLD-056 golden (5B gate)

**Files:** Create `tests/golden/test_phase6b_golden.py`.

- [ ] **Step 1: Write the golden tests** — use the verbatim GOLD-054/055/056 bodies from the design (10 §54 fields; baseline trains → PENDING; OosGate DEFERRED without runner; complex model can't register with promoting verdict but promote() works; calibrate → statistical + definition; from_llm_confidence raises; uncalibrated not statistical; meta-label §56 example + evaluate improves precision).

```python
# tests/golden/test_phase6b_golden.py
"""GOLD-054 §54 ML registry + baseline + OosGate seam; GOLD-055 §55 calibration
+ not-LLM-confidence; GOLD-056 §56 meta-labeling."""
import pytest
from core.signal_engine import SignalHorizon
from core.ml import (ModelRecord, ModelRegistry, TargetSpec, OosGate, OosVerdict,
                     OosVerdictLabel, train_baseline, CalibrationResult, CalibrationError,
                     ProbabilityDefinition, calibrate, MetaLabeler, evaluate_meta_labeling)

pytestmark = pytest.mark.golden


def test_gold054_model_record_has_ten_section_54_fields():
    rec = ModelRecord(model="m", version="v1", features=("rsi",), feature_version="v1",
                     target=TargetSpec(horizon=SignalHorizon.SWING, condition="c"),
                     horizon=SignalHorizon.SWING, training_range=("a", "b"),
                     validation_range=("c", "d"), test_range=("e", "f"), trained_at="t",
                     is_simple_baseline=True)
    for f in ("model", "version", "features", "feature_version", "target", "horizon",
              "training_range", "validation_range", "test_range", "trained_at"):
        assert hasattr(rec, f)


def test_gold054_simple_baseline_trains_and_registers_pending():
    feats = [{"rsi": 50.0}, {"rsi": 30.0}, {"rsi": 70.0}, {"rsi": 60.0}]
    outs = [0.0, 1.0, 0.0, 1.0]
    _, rec = train_baseline(feats, outs, feature_names=("rsi",), horizon=SignalHorizon.SWING,
                            target_condition="P(return>0 over next 5-20 sessions)",
                            training_range=("a", "b"), validation_range=("c", "d"),
                            test_range=("e", "f"), trained_at="t")
    assert rec.is_simple_baseline is True
    assert rec.oos_verdict == OosVerdictLabel.PENDING.value


def test_gold054_oos_gate_deferred_without_phase7_runner():
    gate = OosGate()
    base = ModelRecord(model="b", version="1", features=(), feature_version="v1",
                     target=TargetSpec(horizon=SignalHorizon.SWING, condition="c"),
                     horizon=SignalHorizon.SWING, training_range=("", ""), validation_range=("", ""),
                     test_range=("", ""), trained_at="t", is_simple_baseline=True)
    chal = ModelRecord(model="c", version="1", features=(), feature_version="v1",
                     target=TargetSpec(horizon=SignalHorizon.SWING, condition="c"),
                     horizon=SignalHorizon.SWING, training_range=("", ""), validation_range=("", ""),
                     test_range=("", ""), trained_at="t", is_simple_baseline=False)
    v = gate.evaluate_oos_improvement(base, chal)
    assert v.available is False and v.verdict == OosVerdictLabel.DEFERRED_PHASE7.value


def test_gold054_complex_model_cannot_register_promoted_but_promote_works():
    reg = ModelRegistry()
    complex_rec = ModelRecord(model="c", version="1", features=(), feature_version="v1",
                              target=TargetSpec(horizon=SignalHorizon.SWING, condition="c"),
                              horizon=SignalHorizon.SWING, training_range=("", ""), validation_range=("", ""),
                              test_range=("", ""), trained_at="t", is_simple_baseline=False,
                              oos_verdict=OosVerdictLabel.ROBUST.value)
    with pytest.raises(ValueError):
        reg.register(complex_rec)
    pending = ModelRecord(model="c", version="1", features=(), feature_version="v1",
                         target=TargetSpec(horizon=SignalHorizon.SWING, condition="c"),
                         horizon=SignalHorizon.SWING, training_range=("", ""), validation_range=("", ""),
                         test_range=("", ""), trained_at="t", is_simple_baseline=False)
    reg.register(pending)
    promoted = reg.promote(pending, OosVerdict(available=True, verdict=OosVerdictLabel.ROBUST.value))
    assert promoted.oos_verdict == OosVerdictLabel.ROBUST.value


def test_gold055_calibrate_is_statistical_with_definition():
    d = ProbabilityDefinition(horizon=SignalHorizon.SWING,
                             condition="P(return > 0 over next 5-20 sessions)")
    res = calibrate([0.1, 0.4, 0.6, 0.9], [0, 0, 1, 1], definition=d, method="isotonic")
    assert res.is_statistical_probability is True
    assert res.sample_size == 4 and "P(return > 0" in res.definition.condition


def test_gold055_llm_confidence_not_statistical():
    with pytest.raises(CalibrationError):
        CalibrationResult.from_llm_confidence(confidence=0.78)


def test_gold055_uncalibrated_not_statistical():
    d = ProbabilityDefinition(horizon=SignalHorizon.SHORT_TERM, condition="uncalibrated logistic")
    res = CalibrationResult.uncalibrated(d, sample_size=100)
    assert res.is_statistical_probability is False and res.method == "none"


def test_gold056_section_56_example_and_evaluation():
    assert MetaLabeler.combine("LONG", "DO_NOT_EXECUTE") == "NO_TRADE"
    assert MetaLabeler.combine("LONG", "EXECUTE") == "LONG"
    assert MetaLabeler.combine("LONG", "INSUFFICIENT") == "NO_TRADE"
    ev = evaluate_meta_labeling(["LONG"] * 4, ["EXECUTE", "EXECUTE", "DO_NOT_EXECUTE", "EXECUTE"],
                                ["LONG", "LONG", "NO_TRADE", "NO_TRADE"], positive_label="LONG")
    assert ev.sample_size == 4 and ev.meta_labeling_improves_precision is True
```

- [ ] **Step 2: Run** — `pytest tests/golden/test_phase6b_golden.py -v` → PASS.
- [ ] **Step 3: Commit** — `test(phase6b): GOLD-054/055/056 golden — ML + calibration + meta-label`

---

### Task 10: 5C integration — `pre_earnings/decision.py` signal input + `market_data.py` regime + config

**Files:** Modify `core/pre_earnings/decision.py`, `core/ai/tools/market_data.py`, `core/config.py`, `config.example.json`; Test `tests/unit/test_pre_earnings_decision.py` (append) + `tests/unit/test_signal_engine.py` (append).

**Interfaces:** Consumes `core.signal_engine.Signal`, `MarketRegimeEngine`. Produces a real `signal` input in the §51 context; additive market-wide regime in the §39 tool; `DEFAULT_CONFIG["ml"]` + `["signal_engine"]`.

- [ ] **Step 1: Write the failing test** (read `core/pre_earnings/decision.py` first to find the `signal` stub and `_honest_stub`)

```python
# append to tests/unit/test_pre_earnings_decision.py — the §51 "signal" input is real
def test_pre_earnings_signal_input_is_real_when_item_has_signal():
    from core.pre_earnings.decision import build_pre_earnings_decision_context
    item = {"symbol": "NVDA", "direction": "LONG", "signal_score": 72.0,
            "mode": "MOMENTUM", "signal_type": "v1_logistic"}
    ctx = build_pre_earnings_decision_context(item, services={})
    sig = ctx["signal"]
    assert sig["available"] is True
    assert sig["horizon"] == "SWING"
    assert sig["is_not_probability"] is True


def test_pre_earnings_signal_input_honest_when_absent():
    from core.pre_earnings.decision import build_pre_earnings_decision_context
    ctx = build_pre_earnings_decision_context({"symbol": "NVDA"}, services={})
    assert ctx["signal"]["available"] is False  # no signal data on the item
```

```python
# append to tests/unit/test_signal_engine.py — config + market tool integration
def test_ml_and_signal_engine_config_present():
    from core.config import DEFAULT_CONFIG
    assert "ml" in DEFAULT_CONFIG and "registry" in DEFAULT_CONFIG["ml"]
    assert "signal_engine" in DEFAULT_CONFIG
```

- [ ] **Step 2: Run test to verify it fails** — FAIL.
- [ ] **Step 3: Implement** — In `core/pre_earnings/decision.py`: add `from core.signal_engine import Signal` and replace the `"signal"` stub entry with a `_signal_input(item)` helper that builds `Signal.from_dict(item)` when the item carries signal fields, else returns the honest stub. Keep all other §51 inputs unchanged. In `core/ai/tools/market_data.py`: add an optional `universe_regimes` path to the market-regime tool that calls `MarketRegimeEngine().detect_market(...)` when supplied; the per-ticker path is unchanged. In `core/config.py` + `config.example.json`: add `DEFAULT_CONFIG["ml"] = {"registry": [], "qlib_enabled": False, "baseline_feature_version": "v1"}` and `DEFAULT_CONFIG["signal_engine"] = {"market_regime_universe_min": 10}` (read the files first to place additively).
- [ ] **Step 4: Run test to verify it passes** — PASS.
- [ ] **Step 5: Commit** — `feat(phase6c): wire signal_engine+ml into §51 context + §39 regime tool + config`

---

### Task 11: Control docs + exit gate + memory

**Files:** Modify `docs/V9_ARCHITECTURE.md`, `docs/V9_AI_SYSTEM.md`, `docs/V9_CHANGELOG.md`, `docs/V9_IMPLEMENTATION.md`; Create `.superpowers/sdd/2026-08-12-v9-phase6-signal-ml-engine/progress.md`; Update `memory/v9-migration-project.md` + `memory/MEMORY.md`.

- [ ] **Step 1: Curated regression** — `pytest tests/unit tests/integration tests/contract tests/golden tests/perf -q` → expect PASS (record count).
- [ ] **Step 2: Full exit gate** — `pytest tests tests_e2e -q` → expect PASS, ~36 skipped (record count).
- [ ] **Step 3: §4 sweep** — grep Phase-6-touched files for `mostly complete|should work|99%|mock for now|todo later|placeholder` → 0 §4 violations (hits are pre-existing honesty guards).
- [ ] **Step 4: Deps diff** — `git diff f24aedf..HEAD -- requirements* pyproject.toml setup.py setup.cfg` → empty (§118).
- [ ] **Step 5: V7 untouched** — `git -C "Market Signals V7 - Premium" rev-parse HEAD` → `f912a33`.
- [ ] **Step 6: Control docs** — append Phase 6 section to `V9_ARCHITECTURE.md` + `V9_AI_SYSTEM.md` (§54 registry + §55 not-LLM-confidence + §56 meta-label), `V9_CHANGELOG.md` entry, `V9_IMPLEMENTATION.md` Phase 6 detail + row status PASS.
- [ ] **Step 7: SDD ledger** — write `.superpowers/sdd/2026-08-12-v9-phase6-signal-ml-engine/progress.md` (tasks, deviations, gate numbers).
- [ ] **Step 8: Commit control docs** — `docs/V9_ARCHITECTURE.md docs/V9_AI_SYSTEM.md docs/V9_CHANGELOG.md docs/V9_IMPLEMENTATION.md`.
- [ ] **Step 9: Update memory** — `memory/v9-migration-project.md` Phase 6 = PASS (6A+6B+6C), next Phase 7; `memory/MEMORY.md` index hook.
- [ ] **Step 10: Final commit** — `docs(phase6): control docs + Phase 6 exit gate — PASS (6A/6B/6C)`.

---

## Self-Review (run before execution)

- **Spec coverage:** §52 (catalog, Task 2) ✓; §53 (horizons, Task 1) ✓; §54 (registry + baseline + OosGate, Tasks 6–7) ✓; §55 (calibration + from_llm_confidence, Task 8) ✓; §56 (meta-labeler + evaluate, Task 8) ✓; market/sector regime (Task 3 + Task 10) ✓; §49 `signal` input made real (Task 10) ✓.
- **Placeholders:** none — every step carries verbatim code.
- **Type consistency:** `SignalHorizon` is the one enum used across `horizons.py`, `Signal.horizon`, `ModelRecord.horizon`, `TargetSpec.horizon`, `ProbabilityDefinition.horizon`, `ModelRegistry.list_by_horizon`. `ModelRecord`/`OosVerdict`/`OosGate` names match across registry + baseline + golden.
- **Out of scope:** §57–§61 → Phase 7 (OosGate seam); §63/§64 → Phase 8; news/finbert/sec → Phase 10; UI → Phase 12 — all honest stubs.
- **Deps:** zero new (qlib import-guarded optional; baseline/calibration stdlib).