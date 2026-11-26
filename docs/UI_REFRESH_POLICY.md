# UI Refresh Policy

## Principiu
- UI consuma snapshot-uri.
- Refresh extern se face prin agenti sau endpointuri `POST .../refresh`.

## Scheduler curent
- `coreLoop`:
  - Home: ~5s (cu jitter/backoff)
  - non-Home: ~8.5s (mai lent)
  - fetch: `/api/state` + `/api/dashboard` doar pe Home
- `watchlistLoop`:
  - Home/Watchlist: ~30s
  - alte tab-uri: ~45s
  - fetch: `/api/watchlist`
- `scannerLoop`:
  - Home/Scanner: ~15s
  - alte tab-uri: ~26s
  - fetch: `/api/scanner/state`
- tab hidden (`document.hidden`): delay multiplicat (factor 2.5)

## Taburi dedicate
- News tab:
  - poll doar cand view activ = `news`
  - fetch: `/api/news-tab/state`
- TradeRadar tab:
  - fetch: `/api/traderadar/state`
- Premarket tab:
  - fetch: `/api/premarket`
- Pre-Earnings tab:
  - fetch: `/api/pre-earnings`

## Version handshake
- `/api/state`:
  - client trimite `if_version`
  - server poate raspunde `{ "unchanged": true, "version": ... }`
- `/api/dashboard`:
  - acelasi model (`if_version` / `unchanged`)

## Local-first
- Snapshot versionat (`SNAPSHOT_VERSION=2`) pentru render instant.
- Dashboard snapshot separat (`dashboard_version=2`).
- Datele noi sunt reconciliate incremental in memorie, fara reset complet de UI.
