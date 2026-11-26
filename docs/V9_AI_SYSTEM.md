# V9_AI_SYSTEM

## Phase 11 - AI decisions cannot bypass PaperBroker

- An AI action remains a proposal until the application creates a deterministic `OrderIntent`; AI text cannot choose order identity, authoritative size/accounting, owner risk values, or lifecycle state.
- Every automated OPEN/REDUCE/CLOSE crosses `PaperBroker` and the persistent idempotency ledger. Duplicate decisions cannot create duplicate Capital mutations.
- New risk requires the owner-selected PAPER_AUTO mode, active Risk Constitution, REDUCE percentage, healthy reconciliation, enabled Paper agent, and inactive Kill Switch. Existing-position monitoring and safe risk reduction remain active when entries are blocked.

Living document. Structure from spec §34–§44. Implementation status `NOT STARTED` at Phase 0; built in Phases 3–4.

## AIProvider interface (§34)
Normalized capabilities: `list_models()`, `health_check()`, `capabilities()`, `generate()`, `generate_structured()`, `tool_call()`, `stream()`, `usage()`. Domain logic is vendor-independent via this interface (§17). Replaces V7 `core/ollama_client.py` / `core/ollama_decision.py` in Phase 3.

## Providers (§35)
Required: Ollama Local, Ollama Cloud, OpenAI, Anthropic, Google Gemini. Adapter candidates: xAI, DeepSeek, Mistral, OpenRouter. Model names not hardcoded; verify current official APIs at implementation time (Phase 3).

## Model registry (§36)
Fields: provider, model_id, display_name, capabilities, tool_support, structured_output_support, context_metadata, health, enabled, last_checked. Dynamic discovery where supported; manual config otherwise.

## Model assignment roles (§37)
Market Analyst, Pre-Earnings Analyst, News Analyst, Quant Analyst, Portfolio Manager, Risk Critic, Research Scientist, Coding Agent, Code Reviewer, Security Reviewer. One model for all roles allowed.

## Model router (§38)
fast/cheap → initial triage; strong reasoning → deeper analysis; independent model → critic. Do not spend expensive calls on thousands of low-quality candidates.

## AI tool architecture (§39)
AI reasons through registered application tools (allow-listed), e.g.: get_market_snapshot, get_ohlcv, get_technical_features, get_market_regime, get_sector_regime, get_earnings_event, get_historical_earnings, get_earnings_surprises, get_options_analysis, get_pre_earnings_analysis, get_news, get_news_sentiment, get_sec_context, get_signal, get_strategy, get_strategy_performance, compare_strategies, run_backtest, get_backtest_result, get_portfolio, get_position, get_portfolio_risk, get_available_capital, calculate_allowed_position_size, get_decision_history, get_trade_history, propose_order, propose_close.

## AI must NOT have unrestricted access (§40)
No shell, arbitrary Python, SQL, filesystem, arbitrary HTTP, broker credentials, or secrets. Allow-listed domain tools only.

## AI output schema (§41)
Validated JSON, e.g.:
```json
{ "action": "OPEN_LONG", "ticker": "NVDA", "horizon": "SWING", "confidence": 0.78,
  "strategy_id": "momentum_v3", "thesis": "...", "supporting_evidence": [],
  "opposing_evidence": [], "risk_flags": [], "data_quality": 0.94, "requires_more_data": false }
```
Allowed actions: WATCH, NO_TRADE, OPEN_LONG, OPEN_SHORT, HOLD, REDUCE, CLOSE.

## AI validation pipeline (§42)
PARSE → SCHEMA VALIDATION → SEMANTIC VALIDATION → AUTHORITATIVE DATA CHECK → POLICY VALIDATION → RISK GATE. AI prose alone never triggers an order.

## AI numbers not authoritative (§43)
AI does not supply price, P/L, IV, EPS, portfolio value, allowed capital, position size. Application services calculate them.

## Model Lab (§88, Phase 13)
`core/decision_ledger/model_lab.ModelLab` compares AI models per role (`pre_earnings`, `market_analyst`, `news_analyst`, `quant`, `risk_critic`, `research`, `coding`, `review`) over ONE frozen point-in-time snapshot: every candidate in a run receives the identical prompt (pinned by `inputs_sha256`), and per-model validity/decision/critic outcomes persist in the Decision Ledger store. There is deliberately NO cross-role/global AI score. Providers are injected by the API layer from owner configuration; a missing provider is recorded `ai_available=false`, never simulated.

## AI replay (§89, Phase 13)
`core/decision_ledger/replay` rebuilds decision inputs at historical time T through the Phase 1 point-in-time contract (`HistoricalStore.as_of`; anything without a qualifying record stays `UNAVAILABLE` — never reconstructed from future data). `run_ai_replay` re-runs the decision engine on that snapshot and stores the result as a `REPLAY` ledger entry linked to the original (`replay_of`); replays are audit artifacts and can NEVER create orders. `evaluate_decision_outcome` computes deterministic forward-return outcomes over bars strictly after T (insufficient data stays INCONCLUSIVE).

## Decision recording (§90/§97, Phase 13)
Every agent_manager decision — accepted, rejected or NO_TRADE — is recorded by `core/decision_ledger/recorder` into the persistent SQLite Decision Ledger (idempotent on `decision_id`, fail-open for the trading path) with prompt/model/prompt-version capture (`EvaluationResult.prompt/model/prompt_version`, `TRADE_REVIEW_PROMPT_VERSION`), §42/§44 evidence and app-authoritative §43 numbers. `evaluate_no_trade_decisions` measures rejection quality: correctly rejected vs incorrectly-rejected-profitable vs inconclusive.

## AI critic (§44)
Important autonomous decisions challenged independently: contradictory evidence, stale info, provider conflict, weak backtest, fragile parameters, poor sample size, excessive volatility, concentration, catalyst risk, regime mismatch. Valid final result may be NO_TRADE.

### Phase 4C — critic workflow + autonomy enforcement
- **Critic workflow** — `core/ai/critic.run_critic` runs POST-PIPELINE (it is NOT a §42 stage; `STAGE_ORDER` stays 6). It fires only on non-`NO_TRADE`/`WATCH` outcomes, uses the router's independent `critic` tier via `generate_with_fallback`, reviews provided evidence only (§40 — no §39 tool loop), and a `veto=True` forces `Action.NO_TRADE` with `review.recommendation="avoid"` (a decisive rejection, NOT an unavailable result). `critic_force_no_trade_on_fail` (default True) fail-closes on provider/parse failure.
- **Decision engine** — `core/ai/decision_engine.evaluate_trade_decision` composes `generate_with_fallback → run_pipeline → run_critic` and returns a backward-compatible `review` dict plus the §41 `Decision`/§42 `ValidationResult`/§44 `CriticVerdict`. Malformed output (PARSE/SCHEMA/SEMANTIC) + AI-unavailable → `available=False`; decisive rejection (POLICY/NO_TRADE/RISK_GATE veto/critic veto) → `available=True, recommendation="avoid"` (never falls through to paper-only fallback). §43: `leverage=1` always; agent_manager caps via config.
- **Autonomy modes (§45)** — `core/autonomy.AutonomyMode`: OFF/ANALYZE/RECOMMEND/PAPER_AUTO/LIVE_ASSISTED/LIVE_AUTO. Default ANALYZE (never LIVE_AUTO); unknown mode fail-safes to ANALYZE. Only PAPER_AUTO/LIVE_ASSISTED/LIVE_AUTO allow paper orders; only LIVE_AUTO allows live orders. `agent_manager._autonomy_order_gate` enforces this + the §74 kill switch at both autonomous order paths (fail-safe: unreadable config → ANALYZE → blocks both).
- **No-weakening guard (§48)** — `core/autonomy.assert_not_weakening` raises `AutonomyViolationError` if an autonomous agent tries to weaken a protected surface (risk_constitution/kill_switch/promotion_gates/critical_tests/live_permissions/holdout_policy) without explicit owner authorization. Enforced in code, not just prose.
- **Paper-Auto before Live-Auto (§46)** — `can_enable_live_auto` requires the owner flag AND a passed Paper-Auto acceptance battery. The battery (`PAPER_AUTO_SCENARIOS`, 8 deterministic scenarios) is re-run with fakes on CI/owner action; at runtime the recorded `paper_auto_acceptance_passed` flag is consulted — the autonomous loop never re-runs live AI to authorize its own promotion to LIVE_AUTO.

### Phase 5 — Pre-Earnings decision system (§49/§50/§51)
- **§50 PRE-EARNINGS SCORE is NOT a probability (§50)** — `core/pre_earnings/score.compute_pre_earnings_score` evolves the V7 `_compute_opportunity_score` formula into a canonical score with VISIBLE components: `score = clamp(round(signal_strength * coverage_score/100), 0, 100)` where `signal_strength = 0.65*gap_risk + 0.35*|direction_bias|` (exact V7 back-compat when `weight_overrides` is None; override guard uses `value >= 0` so an owner can zero a component). The triple — SCORE (0-100) + DATA COVERAGE + DATA CONFIDENCE — is pinned on every built item, with `pre_earnings_is_not_probability = True` constant + component note. `compute_data_confidence` evolves the V7 `historical_samples` bands so coverage is a DOWNGRADE-ONLY modifier (HIGH needs ≥3 quarters AND coverage ≥45; coverage can lower HIGH→MEDIUM but never raises LOW→MEDIUM); `is_insufficient` stays True only at `historical_samples==0`.
- **§51 decision context + engine bridge + autonomy clamp** — `core/pre_earnings/decision.evaluate_pre_earnings_decision(item, *, config, services=None)` assembles the §51 input stack (Pre-Earnings + strategy evidence + signal + backtest + AI + critic + data quality + portfolio + risk + capital) and bridges to the Phase 4C `core.ai.decision_engine.evaluate_trade_decision` (reusing `generate_with_fallback → run_pipeline → run_critic` + the §42 validation + §44 critic veto). Later-phase inputs are HONEST STUBS (`available:False`+reason) for Phases 6/7/8/9/10 — never faked numbers (§4/§43). Per §109 (Decision→Risk→Capital→Paper order), the capital/risk §42 stages that depend on Phase 9 are injected as permissive stubs here so the DECISION reflects the AI+critic+policy+autonomy conclusion; the REAL capital/risk gating stays in `agent_manager`'s order path.
- **Autonomy clamp semantics** — AI-unavailable → WATCH ("keep watching", NOT a decisive NO_TRADE). Only ORDER-bearing actions (OPEN_LONG/OPEN_SHORT) are gated — clamped to WATCH when `kill_switch_active`, when the mode forbids paper orders, or when LIVE_AUTO lacks `can_enable_live_auto`. NO_TRADE (critic veto / policy / risk veto) and WATCH/HOLD/REDUCE/CLOSE pass through; a decisive NO_TRADE is never relaxed to WATCH. The returned `EvaluationResult.decision` is the authoritative final action (from `review.decision`, possibly clamped).
- **Ship wired-but-off (§45 default ANALYZE)** — the `run_cycle` decision pass is gated by `pre_earnings.decision_enabled` (default False) and bounded by `decision_max_tickers` (25); it attaches `item["pre_earnings_decision"]` (action/recommendation/confidence/available/reason/validation_stage/critic_veto). The owner flips the flag to activate; until then the agent behaves exactly as before (the 11,857-line body is otherwise untouched).

### Phase 6 — Signal & ML Engine (§54/§55/§56)
- **§54 Model Registry — 10 fields tracked, simple baseline first, OOS-gated promotion** — `core/ml/registry.ModelRecord` is frozen with the 10 §54 fields (model/version/features/feature_version/target/horizon/training_range/validation_range/test_range/trained_at) plus `is_simple_baseline` and `oos_verdict` (PENDING default; `OosVerdictLabel` = PENDING/DEFERRED_PHASE7/ROBUST/MODERATE/FRAGILE/INSUFFICIENT_EVIDENCE). `ModelRegistry.register` raises if a complex model registers with a promoting verdict — complex models MUST prove OOS improvement before promotion, and the proof is Phase 7's OOS backtest (§57). Phase 6 ships the `OosGate` seam: `OosGate.evaluate_oos_improvement(baseline, challenger, backtest_runner=None)` returns `OosVerdict(available=False, verdict=DEFERRED_PHASE7)` when no runner is supplied — so a complex model cannot be silently promoted in Phase 6. `promote()` requires a real available `OosVerdict` in {ROBUST, MODERATE}. The simple baseline (`core/ml/baseline.SimpleBaselineModel`, stdlib logistic GD — zero new deps §118) trains and registers as PENDING (`is_simple_baseline=True`).
- **§55 Calibration — LLM confidence is NEVER a statistical probability (enforced in code)** — `core/ml/calibration.CalibrationResult.from_llm_confidence(...)` RAISES `CalibrationError`. A probability must mean a precise thing: `ProbabilityDefinition(horizon, condition)`, e.g. `P(return > 0 over next N sessions)`. `calibrate(raw_scores, outcomes, *, definition, method="isotonic")` uses a stdlib PAV isotonic fit and returns `CalibrationResult(is_statistical_probability=True, ...)` with a step mapping; `CalibrationResult.uncalibrated(definition)` returns `is_statistical_probability=False`; `apply_calibration` step-interpolates (pass-through when not statistical). The §51 `signal` input pins `is_not_probability=True` regardless. `core/scoring_system.run_weight_calibration_audit` evolves its "Module 5/6" stub to delegate the calibration label to `core.ml` additively (original dict keys preserved + available/is_statistical_probability/calibration_method).
- **§56 Meta-labeling — primary LONG + meta DO_NOT_EXECUTE → final NO_TRADE is valid** — `core/ml/meta_label.MetaLabeler.combine(primary, meta)`: DO_NOT_EXECUTE/INSUFFICIENT → NO_TRADE; EXECUTE → primary (LONG/SHORT); unknown → NO_TRADE fail-safe. `evaluate_meta_labeling(...)` reports primary/meta precision+recall and `meta_labeling_improves_precision`. Meta-labeling is greenfield; it does not change the §41 Decision action enum.

### Phase 7 — OOS verdicts and no-forced-strategy (§57/§60/§61)

- `core/backtest/oos_gate_runner.OosGateRunner` completes the Phase 6 seam. `OosGate.evaluate_oos_improvement(..., backtest_runner=runner)` now returns `available=True` with a shared verdict: ROBUST, MODERATE, FRAGILE, or INSUFFICIENT_EVIDENCE.
- Only authoritative backtest results participate. Missing point-in-time data or a non-authoritative engine fails closed as INSUFFICIENT_EVIDENCE. A challenger must strictly improve baseline OOS expectancy before overfitting diagnostics are considered.
- ROBUST requires explicit PBO, DSR, and parameter-stability evidence plus the configured profit-factor/drawdown thresholds. Missing IS rankings do not produce a fabricated PBO and cap a qualifying result at MODERATE.
- §61 is explicit in `EvidenceResolution`: thin ticker evidence falls through INDUSTRY → SECTOR → UNIVERSE; if none has an authoritative minimum sample, the result is `NO_VALIDATED_TICKER_SPECIFIC_STRATEGY`. The AI may explain this result but cannot relabel broader evidence as ticker-specific.
- Backtest statistics keep `is_not_probability=True`; they are realized historical measurements and must not be presented as calibrated future probabilities.

### Phase 8 — Strategy governance is application-authoritative (sections 62/63/64)

- AI output cannot register or transition a strategy directly to CHAMPION. `StrategyRegistry.promote()` is the only champion path and requires deterministic OOS, risk-adjusted, stability, cost, regime, recent, shadow, and untouched-holdout evidence.
- Promotion policy and evidence are bound by a canonical SHA-256 fingerprint and recorded in the append-only lifecycle audit. When a challenger wins, incumbent demotion and challenger promotion are one atomic persistence operation.
- The V2 migration creates RESEARCH records only. Optional selector enforcement accepts only APPROVED/CHAMPION and fails closed on missing, corrupt, or unapproved lifecycle state.
- Optuna search is research-only, persistent, budgeted, excludes FINAL_HOLDOUT, and cannot auto-promote its best trial. AI prose and a single optimization result carry no promotion authority.

### Phase 9 - Capital and risk remain application-authoritative (sections 43/47/48/65/66/67)

- AI output does not supply equity, cash, position size, fees, liquidity, loss at stop, sector exposure, correlation, or protected limits. `core.portfolio.adapter` builds risk inputs from application state and explicit market/execution evidence.
- `RiskAgent` composes the complete deterministic constraint engine only after an explicit, valid `owner_confirmed: true` Risk Constitution is present. A malformed confirmed mapping or missing required evidence rejects; an unconfirmed template cannot become active through AI output.
- The Phase 9 veto runs before, and in addition to, the retained V7 risk checks. It cannot weaken an existing cap.
- Every new paper position stores thesis, supporting/opposing evidence, strategy, signal identity, AI-model provenance when one actually exists, invalidation rules, and risk state. Manual positions are never mislabeled as AI-generated.
- AI may recommend HOLD/REDUCE/CLOSE, but deterministic reassessment owns the action contract. Kill Switch blocks new risk while existing-position monitoring continues.

### Phase 10 - FinBERT and external-text trust boundary (sections 16/68/69)

- FinBERT is deterministic model inference, not an LLM opinion. Every result records source, model, immutable revision, confidence, availability, and fallback state.
- The V7 lexicon may keep News usable when FinBERT is unavailable, but it is explicitly `sentiment_lexicon` / `ESTIMATED`; it is never relabelled as FinBERT.
- `get_news`, `get_news_sentiment`, and `get_sec_context` return `UNTRUSTED_EXTERNAL_DATA` envelopes. Suspected embedded instructions are replaced by SHA-256 quarantine markers before the provider sees the tool result.
- This boundary complements, not replaces, the Phase 4 allow-list, structured decision validation, critic veto, risk gate, and no-direct-order-from-prose rules.

## Provider API keys & global selection (owner-driven addition, 2026-08-16)

The Watchlist > Global Settings > AI Model tab is the owner surface for §108:
- Provider selector over all configured providers — Ollama (local, keyless) plus cloud: OpenAI, Anthropic, Gemini, xAI, DeepSeek, Mistral, OpenRouter, KIMI (api.moonshot.ai/v1), z.ai (api.z.ai/api/paas/v4); the last two are `openai_compatible` with no hardcoded model ids (§35).
- Write-only API-key field: `POST /api/ai/providers/{name}/key` stores the key server-side (process env + `.env`); it is never returned, logged, or sent to the frontend — `list_ai_providers` keeps exposing only `has_key` (§106). Clearing removes the env var and the `.env` line.
- Test refreshes the per-provider model list (Ollama status / cloud discovery via `/api/ai/providers/{name}/test`).
- Set as Global AI: `POST /api/ai/providers/{name}/select` persists the provider `default_model`, `ai.primary` (the decision engine orders its provider chain by it), the §37 assignment (all roles) and the §38 tier router; Ollama selections stay synced with the legacy selectors and Pre-Earnings bridge.

### Transport isolation (2026-08-16)
Cloud AI hosts (`api.openai.com`, `api.anthropic.com`, `generativelanguage.googleapis.com`, `api.x.ai`, `api.deepseek.com`, `api.mistral.ai`, `openrouter.ai`, `api.moonshot.ai`, `api.z.ai`) map to dedicated provider buckets in `core/http_utils` (concurrency/circuit/rate tables incl. `kimi` and `zai`), so an unrelated generic-bucket 401/cooldown can never collateral-damage AI provider calls. Live-verified with a real z.ai key: model discovery returns the glm family; generation correctly surfaces the provider's own balance error.
