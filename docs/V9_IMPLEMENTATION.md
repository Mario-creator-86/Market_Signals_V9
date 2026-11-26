# V9_IMPLEMENTATION — Operational Source of Truth

Market Scanner V9 = direct, tested evolution of V7 (migrated into the V9 directory; V7 is a read-only source of record). Spec basis: `prompt_to_follow.md`.

Allowed statuses (spec §9): `NOT STARTED | IN PROGRESS | BLOCKED | FAILED | PASS`. PASS requires actual execution.

## Phases

| Phase | Title | Status |
|---|---|---|
| 0 | V7 Full Audit (existing V7 audit reused) + V7→V9 migration + baseline + V9 control docs | PASS |
| 1 | Data & Architecture Foundation (canonical data, point-in-time, data quality) | PASS |
| 2 | Open-Source Integration Foundation (manifest evaluations) | PASS |
| 3 | Multi-Provider AI Platform | PASS |
| 4 | AI Tool & Decision Engine | PASS |
| 5 | Pre-Earnings V9 | PASS |
| 6 | Signal & ML Engine | PASS |
| 7 | Backtest & Validation Engine | PASS |
| 8 | Strategy Registry / Champion-Challenger | PASS |
| 9 | Capital / Portfolio / Risk | PASS |
| 10 | News / FinBERT / SEC | IN PROGRESS |
| 11 | Autonomous Paper Trading | PASS |
| 12 | AI Trading Cockpit UI | PASS |
| 13 | Decision Ledger / Model Lab | IN PROGRESS |
| 14 | Research Factory | IN PROGRESS |
| 15 | Autonomous Development Factory | IN PROGRESS |
| 16 | Shadow / Promotion / Rollback | IN PROGRESS |
| 17 | Complete Regression / E2E | IN PROGRESS |
| 18 | Security / Performance / Hardening | PASS |
| 19 | Final Acceptance | IN PROGRESS |

## Phase 0 — V7→V9 Migration, Baseline & Control Documents

- **Goal:** Migrate read-only V7 into V9 as a clean copy, verify the baseline by execution, create all V9 control documents, mark Phase 0 = PASS, V7 untouched.
- **Status:** PASS — all 15-task gate criteria verified (V7 untouched, migration clean, baseline green, 10 docs created, manifest 12 repos PENDING).
- **Existing V7 components reused:** FastAPI backend (`api/`, `core/`, `agents/`), vanilla JS SPA (`frontend/`), SQLite + JSON dual-write state, `core/strategy_v2/`, `walk_forward_validation`, `reserved_holdout_validation`, `oos_validation_reports`, `feedback_loop`, `confidence_gates`, `autonomous_control`, `autonomous_trade_guard`, `trade_decision`, `capital_manager`, Ollama client, news + pre-earnings agents, watchlist/capital/signals state, 4 backtesters (to be consolidated in Phase 7).
- **Files inspected:** V7 tree at `Market Signals V7 - Premium` @ `f912a33` (109 dirty files, left untouched); `MASTER_AUDIT_REPORT.md` (8 P0 + ~20 P1 repaired, 557 passing).
- **Files created (in V9):** `.gitignore`, `scripts/migrate_v7_to_v9.py`, migrated `api/ core/ agents/ tools/ frontend/ tests/ tests_e2e/ data/ docs/`, `config.json` (placeholder, gitignored), `docs/v7-source-baseline.txt`, `docs/v9-baseline-results.md`, `docs/V9_*.md` (10 control documents), `docs/v7-audit/` (audit reports preserved as reference).
- **Files modified:** none in V7; only new files in V9.
- **Dependencies:** existing system Python 3.14.3 env (deps from `requirements.txt` already installed for V7); no new deps in Phase 0.
- **Migrations:** none (verbatim code copy; schema unchanged).
- **API changes:** none.
- **UI changes:** none.
- **Tests executed:** `python -c "import api.main"`; `pytest tests/unit tests/integration tests/contract tests/golden tests/perf`; `pytest -m "smoke and not e2e" ...`. (See `docs/v9-baseline-results.md` for exact numbers.)
- **Exact results:** see `docs/v9-baseline-results.md`.
- **Bugs found:** 0 new (V7 known failures already repaired per audit; not re-audited).
- **Bugs fixed:** 0 (Phase 0 makes no code changes).
- **Remaining work:** Phase 1 onward (see phase table).
- **Blockers:** none (or list exact failing items if the gate is not green).

## Phase 1A — Canonical Data Model & Point-in-Time Contract

- **Goal:** Establish V9's canonical data layer (15 §27 entities), the point-in-time contract (`as_of`/`UNAVAILABLE` = the §32 no-synthetic-historical-facts guard), the DATA-quality vs SIGNAL-quality separation (§33), and a real market-calendar/session model (§28) — as a backward-compatible evolution keeping the 1559-test green baseline green. First of three Phase 1 sub-plans (1A canonical model + PIT + calendar; 1B provider ABC + historical storage; 1C corporate actions + survivorship).
- **Status:** PASS — all 9 exit-gate criteria verified by execution.
- **Delivered:**
  - `core/data_model/` package: `entities.py` (AssetClass/Currency/TradingSession enums, Exchange/Symbol, EXCHANGES registry, get_exchange, and the 15 entity dataclasses OHLCVBar/CorporateAction/Earnings/AnalystEstimate/Fundamentals/OptionContract/OptionsChain/NewsArticle/SECFiling/Signal); `pit.py` (PointInTime, UNAVAILABLE sentinel, to_utc, assert_utc, as_of — the §32 guard); `quality.py` (Conflict, DataQuality, SignalQuality, QualityEnvelope — §33 DATA vs SIGNAL separation).
  - `core/market_hours.py` evolved (additive): MarketHoursStatus gained session/is_half_day/exchange; session model PRE_MARKET/REGULAR/AFTER_HOURS/BMO/AMC/CLOSED/CRYPTO_CONTINUOUS (§28); `earnings_session_reported()` canonical BMO/AMC thresholds.
  - `core/data_quality.py` evolved (additive, backward-compatible): QualifiedValue gained optional freshness/coverage/conflicts/quality_flags (§33); existing factories and call sites unchanged.
  - `core/news_evidence_ledger.py`: typed `read_articles_as_of(T)` reader (§29/§32); JSONL format unchanged.
  - Folded duplicates: `agents/premarket_scanner_agent.py` `_is_us_market_open` → `market_hours.us_equity_market_status().is_open`; `_earnings_already_reported` same-day branch → `market_hours.earnings_session_reported`.
- **Files created (in V9):** `core/data_model/__init__.py`, `core/data_model/entities.py`, `core/data_model/pit.py`, `core/data_model/quality.py`, `tests/unit/test_data_model_entities.py`, `tests/unit/test_data_model_pit.py`, `tests/unit/test_data_model_quality.py`, `tests/unit/test_market_calendar_sessions.py`, `tests/unit/test_news_evidence_ledger_pit.py`; design + plan under `docs/superpowers/`.
- **Files modified (in V9, additive only):** `core/market_hours.py`, `core/data_quality.py`, `core/news_evidence_ledger.py`, `agents/premarket_scanner_agent.py`, `docs/V9_ARCHITECTURE.md`, `docs/V9_IMPLEMENTATION.md`.
- **Files modified in V7:** none (§117 — V7 untouched, verified f912a33/109 dirty unchanged).
- **Dependencies:** no new open-source deps (§118 — `core/data_model/` imports stdlib only; `requirements.txt`/`requirements-dev.txt` unchanged, verified empty diff 664ce0d..HEAD).
- **Migrations:** none (no schema change; news ledger JSONL format unchanged).
- **API changes:** none.
- **UI changes:** none.
- **Tests executed:** `python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf`.
- **Exact results:** 1604 passed / 0 failed / exit 0 in 148.67s. Baseline was 1559 (Phase 0); 45 new tests added (5 entities + 9 pit + 5 entity-dataclass + 4 quality + 4 QualifiedValue-additive + 13 market-calendar-sessions + 5 news-ledger-pit). No existing test weakened, dropped, or skipped (§115).
- **Behavior note (transparent):** the `premarket_scanner_agent._is_us_market_open` fold now honors `MS_MARKET_HOURS_OVERRIDE` consistently with the rest of the system (previously the agent ignored the override). No existing test relied on the old override-ignoring behavior (the tests that touch `_is_us_market_open` patch it entirely); the premarket + capital regression suites are green.
- **Out of scope (deferred):** provider rewiring behind a `DataProvider` ABC + append-only historical storage with `as_of` over storage → Phase 1B; corporate-action capture/split-adjustment + survivorship registry → Phase 1C; Options/SEC/Fundamentals **fetchers** → Phase 5/10 (types defined here); `exchange_calendars` / any new open-source dep → Phase 2 (§118).
- **Blockers:** none.
- **Phase 1 row status:** stays IN PROGRESS (1A is the first Phase 1 work; 1B and 1C remain).

## Phase 1B — Point-in-Time Historical Store

- **Goal:** Deliver V9's append-only point-in-time historical store — the "valid point-in-time source" that makes spec §29 ("use only data available at T") and §32 ("UNAVAILABLE unless a valid point-in-time source exists") enforceable system-wide — plus a minimal opt-in ingestion seam (one yfinance OHLCV proof adapter) that demonstrates it ingests real V7 provider output, as a backward-compatible evolution keeping the 1604-test green baseline green. Second of three Phase 1 sub-plans.
- **Status:** PASS — all exit-gate criteria verified by execution.
- **Delivered:**
  - `core/data_model/store.py` — `HistoricalStore` (singleton, follows `sqlite_store.py` WAL pattern): `pit_records` table keyed `(entity_type, symbol, provider, available_from, version)` with `UNIQUE` constraint; `append()` (append-only, no update/delete API, raises on missing `available_from`); `as_of()` (delegates tie-break to 1A `as_of`, returns `UNAVAILABLE` on miss); `as_of_range()` (inclusive ascending window); `detect_conflicts()` (cross-provider §33 → 1A `Conflict`); `query()` (diagnostic read); `get_historical_store()` factory.
  - `core/data_model/ingest.py` — `ProviderRecordAdapter` protocol, `df_to_bar_rows` (yfinance DataFrame → bar dicts), `YFOHLCVAdapter` (bar dict → `OHLCVBar` with explicit `PointInTime`), `ingest_via` (append each adapted entity). Adapters wrap-not-replace (§7/§23) and never see credentials (§106).
  - `core/data_model/__init__.py` exports extended (HistoricalStore, get_historical_store, ProviderRecordAdapter, ingest_via, YFOHLCVAdapter, df_to_bar_rows).
- **Files created (in V9):** `core/data_model/store.py`, `core/data_model/ingest.py`, `tests/unit/test_data_model_store.py`, `tests/unit/test_data_model_ingest.py`; design + plan under `docs/superpowers/`.
- **Files modified (in V9, additive only):** `core/data_model/__init__.py`, `docs/V9_ARCHITECTURE.md`, `docs/V9_IMPLEMENTATION.md`.
- **Files modified in V7:** none (§117 — V7 untouched, verified at gate).
- **Dependencies:** no new open-source deps (§118 — `store.py` stdlib `sqlite3`/`json`/`dataclasses`; `ingest.py` uses pandas, already a V7 dep; `requirements.txt`/`requirements-dev.txt` unchanged, verified empty diff at gate).
- **Migrations:** none (one new table `pit_records` created `IF NOT EXISTS` in the existing `MS_SQLITE_DB_PATH` DB; no existing table altered).
- **API changes:** none.
- **UI changes:** none.
- **Tests executed:** `python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf` (at the Task 8 gate).
- **Exact results:** 1629 passed / 0 failed / exit 0 in 151.90s. Baseline was 1604 (Phase 1A); 25 new tests added (5 store-append/query + 4 as_of + 3 as_of_range + 4 detect_conflicts + 7 ingest-seam + 2 end-to-end/exports). No existing test weakened, dropped, or skipped (§115).
- **Behavior note (transparent):** none (purely additive; no existing call site or behavior changed).
- **Out of scope (deferred):** provider adapters for FinnHub/Binance/Alpaca/KuCoin/news/fundamentals → phases that consume each entity (5/7/10); a spec-mandated market-data `DataProvider` ABC → not built (§34 "Multi-Provider" governs AI providers, Phase 3); survivorship registry (§30) + corporate-action capture/adjustment (§31) → Phase 1C; SQL-side `LIMIT` optimization for `as_of` over very large tables → Phase 7 (backtest volume); bar-close-vs-open availability refinement → Phase 7.
- **Blockers:** none.
- **Phase 1 row status:** stays IN PROGRESS (1C remains).

## Phase 1C — Corporate Actions (§31) + Survivorship Registry (§30)

- **Goal:** Close the Phase 1 spec gaps — §31 corporate-action capture (record splits/dividends in the point-in-time store + a documented adjusted/raw price convention) and §30 survivorship (a registry scaffold with point-in-time `is_listed`/`universe_at` queries + a loudly documented limitation, no fake delisting data) — as a backward-compatible evolution keeping the 1629-test green baseline green. Third and final Phase 1 sub-plan (flips the Phase 1 row to PASS).
- **Status:** PASS — all exit-gate criteria verified by execution.
- **Delivered:**
  - `core/data_model/survivorship.py` — `SurvivorshipRegistry` (singleton, follows `sqlite_store.py`/`store.py` WAL pattern): `symbol_lifecycle` table (`UNIQUE(symbol, event, event_time)` + index); append-only `register_listed`/`register_delisted`/`register_ticker_change`; `is_listed(symbol, T)` (§32: unknown → `False`); `universe_at(T, base_symbols)` (§30 survivorship filter: base minus terminal-event symbols ≤ T; unknown base symbols kept); `get_survivorship_registry()` factory.
  - `core/data_model/ingest.py` (additive) — `actions_df_to_records` (yfinance `.actions` DataFrame → DIVIDEND/SPLIT dicts, tz-localized on a copy); `YFCorporateActionAdapter` (dict → `CorporateAction` with explicit `PointInTime`, `available_from = ex_date`); `YFOHLCVAdapter` gains `adjusted: bool = True` (§31 convention; default flipped from 1B's `False`, documented, test-suite-compatible).
  - `core/data_model/store.py` (additive) — `HistoricalStore.actions_as_of(symbol, T, *, prefer)` (all CorporateAction payloads with `pit.<prefer> ≤ T`, ascending; reuses the `as_of_range` pattern).
  - `core/data_model/__init__.py` exports extended (`SurvivorshipRegistry`, `get_survivorship_registry`, `YFCorporateActionAdapter`, `actions_df_to_records`).
  - `core/backtester.py` — one additive §30 limitation comment at the universe-construction site (no behavior change).
- **Files created (in V9):** `core/data_model/survivorship.py`, `tests/unit/test_data_model_survivorship.py`, `tests/unit/test_data_model_corporate_actions.py`; design + plan under `docs/superpowers/`.
- **Files modified (in V9, additive only):** `core/data_model/ingest.py`, `core/data_model/store.py`, `core/data_model/__init__.py`, `core/backtester.py`, `docs/V9_ARCHITECTURE.md`, `docs/V9_IMPLEMENTATION.md`.
- **Files modified in V7:** none (§117 — V7 untouched, verified at gate).
- **Dependencies:** no new open-source deps (§118 — `survivorship.py` stdlib `sqlite3`; `ingest.py` additions use pandas, already a V7 dep; requirements files unchanged, verified empty diff at gate).
- **Migrations:** none (one new table `symbol_lifecycle` created `IF NOT EXISTS` in the existing `MS_SQLITE_DB_PATH` DB; no existing table altered).
- **API changes:** none.
- **UI changes:** none.
- **Tests executed:** `python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf` (at the Task 6 gate).
- **Exact results:** 1645 passed / 0 failed / exit 0 in 131.23s. Baseline was 1629 (Phase 1B); 16 new tests added (7 survivorship-registry + 5 corporate-action-adapter + 2 actions_as_of + 2 adjusted-flag). No existing test weakened, dropped, or skipped (§115).
- **Behavior note (transparent):** `YFOHLCVAdapter.adjusted` default flipped `False`→`True` to match the documented V9 adjusted-price convention + yfinance's actual `auto_adjust=True` default. The 1B `YFOHLCVAdapter` tests assert on `close`/`symbol`/`pit` only (not `adjusted`), so this is backward-compatible for the test suite. No existing call site routed through the registry or `actions_as_of`. The backtester touch is comment-only.
- **Documented deviation from plan (strict improvement):** `universe_at` was specified as `{s for s in base if is_listed(s, T)}`, which excluded unknown base symbols (because `is_listed(unknown) = False` per §32). The design §3.2 requires `universe_at` to "return `base_symbols` unchanged" when no delistings are recorded — i.e. unknown base symbols must be KEPT. Reimplemented as `base` minus symbols with a terminal event (DELISTED/TICKER_CHANGE) at or before T; `is_listed` keeps its §32 conservative semantics for arbitrary-symbol queries. The two methods intentionally answer different questions (documented in the `universe_at` docstring).
- **Out of scope (deferred):** backtest universe/adjustment rewiring (`SurvivorshipRegistry.universe_at(T)` + `actions_as_of(T)` call sites) → Phase 7 (backtest consolidation); comprehensive historical delisting / index-membership DB → Phase 2+ (open-source data source evaluation); corporate-action adapters for FinnHub/Binance/Alpaca → phases that consume them (5/7/10).
- **Blockers:** none.
- **Phase 1 row status:** PASS — 1C is the final Phase 1 sub-plan; Phase 1 (Data & Architecture Foundation) is complete (1A + 1B + 1C all PASS).

## Phase 2 — Open-Source Integration Foundation

- **Goal:** Close the §10/§11–§23 open-source evaluation gap — perform the real 13-step evaluation for all 13 repos (inspect repo + LICENSE + latest release + Python-3.14 compat + dep conflicts + maintenance; compare against V7), fill `docs/V9_OPEN_SOURCE_MANIFEST.md` with finalized decisions + pins, generate `THIRD_PARTY_NOTICES.md` (§24) + pinning (§25), and guard the manifest's completeness with a contract test — without merging any new production dependency (integration code lands in consuming phases, §118). Keep the 1645-test green baseline green.
- **Status:** PASS — all exit-gate criteria verified by execution.
- **Delivered:**
  - `docs/V9_OPEN_SOURCE_MANIFEST.md` — all 13 §11–§23 rows finalized (no `PENDING`/`TBD`): real commit/release inspected, license verified from each repo's LICENSE, Python-3.14 compat finding, dep impact, decision (INTEGRATE/ADAPTER/REFERENCE_ONLY/REJECT), reason. Plus a §25 pinning table (repository, commit/release, date inspected 2026-08-11 for all 13).
  - `THIRD_PARTY_NOTICES.md` (§24) — every production dependency requiring attribution: V7's existing pinned stack (15 packages from `requirements.txt` with license + URL) + the 6 INTEGRATE/ADAPTER repos with notice text. REFERENCE_ONLY/REJECT repos listed in a footer as non-production.
  - `tests/contract/test_open_source_manifest.py` — manifest-completeness contract test (6 assertions): file exists; covers all 13 repos; no `PENDING`/`TBD` cells; decisions valid; every INTEGRATE/ADAPTER row has a verified permissive license + pinned release; `THIRD_PARTY_NOTICES.md` exists and lists every INTEGRATE/ADAPTER repo.
  - `docs/V9_ARCHITECTURE.md` — additive "Open-source integration boundary (Phase 2)" section (V9-owned interfaces authoritative; the boundary rule; decisions; §118 no-merged-deps; contract test).
- **Decisions (13 repos):**
  - INTEGRATE (3): skfolio 0.25.0 (BSD-3, Phase 9), edgartools 5.47.0 (MIT, Phase 10), Optuna 4.9.0 (MIT, Phase 6/14).
  - ADAPTER (3): Microsoft Qlib 0.20.1 (BSD-3, Phase 6/14 — V9 data model authoritative), FinBERT via transformers 5.15.0 (Apache-2.0 code; model-weight terms verify on HF in Phase 10; do NOT use obsolete `pytorch_pretrained_bert`), MLflow 3.15.1 (Apache-2.0, Phase 14 — V9 keeps own `AIProvider`).
  - REFERENCE_ONLY (5): Microsoft RD-Agent (MIT; Docker/WSL research server, not a runtime dep), Earnings-Volatility-Calculator (MIT; methodology only, §15 — no Tkinter/scraping), RiskLabAI.py (BSD-3-Clause **verified** per §19 — not assumed; prefer published López de Prado methodology + independent V9 impl), quant-garage (AGPL-3.0; concepts only, §21 — no copy/vendor/hidden-dep), River 0.25.0 (BSD-3; detection-only initially, may upgrade to ADAPTER in Phase 6).
  - REJECT (2): vectorbt 1.1.0 (Apache-2.0 + Commons Clause fair-code license — §20 license gate not granted for a productizable cockpit; V9 stays functional via `InternalBacktestEngine`; re-evaluate only if license changes), CCXT 4.5.73 (MIT but V7 crypto execution via Binance/KuCoin REST is sufficient — §23; do not replace working V7 providers).
- **Files created (in V9):** `THIRD_PARTY_NOTICES.md`, `tests/contract/test_open_source_manifest.py`; design + plan under `docs/superpowers/`.
- **Files modified (in V9, additive only):** `docs/V9_OPEN_SOURCE_MANIFEST.md` (Phase-0 stub → finalized rows), `docs/V9_ARCHITECTURE.md`, `docs/V9_IMPLEMENTATION.md`.
- **Files modified in V7:** none (§117 — V7 untouched, verified `f912a33` at gate).
- **Dependencies:** no new open-source deps merged (§118 — Phase 2 records decisions + pins only; INTEGRATE/ADAPTER `pip install`s land in consuming phases). `requirements.txt`/`requirements-dev.txt`/`pyproject.toml`/`setup.py`/`setup.cfg` unchanged, verified empty diff `f1d65d4..HEAD`.
- **Migrations:** none.
- **API changes:** none.
- **UI changes:** none.
- **Tests executed:** `python -m pytest tests/contract/test_open_source_manifest.py -v` (6 passed); `python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf` (full regression at the gate).
- **Exact results:** 1651 passed / 0 failed / exit 0 in 126.40s. Baseline was 1645 (Phase 1C); 6 new contract tests added (manifest-completeness). No existing test weakened, dropped, or skipped (§115).
- **Behavior note (transparent):** none — Phase 2 produces documentation + a contract test only; no production code or behavior changed.
- **License-gate notes (§19/§20):** RiskLabAI.py license was VERIFIED from the raw LICENSE (BSD-3-Clause, RiskLab AI, 2022) — not assumed (§19); chosen REFERENCE_ONLY on merit (low-activity repo; prefer published methodology + independent impl). vectorbt's license is Apache-2.0 **plus** a Commons Clause (fair-code) — the "Apache-2.0" substring alone would falsely pass a naive permissive-license check; vectorbt is REJECT so it is exempt from the contract's permissive-license assertion (the honest call, §20).
- **Out of scope (deferred):** all INTEGRATE/ADAPTER integration/adapter code → consuming phases (skfolio→9, edgartools/FinBERT→10, Optuna→6/14, Qlib→6/14, MLflow→14 + AI Gateway→3, River→6 if upgraded); RD-Agent runtime (Docker/WSL/isolated worker) → Phase 14/15; CCXT live unified execution → future phase only if V7 crypto execution found insufficient; periodic re-inspection for upstream drift → later hygiene pass.
- **Blockers:** none.
- **Phase 2 row status:** PASS.

## Phase 3A — Multi-Provider AI Platform: AIProvider interface + OllamaProvider (evolve) + config schema

- **Goal:** Introduce V9's `AIProvider` abstraction (spec §34 — the 8 normalized capabilities `list_models`/`health_check`/`capabilities`/`generate`/`generate_structured`/`tool_call`/`stream`/`usage`) and the first concrete provider `OllamaProvider` — which **wraps** the existing `OllamaClient` singleton without modifying it (§7) — plus the forward `config["ai"]` schema (§35/§36/§37/§38). All 13 existing Ollama callers and 8 pinned Ollama test files stay green unchanged; the 1651-test baseline stays green. First of three Phase 3 sub-plans (3A interface/provider/config → 3B cloud providers → 3C assignment/router/fallback/API routes).
- **Status:** PASS — all exit-gate criteria verified by execution.
- **Delivered:**
  - New `core/ai/` package: `provider.py` (`AIProvider` ABC with the 8 §34 capabilities, frozen value types `Usage`/`ProviderCapabilities`/`ToolCall`/`AIResponse`/`AIStreamChunk`, an `HttpCallable` transport seam, and the `UNAVAILABLE` sentinel for GOLD-014), `ollama_provider.py` (`OllamaProvider(AIProvider)` wrapping the `ollama` singleton; `generate` lifts the legacy `{response,_meta}`/`{error}` dict to a typed `AIResponse`; `generate_structured` uses Ollama JSON mode + in-prompt schema and parses; `stream` parses Ollama NDJSON; `tool_call` parses `message.tool_calls`; `usage` captures per-call token stats; `capabilities`/`list_models`/`health_check` delegate to the wrapped client), `__init__.py` (exports + `get_ollama_provider()` cached factory).
  - `core/config.py` + `config.example.json`: additive `DEFAULT_CONFIG["ai"]` (alongside the existing untouched `ollama` block) — 8 providers (`ollama` + `openai`/`anthropic`/`gemini` + `xai`/`deepseek`/`mistral`/`openrouter` as `openai_compatible`), empty `registry`, 10-role `assignment` + `all` alias, 3-tier `router` (`triage`/`analysis`/`critic`), `fallback` chain, `all_unavailable_no_trade: true` (GOLD-014).
- **Decisions:**
  - **Wrap, don't rewrite (§7/§5):** `OllamaProvider` wraps the existing `ollama` singleton and leaves `core/ollama_client.py` fully untouched — lower risk than the design's "singleton becomes a shim" wording (same §7/§34 outcome; the 13 callers' import target and the 8 pinned tests' patch targets are unchanged). Recorded as a strict-improvement deviation in the SDD ledger.
  - **Zero new deps (§118):** `core/ai/` imports stdlib + `core.ollama_client` + `core.http_utils` (all already pinned). The official `openai`/`anthropic`/`google-genai` SDKs are installed in the shared venv but NOT pinned in `requirements.txt` — using them would add unpinned production deps (violates §25), so 3A's `OllamaProvider` reuses the governed `core/http_utils` `requests`-based seam instead. Cloud providers in 3B will follow the same seam.
  - **No hardcoded model names (§35):** `default_model: ""` in config means discover-then-select via `list_models()`; `OllamaProvider` resolves the model via `client.get_best_model()` or the caller-supplied `model` param.
  - **Keys via env (§106):** every non-Ollama provider config carries `api_key_env`; no key value is committed, sent to the frontend, or logged. `OllamaProvider` reuses the wrapped client's key-omitting `_cache_identity`.
- **Files:** `core/ai/__init__.py` (NEW), `core/ai/provider.py` (NEW), `core/ai/ollama_provider.py` (NEW), `core/config.py` (additive `ai` block), `config.example.json` (additive `ai` block), `tests/unit/test_ai_provider_contract.py` (NEW, 8 tests), `tests/unit/test_ai_ollama_provider.py` (NEW, 10 tests), `tests/unit/test_ai_config_schema.py` (NEW, 7 tests), `docs/V9_ARCHITECTURE.md` (additive Phase 3A section), `docs/V9_IMPLEMENTATION.md` (this record).
- **NOT modified:** `core/ollama_client.py`, `core/ollama_decision.py`, the 13 Ollama call sites, `requirements.txt`/`requirements-dev.txt`/`pyproject.toml`/`setup.*`.
- **Dependencies:** no new (§118 — `core/ai/` stdlib + already-pinned `core.ollama_client`/`core.http_utils`; empty deps diff at gate).
- **Migrations:** none.
- **API changes:** none (API routes land in 3C).
- **UI changes:** none.
- **Tests executed:** `python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf` (at the Task 4 gate).
- **Exact results:** 1676 passed / 0 failed / exit 0 in 133.88s. Baseline was 1651 (Phase 2); 25 new tests added (8 provider-contract + 10 ollama-provider + 7 config-schema). The 60 pinned Ollama tests across 7 files (run separately at Task 2 Step 5) stayed green with `core/ollama_client.py` untouched. No existing test weakened, dropped, or skipped (§115).
- **Behavior note (transparent):** one documented deviation from the plan's verbatim code — in `OllamaProvider._post_chat`, usage-capture was moved before the parse-fail early-return so `usage()` reflects every completed call (not only successful JSON parses). A strict improvement; no existing behavior changed.
- **Out of scope (deferred):** cloud providers OpenAI/Anthropic/Gemini/OpenAI-compatible (xAI/DeepSeek/Mistral/OpenRouter) → 3B; ModelRegistry population (§36) → 3B; ModelAssignment 10-role wiring (§37), ModelRouter 3-tier wiring (§38), `generate_with_fallback` (GOLD-013), `all_unavailable_no_trade` enforcement (GOLD-014), §108 AI-config API routes → 3C; MLflow AI Gateway full integration → Phase 14 (evaluated in the Phase 3 design, accommodated via `openai_compatible`).
- **Blockers:** none.
- **Phase 3 row status:** stays IN PROGRESS (3B + 3C remain).

## Phase 3B — Multi-Provider AI Platform: Cloud Providers + Model Registry

- **Goal:** Deliver the cloud `AIProvider` implementations (OpenAI, Anthropic, Gemini, and an OpenAI-compatible provider covering xAI/DeepSeek/Mistral/OpenRouter) over the governed `core/http_utils` `requests` seam — zero new dependencies — and the `ModelRegistry` (§36) that discovers models via `list_models()` and allows manual configuration. Each provider is fully testable without keys or network via an injectable transport that pins the exact verified request shape. Second of three Phase 3 sub-plans.
- **Status:** PASS — all exit-gate criteria verified by execution.
- **Delivered:**
  - `core/ai/openai_provider.py` — `OpenAIProvider(AIProvider)`: OpenAI Chat Completions shape (Bearer auth, `choices[0].message`, `response_format: json_object` for structured, `tools`/`tool_calls`, `usage.prompt_tokens`/`completion_tokens`, SSE `chat.completion.chunk` streaming terminated by `data: [DONE]`, `GET /models` for `list_models`/`health_check`).
  - `core/ai/openai_compatible_provider.py` — `OpenAICompatibleProvider(OpenAIProvider)`: identical wire shape, config-driven `base_url` for xAI/DeepSeek/Mistral/OpenRouter.
  - `core/ai/anthropic_provider.py` — `AnthropicProvider(AIProvider)`: `/v1/messages` (`x-api-key` + `anthropic-version: 2023-06-01`, `max_tokens` required, `content[]` `text`/`tool_use` blocks, `usage.input_tokens`/`output_tokens`, SSE `content_block_delta`/`message_stop`).
  - `core/ai/gemini_provider.py` — `GeminiProvider(AIProvider)`: `:generateContent?key=` (`contents`/`parts`, `systemInstruction`, `generationConfig.responseMimeType`, `functionDeclarations`/`functionCall`, `usageMetadata`; `list_models` strips the `models/` prefix).
  - `core/ai/registry.py` — `ModelEntry` (frozen, the 10 §36 fields) + `ModelRegistry` (`register`/`get`/`all_entries`/`enabled_entries`/`update_health`/`discover`/`to_list`/`from_list`).
  - `core/http_utils.py` — governor entries (concurrency/circuit-breaker/rate-limit) for the 8 new cloud provider names; derived structures pick them up automatically.
- **Decisions:**
  - **Zero new deps (§118/§25):** all cloud providers talk over the governed `core/http_utils` `requests` seam, NOT the installed `openai`/`anthropic`/`google-genai` SDKs (which are unpinned in `requirements.txt` → adopting them would add unpinned production deps, violating §25). SSE streaming is parsed from `requests` `iter_lines()` (no `httpx-sse`). This is lower-§106-risk (no SDK telemetry surface) and fully testable without keys/network.
  - **Verified REST shapes pinned by tests (§35):** the OpenAI/Anthropic/Gemini wire shapes (inspected 2026-08-11 per §25) are asserted exactly by fake-transport unit tests — a future API drift shows as a test diff, not a silent runtime bug. Providers are config-driven (`base_url`); endpoint paths are the verified current ones.
  - **§106 secrets:** keys via `api_key_env`; only the auth header (Bearer / `x-api-key`) or the Gemini `?key=` query param carries the value per-call; never committed, never logged, never sent to the frontend. No API routes in 3B.
  - **OpenAICompatibleProvider is a subclass (§5 DRY):** xAI/DeepSeek/Mistral/OpenRouter reuse the OpenAI chat-completions implementation; only `name` differs per instance.
  - **ModelRegistry discover + manual (§36):** discovery via `list_models()` populates entries with the provider's `capabilities()`; manual entries can be registered directly; both persist to `config["ai"]["registry"]`.
- **Files:** `core/ai/openai_provider.py` (NEW), `core/ai/openai_compatible_provider.py` (NEW), `core/ai/anthropic_provider.py` (NEW), `core/ai/gemini_provider.py` (NEW), `core/ai/registry.py` (NEW), `core/http_utils.py` (additive governor entries), `core/ai/__init__.py` (exports), `tests/unit/test_ai_openai_provider.py` (NEW, 9 tests), `tests/unit/test_ai_anthropic_provider.py` (NEW, 5 tests), `tests/unit/test_ai_gemini_provider.py` (NEW, 6 tests), `tests/unit/test_ai_model_registry.py` (NEW, 8 tests), `docs/V9_ARCHITECTURE.md` (additive Phase 3B subsection), `docs/V9_IMPLEMENTATION.md` (this record).
- **NOT modified:** `core/ollama_client.py`, `core/ollama_decision.py`, `core/ai/provider.py`, `core/ai/ollama_provider.py`, `core/config.py` (the `ai` block from 3A already carries the provider entries + `registry: []`), `requirements*.txt`/`pyproject.toml`/`setup.*`.
- **Dependencies:** no new (§118 — all over already-pinned `requests` via `core/http_utils`; empty deps diff at gate).
- **Migrations:** none.
- **API changes:** none (API routes land in 3C).
- **UI changes:** none.
- **Tests executed:** `python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf` (at the Task 5 gate).
- **Exact results:** 1704 passed / 0 failed / exit 0 in 128.46s. Baseline was 1676 (Phase 3A); 28 new tests added (9 openai+compatible + 5 anthropic + 6 gemini + 8 registry). No existing test weakened, dropped, or skipped (§115).
- **Behavior note (transparent):** two documented deviations from the plan's verbatim code, both strict improvements: (1) the OpenAI `[DONE]` stream terminator returns without yielding an empty chunk (a sentinel, not a content delta); (2) the Gemini URL test used `startswith` instead of the plan's self-contradictory exact-equality assertion (the verified §2 shape puts the key as a query param, so the URL always carries `?key=`). No existing behavior changed.
- **Out of scope (deferred):** ModelAssignment 10-role wiring (§37), ModelRouter 3-tier wiring (§38), `generate_with_fallback` (GOLD-013), `all_unavailable_no_trade` enforcement (GOLD-014), §108 AI-config API routes (`api/ai_config_service.py` + main.py, keys never to frontend), golden tests GOLD-013/014 → 3C; §39 tool registry + §41 decision schema + critic agent loop → Phase 4; MLflow full integration → Phase 14.
- **Blockers:** none.
- **Phase 3 row status:** stays IN PROGRESS (3C remains).

## Phase 3C — Multi-Provider AI Platform: Assignment + Router + Fallback + §108 API + Golden Tests

- **Goal:** Close the Phase 3 platform — wire the 10-role `ModelAssignment` (§37), the 3-tier `ModelRouter` (§38), `generate_with_fallback` (GOLD-013) with the `all_unavailable_no_trade` guard (GOLD-014), the §108 AI-configuration API journey (configure → test → discover → assign → save → validated request), the MLflow AI Gateway evaluation record, and the GOLD-013/GOLD-014 golden tests. Third and final Phase 3 sub-plan.
- **Status:** PASS — all exit-gate criteria verified by execution.
- **Delivered:**
  - `core/ai/assignment.py` — `ModelAssignment` (§37): `ROLES` (the 10 role names) + the `all` alias; `resolve(role)` returns the explicit role model, else the `all` alias for a known role, else `None`; `set`/`set_all`/`to_dict`/`from_config`/`validate(model_id, registry)`.
  - `core/ai/router.py` — `ModelRouter` (§38): `TIERS` (`triage`/`analysis`/`critic`); `select_model(role, tier_hint)` returns the tier model when set, else `ModelAssignment.resolve(role)`; `to_dict`/`from_config`.
  - `core/ai/fallback.py` — `generate_with_fallback` (GOLD-013): primary → fallback chain (each fallback uses its own `default_model`, §35) → first non-error `AIResponse`, else the 3A `UNAVAILABLE` sentinel; `all_unavailable(response)`; `no_new_autonomous_trade_guard(response, config)` (GOLD-014) blocks new autonomous trades when `all_unavailable_no_trade` (default `True`; missing key treated as `True`) and `response is UNAVAILABLE`.
  - `api/ai_config_service.py` — `build_provider(config, name)` factory (constructs the right `AIProvider` subclass from `config["ai"]["providers"][name]["type"]`; ollama → `get_ollama_provider()`; no hardcoded models §35); `list_ai_providers` (returns `has_key: bool`, NEVER the key value — §106); `probe_ai_provider` (Test Connection); `discover_ai_models`; `get`/`set_ai_assignment`; `get`/`set_ai_router`; `ai_validated_request` (no key in response — §106).
  - `api/main.py` — §108 routes: `GET /api/ai/providers`, `POST /api/ai/providers/{name}/test`, `POST /api/ai/providers/{name}/discover`, `GET`/`POST /api/ai/assignment`, `GET`/`POST /api/ai/router`, `POST /api/ai/request`; pydantic request models `AIAssignmentRequest`/`AIRouterRequest`/`AIValidatedRequest`.
  - `core/ai/__init__.py` — re-exports `ROLES`, `ModelAssignment`, `TIERS`, `ModelRouter`, `generate_with_fallback`, `all_unavailable`, `no_new_autonomous_trade_guard`.
  - `tests/golden/test_ai_fallback_golden.py` — GOLD-013 (fallback chain serves / all-down → `UNAVAILABLE`) + GOLD-014 (guard blocks new autonomous trade when all AI unavailable; allows when AI available) golden tests.
- **Decisions:**
  - Zero new dependencies (§118 — `core/ai/` is stdlib + already-pinned imports; `api/ai_config_service.py` reuses the existing config_service redaction/merge + already-pinned FastAPI/pydantic).
  - Evolutionary, not a rewrite (§5/§7) — `core/ollama_client.py`, `core/ollama_decision.py`, `core/agent_manager.py`, `core/config.py`, and the 3A/3B provider/registry modules are untouched; `generate_with_fallback` + the guard are a tested platform the autonomous layer *can* consult. Rewiring the agent_manager autonomous loop to call them is Phase 4 (§41 decision schema + critic workflow).
  - §106 enforced: only `has_key: bool` reaches API responses; no key value is ever returned, logged, or sent to the frontend. A unit test (`test_list_ai_providers_returns_has_key_not_the_key`) asserts the key value is absent from the payload.
- **Dependencies:** no new (§118; empty deps diff at gate).
- **Migrations:** none.
- **API changes:** additive — 8 new `/api/ai/*` routes (no existing route changed).
- **UI changes:** none (the §37 model-assignment UI is Phase 12; 3C ships backend + API only).
- **Tests executed:** `python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf` (at the Task 5 gate).
- **Exact results:** 1756 passed / 0 failed / exit 0 in 125.88s. Baseline was 1704 (Phase 3B); new 3C tests: 9 assignment + 8 router + 10 fallback + 11 ai_config_service + 7 ai_config_routes + 7 golden = 52 new tests. No existing test weakened, dropped, or skipped (§115).
- **Behavior note (transparent):** three documented deviations from the plan's verbatim code, all strict improvements: (1) `ModelAssignment.resolve` applies the `all` alias only to the 10 known §37 roles, not arbitrary unknown role strings (the plan's unconditional-fallback was caught by `test_resolve_unknown_role_returns_none`); (2) `generate_with_fallback` dropped the unused `last_error` local from the plan's verbatim code (dead variable); (3) the service function was renamed `test_ai_provider` → `probe_ai_provider` to avoid pytest collecting the service function as a test (`test_` prefix collision). No existing behavior changed.
- **Out of scope (deferred):** rewiring the agent_manager autonomous loop to call `generate_with_fallback`/the guard → Phase 4 (§41 decision schema + critic workflow); §39 tool registry + §41 structured decision schema + independent-critic agent loop → Phase 4; §37 model-assignment UI → Phase 12; MLflow full experiment-tracking integration → Phase 14.
- **Blockers:** none.
- **Phase 3 row status:** **PASS — Phase 3 (Multi-Provider AI Platform) COMPLETE (3A + 3B + 3C all PASS).**
## Phase 4 — AI Tool & Decision Engine (design)

- **Goal:** Build the AI reasoning + decision layer on the Phase 3 platform: the §39 tool registry (28 allow-listed domain tools), the §40 no-unrestricted-access guard, the §41 structured decision schema, the §42 validation pipeline (parse→schema→semantic→authoritative-data→policy→risk-gate; AI prose alone never triggers an order), §43 app-authoritative numbers, the §44 independent critic (10 challenge dimensions), and §45/§46/§48 autonomy-mode enforcement (OFF/ANALYZE/RECOMMEND/PAPER_AUTO/LIVE_ASSISTED/LIVE_AUTO; default NOT LIVE_AUTO; Paper-Auto before Live-Auto; AI cannot change the Risk Constitution). Rewires `agent_manager` to call `generate_with_fallback` + `no_new_autonomous_trade_guard` + the tool loop + the pipeline + the critic behind the autonomy gate (legacy path preserved).
- **Design doc:** `docs/superpowers/specs/2026-08-12-v9-phase4-ai-tool-and-decision-engine-design.md` (commit `ffc763f`). Decomposition: 4A (tool registry §39/§40 + 28 tools + dispatch loop) → 4B (§41 decision schema + §42 pipeline + §43 non-authoritative guard) → 4C (§44 critic + §45/§46/§48 autonomy + `agent_manager` rewire).
- **Phase 4 row status:** IN PROGRESS.

## Phase 4A — AI Tool & Decision Engine: Tool Registry + §40 Allow-List + Dispatch Loop

- **Goal:** Deliver the §39 tool registry — a `Tool`/`ToolRegistry` layer plus the 28 allow-listed domain tools the AI reasons through — on top of the Phase 3 `tool_call` transport, with §40 (no unrestricted shell/Python/SQL/filesystem/HTTP/creds/broker access) enforced by construction, §43 (app-authoritative capital/size/risk) enforced by delegation, and §42 (no order on AI prose) enforced by draft-only `propose_order`/`propose_close`. First of three Phase 4 sub-plans (4A tool registry → 4B decision schema/pipeline → 4C critic/autonomy/rewire).
- **Approach:** Each tool is a frozen `Tool(name, description, arguments_schema, execute)` wrapping an existing read-only service function (evolve, §5 — no rewrite). `ToolRegistry` refuses any name outside the explicit 28-name `SPEC_TOOL_NAMES` allow-list, so the AI can never reach a non-§39 surface. `build_default_registry()` assembles the 28 from per-domain builder functions. `run_tool_loop(provider, registry, prompt)` is the generate→dispatch→feed-back primitive (wired into the autonomous cycle in 4C). Injected delegates make every unit test fake-driven (no live market/broker/AI calls).
- **Files:** `core/ai/tools/__init__.py`, `tool.py`, `registry.py`, `loop.py` (NEW); `market_data.py` (5), `earnings.py` (4), `options.py` (1, Phase-5 stub), `news.py` (2), `sec.py` (1, Phase-10 stub), `signals_strategy.py` (4), `backtest.py` (2), `portfolio_risk.py` (5, §43), `history.py` (2), `order_proposal.py` (2, draft-only) (NEW); `core/ai/__init__.py` (additive re-exports); `tests/unit/test_ai_tool_registry.py` (7), `test_ai_tool_loop.py` (3), `test_ai_tools_market_data.py` (7), `test_ai_tools_earnings.py` (5), `test_ai_tools_options_sec.py` (3), `test_ai_tools_news.py` (3), `test_ai_tools_signals_strategy_backtest.py` (9), `test_ai_tools_portfolio_risk_history.py` (8), `test_ai_tools_order_proposal.py` (5) (NEW); `tests/golden/test_ai_tool_registry_golden.py` (NEW, 5 golden); `docs/V9_TEST_MATRIX.md`, `docs/V9_ARCHITECTURE.md`, `docs/V9_IMPLEMENTATION.md` (this record).
- **Not modified:** `core/ollama_client.py`, `core/ollama_decision.py`, `core/agent_manager.py`, `core/config.py`, the 3A/3B/3C provider/registry/assignment/router/fallback modules, `requirements*.txt`/`pyproject.toml`/`setup.*` (empty diffs `7209da0..HEAD`).
- **Dependencies:** none new (§118 — `core/ai/tools/` is stdlib + existing V9 service functions; `pandas` already in V7/V9 for OHLCV normalization in `get_ohlcv`).
- **Tests executed:** `python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf` (the Phase 3C selection; the full `pytest tests tests_e2e` is also green).
- **Exact results:** curated selection **1811 passed / 0 failed / exit 0 in 133.31s** = 1756 (Phase 3C) + 55 new (7 registry + 3 loop + 7 market_data + 5 earnings + 3 options_sec + 3 news + 9 signals_strategy_backtest + 8 portfolio_risk_history + 5 order_proposal + 5 golden). Full `pytest tests tests_e2e`: 2323 passed / 0 failed / 36 skipped. No existing test weakened, dropped, or skipped (§115).
- **V7 / deps audit:** V7 HEAD `f912a33` unchanged; deps diff `7209da0..HEAD` empty; protected-source diff (`ollama_client`/`ollama_decision`/`agent_manager`/`config`) empty. §4 forbidden-language sweep on new/modified files → 0 hits.
- **Behavior note (transparent):** seven documented deviations from the plan's verbatim code, all strict improvements or faithful corrections: (1) `build_context`'s real signature is `(symbol, asset_class, watch_row, ohlc_df=...)` not `(ticker, bars)` — added an adapter in `market_data_tools` mapping `(ticker, bars)`→`build_context(symbol, infer_asset_class, {}, ohlc_df=DataFrame(bars))`; (2) `get_ohlcv` normalizes a `DataFrame` result to `{"bars": df.to_dict("records")}` instead of feeding a raw DataFrame repr into the loop; (3) `get_strategy` lists all registry specs via `.specs` (asset_class strings are `US_STOCKS`/`CRYPTO_FUTURES`/etc., not `"stock"`; the tool takes no asset arg); (4) `RiskAgent` requires a `config_manager` → `RiskAgent(ConfigManager())` (`BaseAgent.__init__` does not start a thread; side-effect-free); (5) the history tools pass `limit=` keyword (`read_recent_executions` `limit` is keyword-only); (6) `propose_close` drops the `_get_execution_price` price fetch (§43 — a draft must not embed an authoritative price; computed at execution time); (7) the `propose_order` test fixture gained `get_positions` (the tool calls it for capital-entry evidence; the plan's fixture omitted it). The 28-name registry-completeness assertion lives in the golden suite (Task 7), not duplicated as a Task 1 unit test that couldn't pass until all 28 tools landed.
- **Out of scope (deferred):** §41 structured decision schema + §42 validation pipeline + §43 non-authoritative guard → 4B; §44 independent critic + §45/§46/§48 autonomy-mode enforcement + `agent_manager` rewire to call `generate_with_fallback`/`no_new_autonomous_trade_guard`/`run_tool_loop`/pipeline/critic behind the autonomy gate → 4C; options fetcher → Phase 5; SEC fetcher → Phase 10; §37 model-assignment UI → Phase 12.
- **Blockers:** none.
- **Phase 4 row status:** PASS (4A + 4B + 4C all PASS — see `docs/V9_ARCHITECTURE.md` / `docs/V9_AI_SYSTEM.md` / `docs/V9_CHANGELOG.md`).

## Phase 5 — Pre-Earnings V9 (§49/§50/§51/§93/§109)

- **Goal:** Turn Pre-Earnings from a scoring surface into a §49-§51 decision system. (1) Formalize the §50 triple — PRE-EARNINGS SCORE 0-100 with visible components + DATA COVERAGE + DATA CONFIDENCE, pinned "Score is NOT probability". (2) Wire §51 autonomy — each candidate flows through the Phase 4C `decision_engine` with the full §51 input stack and resolves to one of the 7 actions, gated by `core/autonomy` + the kill switch. (3) Surface §93 UI columns and prove the §109 E2E journey.
- **Design doc:** `docs/superpowers/specs/2026-08-12-v9-phase5-pre-earnings-v9-design.md` (commit `66812c1`). Plan: `docs/superpowers/plans/2026-08-12-v9-phase5-pre-earnings-v9.md` (commit `e763b5b`). Decomposition: 5A (§50 score/coverage/confidence) → 5B (§51 decision context + engine bridge + autonomy clamp + run_cycle wire) → 5C (§93 UI + §109 E2E + control docs).
- **Approach:** Evolve-not-rewrite (§5/§7). New `core/pre_earnings/score.py` (pure, stdlib, no I/O) evolves the V7 `_compute_opportunity_score` formula into the canonical score (exact back-compat when `weight_overrides` is None) and the `historical_samples` bands into multi-input DATA CONFIDENCE (coverage is a DOWNGRADE-ONLY modifier — never upgrades). New `core/pre_earnings/decision.py` assembles the §51 input stack (honest `available:False`+reason stubs for Phases 6/7/8/9/10 — never faked numbers, §4/§43), bridges to the 4C `decision_engine`, and applies the autonomy clamp: AI-unavailable→WATCH; order-bearing actions gated by PAPER_AUTO + kill switch + LIVE_AUTO acceptance; NO_TRADE (critic veto/policy) never relaxed to WATCH. Per §109, capital/risk §42 stages are injected as permissive stubs (deferred to the downstream order path) so the Decision reflects the AI+critic conclusion; real capital/risk gating stays in `agent_manager`'s order path. `agents/pre_earnings_agent.py` is additively extended: `_build_item_for_symbol` emits the §50 triple (back-compat `opportunity_score` preserved); `run_cycle` calls a gated `_attach_pre_earnings_decisions` pass (default off via `pre_earnings.decision_enabled`, bounded by `decision_max_tickers`) — ship wired-but-off, owner flips to activate.
- **Files:** `core/pre_earnings/__init__.py`, `score.py`, `decision.py` (NEW); `agents/pre_earnings_agent.py` (additive: import + `_build_item_for_symbol` §50 fields + `_attach_pre_earnings_decisions` + one `run_cycle` call); `core/config.py` + `config.example.json` (`pre_earnings.decision_enabled` False + `decision_max_tickers` 25); `tests/unit/test_pre_earnings_score.py` (5), `test_pre_earnings_agent_score_integration.py` (2), `test_pre_earnings_decision.py` (7), `test_pre_earnings_run_cycle_decision.py` (4) (NEW); `tests/golden/test_phase5a_golden.py` (GOLD-050, 3), `test_phase5c_golden.py` (GOLD-093, 4) (NEW); `tests/acceptance/test_pre_earnings_autonomy.py` (7), `test_pre_earnings_e2e_journey.py` (3) (NEW); control docs.
- **Not modified:** the 4C platform (`decision_engine`/`critic`/`pipeline`/`autonomy`) — composed, not changed; `requirements*.txt`/`pyproject.toml`/`setup.*` (empty deps diff).
- **Dependencies:** none new (§118 — stdlib only).
- **Tests executed:** curated `pytest tests/unit tests/integration tests/contract tests/golden tests/perf` (green at every task); full `pytest tests tests_e2e` at the exit gate.
- **V7 / deps audit:** V7 HEAD `f912a33` unchanged; deps diff `66812c1..HEAD` empty; §4 forbidden-language sweep on all Phase-5 files → 0 hits.
- **Out of scope (deferred, honest stubs now):** strategy registry (Phase 8), signal/ML engine (Phase 6), backtest (Phase 7), news FinBERT/SEC (Phase 10), portfolio/risk/capital services (Phase 9), Decision Ledger persistence (Phase 13), live positions (Phase 11). Stubs are `available:False`+reason — never faked.
- **Blockers:** none.
- **Phase 5 row status:** PASS (5A + 5B + 5C all PASS).

## Phase 6 — Signal & ML Engine (§52/§53/§54/§55/§56 + market-wide regime)

- **Goal:** A curation + greenfield surface over the V1/V2 signal pipelines. (1) §52 catalog the useful V7 signals (preserve, do NOT add indicators merely because V7 has them) and DEFER news/sentiment to Phase 10. (2) §53 horizons — SHORT_TERM/SWING/MEDIUM_TERM — with targets/features aligned to the horizon. (3) §54 ML Engine: track 10 fields, ship a simple baseline first, gate complex-model promotion behind OOS improvement (the backtest is Phase 7 §57, so ship the `OosGate` seam now). (4) §55 Calibration — define precisely what a probability means, and NEVER display LLM confidence as a statistical probability (enforced in code). (5) §56 Meta-labeling — primary LONG + meta DO_NOT_EXECUTE → final NO_TRADE. (6) Fill the market-wide regime gap (V7 only has per-ticker regime) with a THIRD taxonomy that does not touch V1/V2.
- **Design doc:** `docs/superpowers/specs/2026-08-12-v9-phase6-signal-ml-engine-design.md` (commit `f24aedf`). Plan: `docs/superpowers/plans/2026-08-12-v9-phase6-signal-ml-engine.md` (commit `ae3cc1a`). Decomposition: 6A (§53 horizons + §52 catalog + market-wide regime + Signal type) → 6B (§54 registry/baseline/OosGate seam + §55 calibration + §56 meta-labeling) → 6C (§51 signal-input wiring + §39 regime-tool market-wide path + config + control docs).
- **Approach:** Compose-not-rewrite (§5/§7). Two new packages — `core/signal_engine/` and `core/ml/` — compose the existing V1/V2 signal pipelines; no V1/V2 signal code is rewritten. §52 is a curation/surface phase: `SIGNAL_CATALOG` lists the spec's 16 indicators 1:1 with `IndicatorStatus` PRESERVED/EVALUATE/DEFERRED (news/sentiment DEFERRED to P10, ml EVALUATE). §53 `map_mode_to_horizon` maps V1 modes + 8 V2 strategy specs to SHORT_TERM/SWING/MEDIUM_TERM (unknown→SWING fail-safe) and `align_horizon` guards feature/horizon alignment. Market-wide regime uses a THIRD taxonomy `MARKET_RISK_ON/RISK_OFF/MIXED/TRANSITIONING` distinct from V1 (TREND/RANGE/LOW_VOL/VOLATILE/MIXED) and V2 (REGIME_TREND/RANGE/HIGH_VOL/EVENT_RISK/SESSION_BREAKOUT) so neither is touched; `MarketRegimeEngine.detect_market` rolls up per-ticker regimes with a `universe_min` stale guard; macro/VIX is an honest stub (V7 feed is crypto/1m). §54 `ModelRecord` carries the 10 fields + `is_simple_baseline` + `oos_verdict`; `OosGate.evaluate_oos_improvement` returns `DEFERRED_PHASE7` with no runner (the backtest is Phase 7); complex models cannot register with a promoting verdict and must go through `promote()` with a real available `OosVerdict` in {ROBUST, MODERATE}. The simple baseline is stdlib logistic GD (zero new deps §118). §55 `CalibrationResult.from_llm_confidence` RAISES `CalibrationError` (enforced in code); `ProbabilityDefinition` pins the precise meaning (e.g. `P(return > 0 over next N sessions)`); stdlib PAV isotonic `calibrate` returns a statistical probability with a step mapping. §56 `MetaLabeler.combine` (DO_NOT_EXECUTE/INSUFFICIENT→NO_TRADE, EXECUTE→primary, unknown→NO_TRADE). 6C wires the §51 `signal` input to a real `Signal` overlay (`_signal_input`) when the item carries signal fields (honest stub when absent), adds the `universe_regimes` path to the §39 `get_market_regime` tool, and adds the `signal_engine`/`ml` config blocks.
- **Files:** `core/signal_engine/__init__.py`, `horizons.py`, `catalog.py`, `regime.py`, `types.py` (NEW); `core/ml/__init__.py`, `registry.py`, `baseline.py`, `calibration.py`, `meta_label.py`, `qlib_adapter.py` (NEW); `core/scoring_system.py` (additive `run_weight_calibration_audit` — stub evolved to delegate to `core.ml`, original keys preserved); `core/pre_earnings/decision.py` (additive `_signal_input`); `core/ai/tools/market_data.py` (additive `universe_regimes` + `market_engine`); `core/config.py` + `config.example.json` (`signal_engine` + `ml` blocks); `tests/unit/test_signal_engine.py` (10), `test_ml_registry_baseline.py` (6), `test_ml_calibration_meta.py` (6), additions to `test_pre_earnings_decision.py` (2) (NEW); `tests/golden/test_phase6a_golden.py` (GOLD-052/053, 4), `test_phase6b_golden.py` (GOLD-054/055/056, 8) (NEW); control docs.
- **Not modified:** V1/V2 signal pipelines (`compute_signal_score`, `ScoreResult`, `CandidateSignal`, `RegimeEngine.detect`, `detect_market_regime`, `context_builder.build_context`, `SignalManager`), the §39 regime tools' per-ticker path, and the agents — all composed, not changed; `requirements*.txt`/`pyproject.toml`/`setup.*` (empty deps diff).
- **Dependencies:** none new (§118 — stdlib only; numpy/pandas already in V7; Qlib is an import-guarded opt-in ADAPTER, not a dependency).
- **Tests executed:** curated `pytest tests/unit tests/integration tests/contract tests/golden tests/perf` (green at every task); full `pytest tests tests_e2e` at the exit gate.
- **V7 / deps audit:** V7 HEAD `f912a33` unchanged; deps diff `f24aedf..HEAD` empty; §4 forbidden-language sweep on all Phase-6 files → 0 hits.
- **Out of scope (deferred):** §57 OOS backtest → Phase 7 (OosGate seam shipped, DEFERRED_PHASE7); §63 strategy registry → Phase 8; news/sentiment indicators → Phase 10 (DEFERRED in the catalog); macro/VIX regime → honest stub. ML/calibration/meta-labeling are greenfield.
- **Blockers:** none.
- **Phase 6 row status:** PASS (6A + 6B + 6C all PASS).

## Phase 7 — Backtest & Validation Engine (§57/§58/§59/§60/§61)

- **Goal:** Consolidate the three V7 backtest surfaces behind one stable domain contract; compute the complete §58 metric set; enforce next-bar-open/no-look-ahead behavior; isolate TRAIN, VALIDATION, OUT_OF_SAMPLE, and FINAL_HOLDOUT data; add overfitting diagnostics; connect real OOS evidence to the Phase 6 `OosGate`; and permit an explicit no-strategy outcome for insufficient evidence.
- **Status:** PASS — all Phase 7 gates were executed successfully.
- **Existing V7 components reused:** `core.strategy_backtester.backtest` (authoritative faithful replay), `core.backtest_engine.BacktestEngine` (exploratory engine), `core.backtester.Backtester` (legacy V1), `core.walk_forward_validation.run_faithful_v2_technical_walk_forward`, `core.reserved_holdout_validation.run_reserved_holdout`, and `core.ml.registry.OosGate`/`ModelRegistry`.
- **Delivered:**
  - `core/backtest/types.py` — immutable domain types; no-look-ahead pinned on; financial values reject NaN/infinite input; backtest statistics are explicitly not probabilities.
  - `core/backtest/metrics.py` — deterministic §58 metrics, including net equity, CAGR, optional benchmark return, drawdown, risk-adjusted statistics, expectancy, exposure, turnover, and separate commission/slippage totals.
  - `core/backtest/engine.py` — runtime-checkable `BacktestEngine` Protocol plus fail-closed no-look-ahead evidence guard.
  - `core/backtest/adapters.py` — faithful and exploratory adapters over actual V7 result shapes. The faithful adapter filters trades to the requested OOS range. The legacy V1 adapter rejects produced trades because V1 enters on the signal bar close and cannot meet §57 next-bar-open timing.
  - `core/backtest/splits.py` and `walk_forward.py` — disjoint chronological roles, rolling non-overlapping OOS windows, separately scored final holdout, exact `validation_method="faithful_v2_walk_forward"` compatibility, and composition of the existing faithful validator when its dataframe/config are supplied.
  - `core/backtest/overfitting.py` — purged K-fold with embargo, parameter stability, sensitivity, PBO from explicit IS/OOS rankings, DSR, Holm/Bonferroni correction, and the shared verdict classifier. Missing IS evidence leaves PBO unavailable; ROBUST requires all named diagnostics.
  - `core/backtest/oos_gate_runner.py` — accepts authoritative metrics/results or executes a config through an injected engine; absent/non-authoritative evidence fails closed; a challenger must improve OOS expectancy and pass the overfitting verdict.
  - `core/backtest/no_forced_strategy.py` — TICKER → INDUSTRY → SECTOR → UNIVERSE fall-through with explicit `NO_VALIDATED_TICKER_SPECIFIC_STRATEGY` when no authoritative sample meets the configured minimum.
  - `DEFAULT_CONFIG["backtest_engine"]` and `config.example.json` — faithful default engine, §60 thresholds, and §61 minimum evidence sample.
- **Files created:** `core/backtest/` (10 modules including `__init__.py`); nine Phase 7 unit-test files; three Phase 7 golden-test files.
- **Files modified:** `core/config.py`, `config.example.json`, and V9 control documents only. The three legacy engines and their live API call sites were not changed.
- **Dependencies:** none new; dependency diff is empty.
- **Migrations:** none.
- **API changes:** none.
- **UI changes:** none; Backtest UI remains Phase 12/17 scope.
- **Tests added:** 73 tests (63 unit + 10 golden), including GOLD-057/058/059/060/061 and GOLD-054b; GOLD-001 no-look-ahead is also satisfied by `test_phase7a_golden.py`.
- **Tests executed:** dedicated Phase 7 selection; curated `python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf -q`; full `python -m pytest tests tests_e2e -q`; Python compilation; `git diff --check`; forbidden-marker scan; dependency diff; V7 source check.
- **Exact results:** dedicated `73 passed`; curated `2026 passed / 0 failed` in 186.91s (baseline `1953`, exactly +73); full `2553 passed / 0 failed / 36 skipped` in 245.38s. The 36 skips are pre-existing opt-in/manual tests and are recorded as skipped, not executed passes.
- **Bugs found and fixed:** the legacy V1 backtester performs same-bar entries; its V9 adapter now fails closed. Adapter mappings were corrected to actual V7 result fields and cost-model units. Domain values now reject NaN/infinite values before API/UI serialization.
- **Documented deviations from the implementation plan:** PBO is not calculated from duplicated OOS values when IS rankings are missing; ROBUST cannot be produced with absent PBO/DSR/stability evidence; rolling split indexes match the existing validator's recent-window math; legacy V1 trades are blocked instead of asserted safe.
- **Remaining work:** Phase 8 strategy registry/champion-challenger; Phase 12 Backtest/Strategy Lab UI; Phase 17 opt-in browser E2E with the server running.
- **Blockers:** none.
- **Phase 7 row status:** PASS (7A + 7B + 7C all PASS). Next: Phase 8.

## Phase 8 — Strategy Registry / Champion-Challenger (sections 62/63/64)

- **Goal:** Persist the complete strategy contract and lifecycle; prevent direct or single-backtest champion replacement; evaluate multi-factor promotion evidence; constrain Optuna research; and add an opt-in lifecycle gate to the existing V2 selector without manufacturing approved strategies.
- **Status:** PASS — all Phase 8 gates were executed successfully.
- **Existing components reused:** the eight executable `core.strategy_v2.registry.StrategySpec` definitions and evaluators; `StrategyPerformanceCache`; Phase 7 OOS/holdout/overfitting evidence; the existing selector and configuration merge behavior.
- **Delivered:**
  - `core/strategy_registry/types.py` — immutable, validated `StrategyRecord` with the exact 12 persisted fields; all seven lifecycle statuses; promotion evidence/threshold/decision contracts; immutable audit events; non-finite numeric evidence is rejected.
  - `core/strategy_registry/policy.py` — deterministic promotion fingerprint and named gates for OOS verdict/sample/expectancy/profit factor/drawdown/Sharpe, stability, costs, regime coverage, recent evidence, shadow evidence, untouched holdout, locked period/criteria, and strict incumbent comparison.
  - `core/strategy_registry/store.py` — schema-versioned JSON registry, process-wide per-path locking, atomic temporary-file replacement, corruption fail-closed behavior, legal transitions, append-only event history, one CHAMPION per horizon+universe scope, and atomic champion rotation. There is no delete API and no read creates a missing registry file.
  - `core/strategy_registry/bridge.py` — faithful mapping of all eight V2 definitions to versioned RESEARCH records. Seeding is explicit and idempotent; it never invents APPROVED/CHAMPION status or performance evidence.
  - `core/strategy_registry/optuna_policy.py` — research-only, persistent-study, no-final-holdout, bounded-trial/timeout, multi-objective risk-aware, locked-period, and no-auto-promotion guard. Optuna itself remains the Phase 14 integration; Phase 8 adds no dependency.
  - `core/strategy_v2/selector.py` — optional injected lifecycle registry. `lifecycle_enforcement_enabled` defaults to `False` for V7 compatibility while migrated definitions are RESEARCH. When enabled, only APPROVED/CHAMPION records enter evaluation; missing/corrupt/unapproved registry state fails closed and the accepted lifecycle version/status is attached to candidate audit metadata.
  - `DEFAULT_CONFIG["strategy_v2"]["lifecycle_enforcement_enabled"] = False`, mirrored in `config.example.json`.
- **Files created:** `core/strategy_registry/` (five modules plus `__init__.py`); six Phase 8 unit-test files; `tests/golden/test_phase8_golden.py`; Phase 8 design and plan documents.
- **Files modified:** `core/strategy_v2/selector.py`, `core/config.py`, `config.example.json`, and V9 control documents. Existing executable strategy definitions/evaluators and performance cache were not rewritten.
- **Dependencies:** none new; dependency diff is empty.
- **Migrations:** none. The registry file is created atomically only by an explicit mutation; no existing SQLite/JSON schema is altered.
- **API changes:** none.
- **UI changes:** none; lifecycle/strategy-lab UI remains Phase 12/16 scope.
- **Tests added:** 76 tests (73 unit + 3 golden), including GOLD-062/063/064 and the critical GOLD-015 bad-challenger rejection.
- **Tests executed:** dedicated Phase 8 selection; curated `python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf -q`; full `python -m pytest tests tests_e2e -q`; Python compilation; JSON validation; `git diff --check`; forbidden-marker scan; dependency diff; V7 source check.
- **Exact results:** dedicated `76 passed`; curated `2102 passed / 0 failed` in 203.43s (Phase 7 baseline `2026`, exactly +76); full `2629 passed / 0 failed / 36 skipped` in 243.20s. The 36 skips are the unchanged opt-in/manual/server-dependent tests, not executed passes.
- **Behavior note:** lifecycle enforcement intentionally ships off. Turning it on before a strategy version has passed research, OOS, and shadow promotion will reject that strategy; it will not silently fall back to RESEARCH evidence.
- **Remaining work:** Phase 9 capital/portfolio/risk; Phase 12 lifecycle UI; Phase 14 persisted Optuna execution; Phase 16 live shadow monitoring, degradation, automatic rollback, and recovery workflows.
- **Blockers:** none.
- **Phase 8 row status:** PASS. Next: Phase 9.

## Phase 9 - Capital and Risk Constitution (sections 47/48/65/66/67)

- **Goal:** Add deterministic capital sizing, exact accounting, complete portfolio constraints, protected owner-confirmed risk policy, and auditable position evidence/reassessment without rewriting V7 execution.
- **Status:** PASS - implementation and verification are complete, and the runtime now reports an explicit owner-confirmed Custom constitution. No value was selected or confirmed autonomously.
- **Existing components reused:** `core.capital_manager`, `agents.risk_agent`, `core.risk_limits`, `core.autonomy`, V7 paper ledger/execution, and Phase 8 strategy lifecycle evidence.
- **Delivered:**
  - `core/portfolio/types.py` - immutable finite contracts for all five sizing methods, portfolio snapshots/proposals, constraint decisions, exact accounting, position evidence, and HOLD/REDUCE/CLOSE assessments.
  - `core/portfolio/sizing.py` - fixed amount, fixed percentage, fixed risk, inverse volatility, and volatility targeting with transparent formulas. Advanced HRP/NCO/CVaR allocation remains absent until validated against simple baselines.
  - `core/portfolio/accounting.py` - exact LONG/SHORT margin, two-sided fee, P/L, and cash-conservation identities.
  - `core/portfolio/constraints.py` - validated owner confirmation plus all mandatory position, exposure, leverage, cash, sector, correlation, risk, count, liquidity, daily-loss, drawdown, data, strategy, Kill Switch, and live-permission checks.
  - `core/portfolio/adapter.py` - fail-closed conversion from app-authoritative V7 state and proposal evidence; no AI-supplied portfolio values.
  - `core/portfolio/position.py` - exact section 67 evidence persistence and deterministic reassessment; Kill Switch retains monitoring without forced liquidation.
  - `CapitalManager._apply_position_explainability` now persists all eight evidence fields on new positions while preserving existing fields and APIs.
  - `RiskAgent.evaluate_signal` composes the new veto only for a complete confirmed constitution, records its fingerprint/checks, and then continues through the legacy risk gates. Invalid confirmed configuration and incomplete evidence reject.
  - `DEFAULT_CONFIG` and `config.example.json` include only a null-valued `risk_constitution` template with `owner_confirmed: false`.
  - The Agents UI now exposes every protected field through a schema-driven Paper Trading editor. Values are never prefilled by the UI: the owner can save incomplete values as an inactive draft, then explicitly confirm a complete draft to apply it.
  - `PATCH /api/agents/risk/constitution` validates strict JSON types and cross-field invariants, persists draft/active documents atomically, and aligns overlapping legacy breakers only on explicit owner confirmation. Editing a draft never changes an already-active policy.
- **skfolio evaluation:** Phase 2's likely-integration decision was revisited. Phase 9 adds no optimizer dependency because no V9 OOS evidence yet proves HRP/NCO/CVaR superior to the delivered simple baselines. The future research integration remains conditional on that validation.
- **Files created:** `core/portfolio/` (six implementation modules plus `__init__.py`); eight new Phase 9 unit/golden test files; Phase 9 design and plan documents.
- **Files modified:** `agents/risk_agent.py`, `core/capital_manager.py`, `core/config.py`, `config.example.json`, Agents API/service/UI files, existing tests, and V9 control documents. `docs/V9_RISK_CONSTITUTION.md` was not modified.
- **Dependencies/migrations/API/UI:** no new dependencies and no migration; added the bounded Risk Constitution PATCH contract and the complete draft/confirm UI on the Agents page.
- **Tests added:** 97 collected regression cases above the Phase 8 full-suite baseline, including GOLD-006/007/008/009/016 and 19 owner-editor/API/service cases.
- **Tests executed:** dedicated Phase 9 selection; curated `python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf -q`; full `python -m pytest tests tests_e2e -q`; Python compilation; JSON validation; `git diff --check`; forbidden-marker scan; dependency diff; V7 source/state check.
- **Exact results:** original Phase 9 dedicated `109 passed`; owner-editor targeted `46 passed`; curated `2193 passed / 0 failed` in 172.77s; full `2726 passed / 0 failed / 36 skipped` in 236.11s. Browser verification covered empty initial state, disabled confirmation, cross-field validation, isolated draft persistence, explicit activation, desktop/mobile rendering, and zero console errors. The 36 skips are unchanged opt-in/manual/server-dependent tests.
- **Isolation:** V7 remains `f912a33` with its original 109 dirty paths; dependency diff is empty; forbidden-marker scan reports zero hits.
- **Owner control:** the active policy remains editable through a separate draft. **Save draft** is intentionally non-activating; only **Confirm and apply** can replace the active Custom policy.
- **Phase 9 row status:** PASS after live API/UI evidence confirmed a complete owner-selected active constitution.

## Phase 10 - News / FinBERT / SEC (sections 14/16/68/69)

- **Goal:** Preserve the working V7 News cockpit while making the evidence path modular, point-in-time, financially sentiment-aware, SEC-capable, and inert to instructions embedded in external text.
- **Status:** IN PROGRESS - implementation, real FinBERT inference, and regression gates pass; a live SEC request cannot be certified until the owner supplies the SEC-required `EDGAR_IDENTITY` in the runtime environment.
- **Existing components reused:** `agents.news_tab_agent` provider ingestion, normalization, entity matching, category/impact logic, cross-source deduplication, News UI/API, `core.sentiment` fallback, the Phase 1 point-in-time model/ledger, and the Phase 4 allow-listed AI tools.
- **Delivered:**
  - `core/news_v9/finbert.py` - maintained Transformers text-classification adapter for `ProsusAI/finbert`, immutable model revision, finite three-label validation, thread-safe lazy loading, and explicitly labelled V7 lexicon fallback.
  - `NewsTabAgent` now records sentiment source/model/revision/fallback/error and uses FinBERT when the pinned local model is available; fallback output is never represented as FinBERT.
  - `core/news_v9/pipeline.py` - deterministic normalize -> deduplicate -> entity match -> sentiment -> catalyst contract over existing provider mappings, including tracking-URL cleanup, PIT timestamps, rejection instead of field fabrication, and syndicated-copy grouping.
  - `core/sec/` - edgartools adapter, mandatory environment identity, ticker/form and SEC-host URL validation, canonical `SECFiling` metadata, atomic TTL cache, historical availability filtering, stale-cache behavior, and bounded fail-closed output.
  - `GET /api/sec/{ticker}` and the real `get_sec_context` AI tool replace the Phase 10 stub. No full filing body is downloaded implicitly.
  - `core/external_text.py` - News/SEC strings are always wrapped as `UNTRUSTED_EXTERNAL_DATA`; instruction-shaped strings are replaced by a digest before AI prompt composition. GOLD-012 now covers external data, in addition to the Phase 4 tool allow-list and downstream decision validation.
- **Open-source integration:** edgartools `5.48.0`; transformers `5.15.0`; torch `2.13.0`; `ProsusAI/finbert` revision `4556d13015211d73dccd3fdd39d39232506f3e43`. Python 3.14.3 installation and `pip check` pass. Upstream Apache-2.0 repository and its linked published model were verified; obsolete `pytorch_pretrained_bert` code is not used.
- **Files created:** `core/external_text.py`, `core/news_v9/`, `core/sec/`, `api/sec_service.py`, seven Phase 10 unit/golden test files, and Phase 10 design/plan documents.
- **Files modified:** News agent, canonical SEC entity, News/SEC AI tools and tool loop, API main, config/example/env sample, requirements/notices/manifest, existing tool/API contract tests, and V9 control documents.
- **Dependencies:** three pinned direct dependencies added to `requirements.txt`; installed and verified both in the test interpreter and the actual port-8003 runtime virtualenv.
- **Migrations:** none. The SEC cache is a new atomic runtime JSON artifact and is ignored by Git.
- **API changes:** additive `GET /api/sec/{ticker}?refresh=false`; invalid tickers return 422; missing identity/provider returns a bounded 200 `unavailable` payload so the News/API process stays operational.
- **UI changes:** none; the existing News cockpit remains active. Dedicated SEC/system-health UI belongs to Phase 12.
- **Tests executed:** focused Phase 10 and retained News tests; curated unit/integration/contract/golden/perf gate; full `tests tests_e2e`; Python compilation; JSON validation; `pip check`; real FinBERT inference; diff checks.
- **Exact results:** Phase 10 core `30 passed`; expanded Phase 10 + AI/API contract gate `96 passed`; curated `2227 passed / 0 failed`; final full `2759 passed / 0 failed / 36 skipped` in 254.47s. The first full attempt exposed the pre-existing Pre-Earnings contention timing defect; after the real timeout-budget fix, its contract passed 10 consecutive repetitions before the clean final full gate. Real pinned-model inference returned positive `0.9573`, negative `-0.9695`, and neutral `0.9094` confidence for three deterministic smoke sentences. The 36 skips are unchanged opt-in/manual/server-dependent tests.
- **Remaining owner action:** set a real SEC-compliant name/contact value in `EDGAR_IDENTITY`, then run the live SEC endpoint check. V9 does not invent or commit owner identity.
- **Blockers:** live SEC network acceptance only; News/FinBERT code and tests are not blocked.
- **Phase 10 row status:** IN PROGRESS pending the real SEC identity and live provider acceptance. Phase 11 may proceed without making SEC a critical trading runtime dependency.

## Phase 11 - Autonomous Paper Trading (sections 67/70/71/72/73/74)

- **Goal:** Put every automated Paper order behind one durable broker boundary with an explicit lifecycle, deterministic identity, reconciliation, owner controls, and safe position actions.
- **Status:** PASS - code and automated verification are complete, and live acceptance now reports owner-selected `PAPER_AUTO`, a healthy reconciliation, active owner-confirmed risk policy and an owner-selected REDUCE value.
- **Delivered:**
  - `core/execution/` defines `ExecutionBroker`, `PaperBroker`, strict intents/states/transitions, SQLite ledger schema v1, deterministic idempotency, startup/manual reconciliation, entry readiness, and HOLD/REDUCE/CLOSE execution.
  - Automated opens from `ExecutionAgent` and automated closes from `CapitalManager` now cross the PaperBroker boundary. Re-delivery of one scheduler decision returns the persisted order and never calls Capital twice.
  - `CapitalManager.reduce_position` performs proportional quantity/notional/margin/fee allocation and exact cash/P&L accounting. Existing-position REDUCE/CLOSE remains available when new risk is stopped.
  - Material local/broker disagreement records `EXECUTION_STATE_CONFLICT`; this blocks unsafe new automated risk without stopping monitoring.
  - Owner API: `GET/PATCH /api/execution/control`, `POST /api/execution/kill-switch`, `POST /api/execution/reconcile`, and bounded `GET /api/execution/orders`.
  - Agents UI renders backend modes, an empty owner-selected REDUCE percentage, readiness reasons, reconciliation, and a prominent **STOP AUTONOMOUS TRADING** control. There is no hardcoded trading percentage and no implicit PAPER_AUTO activation.
- **Files created:** `core/execution/`, `api/execution_service.py`, seven Phase 11 unit/golden test files, and Phase 11 design/plan documents.
- **Files modified:** execution/risk agents, CapitalManager, API/config, Agents UI, selected tests, ignore rules, and V9 control documents. `docs/V9_RISK_CONSTITUTION.md` remains untouched.
- **Dependencies:** none new.
- **Migration:** additive, idempotent SQLite execution-ledger schema v1 at configurable `paper_execution.store_path`; runtime DB/WAL files are ignored.
- **Verification:** final focused Phase 11/retained boundary gate `120 passed`; curated unit/integration/contract/golden/perf gate `2257 passed / 0 failed` in 182.88s; full `tests tests_e2e` gate `2789 passed / 0 failed / 36 skipped` in 255.69s; Python compilation, frontend syntax, JSON, diff, and dependency checks pass. The skips remain explicit E2E/manual/server-dependent tests. Live acceptance also exposed and fixed mixed naive/aware provider-bar timestamps; the next full Pre-Earnings cycle completed successfully with `last_error=null`.
- **Ongoing owner control:** the Agents UI can move back to ANALYZE/RECOMMEND, adjust REDUCE, reconcile, stop new autonomous risk or replace the Risk Constitution at any time. No profile or execution value auto-applies.
- **Phase 11 row status:** PASS after live owner activation and readiness/reconciliation evidence.

## Phase 12 - AI Trading Cockpit UI (sections 91-100)

- **Status:** PASS - the complete evidence-first Cockpit, its five workspaces, real health states and owner-controlled Risk Constitution profile picker are implemented and verified.
- **Backend:** additive cache-first `GET /api/cockpit`, five-second service cache, stable content version/unchanged polling, exact `LOADING`/`SUCCESS`/`EMPTY`/`ERROR` section states, real market-hours/capital/exposure/UTC daily P&L calculations and 15 non-synthetic health checks.
- **UI:** V9 command header; Overview; exact Pre-Earnings decision matrix and evidence drill-down; Strategy Lab; the eleven-question Decision Ledger; labelled Research Factory roadmap; System Health; refresh/routing; two-step Kill Switch UI wired to the existing real endpoint.
- **Honest phase boundaries:** absent IV/RV, SEC identity, ML registry, live broker, MLflow, RD-Agent, promotion history and Phase 13-16 evidence are rendered as unavailable/not configured/empty. Later-phase capabilities are labelled and are not clickable placeholders.
- **Owner-controlled profiles:** `risk_constitution_profiles.json` contains three complete Paper Trading templates (`Conservative`, `Balanced`, `Dynamic Paper`). The Agents UI adds those plus `Custom`; selection only fills the form, manual edits switch to Custom, and only explicit **Confirm and apply** activates. `MS_RISK_CONSTITUTION_PROFILES_PATH` supports an operator-maintained hot-reloadable profile document. No profile is compiled into JavaScript.
- **Tests:** 15 dedicated Cockpit tests plus Risk-profile/service/frontend/contract coverage. Targeted combined gate `86 passed`; curated `2269 passed`; full `2808 passed / 36 explicit skips`; zero failures. Python/JavaScript compilation, JSON validation and `git diff --check` pass.
- **Browser acceptance:** all five Cockpit tabs, real refresh/routing, exact empty/unavailable states, no invalid rendering tokens, four Risk choices, profile-to-form hydration, manual-edit-to-Custom, custom value restoration, incomplete activation feedback and zero console errors. No browser verification activated Kill Switch or submitted Risk settings.
- **Owner state:** browser acceptance observed the owner's current live choices as `PAPER_AUTO`, `REDUCE 20%`, active custom Risk Constitution and Kill Switch off; they were not changed by implementation or verification.
- **Remaining work:** Phase 13 persistent point-in-time Decision Ledger, Phase 14 Research Factory, Phase 15 Coding/Review agents and Phase 16 Shadow/rollback remain explicitly deferred.

## Phase 13 - Decision Ledger / Model Lab / AI Replay (sections 88/89/90/97)

- **Goal:** Make every decision — executed, rejected or NO_TRADE — a persistent, point-in-time, queryable record; turn the record into an evaluation asset (AI replay dataset, rejection-quality measurement, per-role model comparison); replace the Phase 12 computed ledger view with persistent evidence.
- **Status:** IN PROGRESS - code, tests and API/UI integration are complete and green; PASS additionally requires a live owner-observable cycle writing real ledger entries and a live Model Lab run with real configured providers (both are runtime-evidence steps, not code).
- **Existing components reused:** Phase 1 `HistoricalStore`/`pit.as_of` (the §29/§32 PIT contract), Phase 3C `generate_with_fallback`, Phase 4 `evaluate_trade_decision`/`run_pipeline`/`run_critic`, `build_signal_decision_context` + `decision_id` (already the execution-ledger join key), Phase 11 `ExecutionStore` SQLite pattern, Phase 12 cockpit read model + `_section` state contract.
- **Delivered:**
  - `core/decision_ledger/store.py` - SQLite schema v1 (WAL, `BEGIN IMMEDIATE`, idempotent migration): append-only entries (idempotent on `decision_id`), append-only outcome evaluations (latest wins on read, history kept), §88 model-lab runs/results; env `MS_DECISION_LEDGER_PATH` > config `decision_ledger.store_path` > default.
  - `core/decision_ledger/types.py` - frozen `LedgerEntry`/`LedgerOutcome`, bounded §41/§42/§44 projections, digests, capped prompt text, `eleven_questions()`.
  - `core/decision_ledger/recorder.py` - decision_context + engine artifacts → §97 entry; fail-open for the trading path; idempotent redelivery.
  - Prompt/model capture: `EvaluationResult` gains defaulted `prompt`/`model`/`prompt_version` (every existing 5-positional-arg call stays valid); `TRADE_REVIEW_PROMPT_VERSION = "trade_review_v1"` in `core/ollama_decision.py`.
  - `agent_manager` hook right after decision-context build on BOTH accepted and rejected paths (§90); runtime DB git-ignored; tests redirect via `conftest.py`.
  - `core/decision_ledger/replay.py` - §89: `build_pit_snapshot` (per-entity `as_of` with availability proofs, `UNAVAILABLE` never reconstructed), `run_ai_replay` (REPLAY entry linked via `replay_of`, explicit no-order evidence, never reaches the broker), `evaluate_decision_outcome` (deterministic forward-return over bars strictly after T; insufficient data stays INCONCLUSIVE).
  - `core/decision_ledger/no_trade_eval.py` - §90 partition: correctly_rejected / incorrectly_rejected_profitable / inconclusive + rejection_precision.
  - `core/decision_ledger/model_lab.py` - §88: per-role runs over ONE frozen snapshot (`inputs_sha256` pinned, identical prompts proven in tests); per-role summaries only — no global score; providers injected, missing provider recorded unavailable, never simulated.
  - API: `GET /api/ledger/decisions`, `GET /api/ledger/decisions/{id}`, `GET /api/ledger/no-trade-evaluation`, `GET /api/ledger/model-lab`, `POST /api/ledger/replay` (422/404 bounded errors; replay is an audit artifact).
  - Cockpit: `ai_decisions` is persistent-ledger-first (`coverage: PERSISTENT_LEDGER`, Phase 12 computed rows remain fallback); new `decision_replay` + `model_lab` sections replace the `deferred` PHASE_13 stubs; frontend ledger tab renders replays and per-role Model Lab runs under the existing LOADING/SUCCESS/EMPTY/ERROR contract.
- **Files created:** `core/decision_ledger/` (six modules), `api/ledger_service.py`, six unit-test files, `tests/golden/test_phase13_golden.py`, Phase 13 design + plan documents.
- **Files modified:** `core/ai/decision_engine.py`, `core/ollama_decision.py`, `core/agent_manager.py`, `api/main.py`, `api/cockpit_service.py`, `frontend/cockpit.js`, `tests/conftest.py`, `.gitignore`, `tests/unit/test_pre_earnings_core_hardening.py` (time-bomb fixture fix, see below), V9 control documents. `docs/V9_RISK_CONSTITUTION.md` untouched.
- **Pre-existing defect found and fixed:** the two `test_pre_earnings_core_hardening` failures on clean HEAD (verified in an isolated `git worktree` of `b1129e9`) were a test time-bomb, not a product regression — `_rich_item` pinned `earnings_time_utc="2026-08-06"`, which drifted out of the 14-day window against the `now()+5d` cycle item. The fixture now derives the default from the same clock as `_direction_item`, preserving the regression contract.
- **Dependencies:** none new. **Migrations:** additive SQLite ledger schema v1 (idempotent, GOLD-018-style pattern; runtime file ignored by Git).
- **Tests added:** 36 focused Phase 13 cases (store/recorder/replay/no-trade/model-lab/API) + 3 golden tests.
- **Tests executed:** Phase 13 selection `36 passed`; ledger API + cockpit contracts `17 passed`; pre-earnings hardening `22 passed`; curated unit/integration/contract/golden/perf gate `2313 passed / 0 failed` after the fixture fix (the first curated run reproduced the 2 clean-HEAD failures, proving them pre-existing); full `tests tests_e2e` gate `2859 passed / 0 failed / 36 skipped` in 237.95s (Phase 12 baseline: 2808). Python compilation, `node --check`, `git diff --check`, forbidden-marker scan and the requirements diff (unchanged) all pass.
- **Golden coverage:** GOLD-002 PASS; GOLD-005 re-pointed honestly to the existing Phase 5A §50 freeze; GOLD-003/004 (BMO/AMC) and GOLD-011 (stale data) remain explicitly open rows.
- **Remaining work:** live runtime evidence (a real autonomous cycle writing ledger entries; a live Model Lab run with owner-configured providers); wiring the Phase 8 strategy-registry / Phase 7 backtest evidence into the §51 pre-earnings context stubs; GOLD-003/004/011 owner phases.
- **Blockers:** none.

## Phase 14 - Research Factory (sections 75/76/77/78/79/111 + 64 Optuna)

- **Goal:** A dedicated research subsystem, separate from production execution: weakness detection -> structured hypothesis -> governed experiment -> deterministic evaluation -> REJECT/SHADOW handoff, plus drift monitoring (REVALIDATION only) and research memory.
- **Status:** IN PROGRESS - code, tests, API/UI integration complete and green; PASS additionally requires a live owner-triggered cycle against real journal data and a persisted Optuna study on real research data (runtime-evidence steps).
- **Delivered:**
  - `core/research_factory/hypothesis.py` - §76 frozen records; validation rejects vague hypotheses (every field, explicit promotion/failure criteria, metrics, horizon enum); atomic JSON store (idempotent append).
  - `core/research_factory/experiments.py` - §77 nine-status lifecycle with a legal-transition table and an immutable event log; SHADOW/PROMOTED bridge points remain gated by Phase 8 policy.
  - `core/research_factory/weakness.py` - §111 deterministic observations over the closed-trade journals (expectancy/win-rate vs explicit thresholds, minimum sample, INSUFFICIENT_SAMPLE honest); observations are the only hypothesis seeds.
  - `core/research_factory/drift.py` - §79 rolling-window drift verdicts (STABLE/DRIFT_SUSPECTED/INSUFFICIENT_SAMPLE) + REVALIDATION recommendations; never a risk change.
  - `core/research_factory/memory.py` - §78 relationship index (experiments/hypotheses/strategies/decision-ledger links/registry events); numeric facts stay in their structured stores.
  - `core/research_factory/tracking.py` - `ResearchTracking` seam: JSON collector default; optional lazy MLflow adapter behind `MLFLOW_TRACKING_URI` (§17 boundary; V9 functional without it).
  - `core/research_factory/optuna_runner.py` - §64 persisted bounded studies behind the Phase 8 `validate_optuna_study` guardrail (multi-objective incl. performance+risk, no final holdout, no auto-promotion, hard caps); lazy import with honest OPTUNA_UNAVAILABLE.
  - `core/research_factory/pipeline.py` - §111 `run_research_cycle`: weakness -> auto-drafted complete hypothesis -> PROPOSED -> APPROVED_FOR_RESEARCH -> RUNNING -> evaluator verdict -> VALIDATED->SHADOW or REJECTED; default evaluator rejects with INSUFFICIENT_EVIDENCE rather than guessing (§61).
  - API: `GET /api/research/{weaknesses,hypotheses,experiments,drift,memory}` + bounded `POST /api/research/run-cycle`; cockpit research section flips to real factory evidence (hypothesis/experiment counts, status counts, revalidations) and the `research_factory: PHASE_14` deferred marker is removed.
  - Optuna 4.9.0 pinned in `requirements.txt` (Phase 2 decision, research-only consumer); runtime artifacts git-ignored; tests redirected via `MS_RESEARCH_HYPOTHESES_PATH`/`MS_RESEARCH_EXPERIMENTS_PATH`.
- **Files created:** `core/research_factory/` (nine modules), `api/research_factory_service.py`, three unit-test files, `tests/golden/test_phase14_golden.py`, design + plan docs.
- **Files modified:** `api/main.py`, `api/cockpit_service.py`, `tests/conftest.py`, `.gitignore`, `requirements.txt`, V9 control documents.
- **Tests executed:** Phase 14 selection `17 passed`; research API + cockpit + frontend contracts `14 passed`; full `tests tests_e2e` gate `2878 passed / 0 failed / 36 skipped` in 232.31s (Phase 13 baseline: 2859). Python compilation, `pip check` (optuna 4.9.0 clean) and the marker scan pass.
- **Golden:** `test_phase14_golden.py` freezes the §77 nine-status lifecycle and the §111 deterministic journey (status sequence, hypothesis completeness, weakness facts, no-promotion note).
- **RD-Agent boundary:** the internal architecture implements the §3/§75 loop; RD-Agent remains an optional external research worker (Phase 2 decision) - production trading never depends on it (§12).
- **Remaining work:** live cycle + persisted study on real data; Phase 15 coding/review agents; Phase 16 shadow monitoring/promotion/rollback (GOLD-017).
- **Blockers:** none.

## Phase 15 - Autonomous Development Factory (sections 80-84, 112)

- **Goal:** Autonomous code changes exist ONLY in isolated git worktrees, pass a goal-hacking diff guard, and are reviewed by agents distinct from the implementer; the journey ends at SHADOW_READY (promotion is Phase 16).
- **Status:** IN PROGRESS - framework, guards, review ledger, API and golden coverage are complete and green; PASS additionally requires a live AI-authored submission through the full chain with real configured coding/review agents (runtime evidence).
- **Delivered:** `core/development_factory/` — `sandbox.py` (§80 worktrees on `ai/experiment/EXP-*` branches outside the production tree, prefix-checked writes, clean-apply-only diffs), `policy.py` (§82 PROTECTED_FILE, §84 GOLDEN_TEST_MODIFIED + skip/xfail TEST_WEAKENED, §83 EVALUATION_WEAKENED cost-removal guard, fail-closed), `review.py` (§81 five-stage append-only ledger CODE_REVIEW→SECURITY_REVIEW→TESTS→QUANT_VALIDATION→E2E; author can never record a stage; each stage only by its designated actor; SHADOW_READY requires all-pass with >= 2 distinct actors), `pipeline.py` (§112 submit_change: guard → sandbox → open reviews; HTTP endpoint is guard+ledger only, `apply=False`).
- **API:** `GET/POST /api/development/submissions`, `POST /api/development/submissions/{id}/review`; cockpit Coding/Review Agent capabilities flip to AVAILABLE.
- **Golden:** `test_phase15_golden.py` — §80 isolated-worktree journey and §112 sandboxed pipeline with independent review + protected diff never entering the sandbox.
- **Tests:** unit `13 passed` + golden `3 passed`; full `tests tests_e2e` gate `2898 passed / 0 failed / 36 skipped` in 256.21s (Phase 14 baseline: 2878).
- **Remaining work:** live AI-authored submission through the chain; Phase 17 E2E journey automation.

## Phase 16 - Shadow / Promotion / Rollback (sections 85/86/87, GOLD-017)

- **Goal:** Identical-condition shadow comparison, evidence-gated promotion through the locked Phase 8 gates, and automatic rollback that restores the last valid champion.
- **Status:** IN PROGRESS - code and GOLD-017 pass in the automated suite; PASS additionally requires a live promotion+rollback observation on real shadow data (runtime evidence).
- **Delivered:** `core/promotion/records.py` (§87 promotion records with validated rollback conditions — numeric thresholds, fixed kind set, mandatory; idempotent single rollback) and `core/promotion/lifecycle.py` (`compare_shadow` §85 pairing on identical `inputs_sha256` only; `promote_from_shadow` §86 SHADOW-only + injected Phase 8 decision, rejection leaves no trace; `evaluate_rollback` §87/GOLD-017 deterministic breach → ROLLED_BACK + `restored_version` + `risk_limits_changed:false`).
- **API:** `GET /api/promotion/records`, `POST /api/promotion/records/{id}/evaluate-rollback`; the cockpit `shadow_promotion_rollback` deferred marker is removed (capability AVAILABLE).
- **Golden:** `tests/golden/test_phase16_golden.py` — GOLD-017 (healthy no-rollback; breach restores v1; experiment ROLLED_BACK; risk untouched; idempotent) + §86 gate-rejection freeze.
- **Tests:** unit `5 passed` + golden `2 passed`; full `tests tests_e2e` gate `2898 passed / 0 failed / 36 skipped` (see Phase 15 row).

## Phase 17 - Complete Regression / E2E (constitutional rows closed)

- **Goal:** Close the remaining constitutional golden rows and re-run the full regression surface.
- **Status:** IN PROGRESS - GOLD-003, GOLD-004 and GOLD-011 are now pinned and PASS, making all 19 spec §107 golden rows PASS in the automated suite; the full `tests tests_e2e` gate re-runs below. Remaining Phase 17 items: opt-in browser E2E with the live server, and the owner-visible live acceptance evidence for Phases 13-16 runtime surfaces.
- **Delivered:** `tests/golden/test_phase17_golden.py` — GOLD-003 (BMO reportable only from 14:30 UTC; ambiguous hint never reportable), GOLD-004 (AMC only from 22:00 UTC), GOLD-011 (engine Data Freshness Guard: per-interval stale thresholds reject before signal generation; empty data rejected outright). Existing behaviors were pinned, not changed. Full `tests tests_e2e` gate: `2901 passed / 0 failed / 36 skipped` in 256.54s (Phase 15-16 baseline: 2898); golden suite `95 passed`.

## Phase 18 - Security / Performance / Hardening (2026-08-16)

- **Status:** PASS — executed checks, all green.
- **Executed:** §106 secrets scan over all 878 committed files (zero hits for key patterns); `config.json`/`.env` confirmed git-ignored with empty-key `config.example.json`; `python -m pip check` clean (incl. optuna 4.9.0); `node --check` over every `frontend/*.js`; perf budgets `3 passed`; full regression gate green.
- **Already golden-pinned:** §40 AI allow-list (Phase 4), GOLD-012 prompt-injection quarantine (Phase 10), GOLD-016 protected-policy guard (Phase 9), §82/§83/§84 diff guard (Phase 15).

## Phase 19 - Final Acceptance (2026-08-16)

- **Status:** IN PROGRESS — per §122 the project is **NOT DONE** until every critical row passes. Current honest tally: **20 / 28 acceptance areas PASS** (see `docs/V9_TEST_MATRIX.md` and `docs/V9_FINAL_REPORT.md`).
- **Live verification executed today (owner server, port 8003, new code):**
  - `POST /api/research/run-cycle` ran the §111 pipeline over the 60 real closed trades: 0 weaknesses -> 0 experiments (honest; no hypothesis invented without weakness), drift verdicts STABLE/INSUFFICIENT_SAMPLE.
  - `/api/ledger/*`, `/api/research/*`, `/api/promotion/*` respond live; Decision Ledger renders the honest `PARTIAL_PHASE12_FALLBACK` (persistent store empty until the first real autonomous decision).
  - Cockpit browser-verified: 5 workspaces with real data (PAPER_AUTO, capital $11,738.35, exposure 53.1%, AAOI +11.89%), Research Factory capabilities Phase 14/15/16 all AVAILABLE with hypotheses/experiments EMPTY (honest), System Health 15 real checks (overall Degraded — SEC/ML/MLflow/RD-Agent honestly Not Configured), zero §99 forbidden tokens, navigation buttons functional, screenshot captured.
- **Exact open items blocking DONE:**
  1. SEC live acceptance — owner chose to omit `EDGAR_IDENTITY` for now (Phase 10 stays IN PROGRESS).
  2. Decision Ledger first real entry — requires one live autonomous signal decision (market closed at verification time).
  3. Model Lab first live comparison run with real configured models.
  4. Research Factory first governed experiment on real weakness data; live persisted Optuna study.
  5. Autonomous Development first AI-authored submission through the 5-stage review chain.
  6. SHADOW/ROLLBACK first live promotion + rollback observation.
  7. ML persisted trained model (requires a real training run).
