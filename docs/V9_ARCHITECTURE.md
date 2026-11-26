# V9_ARCHITECTURE

## Phase 11 - Paper execution boundary (sections 67/70/71/72/73/74)

All automated Capital mutations now pass through `ExecutionBroker`; Phase 11 supplies only `PaperBroker`, so live execution remains unavailable. An immutable `OrderIntent` binds `decision_id`, `order_intent_id`, and `idempotency_key` to OPEN_LONG, OPEN_SHORT, REDUCE, or CLOSE. The persistent SQLite ledger owns the exact PROPOSED -> VALIDATED -> SUBMITTED -> ACKNOWLEDGED/PARTIALLY_FILLED -> terminal lifecycle and its event history.

`PaperBroker` validates owner readiness only for actions that open new risk. REDUCE and CLOSE remain available for safe position management while the Kill Switch is active. Reconciliation compares in-flight/filled ledger evidence with authoritative `CapitalManager` positions, repairs only provable outcomes, and otherwise persists `EXECUTION_STATE_CONFLICT`; the entry policy fails closed on that state.

Owner settings remain application-authoritative: Risk Constitution activation, REDUCE percentage, autonomy mode, reconciliation, and Kill Switch are exposed through bounded API/UI controls. Null is the only shipped REDUCE default, and PAPER_AUTO cannot be persisted until all prerequisites pass.

Living document. Structure mandated by spec §26 and §101. Content minimal at Phase 0; populated in Phases 1–12.

## Conceptual architecture (spec §26, adapted to real V7 modules)

```text
DATA PROVIDERS  (yfinance/Yahoo, Binance REST via core/http_utils, FinnHub, RSS, FMP stub)
      │
      ▼
DATA INGESTION   (core/engine.py ~3.8k lines; agents/scanner_agent, premarket_scanner, us_market_news, news_tab, economic_calendar, pre_earnings)
      │
      ▼
POINT-IN-TIME STORE  (TO BE BUILT, Phase 1) — currently SQLite (data/market_signals.db) + JSON dual-write (state.json, signals_state.json, capital.json, watchlist.json)
      │
      ▼
DATA VALIDATION  (core/data_guard.py, core/data_quality.py, core/price_integrity.py) — TO BE EXTENDED, Phase 1
      │
      ▼
FEATURE ENGINE  (core/ta.py, core/market_structure.py, core/volume_structure.py, core/scoring_system.py)
      │
      ├──────────────┬──────────────┐
      ▼              ▼              ▼
PRE-EARNINGS    SIGNALS        NEWS / SEC
(agents/pre_earnings_agent,   (core/signals.py,    (agents/news_tab_agent,
 core/autonomous_candidates)    core/signal_quality) agents/us_market_news_agent,
                                 TO ADD edgartools, Phase 10)
      └──────────────┴──────────────┘
      ▼
STRATEGY ENGINE  (core/strategy_v2/: regime_engine, selector, evaluators, risk_manager, registry, performance_cache)
      │
      ▼
ML ENGINE  (TO BE BUILT around Qlib adapter, Phase 6) — currently core/feedback_loop, core/confidence_gates, core/ticker_learning
      │
      ▼
AI ANALYST  (core/ollama_client, core/ollama_decision) — TO BE REPLACED by AIProvider, Phase 3
      │
      ▼
AI CRITIC  (TO BE BUILT, Phase 4)
      │
      ▼
DECISION ENGINE  (core/trade_decision, core/autonomous_control, core/autonomous_trade_guard, core/capital_entry_guard) — TO BE EXTENDED, Phase 4
      │
      ▼
DETERMINISTIC RISK  (core/risk_limits, core/strategy_v2/risk_manager) — TO BE EXTENDED per Risk Constitution, Phase 9
      │
      ▼
CAPITAL ENGINE  (core/capital_manager, core/position_sizing, core/trading_costs)
      │
      ▼
EXECUTION ENGINE  (ExecutionBroker interface TO BE BUILT, Phase 11) — currently PaperBroker path in capital_manager
      │
      ▼
POSITIONS  (capital.json + SQLite) → POSITION MONITOR (core/exit_manager, core/feedback_loop)
      │
      ▼
DECISION LEDGER  (core/decision_ledger — persistent SQLite §97 ledger, §89 PIT replay, §88 Model Lab, §90 no-trade evaluation; legacy core/trade_audit + core/execution_archive remain the Phase 12 computed fallback) — Phase 13
      │
      ▼
OUTCOME STORE  (core/decision_ledger outcomes — append-only §89 evaluations; core/feedback_loop + core/strategy_v2/performance_cache for strategy outcomes) — Phase 13
      │
      ▼
RESEARCH FACTORY  (TO BE BUILT, Phase 14) → CHALLENGER PIPELINE (Phase 16) → PROMOTE / REJECT (Phase 16)
```

Adapt this to V7. Do not introduce distributed infrastructure unnecessarily (spec §26).

## Canonical data layer (Phase 1A)

`core/data_model/` is V9's single source of truth for data entity types (spec §27):
- `entities.py` — `AssetClass`/`Currency`/`TradingSession` enums, `Exchange`/`Symbol` value types, the `EXCHANGES` registry, and the 15 entity dataclasses (`OHLCVBar`, `CorporateAction`, `Earnings`, `AnalystEstimate`, `Fundamentals`, `OptionContract`, `OptionsChain`, `NewsArticle`, `SECFiling`, `Signal`).
- `pit.py` — `PointInTime` (the §29 timestamp tuple) + `UNAVAILABLE` sentinel + `as_of(records, T)` returning the newest record with `available_from <= T` or `UNAVAILABLE`. This is the §32 no-synthetic-historical-facts guard as executable code: through `as_of` it is impossible to read a record that was not available at T.
- `quality.py` — `Conflict`, `DataQuality`, `SignalQuality`, `QualityEnvelope`. DATA quality is separate from SIGNAL/ANALYSIS quality (§33); `QualityEnvelope` carries both side-by-side.

Evolved in place (additive, backward-compatible):
- `core/market_hours.py` — `MarketHoursStatus` gained `session`/`is_half_day`/`exchange`; session model PRE_MARKET 04:00–09:30 / REGULAR 09:30–16:00 / AFTER_HOURS 16:00–20:00 / BMO / AMC / CLOSED / CRYPTO_CONTINUOUS (§28); `earnings_session_reported()` is the canonical home for the BMO/AMC thresholds.
- `core/data_quality.py` — `QualifiedValue` gained optional `freshness`/`coverage`/`conflicts`/`quality_flags` (§33), defaulting so all existing call sites are unchanged.
- `core/news_evidence_ledger.py` — typed `read_articles_as_of(T)` reader mapping `observed_at`→`available_from`, `published_at`→`event_time` (§29/§32). The JSONL format is unchanged.

Not yet done (later phases): corporate-action capture/split-adjustment + survivorship registry → Phase 1C; provider adapters for FinnHub/Binance/Alpaca/KuCoin/news/fundamentals → the phases that consume each entity (5/7/10) — 1B shipped the store + ingestion seam + one yfinance proof adapter only; Options/SEC/Fundamentals **fetchers** → Phase 5/10 (their types are defined here); `exchange_calendars` / any new open-source dep → Phase 2 (§118 — Phase 1 added no new deps).

## Point-in-time historical store (Phase 1B)

`core/data_model/store.py` — `HistoricalStore`: an append-only SQLite store (same
DB file / WAL pattern as `core/sqlite_store.py`) holding serialized canonical §27
entities keyed by `(entity_type, symbol, provider, available_from, version)`.
This is the "valid point-in-time source" spec §32 references — each fetched fact
is written once and never mutated (corrections = new higher-`version` row).

- `append(record, ...)` — append one entity/dict with its `available_from`/`version`/`fetched_at`; no update/delete API (append-only by construction).
- `as_of(entity_type, symbol, T, *, prefer="available_from")` — newest record knowable at T, or `UNAVAILABLE`; delegates tie-break to the 1A `as_of` (DRY).
- `as_of_range(entity_type, symbol, T_start, T_end, *, prefer)` — ascending window series (the backtest replay shape).
- `detect_conflicts(entity_type, symbol, T, *, tolerance_pct, value_field)` — cross-provider disagreement as-of T → 1A `Conflict` objects (§33).
- `query(...)` — filtered diagnostic read.

`core/data_model/ingest.py` — opt-in ingestion seam (§7 wrap, §23 no-replace):
`ProviderRecordAdapter` protocol + `ingest_via` + `df_to_bar_rows` + `YFOHLCVAdapter`
(proof: yfinance bars → `OHLCVBar`). Adapters never call providers or see credentials (§106);
the working V7 clients are untouched. Adapters for other providers/entities land with the
phases that consume them (5/7/10). Survivorship (§30) + corporate actions (§31) → Phase 1C.

## Corporate actions + survivorship (Phase 1C)

**Adjusted/raw price convention (§31).** V9 uses **adjusted** OHLCV bars
(split- and dividend-adjusted) as the default for signal generation and
backtesting, consistent with the yfinance default `auto_adjust=True`. The
`OHLCVBar.adjusted` flag (§27) records whether a bar is adjusted (`True`, the
`YFOHLCVAdapter` default) or raw (`False`, available by constructing the
adapter with `adjusted=False`). The `CorporateAction` records in the
point-in-time store are the audit trail for *why* adjusted differs from raw
for a symbol as of a given time. A backtest at historical time T uses
`HistoricalStore.actions_as_of(symbol, T)` to know which actions have occurred
up to T (§29) — it never reconstructs actions from future data (§32).

**Corporate-action capture (§31).** `core/data_model/ingest.py` adds
`actions_df_to_records` (normalizes a yfinance `.actions` DataFrame of
Dividends + Stock Splits into DIVIDEND/SPLIT dicts) and `YFCorporateActionAdapter`
(dict → `CorporateAction` with an explicit `PointInTime`, available_from =
ex_date). `ingest_via` appends each to the `HistoricalStore`
(entity_type `"CorporateAction"`); `HistoricalStore.actions_as_of(symbol, T)`
returns all actions knowable at T, ascending. Adapters wrap-not-replace (§7/§23)
and never see credentials (§106). Non-yfinance corporate-action adapters land
with the phases that consume them (5/7/10).

**Survivorship (§30).** `core/data_model/survivorship.py` —
`SurvivorshipRegistry` records per-symbol lifecycle events (LISTED /
DELISTED / TICKER_CHANGE) with point-in-time `event_time`, and answers
`is_listed(symbol, T) -> bool` and `universe_at(T, base_symbols) -> set[str]`.
`is_listed` is the §32 conservative query for an ARBITRARY symbol (unknown →
`False`); `universe_at` is the §30 survivorship filter on an asserted-current
set (the current watchlist minus symbols with a terminal event — DELISTED or
TICKER_CHANGE — at or before T; unknown base symbols are kept, so with no
delistings registered it returns `base_symbols` unchanged). §30 honest
limitation: V9 has no comprehensive historical delisting / index-membership
database (§118 forbids adding one in Phase 1). The registry is a SCAFFOLD — it
captures delistings/ticker-changes as they are learned (manually or via a
future Phase 2+ data source). Historical backtests before the registry's first
record remain survivorship-biased for symbols not in the current watchlist;
this is documented at `core/backtester.py` (universe construction) and here,
NOT pretended solved. Wiring the backtest universe to
`SurvivorshipRegistry.universe_at(T)` + `actions_as_of(T)` is Phase 7
(backtest consolidation).

## Open-source integration boundary (Phase 2)

Spec §10 mandates a 13-step per-project evaluation for every external repo, recorded in
a 9-field manifest, with a decision: INTEGRATE | ADAPTER | REFERENCE_ONLY | REJECT.
`docs/V9_OPEN_SOURCE_MANIFEST.md` is the authority for all 13 §11–§23 repos; the
decisions + pins were filled in Phase 2 from real GitHub/LICENSE/release inspection
(date inspected 2026-08-11). `THIRD_PARTY_NOTICES.md` (§24) lists every production
dependency requiring attribution (V7's existing stack + the INTEGRATE/ADAPTER repos).

**Boundary rule (the whole point of the boundary):** V9 domain code never depends on an
external repo's internals directly. Every adoption lands behind a **V9-owned interface**
in its consuming phase:

- INTEGRATE/ADAPTER repos are used **behind** V9-owned interfaces — the V9 data model
  (§27) is authoritative (not Qlib's), V9 keeps its own `AIProvider` (§17, not MLflow's
  surface), and V9's `BacktestEngine` + `InternalBacktestEngine` (Phase 7) stand on their
  own so a rejected repo never breaks the system.
- **INTEGRATE (3):** skfolio (Phase 9 portfolio opt), edgartools (Phase 10 SEC EDGAR),
  Optuna (Phase 6/14 hyperparameter search). Permissive licenses verified (BSD-3/MIT);
  pinned in the manifest.
- **ADAPTER (3):** Microsoft Qlib (Phase 6/14 ML/research), FinBERT via the modern
  Transformers pipeline (Phase 10; do NOT use the obsolete `pytorch_pretrained_bert`),
  MLflow (Phase 14 experiment tracking; V9 keeps own `AIProvider`).
- **REFERENCE_ONLY (5):** methodology/concepts only — no code copied, no vendoring, no
  hidden dependency. RD-Agent, Earnings-Volatility-Calculator (§15), RiskLabAI.py (§19 —
  license verified BSD-3-Clause, but prefer published López de Prado methodology + an
  independent V9 implementation), quant-garage (§21, AGPL-3.0), River (detection-only
  initially, may upgrade to ADAPTER in Phase 6).
- **REJECT (2):** vectorbt (Apache-2.0 + Commons Clause fair-code license — §20 license
  gate not granted for a productizable cockpit; V9 stays functional via
  `InternalBacktestEngine`), CCXT (V7 crypto execution via Binance/KuCoin REST is
  sufficient — §23; re-evaluate only if a future phase needs unified multi-exchange live
  execution, subject to the Risk Constitution).

**Phase 2 merged no new production dependency (§118).** Every INTEGRATE/ADAPTER repo is
pinned in the manifest; the consuming phase `pip install`s the pinned version against its
manifest row. The manifest-completeness contract test
(`tests/contract/test_open_source_manifest.py`) guards: no `PENDING`/`TBD` rows; every
INTEGRATE/ADAPTER row has a verified permissive license + a pinned release; every
INTEGRATE/ADAPTER repo appears in `THIRD_PARTY_NOTICES.md`.

## Multi-provider AI platform (Phase 3A)

V9 domain logic talks to the `AIProvider` abstraction (`core/ai/provider.py`, spec §34) — the 8 normalized capabilities `list_models` / `health_check` / `capabilities` / `generate` / `generate_structured` / `tool_call` / `stream` / `usage` — never to a concrete vendor client, so V9 stays vendor-independent (§17). The first concrete provider is `OllamaProvider` (`core/ai/ollama_provider.py`), which **wraps** the existing `core/ollama_client.OllamaClient` singleton (`ollama`) without modifying it (§7 "preserve what works" — `core/ollama_client.py` is untouched; all 13 existing callers and the 8 pinned Ollama test files stay green). `OllamaProvider` lifts the legacy `ollama.generate()` dict contract (`{response, _meta}` / `{error}`) to a typed `AIResponse` for new callers, and adds the new capabilities on top: `generate_structured` (Ollama JSON mode `format=json` + in-prompt schema, parsed), `stream` (Ollama NDJSON), `tool_call` (Ollama `message.tool_calls`), `usage` (per-call token stats), `capabilities`. Local-vs-cloud routing, Bearer auth, TTL caches, and the `/api/chat`-only endpoint remain owned by the wrapped `OllamaClient` — reused, not duplicated (§5).

`config["ai"]` (`core/config.py` + `config.example.json`) is the forward schema: 8 providers (`ollama` + `openai`/`anthropic`/`gemini` + `xai`/`deepseek`/`mistral`/`openrouter` as `openai_compatible`), an empty ModelRegistry (§36, populated in 3B), a 10-role ModelAssignment + `all` alias (§37, wired in 3C), a 3-tier ModelRouter `triage`/`analysis`/`critic` (§38, wired in 3C), a fallback `chain` (GOLD-013, wired in 3C), and `all_unavailable_no_trade: true` (GOLD-014, enforced in 3C). No model names are hardcoded (§35 — `default_model: ""` means discover-then-select via `list_models()`). API keys are read from env vars named by `api_key_env`, never committed, never sent to the frontend, never logged (§106). Zero new dependencies (§118 — `core/ai/` imports stdlib + `core.ollama_client` + `core.http_utils`, both already pinned).

**Phase 3A scope boundary:** 3A delivers the `AIProvider` interface + `OllamaProvider` + `config["ai"]` only. The cloud providers (OpenAI/Anthropic/Gemini/OpenAI-compatible) land in 3B; the ModelRegistry population, ModelAssignment/ModelRouter wiring, `generate_with_fallback`, the `all_unavailable_no_trade` hook, and the §108 AI-config API routes land in 3C. The existing `ollama_decision` decision layer is untouched in 3A and continues to call the `ollama` singleton directly.

### Cloud providers + Model Registry (Phase 3B)

Phase 3B adds the cloud `AIProvider` implementations over the same governed `core/http_utils` `requests` seam — **zero new dependencies** (§118 — the installed `openai`/`anthropic`/`google-genai` SDKs are deliberately NOT used; they are unpinned in `requirements.txt`, so adopting them would violate §25; each provider takes an injectable `http_post`/`http_get` seam for keyless/networkless tests):

- `OpenAIProvider` (`core/ai/openai_provider.py`) — OpenAI Chat Completions (`POST {base_url}/chat/completions`, Bearer auth, `choices[0].message`, `response_format: json_object` for structured, `tools`/`tool_calls`, `usage.prompt_tokens`/`completion_tokens`, SSE `chat.completion.chunk` streaming terminated by `data: [DONE]`; `GET {base_url}/models` for `list_models`/`health_check`).
- `OpenAICompatibleProvider` (`core/ai/openai_compatible_provider.py`) — a trivial subclass of `OpenAIProvider` with the identical wire shape; config-driven `base_url` covers xAI/DeepSeek/Mistral/OpenRouter (§35 "where appropriate").
- `AnthropicProvider` (`core/ai/anthropic_provider.py`) — Anthropic Messages (`POST {base_url}/v1/messages`, `x-api-key` + `anthropic-version: 2023-06-01` headers, NOT `Authorization`; `max_tokens` required; `content[]` `text`/`tool_use` blocks; `usage.input_tokens`/`output_tokens`; SSE `content_block_delta`/`message_stop`).
- `GeminiProvider` (`core/ai/gemini_provider.py`) — Google Gemini (`POST {base_url}/v1beta/models/{model}:generateContent?key=<key>`, `contents`/`parts`, `systemInstruction`, `generationConfig.responseMimeType` for structured, `functionDeclarations`/`functionCall`, `usageMetadata` token counts; `list_models` strips the `models/` prefix).

The verified REST shapes (inspected 2026-08-11 per §25) are pinned by fake-transport unit tests that assert the exact request shape and parse canned responses — no live API calls, no committed keys (§106 — keys via `api_key_env`, only the auth header/query carries the value, never logged, never sent to the frontend). `core/http_utils.py` gains governor entries (concurrency/circuit-breaker/rate-limit) for the 8 new cloud provider names; the derived semaphores/circuit-state/last-request tables pick them up automatically, so no existing provider's governor behavior changes.

`ModelRegistry` (`core/ai/registry.py`, §36) is the catalog of AI models across all providers: a frozen `ModelEntry` with the 10 §36 fields (`provider`, `model_id`, `display_name`, `capabilities`, `tool_support`, `structured_output_support`, `context_metadata`, `health`, `enabled`, `last_checked`), plus `register`/`get`/`enabled_entries`/`update_health`, dynamic discovery via a provider's `list_models()` + `capabilities()`, and persistence to/from `config["ai"]["registry"]` (a list of dicts). No model names are hardcoded (§35 — discovery + manual config + the `""` discover-then-select default).

**Phase 3B scope boundary:** 3B delivers the 4 cloud providers + `ModelRegistry` + the `http_utils` governor entries. The ModelAssignment 10-role wiring (§37), ModelRouter 3-tier wiring (§38), `generate_with_fallback` (GOLD-013), the `all_unavailable_no_trade` enforcement (GOLD-014), and the §108 AI-config API routes land in 3C. `core/ollama_client.py`, `core/ollama_decision.py`, and `core/config.py` are untouched in 3B.

### Assignment + Router + Fallback + §108 API (Phase 3C)

Phase 3C closes the multi-provider platform — wiring the 10-role assignment, the 3-tier router, the fallback chain, the autonomous-trade guard, and the §108 configuration journey. New `core/ai/` modules:

- `assignment.py` — `ModelAssignment` (§37): the 10 roles (`ROLES`) + the `all` alias. `resolve(role)` returns the explicit role model, else the `all` alias for a known role, else `None` (`all` applies only to the 10 known roles, not arbitrary strings). `from_config` reads `config["ai"]["assignment"]`; `validate(model_id, registry)` checks `ModelRegistry` membership (§36).
- `router.py` — `ModelRouter` (§38): the `triage` / `analysis` / `critic` tiers (`TIERS`). `select_model(role, tier_hint)` returns the tier's configured model when set, else `ModelAssignment.resolve(role)` — so triage runs on the fast/cheap tier and only survivors escalate to analysis/critic (§38 "do not spend expensive AI calls on thousands of low-quality candidates").
- `fallback.py` — `generate_with_fallback` (GOLD-013) walks the primary provider (with the requested model) then each fallback provider with **its own** `default_model` (§35 — no cross-provider hardcoded model), returning the first non-error `AIResponse` or the 3A `UNAVAILABLE` sentinel when every provider fails. `no_new_autonomous_trade_guard(response, config)` (GOLD-014) blocks new autonomous trades when `config["ai"]["all_unavailable_no_trade"]` is true (the 3A default; a missing key is treated as `True` — never silently allow a trade) and `response` is `UNAVAILABLE`.

The §108 E2E journey (configure provider → Test Connection → discover models → select role models → save → validated request) is exposed by `api/ai_config_service.py` + `api/main.py` routes: `GET /api/ai/providers`, `POST /api/ai/providers/{name}/test`, `POST /api/ai/providers/{name}/discover`, `GET`/`POST /api/ai/assignment`, `GET`/`POST /api/ai/router`, `POST /api/ai/request`. `build_provider(config, name)` is the factory that constructs the right `AIProvider` subclass from `config["ai"]["providers"][name]["type"]` (`ollama` → `get_ollama_provider()`; `openai`/`anthropic`/`gemini`/`openai_compatible` → the 3B classes) — no model names hardcoded (§35), models come from `list_models()` discovery.

**§106 (keys never to frontend):** `list_ai_providers` returns `has_key: bool` (derived from the env var named by `api_key_env`), never the key value; `ai_validated_request` returns text/model/usage only. The existing `api.config_service.redact_secrets` already masks any `api_key` field; the AI service additionally never reads a key into a response payload. Golden tests `tests/golden/test_ai_fallback_golden.py` prove GOLD-013 (fallback chain serves / all-down → sentinel) and GOLD-014 (guard blocks new autonomous trade when all AI is unavailable).

**Phase 3C scope boundary — the autonomous loop is NOT rewired in 3C.** The existing `core/ollama_decision.py` + `core/agent_manager.py` autonomous decision path (the 13 Ollama callers) are untouched (§5 evolve, §7 preserve). `generate_with_fallback` + `no_new_autonomous_trade_guard` are delivered as a tested platform the autonomous layer *can* consult; rewiring the agent_manager loop to call them (replacing `evaluate_trade_decision_with_ollama`) lands in Phase 4 with the §41 structured decision schema + the independent-critic workflow. The §37 model-assignment **UI** is Phase 12 (3C ships the backend + API only).

**MLflow AI Gateway (§17) — evaluated, not adopted in Phase 3.** The MLflow AI Gateway exposes an OpenAI-compatible endpoint for many models behind one gateway. V9's `OpenAICompatibleProvider` (3B) can point at an MLflow AI Gateway `base_url` with no MLflow dependency, so the multi-provider layer already *accommodates* it. Phase 3 records this evaluation (ADAPTER-noted, deferred); the full MLflow experiment-tracking integration is Phase 14 per the manifest. This honors §17 ("V9 keeps own `AIProvider`") and "may be evaluated as part of the multi-provider layer." No MLflow code or dependency is added in Phase 3.

### Tool Registry + §40 Allow-List (Phase 4A)

Phase 4A builds the §39 tool architecture the AI reasons through — the only AI-callable surface in V9. New `core/ai/tools/` package:

- `tool.py` — frozen `Tool` (name/description/arguments_schema/execute) + `ToolResult` (ok/data/error) value types.
- `registry.py` — `ToolRegistry` (register/get/names/list_tools→vendor payload/dispatch→`ToolResult`); `SPEC_TOOL_NAMES` = the explicit 28-name §39 allow-list (the §40 boundary); `build_default_registry()` assembles the 28 from the domain modules.
- `loop.py` — `run_tool_loop(provider, registry, prompt, *, max_rounds)` generate→dispatch→feed-back primitive (wired into the autonomous cycle behind the §45 autonomy gate in Phase 4C).
- domain modules: `market_data.py` (5), `earnings.py` (4), `options.py` (1, honest Phase-5 stub), `news.py` (2), `sec.py` (1, honest Phase-10 stub), `signals_strategy.py` (4), `backtest.py` (2), `portfolio_risk.py` (5, §43-authoritative), `history.py` (2), `order_proposal.py` (2, draft-only).

**§40 (no unrestricted access) is enforced by construction.** The registry refuses any tool whose name is not in `SPEC_TOOL_NAMES`, so the AI can never reach a shell/Python/SQL/filesystem/HTTP/secrets/broker surface — none of those names is in the 28. `propose_order`/`propose_close` return **drafts only** (§42 — AI prose alone never triggers an order); they never call `CapitalManager.open_position`/`close_position` or `ExecutionAgent.process_signal` (the golden test monkeypatches those to raise and asserts the drafts still return without reaching them).

**§43 (app-authoritative numbers) is enforced by delegation.** `get_available_capital` delegates to `capital_manager.get_deployable_balance`; `calculate_allowed_position_size` delegates to `position_sizing.compute_size_factor` + `risk_limits.calculate_trade_risk_pct`; `get_portfolio_risk` delegates to `RiskAgent.evaluate_signal`. The AI never computes capital/size/risk independently. A draft order embeds the authoritative available capital + size + loss-at-stop % from those services, not AI-supplied numbers.

**§5/§7 (evolve, preserve).** The 21 tools with existing delegates are thin read-only wrappers over existing service functions (`api/state_service.get_state`, `api/market_data_service.YahooClient.fetch_history`, `core/strategy_v2.context_builder.build_context`, `core/strategy_v2.regime_engine.RegimeEngine.detect`, `agents/economic_calendar_agent`, `core.finnhub_client`, `agents/pre_earnings_agent`, `agents/news_tab_agent`, `core/signals`, `core/strategy_v2.registry`, `api/actions_service`, `core/capital_manager`, `agents/risk_agent`, `core/execution_archive`). No existing file's behavior changed; `ollama_client`/`ollama_decision`/`agent_manager`/`config` are untouched (empty diffs `7209da0..HEAD`). Net-new seams: `get_sector_regime` (per-ticker regime rollup), `get_earnings_surprises` (EPS actual−estimate), `get_signal` (by-id over `SignalManager`), `get_options_analysis` (Phase-5 stub), `get_sec_context` (Phase-10 stub), `propose_order`/`propose_close` (draft-only).

**Phase 4A scope boundary — the autonomous loop is NOT rewired in 4A.** The tool registry + dispatch loop are delivered as a tested platform the autonomous layer *will* consult. Wiring `agent_manager` to call `generate_with_fallback` + `no_new_autonomous_trade_guard` + `run_tool_loop` behind the §45 autonomy gate, plus the §41 structured decision schema, the §42 validation pipeline, the §44 independent critic, and §45/§46/§48 autonomy-mode enforcement, lands in Phase 4B/4C. Golden tests `tests/golden/test_ai_tool_registry_golden.py` prove the §40 allow-list (28 names, no forbidden surface), §42 draft-only, §43 authoritative delegation, and an end-to-end dispatch loop with a fake provider.

## Phase 4B — §41 decision schema + §42 validation pipeline + §43 non-authoritative numbers

New pure-additive modules under `core/ai/` (no existing file's behavior changes; the `ollama_decision`→`generate_with_fallback` bridge and the `agent_manager` rewire are Phase 4C):

- **`core/ai/decision.py`** — `Action` enum (7 §41 actions: WATCH/NO_TRADE/OPEN_LONG/OPEN_SHORT/HOLD/REDUCE/CLOSE), frozen `Decision` dataclass carrying the §41 fields only, and `parse_decision(raw, *, strict_schema=False)` which evolves the legacy `ollama_decision.parse_trade_review` brace-match + coerce pattern into the §41 schema. §43 authoritative fields (`price`/`pnl`/`iv`/`eps`/`portfolio_value`/`allowed_capital`/`position_size`) are stripped from the schema and recorded in `Decision.rejected_fields`; in `strict_schema` mode their presence raises `DecisionSchemaError`.
- **`core/ai/pipeline.py`** — `run_pipeline(raw_decision, signal, positions, *, config, services=None)` runs the §42 stages `PARSE → SCHEMA → SEMANTIC → AUTHORITATIVE_DATA_CHECK → POLICY → RISK_GATE` in that exact order. The first failing stage short-circuits into a `ValidationResult(ok=False, stage=<that stage>)`; AI prose alone yields NO_TRADE at PARSE and never orders. The stages **compose** the existing functions unchanged: `parse_decision` (PARSE/SCHEMA), `core.capital_entry_guard.evaluate_capital_entry` + `evaluate_autonomous_performance`/`evaluate_autonomous_news` (called inside it) + §43 recalc (AUTHORITATIVE), `agents.risk_agent.RiskAgent.evaluate_signal` (POLICY), and `core.ollama_decision.should_veto_trade` via a `Decision → review` adapter (RISK GATE). Stages and §43 service callables are injectable via `services` (design decision 8) so §42/§43/§44 are unit-tested with fakes — no live market/broker/AI.
- **§43 convention** — AI-supplied authoritative numbers are NOT carried in the `Decision`. App-calculated values live in `ValidationResult.checks["authoritative"]` (`allowed_capital` from `resolve_deployable_balance`, `size_factor` from `compute_size_factor`, `trade_risk_pct` from `calculate_trade_risk_pct`, `portfolio_risk` from `RiskAgent.evaluate_signal`). The RISK GATE veto (§44 valid final result can be NO_TRADE) forces the decision to `Action.NO_TRADE` while the pipeline result stays `ok=True`.
- **Config** — `DEFAULT_CONFIG["ai"]["decision"]` (`strict_schema`/`critic_enabled`/`critic_force_no_trade_on_fail`), mirrored in `config.example.json`, backfilled by `ConfigManager.load`'s `deep_merge`. The §44 critic *workflow* and §45/§46/§48 autonomy enforcement + `agent_manager` rewire are Phase 4C; 4B ships the config so the pipeline can read it.
- **Golden** — `tests/golden/test_ai_decision_pipeline_golden.py` (GOLD-042 ordering + prose-never-orders + vetoed→NO_TRADE; GOLD-043 strip/recalc + strict-schema reject). Unit cover in `tests/unit/test_ai_decision.py` + `tests/unit/test_ai_pipeline.py` + `tests/unit/test_ai_decision_config.py`.

## Phase 4C — §44 critic + §45/§46/§48 autonomy + agent_manager rewire

Phase 4C composes the 3C/4A/4B AI platform into the live autonomous decision loop without rewriting it (§5/§7). Approach A: a bridge engine + additive gates, preserving the `review`-dict contract `agent_manager` already consumes.

- **`core/ai/decision_engine.py`** — the bridge. `evaluate_trade_decision(signal, risk_result, *, config, services=None) -> EvaluationResult` composes `generate_with_fallback` (3C, GOLD-014) → `run_pipeline` (4B §42) → `run_critic` (4C §44, post-pipeline) and returns the §41 `Decision`, §42 `ValidationResult`, §44 `CriticVerdict`, and a backward-compatible `review` dict. `STAGE_ORDER` stays frozen at 6 (GOLD-044) — the critic is NOT a 7th stage; it runs only on non-`NO_TRADE`/`WATCH` outcomes.
  - **Review-shape distinction (preserves §115)** — malformed output (PARSE/SCHEMA/SEMANTIC) + AI-unavailable → `available=False` (the legacy UNAVAILABLE path: reject if AI-required, else signal-only paper fallback). Decisive rejection (POLICY gate, `NO_TRADE`, RISK_GATE veto, critic veto) → `available=True`, `recommendation="avoid"` (the veto path). A policy rejection NEVER falls through to paper-only fallback.
  - **§43 leverage-default-1** — the engine always returns `leverage=1`; AI no longer supplies leverage. `agent_manager` caps it via `execution_cfg["max_ai_leverage"]`.
- **`core/ai/critic.py`** — §44 independent critic. `run_critic(decision, signal, *, config, services=None) -> CriticVerdict` challenges on 10 categories (contradictory_evidence/stale_information/provider_conflict/weak_backtest/fragile_parameters/poor_sample_size/excessive_volatility/concentration/catalyst_risk/regime_mismatch) using the router's `critic` tier via `generate_with_fallback`. §40: reviews provided evidence only — no §39 tool loop. `veto=True` forces `NO_TRADE`. `critic_force_no_trade_on_fail` (default True) fail-closes on provider/parse failure.
- **`core/autonomy.py`** — §45 `AutonomyMode` (OFF/ANALYZE/RECOMMEND/PAPER_AUTO/LIVE_ASSISTED/LIVE_AUTO; default ANALYZE, never LIVE_AUTO), `resolve_mode` (unknown→ANALYZE fail-safe), `mode_allows_paper_orders`/`mode_allows_live_orders`, §74 `kill_switch_active`, §45 `live_auto_owner_enabled`, §48 `assert_not_weakening` (PROTECTED_TARGETS + _WEAKEN_VERBS; owner bypass explicit). §46: `PAPER_AUTO_SCENARIOS` (8-scenario deterministic battery) + `run_paper_auto_acceptance`/`paper_auto_acceptance_passed` (CI/owner action path) + `can_enable_live_auto` (owner flag AND recorded `paper_auto_acceptance_passed` flag; with `services` the battery re-runs, at runtime the recorded flag is consulted — the loop never re-runs live AI to authorize its own promotion).
- **`core/ollama_decision.py`** — `evaluate_trade_decision_with_ollama` now delegates to the engine and returns `result.review`; injectable `services` (tests inject fakes, no network — §115). Legacy helpers (`should_veto_trade`/`parse_trade_review`/`default_trade_review`/`build_trade_review_prompt`) unchanged.
- **`core/agent_manager.py`** rewire — (1) `_autonomy_order_gate` (§45 mode + §74 kill switch + LIVE_AUTO via `can_enable_live_auto`) gates both autonomous order paths: the main `on_new_signal` path before the AI review block, and `_attempt_research_cohort_entry` after its data gate. Fail-safe: unreadable autonomy config → ANALYZE → blocks both paper and live. (2) The main AI call site calls the engine directly and enriches `signal_data`/`decision_context` with `ai_decision`/`ai_validation`/`ai_critic` (additive §41/§42/§44 audit). The research-cohort call site keeps the delegate so existing mock contracts hold.
- **Config** — `DEFAULT_CONFIG["autonomy"]` (`mode`/`kill_switch_active`/`live_auto_owner_enabled`/`paper_auto_acceptance_required`/`paper_auto_acceptance_passed`), mirrored in `config.example.json`, backfilled by `deep_merge`.
- **Golden** — `tests/golden/test_phase4c_golden.py` (GOLD-044 critic post-pipeline + STAGE_ORDER=6 + veto→NO_TRADE/avoid; GOLD-045 default ANALYZE + OFF/ANALYZE/RECOMMEND forbid orders + LIVE_AUTO owner-only + kill switch; GOLD-046 LIVE_AUTO disabled without owner + enabled with owner+acceptance + runtime recorded-flag gate). Unit cover in `tests/unit/test_autonomy.py` + `tests/unit/test_ai_critic.py` + `tests/unit/test_ai_decision_engine.py` + `tests/unit/test_ollama_decision_delegate.py` + `tests/unit/test_agent_manager_rewire.py`; acceptance in `tests/acceptance/test_paper_auto_acceptance.py`.

## Phase 5 — Pre-Earnings V9 (§49/§50/§51/§93/§109)

Phase 5 evolves the V7 Pre-Earnings scoring surface into a §49-§51 decision system, composing the Phase 4C engine + autonomy (evolve-not-rewrite, §5/§7). Three sub-phases: 5A (§50 triple), 5B (§51 decision + autonomy), 5C (§93 UI + §109 E2E).

- **`core/pre_earnings/score.py`** — §50 PRE-EARNINGS SCORE 0-100 + DATA COVERAGE + DATA CONFIDENCE. Pure, stdlib, no I/O. `compute_pre_earnings_score(...) -> PreEarningsScoreResult` evolves the V7 `_compute_opportunity_score` formula (`signal_strength = 0.65*gap_risk + 0.35*|direction_bias|) * (coverage/100)`, exact back-compat when `weight_overrides` is None) into the canonical score with VISIBLE components (`gap_risk`, `direction_bias_magnitude`, `coverage_modulator`). `is_not_probability` pinned True (§50 — the score is a signal-strength×coverage composite, never P(up)). DATA COVERAGE delegates to the existing `_compute_analysis_coverage` (6 weighted components, unchanged). `compute_data_confidence(...)` evolves the V7 `historical_samples` bands so coverage is a DOWNGRADE-ONLY modifier (HIGH needs ≥3 quarters AND coverage ≥45; coverage can lower HIGH→MEDIUM but never raises LOW→MEDIUM); `is_insufficient` stays True only at `historical_samples==0`, preserving the V7 `is_insufficient_data` semantics used by the NEWS_ONLY_CALL guard and `gap_risk_unavailable`.
- **`core/pre_earnings/decision.py`** — §51 decision context + engine bridge + autonomy clamp. `build_pre_earnings_decision_context(item, *, services=None)` assembles the §51 input stack (Pre-Earnings + strategy evidence + signal + backtest + AI + critic + data quality + portfolio + risk + capital); later-phase inputs are HONEST STUBS (`available:False`+reason) for Phases 6/7/8/9/10 — never faked numbers (§4/§43). `evaluate_pre_earnings_decision(item, *, config, services=None) -> EvaluationResult` bridges to `core.ai.decision_engine.evaluate_trade_decision` and applies the autonomy clamp. Per §109 (Decision→Risk→Capital→Paper order), the capital/risk §42 stages that depend on Phase 9 are injected as permissive stubs here (`_pe_default_services`: `evaluate_capital_entry` allowed, `risk_agent.evaluate_signal` accepted) so the DECISION reflects the AI+critic+policy+autonomy conclusion; the REAL capital/risk gating stays in `agent_manager`'s order path (its own real guards + the 4C autonomy order gate). Clamp semantics: AI-unavailable→WATCH ("keep watching", NOT a decisive NO_TRADE); only ORDER-bearing actions (OPEN_LONG/OPEN_SHORT) are gated — clamped to WATCH when `kill_switch_active`, mode forbids paper orders, or LIVE_AUTO lacks `can_enable_live_auto`; NO_TRADE (critic veto/policy) and WATCH/HOLD/REDUCE/CLOSE pass through (a decisive NO_TRADE is never relaxed to WATCH). The returned `EvaluationResult.decision` is the authoritative final action (from `review.decision`, possibly clamped) so callers read `result.decision.action`.
- **`agents/pre_earnings_agent.py`** (additive) — `_build_item_for_symbol` delegates the inline `data_confidence` band block + `_compute_opportunity_score` call to `compute_data_confidence`/`compute_pre_earnings_score` (the band block moves after `coverage_score` since the evolved bands take coverage as an input) and additively emits `pre_earnings_score`/`pre_earnings_score_label`/`pre_earnings_score_components`/`pre_earnings_is_not_probability` (back-compat `opportunity_score` preserved, equal to the canonical score). `run_cycle` calls `_attach_pre_earnings_decisions(cfg)` after `_attach_daily_ranking` — gated by `pre_earnings.decision_enabled` (default False: ship wired-but-off, owner flips to activate) and bounded by `decision_max_tickers` (default 25); attaches `item["pre_earnings_decision"]` (action/recommendation/confidence/available/reason/validation_stage/critic_veto). The 11,857-line body is otherwise untouched.
- **Config** — `DEFAULT_CONFIG["pre_earnings"]` gains `decision_enabled: False` + `decision_max_tickers: 25`, mirrored in `config.example.json`.
- **Golden/acceptance** — GOLD-050 (frozen score formula + not-probability invariant + band boundaries) in `tests/golden/test_phase5a_golden.py`; GOLD-093 (§93 scanner column contract; IV/RV + strategy + risk status are documented HONEST GAPS deferred to Phases 6/8/9/10) in `tests/golden/test_phase5c_golden.py`; §51 acceptance (7-action enum reachable per autonomy mode) in `tests/acceptance/test_pre_earnings_autonomy.py`; §109 E2E journey (candidate→Decision Ledger, fake services) in `tests/acceptance/test_pre_earnings_e2e_journey.py`. Unit cover in `tests/unit/test_pre_earnings_score.py` + `test_pre_earnings_agent_score_integration.py` + `test_pre_earnings_decision.py` + `test_pre_earnings_run_cycle_decision.py`.

## Phase 6 — Signal & ML Engine (§52/§53/§54/§55/§56 + market-wide regime)

Phase 6 is a curation + greenfield surface over the existing V1/V2 signal pipelines, composing — not rewriting — `core/scoring_system`, `core/strategy_v2`, the §39 regime tools, and the agents (§5/§7). Three sub-phases: 6A (§52 catalog + §53 horizons + market-wide regime + Signal type), 6B (§54 ML registry/baseline/OosGate seam + §55 calibration + §56 meta-labeling), 6C (§51 signal-input wiring + §39 regime-tool market-wide path + config). §52 does NOT add indicators merely because V7 has them — it preserves the useful ones and DEFERS news/sentiment to Phase 10.

- **`core/signal_engine/horizons.py`** — §53 Signal Horizons. `SignalHorizon` enum (SHORT_TERM 1-5 sessions / SWING 5-20 / MEDIUM_TERM 20-60), `HorizonDefinition` (sessions_min/max, `target_condition_template="P(return > 0 over next {n} sessions)"`, feature_timeframes), `HORIZON_DEFINITIONS` (SHORT_TERM {1m,5m,15m}, SWING {15m,1h,4h}, MEDIUM_TERM {1h,4h,1d}). `map_mode_to_horizon(mode)` maps V1 modes (PULLBACK/BREAKOUT→SHORT_TERM, MOMENTUM/MEAN_REVERSION→SWING) + 8 V2 strategy specs; unknown→SWING fail-safe. `align_horizon(horizon, feature_timeframe)` guard so features align with the horizon.
- **`core/signal_engine/catalog.py`** — §52 Signal Catalog. `IndicatorStatus` (PRESERVED/EVALUATE/DEFERRED) + frozen `IndicatorSpec(name, source_module, status, horizon_fit, incremental_value_note)`. `SIGNAL_CATALOG` = the §52 list 1:1 (trend, momentum, rsi, macd, moving_averages, relative_strength, volume, rvol, atr, volatility, market_regime, sector, earnings, news, sentiment, ml): news/sentiment DEFERRED to Phase 10, ml EVALUATE (simple baseline first). `get_catalog_index()` / `preserved_indicators()`. A curation/surface phase — no indicators added merely because V7 has them.
- **`core/signal_engine/regime.py`** — Market-wide regime (the V7 gap). A THIRD taxonomy `MARKET_RISK_ON/RISK_OFF/MIXED/TRANSITIONING` distinct from V1 (TREND/RANGE/LOW_VOL/VOLATILE/MIXED) and V2 (REGIME_TREND/RANGE/HIGH_VOL/EVENT_RISK/SESSION_BREAKOUT) — neither V1 nor V2 is touched. `MarketRegimeResult` frozen (label, breadth_pct, distribution, sample_size, is_stale, is_not_probability=True). `MarketRegimeEngine(universe_min=10).detect_market(universe_regimes)` rolls up per-ticker regimes (≥0.60 RISK_ON, ≤0.35 RISK_OFF, else MIXED or TRANSITIONING by mean breadth; <universe_min → MIXED + stale). `detect_sector` thin wrapper; `detect_macro_regime() -> {"available": False, "reason": "macro_vix_data_source_deferred"}` honest stub (V7 feed is crypto/1m; VIX/macro is a documented gap).
- **`core/signal_engine/types.py`** — `Signal` frozen dataclass (ticker, horizon: SignalHorizon, direction, score, signal_type, source, catalog_refs, raw, is_not_probability=True). `Signal.from_dict(d, *, horizon=None)` resolves the horizon via `map_mode_to_horizon(mode/strategy_id/signal_type)`; `to_dict()` adds horizon/signal_horizon/direction/score/signal_type/source/catalog_refs/is_not_probability to raw. Composes the V1/V2 signal dicts, does not replace them.
- **`core/ml/registry.py`** — §54 Model Registry. `TargetSpec(horizon, condition)`; `OosVerdictLabel` (PENDING/DEFERRED_PHASE7/ROBUST/MODERATE/FRAGILE/INSUFFICIENT_EVIDENCE); `ModelRecord` frozen with the 10 §54 fields (model/version/features/feature_version/target/horizon/training_range/validation_range/test_range/trained_at) + `is_simple_baseline` + `oos_verdict` (PENDING default). `OosGate.evaluate_oos_improvement(baseline, challenger, backtest_runner=None)` → DEFERRED_PHASE7 when no runner (OOS backtest is Phase 7 §57, so Phase 6 ships the OosGate seam, not the backtest). `ModelRegistry.register` raises if a complex model registers with a promoting verdict (complex models must go through `promote()` with a real available `OosVerdict` in {ROBUST, MODERATE}); simple baselines register as PENDING.
- **`core/ml/baseline.py`** — §54 simple baseline. `SimpleBaselineModel(learning_rate=0.1, epochs=300).fit(X, y) -> TrainedBaseline` (stdlib logistic gradient descent; numpy/pandas already in V7 — zero new deps §118). `train_baseline(features, outcomes, *, feature_names, horizon, target_condition, training_range, validation_range, test_range, trained_at, ...) -> (TrainedBaseline, ModelRecord)` with `is_simple_baseline=True`, `oos_verdict=PENDING`. Complex models must prove OOS improvement before promotion (§54); the simple baseline is the default.
- **`core/ml/calibration.py`** — §55 Calibration. `ProbabilityDefinition(horizon, condition, calibration_method="none")` — the precise thing a probability means, e.g. `P(return > 0 over next N sessions)`. `CalibrationResult` frozen (method, raw_range, calibrated_range, sample_size, is_statistical_probability, definition, mapping). `CalibrationResult.from_llm_confidence(...)` RAISES `CalibrationError` — LLM confidence is NEVER displayed as a statistical probability (§55, enforced in code). `CalibrationResult.uncalibrated(definition)` → `is_statistical_probability=False`. `_isotonic_pav` PAV (stdlib) backs `calibrate(raw_scores, outcomes, *, definition, method="isotonic")` → a statistical probability with a step mapping; `apply_calibration(raw_score, calibration)` step-interpolates (pass-through when not statistical).
- **`core/ml/meta_label.py`** — §56 Meta-labeling. `MetaLabel(primary, meta, final)`. `MetaLabeler.combine(primary, meta)` — DO_NOT_EXECUTE/INSUFFICIENT → NO_TRADE; EXECUTE → primary (LONG/SHORT); unknown → NO_TRADE fail-safe (primary LONG + meta DO_NOT_EXECUTE → final NO_TRADE is valid). `evaluate_meta_labeling(primary_predictions, meta_predictions, outcomes, *, positive_label="LONG") -> MetaLabelEvaluation` (primary/meta precision+recall, sample_size, meta_labeling_improves_precision).
- **`core/ml/qlib_adapter.py`** — §54 Qlib ADAPTER (Phase 2 manifest decision). `is_available()` import-guarded; `adapt() -> {"available": False, "reason": "qlib_not_installed_owner_opt_in"}` when not installed. Owner opts in by installing Qlib; zero new deps by default (§118).
- **`core/scoring_system.run_weight_calibration_audit`** (additive) — the V7 "Module 5/6" stub body now delegates the calibration label to `core.ml` (`CalibrationResult.uncalibrated`), keeping every ORIGINAL dict key (status, current_weights, suggested_changes, confidence_score, summary) and ADDING `available: False`, `is_statistical_probability`, `calibration_method`. The hand-coded `logistic_coeffs` path is untouched.
- **`core/pre_earnings/decision.py`** (additive, 6C) — the §51 `signal` input evolves from the Phase 6 honest stub to a real `Signal` overlay (`_signal_input`) built from the item when it carries signal fields (horizon resolved via `map_mode_to_horizon`); honest stub when absent. `is_not_probability` is always True (§55). All other §51 inputs unchanged.
- **`core/ai/tools/market_data.py`** (additive, 6C) — `make_get_market_regime_tool(*, detect, market_engine=None)` gains a `universe_regimes` path that delegates to `MarketRegimeEngine.detect_market` (§49 market-wide breadth). The per-ticker single-context path stays the default (back-compat); `market_engine` is lazy-imported + injectable.
- **Config** — `DEFAULT_CONFIG["signal_engine"] = {"market_regime_universe_min": 10}` (§49) and `DEFAULT_CONFIG["ml"] = {"registry": [], "qlib_enabled": False, "baseline_feature_version": "v1"}` (§54), mirrored in `config.example.json`, additively after `autonomy`.
- **Out of scope** — §57 OOS backtest → Phase 7 (Phase 6 ships the `OosGate` seam, `evaluate_oos_improvement` returns DEFERRED_PHASE7 with no runner); §63 strategy registry → Phase 8; news/sentiment indicators → Phase 10 (DEFERRED in the catalog); macro/VIX regime → honest stub. ML/calibration/meta-labeling are greenfield; no V1/V2 signal pipeline is rewritten.
- **Golden** — GOLD-052/053 (`tests/golden/test_phase6a_golden.py`: catalog §52 list 1:1 + horizons §53 mapping/align/market-wide regime + Signal type); GOLD-054/055/056 (`tests/golden/test_phase6b_golden.py`: 10 §54 fields + simple-baseline-trains-PENDING + OosGate DEFERRED_PHASE7 + complex-cannot-register-promoted + calibration-is-statistical-with-definition + LLM-confidence-raises + uncalibrated-not-statistical + §56 example + meta-improves-precision). Unit cover in `tests/unit/test_signal_engine.py` + `tests/unit/test_ml_registry_baseline.py` + `tests/unit/test_ml_calibration_meta.py` + `tests/unit/test_pre_earnings_decision.py`.

## Phase 7 — Backtest & Validation Engine (§57/§58/§59/§60/§61)

`core/backtest/` is the stable domain boundary over the retained V7 engines:

```text
BacktestConfig (next-bar-open, no-look-ahead pinned)
        |
        +--> FaithfulReplayAdapter ------> strategy_backtester.backtest
        +--> ExploratoryEngineAdapter ---> backtest_engine.BacktestEngine
        +--> LegacyBacktesterAdapter ----> backtester.Backtester (trades fail closed: same-bar)
        |
        v
BacktestResult + complete BacktestMetrics
        |
        +--> DatasetSplit: TRAIN / VALIDATION / OOS / FINAL_HOLDOUT
        +--> WalkForwardResult (OOS aggregate + separate untouched holdout)
        +--> OverfittingBattery (purge/embargo, stability, sensitivity, PBO, DSR, Holm)
        +--> OosGateRunner ------> core.ml.registry.OosGate
        +--> EvidenceResolution -> TICKER / INDUSTRY / SECTOR / UNIVERSE / NO STRATEGY
```

The faithful adapter is the authoritative default. It slices replay context through the OOS end and retains only trades whose entry and exit fall inside the requested OOS range. The exploratory adapter remains non-authoritative. The legacy V1 engine enters at the signal bar close; the adapter therefore rejects non-empty results under the V9 §57 interface.

Every role range is a half-open bar-index interval. `DatasetSplit` enforces chronology and pairwise disjoint roles. Rolling training windows may overlap across iterations; OOS windows may not. FINAL_HOLDOUT is scored separately and cannot be carved twice or overlap evaluated OOS data.

`OosGateRunner` accepts precomputed authoritative evidence or executes an injected `BacktestConfig`. Missing data, a non-authoritative result, insufficient trades, or unavailable profit factor yields `INSUFFICIENT_EVIDENCE`; a challenger that does not beat baseline OOS expectancy is `FRAGILE`. `ROBUST` requires PBO, DSR, stability, profit factor, and drawdown to all clear named thresholds.

The live §39 `run_backtest` tool and `actions_service.run_backtest` remain unchanged for backward compatibility. Phase 8 now owns strategy persistence and evidence-backed promotion; live degradation monitoring and rollback remain Phase 16 responsibilities.

## Phase 8 — Strategy lifecycle boundary (sections 62/63/64)

`core/strategy_registry/` is governance state; `core/strategy_v2/registry.py` remains the executable catalog and `StrategyPerformanceCache` remains rolling per-symbol performance. These surfaces compose and do not duplicate one another.

```text
V2 StrategySpec --explicit seed--> RESEARCH -> CHALLENGER -> SHADOW
                                                       |        |
Phase 7 OOS/holdout evidence --------------------------+        v
                                                  promotion policy
                                                       |
                                      APPROVED <-> CHAMPION -> DEGRADED
                                                       |
                                                    RETIRED
```

- `StrategyRegistry` persists the exact 12 strategy fields and append-only audit events through schema-versioned atomic JSON replacement. A corrupt file fails closed. There is no delete operation and there can be at most one CHAMPION for each `horizon|sorted-universe` scope.
- `promote()` is the only path to CHAMPION. It requires every locked multi-factor gate and atomically moves an incumbent champion to APPROVED when a better challenger is accepted. A direct registration/transition, an AI explanation, or one superior backtest cannot promote.
- The V2 bridge maps all eight current executable definitions to RESEARCH only. Selector enforcement is injectable and defaults off; when enabled, only an APPROVED/CHAMPION lifecycle version can reach an evaluator.
- `optuna_policy` is the Phase 14 integration guard: studies must be research-only and persistent, use bounded multi-objective risk-aware search, exclude FINAL_HOLDOUT, and never auto-promote the best trial.
- Phase boundary: Phase 8 stores SHADOW state and evidence but does not create live shadow observations. Monitoring, degradation triggers, rollback, and UI are Phase 16/12.

## Phase 9 - Capital and Risk Constitution boundary (sections 47/48/65/66/67)

`core/portfolio/` is a deterministic, immutable boundary over the retained V7 capital implementation:

```text
SizingRequest -> sizing formula -> requested notional
                                      |
CapitalManager state -> V7 adapter -> PortfolioSnapshot + OrderProposal
                                      |
owner-confirmed Risk Constitution -> complete constraint decision
                                      |
RiskAgent legacy checks (still active; never weakened)
                                      |
CapitalManager open/close accounting + persisted position evidence
                                      |
position reassessment -> HOLD / REDUCE / CLOSE decision
```

- `types.py`, `sizing.py`, and `accounting.py` provide finite immutable contracts, the five section 65 sizing baselines, and exact open/close conservation identities.
- `constraints.py` validates all protected values and evaluates kill switch, strategy evidence, freshness/conflicts, leverage, count, position/exposure/cash/sector/correlation, stop risk, liquidity, daily loss, drawdown, and live permission without short-circuiting audit evidence.
- `adapter.py` converts application-authoritative V7 capital state into the new contracts. Missing sector, liquidity, cost, timestamp, correlation, or required lifecycle evidence fails closed after owner activation.
- `position.py` produces the eight section 67 evidence fields and deterministic reassessment decisions. Kill Switch prevents new risk but does not itself liquidate an existing position.
- `RiskAgent` invokes the new constraint engine only for a complete `owner_confirmed: true` mapping, then continues through every legacy gate. Missing/unconfirmed configuration preserves compatibility; malformed confirmed configuration rejects.
- `CapitalManager` remains the execution and ledger owner. Its open/close arithmetic is pinned against the pure Phase 9 accounting contract.

Owner configuration uses an explicit two-state control plane. `risk_constitution_draft` is editable and may be incomplete; it never participates in trade decisions. `risk_constitution` is replaced only by an explicit, complete owner confirmation. The Agents UI receives its field schema from `api/agents_service.py`, renders null values as empty, and sends the complete strict shape to `PATCH /api/agents/risk/constitution`. Activation and synchronization of overlapping V7 limits happen in one atomic config write; later draft changes preserve the active document until reconfirmed.

The protected limits are never activated by code. `risk_constitution` ships as a null-valued template with `owner_confirmed: false`. Phase 12 adds validated starting templates from the external `risk_constitution_profiles.json` document (optionally overridden by `MS_RISK_CONSTITUTION_PROFILES_PATH`), but choosing one only hydrates the browser form; it does not write config. Manual edits become `Custom`, and only the owner-confirmation PATCH replaces the active policy.

## Phase 12 - AI Trading Cockpit read model (sections 91-100)

`api/cockpit_service.py` composes existing bounded service snapshots; it does not call providers or become a new system of record. `/api/cockpit` uses a short service cache, a stable content version and unchanged polling. Each visual section carries an explicit async state. The frontend renders the five workspaces and routes actions back to their authoritative V9 pages/endpoints.

System Health is evidence-based: SQLite uses a read-only `SELECT 1`, workers use lifecycle/error evidence, provider tiles use verified timestamps/source metadata, and missing integrations remain `UNAVAILABLE` or `NOT_CONFIGURED`. There is no green default.

## Phase 10 - News / FinBERT / SEC boundary (sections 14/16/68/69)

```text
V7 News providers -> normalize/entity/category/dedup -> pinned FinBERT
                                             |              |
                                             |              +-> labelled lexicon fallback
                                             +-> PIT evidence ledger
                                             +-> external-text quarantine -> AI tool

EDGAR_IDENTITY -> edgartools -> canonical SECFiling -> atomic TTL/PIT cache
                                                    +-> /api/sec/{ticker}
                                                    +-> get_sec_context tool
```

- V7's operational News fetchers, state, persistence, and UI remain authoritative. `core/news_v9/pipeline.py` defines the deterministic replay/test contract over their provider mappings, while `NewsTabAgent` is wired to the new sentiment service.
- FinBERT uses Transformers 5.15.0 + torch 2.13.0 and immutable model revision `4556d13015211d73dccd3fdd39d39232506f3e43`. Loading is lazy and local-cache-first. Model/runtime failure is observable; only the separately labelled V7 lexicon may fall back.
- SEC metadata is US-equity-only, bounded, cache-first, and read-only. No identity means explicit unavailable. A stale cache remains usable and labelled. Historical `as_of` reads exclude records whose `available_from` is later than T.
- `core.external_text` treats every News/SEC string as data. Suspicious role boundaries, override directives, secret requests, and command/tool requests are replaced before provider prompt composition. The tool allow-list and validated decision pipeline remain the independent downstream safety barriers.

## UI → client → API → service → provider map (spec §101)

| UI tab (frontend/) | client (data_store.js) | API route (api/main.py) | service (api/*_service.py) | provider (core/) |
|---|---|---|---|---|
| Signals | fetch /api/state | /api/state | api/state_service.py | core/engine.py, core/signals.py |
| Watchlist | fetch /api/watchlist | /api/watchlist* | api/state_service.py | core/state_persistence |
| PreMarket | fetch /api/premarket | /api/premarket | api/state_service.py | agents/premarket_scanner_agent |
| PreEarnings | fetch /api/pre-earnings | /api/pre-earnings* | api/state_service.py | agents/pre_earnings_agent |
| Capital | fetch /api/capital | /api/capital* | api/state_service.py | core/capital_manager |
| News | fetch /api/news-tab/* | /api/news-tab/* | api/state_service.py | agents/news_tab_agent, us_market_news_agent |
| SEC (UI: Phase 12) | — | /api/sec/{ticker} | api/sec_service.py | core/sec/service.py, core/sec/edgar_adapter.py |
| Calendar | fetch /api/calendar | /api/calendar* | api/state_service.py | agents/economic_calendar_agent |
| Agents | fetch /api/agents/control-plane | /api/agents*, /api/agents/risk/constitution | api/agents_service.py | core/agent_manager, core/portfolio/constraints.py |
| AI Cockpit | fetch /api/cockpit | /api/cockpit | api/cockpit_service.py | bounded existing V9 services/read models |
| Settings | fetch /api/config | /api/config* | api/config_service.py | core/config.py |
| AI Settings (UI: Phase 12) | — (backend + API in Phase 3C) | /api/ai/providers, /api/ai/providers/{name}/test, /discover, /api/ai/assignment, /api/ai/router, /api/ai/request | api/ai_config_service.py | core/ai/* (provider, assignment, router, fallback, registry, providers) |

Unresolved critical routes at Phase 0: none known (V7 audit §4 verified the frontend→backend contract clean: 60+ URLs, zero mismatches). To be re-verified per phase as new routes are added.
