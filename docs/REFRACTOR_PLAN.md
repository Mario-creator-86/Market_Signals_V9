# Refactor Plan - Cache-First V8

## Observatii
- Frontend ruleaza bucle separate in `frontend/app.js`: `coreLoop`, `watchlistLoop`, `scannerLoop`, plus bucle dedicate pentru News, TradeRadar, Premarket, Pre-Earnings.
- Agentii autostart relevanti in `api/main.py` la startup: `trade_radar`, `strategy_research`, `premarket_scanner`, `pre_earnings` (conditionati de `config.agents.*.enabled`).
- Endpoint-urile cheie pentru UI sunt separate corect: `/api/state` si `/api/dashboard` raman distincte.
- Exista deja observabilitate de request/external call si cache decisions in `core/observability.py`.

## Riscuri
- `/api/state` ramane cel mai scump endpoint (payload mare + agregare multi-source), chiar in cache-only path.
- Daca un endpoint UI trigger-eaza refresh async in acelasi GET path, poate genera fetch extern imediat dupa raspuns (race condition).
- Capital ledger extins (pozitii manuale) necesita sincronizare stricta cu storage pentru a evita drift intre procese.
- Migrarea catre `TickerWorkspace` trebuie facuta incremental ca sa nu rupa contractele existente.

## Ce schimbam
- Middleware strict `cache_only` pentru GET-urile UI critice.
- Contract versionat pentru `/api/state` si `/api/dashboard` (`if_version` + `unchanged`).
- Instrumentare agent run: durata, external fetch count, cache hit/miss delta.
- `TickerWorkspace` nou (`core/ticker_workspace.py`) cu LRU+TTL + persistenta JSON/SQLite.
- Invalideaza `TickerWorkspace` la add/remove/reorder watchlist.
- Capital ledger extins cu pozitii manuale si endpoint nou `POST /api/capital/open-position` (+ close endpoint).
- Frontend: metrics runtime (`requests/min`, `render timing`) + backoff mai agresiv cand tab-ul e hidden.

## Ce nu schimbam
- Separarea `/api/state` vs `/api/dashboard`.
- Modelul general de agenti (polling + refresh endpoint dedicat).
- Structura de baza a paginilor UI (pastram design simplu/pro).
- Contractele existente pentru endpointuri non-obiectiv (backtest, analyze, alerts etc.).

## Harta Dependinte Agenti <-> Endpointuri
- `trade_radar_agent`:
  - read: `GET /api/traderadar/state`
  - trigger: `POST /api/traderadar/refresh`, `POST /api/traderadar/suppress`
- `strategy_research_agent`:
  - status in diagnostice/agent payload, fara UI tab dedicat de fetch heavy
- `premarket_scanner_agent`:
  - read: `GET /api/premarket`
  - trigger: `POST /api/premarket/refresh`
- `pre_earnings_agent`:
  - read: `GET /api/pre-earnings`
  - trigger: `POST /api/pre-earnings/refresh`, `POST /api/pre-earnings/refresh-symbol`
- `news_tab_agent`:
  - read: `GET /api/news-tab/state`, `GET /api/news-tab/ticker/{ticker}`
  - trigger: `POST /api/news-tab/refresh`, `POST /api/news-tab/ticker/{ticker}/refresh`
- `scanner_agent`:
  - read: `GET /api/scanner/state`
  - trigger: `POST /api/scanner/refresh`

## Hot Paths care pot bloca
- `GET /api/state` (in special `lean=false` + hotlist activ).
- `GET /api/dashboard` (agregare + prioritizare watchlist + news summary).
- build/merge hotlist si contextualizare earnings in `api/state_service.py`.
- render combinat `UI.renderAll(...)` pe Home cand sosesc update-uri simultane state/watchlist/scanner.
