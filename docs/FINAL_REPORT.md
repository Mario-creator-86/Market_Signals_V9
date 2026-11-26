# Final Report - Cache-First Refactor (Incremental)

## Ce s-a schimbat

### Backend
- Am introdus `cache_only` strict pentru GET-urile UI critice:
  - `/api/state`, `/api/dashboard`, `/api/news-tab/state`, `/api/premarket`, `/api/pre-earnings`, `/api/scanner/state`.
- Am adaugat versioning si contract de tip diff:
  - `/api/state`: `state_version`, `if_version`, `unchanged`.
  - `/api/dashboard`: `dashboard_state_version`, `if_version`, `unchanged`.
- Am introdus `include` flags pe `/api/state` si `/api/dashboard` pentru payload lean pe componente.
- Am blocat self-heal refresh in cache-only GET paths pentru:
  - Premarket
  - Pre-Earnings
  - Scanner lazy refresh (prin request context).
- Am adaugat `TickerWorkspace` (`core/ticker_workspace.py`) cu LRU+TTL + persistenta JSON/SQLite.
- `api/state_service.py` si `api/dashboard_service.py` folosesc date din `TickerWorkspace` ca fallback/augmentare.
- Invalidez workspace la mutatii watchlist (`add/remove/reorder`) in `api/actions_service.py`.
- Capital:
  - endpoint nou `POST /api/capital/open-position`
  - endpoint complementar `POST /api/capital/close-position`
  - ledger extins in `core/capital_manager.py` cu `positions`, `OPEN_POSITION`, `RELEASE_MARGIN`, `CLOSE_POSITION`.
- Premarket Scanner (contract V8 orientat pe actionability):
  - output backend partitionat explicit: `long_candidates[]`, `short_candidates[]`, `watch_only[]`
  - fiecare candidat include:
    - `premarket_vol_rel`
    - `catalyst_score`
    - `liquidity_score`
    - `premarket_score_long`
    - `premarket_score_short`
    - `why`, `freshness`, `inputs_age`
  - `candidates` ramane disponibil pentru backward compatibility, dar ordonarea vine din scoring backend.
- Pre-Earnings (contract extins fara rupere):
  - `direction_bias` normalizat `[-1, 1]`
  - `direction_bias_score` pastrat (`-100..100`) pentru compatibilitate
  - `direction_label` (`LONG|SHORT|NEUTRAL`)
  - `gap_risk_level` + `gap_risk_numeric`
  - `confidence_score` + `confidence_interval`
  - `flags` per ticker (`missing_earnings_date`, `calendar_news_mismatch`, etc.)

### Frontend
- Instrumentare runtime:
  - `requests/min`
  - `render timing per tab` (via `PerfMonitor` + `window.__MS_PERF`).
- Handshake de versiune pentru state/dashboard (evita payload si rerender cand nu exista schimbari).
- Backoff mai agresiv cand tab-ul browserului este ascuns.
- Capital UI:
  - formular de deschidere pozitie manuala (`ticker`, `direction`, `leverage`, `usd_size`)
  - tabel de pozitii deschise + buton close.
- Premarket UI:
  - consuma direct grupurile backend (`long_candidates/short_candidates/watch_only`)
  - detalii extinse (VolRel, Catalyst/Liquidity score, Long/Short score, freshness, inputs age, `why`).
- Pre-Earnings UI:
  - filtre noi: `Direction`, `Gap Risk (low/med/high)`, `Min Confidence`
  - randare compatibila cu bias normalizat + score legacy.

### Agenti / observabilitate
- Adaugat `core/agent_metrics.py`.
- In buclele agentilor principali se logheaza per run:
  - durata
  - external fetch delta
  - cache hit/miss delta
  - cache hit rate.

## Metrici perf (baseline)
- vezi `docs/PERF_BASELINE.md`
- observatie: endpointul dominant ca latency ramane `/api/state` (mai ales `lean=false`).

## Teste rulate
- `python -m py_compile api/main.py api/state_service.py api/dashboard_service.py core/capital_manager.py core/ticker_workspace.py core/agent_metrics.py agents/scanner_agent.py agents/trade_radar_agent.py agents/strategy_research_agent.py agents/premarket_scanner_agent.py agents/pre_earnings_agent.py api/actions_service.py`
- `node --check frontend/app.js`
- `pytest -q tests/integration/test_ui_cache_only_guard.py tests/integration/test_state_dashboard_versioning.py tests/test_capital_manager_sync.py tests/integration/test_capital_open_position_api.py`
- `pytest -q tests/perf/test_performance_budgets.py`
- `pytest -q tests/test_premarket_scanner.py tests/test_pre_earnings_scanner.py tests/integration/test_ui_cache_only_guard.py`

Rezultat:
- suitele Premarket/Pre-Earnings + cache-only guard au trecut (`50 passed`).
- exista teste deja instabile/necorelate direct cu acest increment:
  - `tests/test_dashboard_cockpit_payload.py` (import intern lipsa)
  - `tests/integration/test_state_dashboard_versioning.py::test_state_endpoint_supports_if_version_unchanged` (endpoint returneaza payload schimbat, nu `unchanged` in contextul actual de date).

## Riscuri ramase
- `/api/state` full inca are cost ridicat; necesita etapa dedicata de payload split pe sub-componente + precompute.
- `TickerWorkspace` este integrat incremental (fallback/augment), nu inca sursa unica completa pentru toate taburile.
- Nu am finalizat in acest increment toate etapele 5-9 (premarket/pre-earnings scoring UX complet, quality gate 2.0 full, news tab simplificare finala).

## Next steps
1. Mutare completa a construirii `tickers_list` pe snapshot `TickerWorkspace` precomputat de agenti.
2. Reducere payload `state` pe Home (separare stricta pe include modules + lazy cards).
3. Finalizare gating semnale (adaptive regime thresholds + duplicate suppression + audit trail extins).
