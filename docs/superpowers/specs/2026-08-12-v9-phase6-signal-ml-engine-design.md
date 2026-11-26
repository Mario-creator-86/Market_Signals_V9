# Phase 6 — Signal & ML Engine (V9) — Design Spec

> Spec source: `prompt_to_follow.md` §52 (Signal Engine), §53 (Signal Horizons), §54 (ML Engine), §55 (Calibration), §56 (Meta-labeling), plus the market/sector-regime inputs named in §49 (lines 1597–1598) and surfaced as §39 tools (`get_market_regime`/`get_sector_regime`).
> Status of this doc: design (brainstorm output). The next step is the implementation plan.

## 1. Goal

Turn the V7 signal surface into the V9 **Signal & ML Engine**: a canonical Signal type with horizons (§53), a curated catalog of the *existing* V7 indicators governed by §52 ("preserve useful signals; do not add indicators merely because they exist"), a market-wide regime layer (the gap), and an ML Engine (§54) that ships a **simple baseline first** with a model registry tracking the 10 §54 fields and an OOS-improvement gate that defers the real backtest to Phase 7, plus calibration (§55) and meta-labeling (§56) surfaces. All additive; the 11,857-line agents and the V1/V2 signal pipelines are composed, not rewritten (§5/§7).

## 2. Scope-governing spec verbs (read literally)

- §52: **Preserve** useful V7 signals. **Evaluate** incremental value of [trend…ML]. **Do not add** indicators merely because they exist. → Phase 6 is a **curation/surface** phase, not "add 16 indicators." The catalog names the existing indicators and their status; it adds none.
- §53: horizons at minimum SHORT_TERM / SWING / MEDIUM_TERM; targets/features **must align** with horizon.
- §54: **Use Qlib where appropriate** (not mandated). **Track** the 10 fields. **Simple baseline models come first.** Complex models **must prove OOS improvement** (OOS engine = Phase 7).
- §55: if displaying a probability, **define precisely** what it means; **do not display LLM confidence as statistical probability**.
- §56: **Evaluate** meta-labeling (primary + meta → final is valid).

## 3. Existing surface (evolve-not-rewrite — verified by code map)

- `core/scoring_system.py` — V1 1m logistic. `@dataclass ScoreResult` (total_score/direction/probability_score/raw_logit_value/mtf_score/rsi_score/vwap_score/ema_score/macd_score/price_action_score/signal_thesis/timeframe/expected_holding_period). `compute_signal_score(row, mode, profile)` over hand-coded `SCORING_DEFAULTS["logistic_coeffs"]`. `detect_market_regime(row, cfg)` → V1 taxonomy TREND/RANGE/LOW_VOL/VOLATILE/MIXED. `run_weight_calibration_audit()` = **stub** ("Module 5/6").
- `core/strategy_v2/` — V2 selector. `@dataclass CandidateSignal` (regime/confidence/…). `regime_engine.RegimeEngine.detect(context) -> {regime, confidence, sub_scores}` → V2 taxonomy `REGIME_TREND/RANGE/HIGH_VOL/EVENT_RISK/SESSION_BREAKOUT`. `context_builder.build_context()` = the de-facto **feature surface** (rsi, macd_hist, atr_pct, adx, ema20/50/200, ema_slope, bb_width, volume_avg, supertrend, chart_pattern, …). `selector.StrategySelector`, `registry.build_default_registry()` (8 specs S1–S8).
- `core/signals.py` — `SignalManager`; signals are **plain dicts** (no lifecycle dataclass).
- `core/ai/decision.py` — `Decision.horizon: str = ""` (free-form LLM string).
- `core/ai/tools/market_data.py` — §39 `get_market_regime` binds to the real `RegimeEngine.detect`; `get_sector_regime` rolls up per-ticker regime across a sector via `core.sectors.get_tickers_by_sector`. **These are REAL, not stubs.**
- `core/walk_forward_validation.py` + `core/strategy_backtester.backtest()` — the real OOS harness (reused by Phase 7 for the §57 unified interface; Phase 6 records the seam, does not build §57).
- ML / calibration / meta-labeling: **entirely greenfield** (no sklearn/qlib/fit/predict; `probability_score` is an uncalibrated sigmoid).

## 4. Out of scope (deferred — honest stubs, §4)

- §57 unified `BacktestEngine` domain interface, §58 results, §59 walk-forward/OOS, §60 overfitting classification (ROBUST/MODERATE/FRAGILE/INSUFFICIENT_EVIDENCE) → **Phase 7**. Phase 6's `OosGate` is an interface seam that records `available:False` + reason `"phase7_backtest_engine"`.
- §61 evidence hierarchy (TICKER→INDUSTRY→SECTOR→UNIVERSE) labeling → Phase 7/8.
- Strategy Registry / Champion-Challenger (§63/§64) → **Phase 8**. Phase 6's `signal`/`strategy_evidence` input in the §51 context stays an honest stub for Phase 8.
- News/FinBERT/SEC sentiment signals → Phase 10 (the catalog names them as DEFERRED).
- Complex ML models (anything beyond the simple baseline) → gated by the OosGate (Phase 7); not built here.
- Optuna (§64) → Phase 14 research factory.

## 5. Decomposition

### 5A — Signal Engine surface + Horizons + Market-wide Regime (§52/§53 + regime)

New package `core/signal_engine/` (no collision with the `core/signals.py` module):

- `horizons.py`
  - `SignalHorizon(str, Enum)` = `SHORT_TERM` / `SWING` / `MEDIUM_TERM` (§53).
  - `HORIZON_DEFINITIONS: Mapping[SignalHorizon, HorizonDefinition]` — each `HorizonDefinition` carries: `sessions` (target window, e.g. SHORT_TERM 1–5 / SWING 5–20 / MEDIUM_TERM 20–60), `target_condition` (the §55 "what probability means" seed, e.g. `"P(return > 0 over next {sessions} sessions)"`), `feature_timeframes` (the timeframes whose features may align to this horizon — SHORT_TERM→{1m,5m,15m}, SWING→{15m,1h,4h}, MEDIUM_TERM→{1h,4h,1d}). This pins §53 "targets/features must align with horizon."
  - `map_mode_to_horizon(mode: str) -> SignalHorizon` — evolves the existing `TIMEFRAME_MAP`/`HOLDING_PERIOD_MAP` per-mode strings (PULLBACK→SHORT_TERM, BREAKOUT→SHORT_TERM, MOMENTUM→SWING, MEAN_REVERSION→SWING) into the enum; unknown → SWING (fail-safe).
  - `align_horizon(horizon, feature_timeframe) -> bool` — the alignment guard.

- `catalog.py`
  - `IndicatorStatus(str, Enum)` = `PRESERVED` / `EVALUATE` / `DEFERRED`.
  - `IndicatorSpec` frozen dataclass: `name`, `source_module` (e.g. `"core.scoring_system"`, `"core.ta"`, `"core.strategy_v2.context_builder"`), `status`, `horizon_fit` (frozenset of `SignalHorizon`), `incremental_value_note`.
  - `SIGNAL_CATALOG: tuple[IndicatorSpec, ...]` — the §52 catalog of the **existing** V7 indicators (trend, momentum, rsi, macd, moving_averages, relative_strength, volume, rvol, atr, volatility, market_regime, sector_regime, earnings, news, sentiment, ml) with `status` set per §52: `PRESERVED` for the ones V7 actually computes and uses; `EVALUATE` for ml (Phase 6B lands the baseline to evaluate it); `DEFERRED` for news/finbert/sec (Phase 10). **No new indicator is added.** The catalog is a governance artifact: a rename/removal of a `PRESERVED` indicator is a golden break.
  - `get_catalog_index() -> dict[str, IndicatorSpec]` for lookups.

- `regime.py` (the market-wide gap)
  - `MarketRegimeEngine` — computes a **market-wide** regime by rolling up per-ticker regimes across a universe (breadth): `detect_market(universe_regimes: Iterable[dict]) -> MarketRegimeResult`. Counts the V2 regime labels across the universe → a breadth distribution → a market regime label (`RISK_ON` / `RISK_OFF` / `MIXED` / `TRANSITIONING`) + `breadth_pct` + `is_stale`. Reuses the existing per-ticker `RegimeEngine.detect` for the per-ticker inputs (callers supply them); this engine only aggregates.
  - `MarketRegimeResult` frozen dataclass: `label`, `breadth_pct` (fraction of `REGIME_TREND`+`REGIME_SESSION_BREAKOUT`), `distribution` (counter), `is_stale`, `is_not_probability=True`.
  - The VIX/macro layer is an **honest stub**: `detect_macro_regime() -> {available: False, reason: "macro_vix_data_source_deferred"}` (§4 — the V7 data feed is crypto/1m; a VIX/macro data source is a later-phase wiring; not faked).
  - Sector regime: **reuse** the existing `core.ai.tools.market_data` rollup; `detect_sector(sector, per_ticker_regimes)` thin wrapper for symmetry, delegating to the existing rollup logic (no duplication).

- `types.py`
  - `Signal` frozen dataclass (the lifecycle type the dict-based `SignalManager` lacks): `ticker`, `horizon: SignalHorizon`, `direction`, `score`, `signal_type`, `source` (e.g. `"v1_logistic"` / `"v2_selector"`), `catalog_refs: tuple[str, ...]` (the `IndicatorSpec.name`s that fed it), `raw: Mapping[str, Any]` (the existing dict, preserved verbatim for back-compat), `is_not_probability: bool = True`.
  - `Signal.from_dict(d, *, horizon=None) -> Signal` / `to_dict() -> dict` round-trip; `horizon` resolved via `map_mode_to_horizon(d.get("mode"))` when not supplied. Additive — `SignalManager` keeps its dict path; `Signal` is an overlay consumers may opt into.

- `__init__.py` re-exports.

### 5B — ML Engine + Calibration + Meta-labeling (§54/§55/§56)

New package `core/ml/`:

- `registry.py`
  - `ModelRecord` frozen dataclass with the 10 §54 fields: `model`, `version`, `features: tuple[str, ...]`, `feature_version`, `target` (a `TargetSpec`: `horizon: SignalHorizon` + `condition`), `horizon: SignalHorizon`, `training_range: tuple[str, str]`, `validation_range: tuple[str, str]`, `test_range: tuple[str, str]`, `trained_at: str`. Plus `is_simple_baseline: bool` and `oos_verdict: str` (one of `PENDING`/`DEFERRED_PHASE7`/`ROBUST`/`MODERATE`/`FRAGILE`/`INSUFFICIENT_EVIDENCE` — but only `PENDING`/`DEFERRED_PHASE7` are settable in Phase 6; the robustness classes are Phase 7 and rejected if set now).
  - `ModelRegistry` — `register(record)`, `get(model, version)`, `list_by_horizon(horizon)`, `to_list`/`from_list` persistence to `config["ml"]["registry"]`.
  - `OosGate` — the interface seam: `evaluate_oos_improvement(baseline: ModelRecord, challenger: ModelRecord, backtest_runner=None) -> OosVerdict`. With no Phase 7 backtest runner → `OosVerdict(available=False, verdict="DEFERRED_PHASE7", reason="phase7_backtest_engine")`. With a runner (Phase 7 injects one) → delegates. §54 "complex models must prove OOS improvement" is enforced as: a `ModelRecord` with `is_simple_baseline=False` may be registered but its `oos_verdict` may not be promoted past `PENDING` without an `OosVerdict(available=True, verdict in {ROBUST, MODERATE})`. This is the §54 gate, deferred to Phase 7's real numbers.

- `baseline.py`
  - `SimpleBaselineModel` — a **stdlib logistic** baseline (§54 "simple baseline first"). Trains via gradient descent on features from `context_builder.build_context()` + outcomes from `SignalManager` closed signals. No sklearn, no qlib (§118). Records itself in the registry with `is_simple_baseline=True`, the 10 fields, and `oos_verdict="PENDING"`.
  - `train_baseline(features: Sequence[Mapping], outcomes: Sequence[float], *, horizon, target) -> TrainedBaseline` → returns weights + registers a `ModelRecord`.
  - Evolves the `run_weight_calibration_audit` stub in `scoring_system.py` to delegate to the baseline (additive — the stub gains a real call, the hand-coded `logistic_coeffs` path is untouched).

- `qlib_adapter.py`
  - `QlibAdapter` — import-guarded (`qlib` optional, ADAPTER per the Phase 2 manifest; V9 data model authoritative). `available() -> bool` (True only if `qlib` importable + owner opt-in). If not available → `adapt(...) -> {available: False, reason: "qlib_not_installed_owner_opt_in"}` (honest stub, §4/§118). Zero new deps; the owner installs qlib to activate (Phase 2 sanctioned the ADAPTER decision; Phase 6 ships the seam, not the dep).

- `calibration.py` (§55)
  - `ProbabilityDefinition` frozen dataclass: `horizon: SignalHorizon`, `condition: str` (e.g. `"P(return > 0 over next 5 sessions)"`), `calibration_method: str` (`"isotonic"` / `"platt"` / `"none"`).
  - `CalibrationResult` frozen: `method`, `raw_range`, `calibrated_range`, `sample_size`, `is_statistical_probability: bool`, `definition: ProbabilityDefinition`.
  - `calibrate(raw_scores, outcomes, *, definition, method="isotonic") -> CalibrationResult` — stdlib isotonic regression (pool-adjacent-violators) or Platt (logistic) — both pure stdlib/numpy (already in V7).
  - `apply_calibration(raw_score, calibration) -> float`.
  - §55 enforcement: `CalibrationResult.from_llm_confidence(...)` raises `CalibrationError` — LLM confidence may **never** be labeled a statistical probability. A `probability_score` surfaced to the UI must carry a `ProbabilityDefinition` or be labeled `is_statistical_probability=False` (the existing uncalibrated `probability_score` stays `is_statistical_probability=False`).

- `meta_label.py` (§56)
  - `MetaLabel` frozen: `primary` (e.g. `LONG`), `meta` (e.g. `DO_NOT_EXECUTE`), `final` (e.g. `NO_TRADE`).
  - `MetaLabeler.combine(primary: str, meta: str) -> str` — implements the §56 example (LONG + DO_NOT_EXECUTE → NO_TRADE; primary + EXECUTE → primary; primary + INSUFFICIENT → NO_TRADE).
  - `evaluate_meta_labeling(primary_predictions, meta_predictions, outcomes) -> MetaLabelEvaluation` — the §56 "evaluate" harness (does meta-labeling improve precision/recall vs primary-only?). stdlib metrics.

- `__init__.py` re-exports.

### 5C — Integration + Golden + Control docs

- `core/pre_earnings/decision.py` — the §51 context input `"signal"` (Phase 5 honest stub) now delegates to `core.signal_engine` when the data exists; `"backtest"` stays an honest stub for Phase 7; `"strategy_evidence"` stays an honest stub for Phase 8. Additive — the existing `_honest_stub` path is replaced for `signal` only.
- `core/ai/tools/market_data.py` — `get_market_regime` keeps its real per-ticker binding; additively consults `MarketRegimeEngine.detect_market` when a universe of per-ticker regimes is available (the per-ticker path stays the default). No behavior change for existing callers.
- `core/config.py` + `config.example.json` — additive `DEFAULT_CONFIG["ml"]` (`registry: []`, `qlib_enabled: false`, `baseline_feature_version: "v1"`) and `DEFAULT_CONFIG["signal_engine"]` (`market_regime_universe_min: 10`).
- Golden tests (GOLD-052..056, deterministic synthetic data — §107):
  - GOLD-052: `SIGNAL_CATALOG` lists the §52 indicators; no indicator has `status == PRESERVED` unless it maps to a real source module; **zero new indicators added** (catalog length == the §52 list length).
  - GOLD-053: `SignalHorizon` enum + `HORIZON_DEFINITIONS` target/feature alignment; `map_mode_to_horizon` deterministic; `align_horizon` rejects misaligned timeframes.
  - GOLD-054: `ModelRecord` carries the 10 §54 fields; `SimpleBaselineModel` trains on synthetic data and registers a record with `is_simple_baseline=True`, `oos_verdict="PENDING"`; `OosGate` with no runner → `available=False, verdict="DEFERRED_PHASE7"`; a non-baseline record cannot promote past `PENDING` without a real `OosVerdict`.
  - GOLD-055: `calibrate` produces a `CalibrationResult` with a precise `ProbabilityDefinition`; `CalibrationResult.from_llm_confidence` raises; an uncalibrated `probability_score` is labeled `is_statistical_probability=False`.
  - GOLD-056: `MetaLabeler.combine(LONG, DO_NOT_EXECUTE) == NO_TRADE`; `evaluate_meta_labeling` reports precision/recall on synthetic data.
- Control docs: `V9_ARCHITECTURE.md` (Phase 6 section), `V9_AI_SYSTEM.md` (Phase 6 subsection: §54 ML registry + §55 not-LLM-confidence + §56 meta-label), `V9_CHANGELOG.md` (entry), `V9_IMPLEMENTATION.md` (Phase 6 detail + row status). SDD ledger `.superpowers/sdd/2026-08-12-v9-phase6-signal-ml-engine/progress.md`.

## 6. Constraints (verbatim from the spec / project)

- §5/§7 evolve-not-rewrite: the V1/V2 signal pipelines, `SignalManager`, `RegimeEngine`, `detect_market_regime`, `context_builder`, the §39 regime tools, and the agents are composed, not rewritten. Diffs are additive.
- §4: no forbidden success language; later-phase inputs (§57 backtest, §63 strategy registry, §10 news/finbert/sec, VIX/macro) are honest stubs (`available:False`+reason), never faked numbers.
- §55: LLM confidence is never a statistical probability (enforced in code: `from_llm_confidence` raises).
- §54: complex models prove OOS improvement; in Phase 6 only the gate+seam exist, the real OOS is Phase 7.
- §118: zero new production deps. Qlib is import-guarded and optional (ADAPTER; Phase 2 sanctioned); the baseline + calibration + meta-label are stdlib/numpy (already in V7).
- §115: tests not weakened; deterministic synthetic data for golden/unit ML tests (no dependence on real signal history).
- §116: no remote push; commit to master only.
- §106: no secrets/keys.
- V7 at `Market Signals V7 - Premium` (HEAD f912a33) is READ-ONLY — never modified.

## 7. Risks & mitigations

- **ML baseline over-reach.** §54 says "simple baseline first." Mitigation: stdlib logistic only; complex models gated by `OosGate` (Phase 7). No real-signal training in tests (synthetic deterministic).
- **Regime taxonomy collision.** V1 (`TREND/RANGE/LOW_VOL/VOLATILE/MIXED`) and V2 (`TREND/RANGE/HIGH_VOL/EVENT_RISK/SESSION_BREAKOUT`) are separate and reused unchanged; the new `MarketRegimeEngine` uses a **third** taxonomy (`RISK_ON/RISK_OFF/MIXED/TRANSITIONING`) for the market-wide layer to avoid touching the existing ones.
- **Qlib dep drift.** Mitigation: import-guarded, `qlib_enabled: false` default; zero deps unless the owner opts in.
- **`probability_score` semantics in UI.** §55 bites if the UI shows the uncalibrated `probability_score` as a probability. Mitigation: label it `is_statistical_probability=False` and carry a `ProbabilityDefinition` stub ("uncalibrated logistic score, not a probability"); UI changes are Phase 12 — flagged here, not built.

## 8. Self-review

- Spec coverage: §52 (catalog, 5A) ✓; §53 (horizons, 5A) ✓; §54 (registry + baseline + OosGate, 5B) ✓; §55 (calibration + from_llm_confidence guard, 5B) ✓; §56 (meta-labeler + evaluate, 5B) ✓; regime (market-wide, 5A; per-ticker/sector reused) ✓.
- Placeholders: none — every module lists concrete classes/functions/signatures.
- Out of scope: §57–§61 → Phase 7; §63/§64 → Phase 8; news/finbert/sec → Phase 10; UI → Phase 12. All honest stubs.
- Type consistency: `SignalHorizon` used in `horizons.py`, `Signal.horizon`, `ModelRecord.horizon`, `TargetSpec.horizon`, `ProbabilityDefinition.horizon`, `ModelRegistry.list_by_horizon` — one enum, one name.
- The plan will pin verbatim code for each module + test.