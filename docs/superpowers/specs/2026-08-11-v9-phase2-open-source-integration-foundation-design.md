# Phase 2 — Open-Source Integration Foundation (Design)

**Date:** 2026-08-11
**Phase:** 2 (Open-Source Integration Foundation). Phase 1 (Data & Architecture Foundation) = PASS as of `f1d65d4` (1A `41bf30b` + 1B `10a3ef6` + 1C `f1d65d4`; 1645 tests green).
**Spec basis:** `prompt_to_follow.md` §10 (Open-Source Integration Policy — the 13-step per-project evaluation + the 9-field manifest), §11–§23 (the 13 mandatory/optional repos), §24 (THIRD_PARTY_NOTICES.md), §25 (dependency pinning), §118 (Phase 2 is the open-source manifest phase — the phase that MAY add new open-source deps), §5 (no rewrite — evolve V7), §7 (extend), §23 (do not replace working V7 providers), §106 (no secrets), §115 (no test weakening), §117 (V7 read-only), §4 (no forbidden success language).

## 1. Problem

Phase 0 stubbed `docs/V9_OPEN_SOURCE_MANIFEST.md` with all 13 repos at `decision = PENDING` and `commit/release/license = TBD`, and explicitly deferred license/feature evaluation to Phase 2 (§118). Phase 1 added **no** new open-source deps (stdlib + the pandas already in V7) precisely because Phase 2 is the designated phase for open-source evaluation. Phase 2 now must: for each of the 13 repos, perform the §10 13-step evaluation (inspect current repo, license, latest stable release, supported Python/runtime versions, dependency conflicts, security, maintenance, exact feature required, compare against existing V7 functionality), choose INTEGRATE | ADAPTER | REFERENCE_ONLY | REJECT, pin version/commit, add tests where code is touched, and record the decision in the manifest. Then generate `THIRD_PARTY_NOTICES.md` (§24) and record pinning (§25).

**Phase 2 produces DECISIONS + the manifest + notices + pinning. It does NOT write integration code.** Each adoption's actual adapter/integration code lands in its **consuming phase** (the manifest decision is the authority the consuming phase implements against). This respects §5/§7 (evolve, no premature rewrite) and the spec's own phase ownership (Qlib→ML/research phases, skfolio→Phase 9 capital, edgartools/FinBERT→Phase 10, MLflow→Phase 14 Research Factory, Optuna→Phase 6/14, VectorBT→Phase 7 backtest). Phase 2's "dependency experiments" (§10 step "dependency experiments" / Phase 2 title) = the real inspection spikes that PROVE a repo can be adopted in V9's environment (license OK, Python 3.14.3-compatible or has a viable install path, no hard dependency conflicts with V7's pinned stack) — recorded as the manifest's `Commit/release`, `License`, and `Dep impact` fields, not as merged integration code.

## 2. The 13 repos and their spec-given guidance

| # | Repo | § | Spec guidance (preliminary integration type) | Consuming phase(s) |
|---|---|---|---|---|
| 1 | Microsoft Qlib | §11.1 | ADAPTER (likely); do not make domain logic depend on Qlib directly; V9 data model authoritative | Phase 6 (Signals/ML) / Phase 14 (Research Factory) |
| 2 | Microsoft RD-Agent | §12 | MANDATORY EVALUATION; research only, NOT direct execution; may need Docker/WSL/isolated Linux worker; production trading must run if RD-Agent offline | Phase 14/15 (Research/Dev Factory) |
| 3 | skfolio | §13 | INTEGRATE (likely); portfolio opt / HRP / NCO / CVaR; compare vs equal-weight / inverse-vol baselines | Phase 9 (Capital/Risk) |
| 4 | edgartools | §14 | INTEGRATE (likely, US equities); configure SEC identity; respect SEC access rules; cache responsibly | Phase 10 (News/FinBERT/SEC) |
| 5 | Earnings-Volatility-Calculator | §15 | SELECTIVE (methodology only); do NOT import Tkinter UI or scraping; one authoritative V9 implementation per calculation | Phase 5 (Pre-Earnings) — methodology reference |
| 6 | FinBERT | §16 | INTEGRATE via modern Transformers pipeline; do NOT use obsolete `pytorch_pretrained_bert` | Phase 10 (News/FinBERT/SEC) |
| 7 | MLflow | §17 | ADAPTER (strongly preferred for Research Factory); V9 keeps own `AIProvider` interface; MLflow AI Gateway optional, evaluated in Phase 3 | Phase 14 (Research Factory) / Phase 3 (AI Gateway) |
| 8 | Optuna | §18 | INTEGRATE (likely); research/training data only; NEVER final holdout; multi-objective (OOS return, drawdown, Sharpe, stability, turnover, costs); trial budgets | Phase 6 / Phase 14 |
| 9 | RiskLabAI.py | §19 | MANDATORY TECHNICAL EVALUATION; **license MUST be verified before any code use**; if incompatible → published methodology + independent implementation + tests | Phase 7 (backtest robustness) — likely REFERENCE_ONLY unless license clean |
| 10 | vectorbt | §20 | OPTIONAL ADAPTER ONLY; license reviewed carefully; not required for core V9; `BacktestEngine` interface + `InternalBacktestEngine` + `VectorBTAdapter`; if rejected V9 stays functional | Phase 7 (Backtest) |
| 11 | quant-garage | §21 | REFERENCE_ONLY by default; do NOT copy/vendor/hidden-dependency; concepts/methodology only | Phase 5/7 (reference) |
| 12 | River | §22 | OPTIONAL; detection only initially (concept-drift), not automatic model mutation | Phase 6 (Signals/ML) — optional |
| 13 | CCXT | §23 | OPTIONAL; only if V7 crypto execution insufficient; do NOT replace working V7 providers; subject to Risk Constitution | Phase 11 (Paper Trading) / live — optional |

**Count note:** the memory/Phase-0 doc says "12 open-source repo evaluations"; the spec §11–§23 actually lists 13 (11 mandatory + 2 optional, with quant-garage reference-only). Phase 2 evaluates all 13.

## 3. Evaluation framework (§10, operationalized)

For each repo, Phase 2 records the §10 9-field manifest row by performing:

1. **Inspect current repo** — WebFetch the GitHub repo page (latest default-branch HEAD, last commit date, maintenance status, open issues/PRs signal).
2. **License** — read `LICENSE` / repo metadata (§19 RiskLabAI: do NOT assume; if unclear/incompatible → REFERENCE_ONLY or REJECT).
3. **Latest stable release** — the most recent tagged stable release (semver) and its date; for repos with no releases, the inspected commit SHA + date.
4. **Supported Python/runtime versions** — **CRITICAL for V9 (Python 3.14.3)**: does the repo declare 3.14 support in `setup.py`/`pyproject.toml` classifiers? If not, is there a viable install path (e.g. pure-Python, or a wheel that builds on 3.14)? Python 3.14 is new (Oct 2025); many repos will not yet declare it — this is a real `dependency impact` finding, not a blocker if the repo is pure-Python or integration is deferred to a consuming phase that can pin a compatible runtime.
5. **Dependency conflicts** — overlap/conflict with V7's pinned stack (pandas, yfinance, fastapi, uvicorn, ollama, pytest, etc.); heavy transitive deps (torch for FinBERT, ray/dask for vectorbt, mlflow's large surface).
6. **Security implications** — known CVEs, telemetry/phone-home, network behavior; for execution libs (CCXT) the Risk Constitution gate.
7. **Maintenance status** — last commit date, release cadence, open-issue triage, maintainer responsiveness.
8. **Exact feature required** — the narrow V9 need (not "all of Qlib" but "Qlib's alpha/feature pipeline behind an adapter").
9. **Compare against existing V7 functionality** — §7/§23: does V7 already do this? (V7 has 4 backtesters, yfinance/FinnHub/Alpaca/Binance/KuCoin providers, news + pre-earnings agents.) Do not duplicate working V7 functionality to say a repo is integrated.

Then: **decision** (INTEGRATE | ADAPTER | REFERENCE_ONLY | REJECT) + **reason** + **pin** (release/commit + date inspected) +, where code is touched in Phase 2, **tests**. Phase 2 touches no integration code (decisions only), so the "add tests" step applies to the manifest/decision artifacts themselves (a test that the manifest has no `PENDING`/`TBD` rows and every decided-INTEGRATE/ADAPTER repo has a pinned release + verified license) — a small contract test guarding the manifest's completeness.

## 4. Decision criteria (autonomous, per spec)

- **INTEGRATE** — the repo is a direct production dependency of a consuming phase, license is permissive (MIT/Apache-2.0/BSD/ISC) and verified, Python-3.14-compatible (or pure-Python with a viable path), no hard conflict with V7's stack. The consuming phase `pip install`s the pinned version against this manifest row.
- **ADAPTER** — the repo is used behind a V9-owned interface (§11 Qlib "V9 data model authoritative"; §17 MLflow "V9 keeps own AIProvider"; §20 vectorbt "BacktestEngine interface + InternalBacktestEngine + VectorBTAdapter"). Same license/Python bar as INTEGRATE. Integration code = the adapter, in the consuming phase.
- **REFERENCE_ONLY** — methodology/concepts only; no code copied, no vendoring, no hidden dependency (§15 selective methodology, §21 quant-garage, §19 RiskLabAI when license is incompatible/unclear). License inspection still recorded but no dep added.
- **REJECT** — license incompatible/unclear AND no clean independent-implementation path, OR Python-3.14-incompatible with no viable path AND needed short-term, OR duplicates working V7 functionality (§23) with no material improvement. V9 stays fully functional without it (§20 vectorbt rule, generalized).

## 5. Per-repo preliminary decision (from spec guidance + V9 codebase; finalized by inspection in execution)

These are the *preliminary* decisions the design commits to, to be confirmed/adjusted by the real §10 inspection during execution:

1. **Qlib** — **ADAPTER**. ML/research pipeline behind a V9 adapter; V9 data model authoritative (§11). Heavy dep surface (pytorch, lightgbm, pandas) — integration deferred to Phase 6/14; Phase 2 records the decision + pin + Python-3.14 compat finding. Likely Phase-2 finding: Qlib may not yet declare 3.14; consuming phase pins a compatible runtime or waits for Qlib 3.14 support.
2. **RD-Agent** — **REFERENCE_ONLY** (Phase 2) → likely ADAPTER in Phase 14/15. RD-Agent is a research/autonomous-coding agent server; it needs Docker/WSL/isolated Linux (§12) and must NOT be a hard dep of the trading engine. Phase 2: evaluate concepts + record that production stays operational if RD-Agent offline; no code dep added in Phase 2.
3. **skfolio** — **INTEGRATE** (Phase 9). Portfolio optimization; permissive license expected (check). Pure-Python-ish (sklearn dep). Phase 2 verifies license + 3.14 + sklearn compat; Phase 9 integrates.
4. **edgartools** — **INTEGRATE** (Phase 10). SEC EDGAR; configure SEC identity (§106: the identity is NOT a secret stored in code — env var). Phase 2 verifies license + 3.14; Phase 10 integrates with caching.
5. **Earnings-Volatility-Calculator** — **REFERENCE_ONLY** (methodology). §15: do NOT import Tkinter UI or scraping. Phase 2 records the methodology inventory (IV/RV, IV30/RV30, ATR, Yang-Zhang, term structure) for the Phase 5 authoritative V9 implementation; no code copied.
6. **FinBERT** — **ADAPTER** (modern Transformers pipeline, §16). Do NOT use `pytorch_pretrained_bert`. Phase 2 verifies the model license/terms (ProsusAI/FinBERT model weights license — `transformers` is Apache-2.0 but the *model weights* terms must be checked) + torch 3.14 compat (torch is the heavy dep). Phase 10 integrates behind a V9 sentiment interface.
7. **MLflow** — **ADAPTER** (Phase 14). V9 keeps own `AIProvider` (§17). MLflow has a large dep surface; Phase 2 verifies license (Apache-2.0) + 3.14 + the adapter boundary; Phase 14 integrates. MLflow AI Gateway → noted for Phase 3.
8. **Optuna** — **INTEGRATE** (Phase 6/14). Hyperparameter optimization; permissive license (MIT); the §18 research-data-only / never-final-holdout / multi-objective / trial-budget rules are recorded as constraints the consuming phase enforces. Phase 2 verifies 3.14 compat.
9. **RiskLabAI.py** — **REFERENCE_ONLY** (unless license verified clean). §19: license MUST be verified before code use; do NOT assume. Phase 2 inspects the license; if incompatible/unclear → REFERENCE_ONLY (published methodology + independent V9 implementation + tests in Phase 7). If clean permissive → ADAPTER in Phase 7.
10. **vectorbt** — **ADAPTER (optional)** (Phase 7). §20: license reviewed carefully; not required for core V9; `BacktestEngine` interface + `InternalBacktestEngine` (always) + `VectorBTAdapter` (optional). Phase 2 inspects vectorbt's license (this is the gate — vectorbt has had license questions) + 3.14 + the `ray`/`dask` heavy-dep impact. If license unclear → REJECT (V9 stays functional via `InternalBacktestEngine`).
11. **quant-garage** — **REFERENCE_ONLY** (§21, default). No copy/vendor/hidden-dep. Phase 2 records the concept inventory; no dep.
12. **River** — **REFERENCE_ONLY** (Phase 2) → optional ADAPTER in Phase 6. §22: detection only initially. Phase 2 verifies license + 3.14; Phase 6 may integrate for concept-drift detection.
13. **CCXT** — **REJECT** (Phase 2) unless V7 crypto execution is found insufficient. §23: do NOT replace working V7 providers (Binance/KuCoin REST paths work). V7 has crypto execution via Binance REST; no clear value from CCXT for V9's current scope. Decision: REJECT with reason "V7 crypto execution sufficient (§23); re-evaluate if a future phase needs unified multi-exchange execution". Subject to Risk Constitution if ever revisited.

**Net Phase 2 dep additions:** likely **zero new production deps merged in Phase 2** (all INTEGRATE/ADAPTER integrations land in consuming phases; Phase 2 only records decisions + pins). The manifest + notices + pinning are the Phase 2 deliverables. This keeps the 1645-test baseline green and adds the manifest-contract test(s).

## 6. What is and is not in scope

**In scope:**
- The §10 13-step evaluation for all 13 repos, performed by **real inspection** (WebFetch each repo's GitHub page + LICENSE + latest release + Python classifiers; WebSearch for maintenance/ CVE context where needed).
- The filled `docs/V9_OPEN_SOURCE_MANIFEST.md` — every row has a real `Commit/release`, `License`, `Files used` (none for REFERENCE_ONLY/REJECT), `Dep impact`, `Decision` (no `PENDING`), `Reason`. No `TBD`.
- `THIRD_PARTY_NOTICES.md` (§24) — every production dependency (V7's existing pinned stack + any Phase-2-decided additions) requiring attribution, with license + notice text.
- Dependency pinning records (§25) — for each evaluated repo: repository, commit/release, date inspected.
- A small **manifest-contract test** (`tests/contract/test_open_source_manifest.py`) asserting: (a) every manifest row has a non-`PENDING`/non-`TBD` decision; (b) every INTEGRATE/ADAPTER row has a pinned release/commit + a verified permissive license; (c) `THIRD_PARTY_NOTICES.md` exists and lists every INTEGRATE/ADAPTER repo. This is the §10 "add tests" step applied to the decision artifacts (Phase 2 touches no integration code, so the contract is the manifest itself).
- V9 control-doc updates: `V9_ARCHITECTURE.md` (open-source integration boundary section), `V9_IMPLEMENTATION.md` (Phase 2 record + Phase 2 row → PASS at gate).

**Explicitly OUT of scope (deferred, with phase owners):**
- Writing any INTEGRATE/ADAPTER integration code (adapters, `pip install`, import paths) → the consuming phases (Qlib→6/14, skfolio→9, edgartools/FinBERT→10, MLflow→14, Optuna→6/14, vectorbt→7, River→6). Phase 2 records the decision + pin; the consuming phase implements against the manifest row.
- RD-Agent runtime deployment (Docker/WSL/isolated worker) → Phase 14/15.
- The MLflow AI Gateway multi-provider evaluation → Phase 3.
- Re-evaluating CCXT for live unified execution → a future phase only if V7 crypto execution is found insufficient (Phase 11+).

## 7. Decomposition into tasks (preview for the plan)

1. **Task 1** — Manifest-contract test (failing first): assert the manifest has no `PENDING`/`TBD` rows, every INTEGRATE/ADAPTER row has a pinned release + permissive license, `THIRD_PARTY_NOTICES.md` exists. (TDD anchor for the whole phase.)
2. **Task 2** — Inspect repos 1–5 (Qlib, RD-Agent, skfolio, edgartools, Earnings-Volatility-Calculator): WebFetch GitHub + LICENSE + latest release + Python classifiers; record findings.
3. **Task 3** — Inspect repos 6–9 (FinBERT, MLflow, Optuna, RiskLabAI): same. (RiskLabAI license is the §19 gate.)
4. **Task 4** — Inspect repos 10–13 (vectorbt, quant-garage, River, CCXT): same. (vectorbt license is the §20 gate.)
5. **Task 5** — Fill `V9_OPEN_SOURCE_MANIFEST.md` with all 13 finalized rows (commit/release, license, dep impact, decision, reason, pin/date-inspected) + the `THIRD_PARTY_NOTICES.md` generation + §25 pinning section.
6. **Task 6** — Control-doc updates (V9_ARCHITECTURE open-source boundary section; V9_IMPLEMENTATION Phase 2 record) + exit gate (manifest-contract test green, full regression 1645+new green, V7 untouched, no merged deps, §4/§106 sweeps) → Phase 2 row PASS.

## 8. Risks and mitigations

- **Python 3.14.3 compat** (V9's runtime is very new) — many repos won't yet declare 3.14. Mitigation: Phase 2 records the compat finding per repo; INTEGRATE/ADAPTER decisions are conditioned on the consuming phase verifying a viable install path (pure-Python OK; compiled-extension repos may need a wait-for-3.14-support or a pinned compatible runtime). No Phase-2-merged dep means no immediate compat break.
- **License uncertainty (§19 RiskLabAI, §20 vectorbt)** — the gate. Mitigation: real `LICENSE` inspection; default to REFERENCE_ONLY/REJECT when unclear (§19 "do not assume"; §20 "V9 stays functional if rejected").
- **Heavy transitive deps** (torch/transformers for FinBERT; ray/dask for vectorbt; mlflow's surface; qlib's pytorch/lightgbm) — Mitigation: all land in consuming phases behind adapters, not as Phase-2 core deps; the manifest records the dep-impact warning for the consuming phase.
- **Stale inspection data** — GitHub repos move. Mitigation: §25 records `date inspected` + commit/release; the manifest-contract test guards completeness, not freshness (a periodic re-inspection is a later hygiene pass).
- **"Duplicate V7 functionality" (§7/§23)** — e.g. CCXT vs V7's working Binance/KuCoin. Mitigation: §10 step 9 (compare against V7) is explicit per repo; CCXT → REJECT with the §23 reason.

## 9. Out of scope reaffirmation (phase owners)

- Integration/adapter CODE for any adopted repo → its consuming phase (3/5/6/7/9/10/11/14/15).
- RD-Agent runtime (Docker/WSL/isolated worker) → Phase 14/15.
- MLflow AI Gateway multi-provider layer → Phase 3.
- CCXT live unified execution → future phase, only if V7 crypto execution insufficient.
- Periodic re-inspection of repos for upstream drift → later hygiene pass (post-Phase-19 or per-phase at consumption).

## Self-review

- **Spec coverage:** §10 (13-step eval + 9-field manifest) → Task 5 + the manifest; §11–§23 (13 repos) → Tasks 2–4 inspections + Task 5 decisions; §24 (THIRD_PARTY_NOTICES.md) → Task 5; §25 (pinning) → Task 5; §118 (Phase 2 = manifest phase) → the phase's whole scope; §5/§7/§23 → no integration code, no V7 duplication (CCXT reject); §106 → SEC identity as env-not-secret; §115 → no test weakening; §117 → V7 untouched; §4 → no forbidden language. ✓
- **Placeholder scan:** no TBD/TODO in the plan-to-be; the manifest's `TBD`/`PENDING` are the Phase-0 stubs Phase 2 fills (the contract test asserts they're gone). Clean.
- **Ambiguity check:** "dependency experiments" clarified = real inspection spikes recorded in the manifest, NOT merged integration code (integration = consuming phases). The "add tests" §10 step → the manifest-contract test. CCXT → REJECT (not "defer") with a concrete §23 reason. ✓

No issues found. Design is complete.