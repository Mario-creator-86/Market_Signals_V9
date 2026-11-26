# V9_OPEN_SOURCE_MANIFEST

Per spec §10, for every external project record: repository, URL, commit/release inspected, license, purpose, integration type (INTEGRATE | ADAPTER | REFERENCE_ONLY | REJECT), files/modules used, dependency impact, decision, reason.

Phase 0 left every row's decision blank and deferred license/feature evaluation to Phase 2 (§118). Phase 2 performed the §10 13-step evaluation for all 13 §11–§23 repos by inspecting each GitHub repository + LICENSE + latest release/PyPI metadata (Python 3.14 compatibility, dependency conflicts, maintenance status), compared each against existing V7 functionality (§7/§23), and recorded the decision + pin below. **Phase 2 merges no new production dependency** — every INTEGRATE/ADAPTER integration lands in its consuming phase against this manifest row. REFERENCE_ONLY = methodology/concepts only, no code copied, no vendoring, no hidden dependency. REJECT = V9 stays fully functional without it.

Date inspected (all rows): 2026-08-11. V9 runtime: Python 3.14.3.

| Repository | URL | Commit/release | License | Purpose | Integration type | Files used | Dep impact | Decision | Reason |
|---|---|---|---|---|---|---|---|---|---|
| Microsoft Qlib | https://github.com/microsoft/qlib | 0.20.1 (2026-04-21) | BSD-3-Clause | ML / quant research pipeline, feature/model research, alpha, training, research backtest | ADAPTER | none yet (consuming phase) | Python 3.10-3.12 classifiers (3.14 not declared); pytorch/lightgbm heavy surface; Phase 6/14 verifies 3.14 install path | ADAPTER | §11 ADAPTER — V9 data model authoritative; Qlib behind a V9 adapter for ML/alpha/research in Phase 6/14; do not make domain logic depend on Qlib directly; do not duplicate working V7 functionality |
| Microsoft RD-Agent | https://github.com/microsoft/RD-Agent | main branch (inspected 2026-08-11) | MIT | autonomous hypothesis/factor/model research, researcher-developer separation | REFERENCE_ONLY | none | Linux-only + Docker required (WSL on Windows); Python 3.10-3.11; research server, must not run in the trading process | REFERENCE_ONLY | §12 research only, not direct execution; runtime needs Docker/WSL/isolated Linux worker; production trading stays operational if RD-Agent offline; concepts evaluated in Phase 2, integration deferred to Phase 14/15 |
| skfolio | https://github.com/skfolio/skfolio | 0.25.0 (2026-05-31) | BSD-3-Clause | portfolio optimization, risk budgeting, HRP/NCO, CVaR, stress testing, CV | INTEGRATE | none yet (consuming phase) | requires_python >=3.11 (3.14 likely OK; verify classifiers in Phase 9); sklearn dependency | INTEGRATE | §13 INTEGRATE — portfolio optimization in Phase 9; compare advanced allocation vs equal-weight / fixed-risk / inverse-volatility baselines; complex optimization is not automatically superior |
| edgartools | https://github.com/dgunning/edgartools | 5.48.0 (2026-08-13) | MIT | SEC EDGAR 10-K/10-Q/8-K/Form 4, structured filing extraction | INTEGRATE | `core/sec/edgar_adapter.py`, `core/sec/service.py` | Python 3.10-3.14 declared and Python 3.14.3 install verified; SEC identity supplied via `EDGAR_IDENTITY`, never committed; atomic TTL cache; bounded metadata only | INTEGRATE | §14 implemented for US equities in Phase 10; identity and SEC-host URL checks are mandatory; cache avoids unchanged re-fetches; provider failure returns stale cache or explicit unavailable |
| Earnings-Volatility-Calculator | https://github.com/Acelogic/Earnings-Volatility-Calculator | main branch (inspected 2026-08-11) | MIT | earnings vol methodology, IV/RV, IV30/RV30, ATR, Yang-Zhang, term structure | REFERENCE_ONLY | none | methodology only; no Tkinter UI or scraping imported (§15); Python 3.10+ | REFERENCE_ONLY | §15 SELECTIVE — do NOT import Tkinter UI or Yahoo/Investing scraping; one authoritative V9 implementation per calculation in Phase 5 using verified formulas + our providers/tests/UI |
| FinBERT | https://github.com/ProsusAI/finBERT | transformers 5.15.0 + torch 2.13.0 + HF model revision `4556d13015211d73dccd3fdd39d39232506f3e43` (verified 2026-08-13) | Apache-2.0 repository work/model linked by the licensed upstream repository | financial sentiment | ADAPTER | `core/news_v9/finbert.py`, `agents/news_tab_agent.py` | Python 3.14.3 install and real positive/negative/neutral inference verified; model is heavy and lazy/local-cache-first; fallback is separately labelled; no obsolete repo runtime code used | ADAPTER | §16 implemented through the maintained Transformers text-classification pipeline; immutable model revision; upstream Apache-2.0 repository explicitly links the published Hugging Face model; source/revision/fallback are exposed in output |
| MLflow | https://github.com/mlflow/mlflow | 3.15.1 (2026-08-03) | Apache-2.0 (Databricks) | experiment tracking, model/prompt registry, AI tracing, evaluation, lineage | ADAPTER | none yet (consuming phase) | requires_python >=3.10 (3.14 to verify in Phase 14); large dependency surface; V9 keeps own AIProvider interface (§17); MLflow AI Gateway evaluated in Phase 3 | ADAPTER | §17 ADAPTER, strongly preferred for the Phase 14 Research Factory; V9 domain code must not depend on MLflow internals — use an adapter/service boundary; V9 exposes its own stable AIProvider so domain logic stays vendor-independent |
| Optuna | https://github.com/optuna/optuna | 4.9.0 (2026-06-01) | MIT | controlled hyperparameter optimization, pruning, multi-objective search | INTEGRATE | none yet (consuming phase) | Python 3.9-3.14 declared OK; research/training data only | INTEGRATE | §18 INTEGRATE in Phase 6/14; Optuna optimizes ONLY on allowed research/training/validation data, NEVER the final untouched holdout; multi-objective (OOS return, drawdown, Sharpe/Sortino, stability, turnover, costs); set trial budgets; no unlimited brute-force mining |
| RiskLabAI.py | https://github.com/risklabai/RiskLabAI.py | main branch (inspected 2026-08-11) | BSD-3-Clause (verified — RiskLab AI, 2022) | triple barrier, meta-labeling, Purged K-Fold, CPCV, walk-forward, DSR, PBO, HRP/NCO | REFERENCE_ONLY | none | Python version unstated; low-activity repo (1 star); methods are López de Prado published methodology (AFML) | REFERENCE_ONLY | §19 license VERIFIED = BSD-3-Clause (not assumed); however prefer the §19 path of published methodology + independent V9 implementation + deterministic tests in Phase 7 (López de Prado, AFML) over depending on a low-activity repo; adapter against the repo code only if a Phase 7 spike vets its quality; no code copied in Phase 2 |
| vectorbt | https://github.com/polakowo/vectorbt | 1.1.0 (2026-07-05) | Apache-2.0 + Commons Clause (fair-code) | large-scale vectorized backtesting, parameter sweeps | REJECT | none | Python 3.11-3.14 OK; Apache-2.0 + Commons Clause restricts selling products/services primarily from the software; ray/dask optional heavy deps | REJECT | §20 OPTIONAL ADAPTER ONLY — license is Apache-2.0 with Commons Clause (fair-code, not a standard permissive license); §20 requires explicit license approval for a productizable trading cockpit, not granted autonomously; V9 stays fully functional via InternalBacktestEngine (Phase 7 BacktestEngine interface); re-evaluate only if license changes |
| quant-garage | https://github.com/rgourley/quant-garage | main branch (inspected 2026-08-11) | AGPL-3.0 | earnings drilldown, event studies, position sizing, factor research, backtest prep | REFERENCE_ONLY | none | AGPL-3.0 strong copyleft — no copy/vendor/hidden-dependency (§21); concepts only | REFERENCE_ONLY | §21 REFERENCE_ONLY by default — do NOT copy code, vendor, or make it a hidden dependency; AGPL-3.0 confirms concepts-only; any direct integration needs explicit owner approval after license analysis |
| River | https://github.com/online-ml/river | 0.25.0 (2026-05-31) | BSD-3-Clause | streaming metrics, online statistics, concept-drift detection, incremental models | REFERENCE_ONLY | none | requires_python >=3.11 (3.14 likely OK; verify in Phase 6); detection only initially (§22) | REFERENCE_ONLY | §22 OPTIONAL — detection only initially, not automatic model mutation; Phase 2 records the evaluation; Phase 6 may upgrade to ADAPTER for concept-drift detection if it adds real value |
| CCXT | https://github.com/ccxt/ccxt | 4.5.73 (inspected 2026-08-11) | MIT | unified crypto exchange execution | REJECT | none | MIT; Python 3 broad; V7 crypto execution (Binance/KuCoin REST) already working | REJECT | §23 OPTIONAL — evaluate only if V7 crypto execution is insufficient; V7 has working Binance/KuCoin REST crypto paths, so do NOT replace working V7 providers merely because CCXT exists; re-evaluate only if a future phase needs unified multi-exchange live execution (subject to Risk Constitution) |

## Summary

- **INTEGRATE (3):** skfolio (Phase 9), edgartools (Phase 10), Optuna (Phase 6/14).
- **ADAPTER (3):** Microsoft Qlib (Phase 6/14), FinBERT (Phase 10), MLflow (Phase 14; AI Gateway Phase 3).
- **REFERENCE_ONLY (5):** Microsoft RD-Agent, Earnings-Volatility-Calculator, RiskLabAI.py, quant-garage, River.
- **REJECT (2):** vectorbt (Commons Clause license), CCXT (V7 crypto sufficient, §23).

**Net Phase 2 dependency additions: none.** All INTEGRATE/ADAPTER integrations are implemented in their consuming phases against the pins above. Phase 2 records the decisions + pins only (§118).

### Phase 9 skfolio consumption note

Phase 9 delivered and pinned the simple deterministic baselines (fixed risk and inverse volatility, plus fixed amount/percentage and volatility targeting). It did not add skfolio or expose HRP/NCO/CVaR as executable allocators because no V9 OOS study yet demonstrates that those complex methods outperform the simple baselines after costs and stability checks. The Phase 2 `INTEGRATE` decision remains conditional for the Research Factory: integrate only after that comparison is available, never merely because the optimizer exists.

## THIRD_PARTY_NOTICES.md
Generated in Phase 2 at `THIRD_PARTY_NOTICES.md` with every production dependency requiring attribution (spec §24), including the INTEGRATE/ADAPTER repos above.

## Dependency pinning (spec §25)

Production dependencies reproducible (V7 `requirements.txt` pinned 2026-07-31). Research repos evaluated in Phase 2 — repository, commit/release, date inspected (all 2026-08-11):

| Repository | Commit/release | Date inspected |
|---|---|---|
| Microsoft Qlib | 0.20.1 | 2026-08-11 |
| Microsoft RD-Agent | main | 2026-08-11 |
| skfolio | 0.25.0 | 2026-08-11 |
| edgartools | 5.48.0 | 2026-08-13 |
| Earnings-Volatility-Calculator | main | 2026-08-11 |
| FinBERT | transformers 5.15.0 + torch 2.13.0 + HF revision 4556d13015211d73dccd3fdd39d39232506f3e43 | 2026-08-13 |
| MLflow | 3.15.1 | 2026-08-11 |
| Optuna | 4.9.0 | 2026-08-11 (inspected); 2026-08-16 installed + consumed by `core/research_factory/optuna_runner.py` (Phase 14) |
| RiskLabAI.py | main | 2026-08-11 |
| vectorbt | 1.1.0 | 2026-08-11 |
| quant-garage | main | 2026-08-11 |
| River | 0.25.0 | 2026-08-11 |
| CCXT | 4.5.73 | 2026-08-11 |
