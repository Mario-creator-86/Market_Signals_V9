# Market Signals V7 - Sistem Watchlist (Trader-Grade)

Documentația descrie implementarea actuală end-to-end pentru Watchlist: backend, API, cache, storage și frontend.

## Arhitectură (pe scurt)

- `core/config.py`
  - gestionează `config.json` + `watchlist.json`
  - normalizează tickerele și face migrare automată schema `watchlist.json`
  - oferă setări efective per ticker (`get_effective_ticker_settings`)
- `api/state_service.py`
  - construiește `tickers_list` pentru `/api/state`
  - injectează earnings, news pulse, risk flags, semnale și metadata UI
- `api/actions_service.py`
  - add/remove/reorder tickere
  - update setări ticker/global + preview
  - quick AI per ticker (on-demand)
- `core/news_pulse.py`
  - agregare deterministică news pulse (24h + 7d) cu cache TTL scurt
- `core/watchlist_quick_ai.py`
  - prompt scurt Ollama cu output JSON strict + validare/fallback
- `frontend/app.js` + `frontend/styles_watchlist.css` + `frontend/styles_news.css`
  - UI Watchlist detaliat trader-grade + panel expand + filtre/sortări noi
  - fix de scroll pentru pagina News

## Storage și compatibilitate

### `watchlist.json` (schema curentă)

```json
{
  "stocks": ["AAPL", "MSFT"],
  "crypto": ["BTC-USD", "ETH-USD"],
  "order": ["BTC-USD", "AAPL", "MSFT"],
  "hot_list": ["AAPL"],
  "settings": {
    "AAPL": {
      "signals_enabled": true,
      "min_win_rate": 55,
      "tp_pct": 4.0,
      "sl_pct": 2.0,
      "allowed_strategies": ["STRONG/PULLBACK"],
      "strategy_notes": ""
    }
  },
  "scanner_exclusions": ["REMOVED-BY-USER"],
  "schema_version": 2
}
```

### Migrare automată

La load:
- convertește chei legacy (`yahoo/binance`, `cryptos`) la schema nouă
- normalizează + dedupe case-insensitive
- repară `order` și `hot_list`
- sanitize pentru `settings`
- creează backup (`watchlist.json.migration.<timestamp>.bak`) înainte de rescriere

## Câmpuri noi în ticker row (`/api/state`)

Fiecare element din `tickers` include acum:
- `earnings_date`, `earnings_days`, `earnings_when`, `earnings_when_desc`
- `news_pulse`:
  - `24h` și `7d` cu `article_count`, `avg_impact_score`, `avg_actionability`,
    `sentiment_distribution`, `top_categories`, `top_headlines`, `breaking_count`
  - `score`, `duplicate_count`, `sentiment_trend`
- `news_pulse_score`, `news_actionability`, `news_sentiment_trend`
- `has_open_signal`, `signal_status`, `signal_score`
- `trend_alignment`, `trend_regime_1d`, `trend_regime_4h`
- `risk_flags`, `top_risk_flag`
- `quick_ai` (placeholder `null`, populat on-demand)

Backward compatibility:
- dacă lipsesc câmpuri în date vechi/cache, se aplică default safe.

## Ownership si eliminare

- Watchlist este user-owned: Scanner Agent publica doar candidati in snapshot-ul Scanner/Trade Radar si nu scrie in `watchlist.json`.
- `DELETE /api/tickers` accepta unul sau mai multe simboluri si le elimina din asset list, `order`, `hot_list` si `settings`.
- Simbolurile eliminate sunt pastrate in `scanner_exclusions`; un add manual ulterior elimina explicit excluderea.
- UI ofera trash per rand, selectare a randurilor vizibile si un singur DELETE bulk cu confirmare.
- Randarea filtreaza defensiv `state.tickers` prin Watchlist-ul autoritativ, astfel un cache vechi nu poate readuce un rand sters.

## Endpointuri Watchlist relevante

- `POST /api/tickers`
- `DELETE /api/tickers`
- `POST /api/tickers/reorder`
- `GET /api/watchlist/settings/{ticker}`
- `POST /api/watchlist/settings`
- `POST /api/watchlist/settings/global/preview`
- `POST /api/watchlist/settings/global`
- `POST /api/watchlist/quick-ai`
- `GET /api/watchlist/pulse`
- `GET /api/state`

## Config nou (`config.json`)

Chei noi suportate:

```json
{
  "watchlist": {
    "default_sort": "ticker",
    "filters": {
      "asset_type": "all",
      "signals_enabled": "all",
      "has_open_signal": "all",
      "earnings_bucket": "all",
      "news_actionability_min": 0,
      "trend_regime": "all",
      "earnings_next_7d": false
    }
  },
  "news_pulse": {
    "ttl_seconds": 45
  },
  "quick_ai": {
    "enabled": true,
    "ttl_near_earnings": 600,
    "ttl_normal": 1800
  }
}
```

## News Pulse și cache

- News pulse se calculează din starea NewsTab (fără apel extern greu per request).
- Cache local per ticker (`core/news_pulse.py`) cu TTL implicit 45s.
- scor `news_pulse_score` 0-100:
  - crește cu impact, actionability, breaking, trend sentiment
  - scade cu penalizare de duplicate/spam

## Quick AI (Ollama)

`POST /api/watchlist/quick-ai` returnează analiză scurtă strict JSON:
- `bias`, `confidence`, `time_horizon`, `top_reasons`, `top_risks`, `action`, `notes`

Reguli:
- dacă output-ul modelului e invalid, parser fallback la payload sigur
- anti-halucinații: promptul forțează `unknown` pentru date lipsă
- TTL adaptiv:
  - `earnings_days` 0-5 -> 10 minute
  - altfel -> 30 minute

## UI Watchlist (Detailed View)

- toolbar avansat cu filtre:
  - `asset_type`, `signals_enabled`, `has_open_signal`, `earnings_bucket`,
    `news_actionability_min`, `trend_regime`, `earnings_next_7d`
- sortări:
  - existente + `earnings_days`, `news_pulse_score`, `signal_score`
- mod sortare:
  - `live` / `snapshot (frozen order)`
- row expand/collapse:
  - top 3 headlines
  - ultimul quick AI
  - best strategy + quick edit
- acțiuni row:
  - Quick AI, Add/Remove HotList, Settings, Remove

Managementul listei include select visible, clear selection, remove selected,
confirmare accesibila si buton trash pe fiecare rand.

## Dashboard Watchlist Widget

Widgetul include secțiuni noi:
- Top movers
- Top risk flags
- Upcoming earnings (next 7 days)

## News scroll fix

`frontend/styles_news.css`:
- `#view-news` are padding inferior extins pentru footer fix
- `#news-page-feed-content` are scroll propriu (`max-height` + `overflow-y:auto`)
- fallback responsive pe ecrane mici (`max-height:none`)

## Teste adăugate

- `tests/test_watchlist_normalize_dedup.py`
  - normalize ticker + dedupe watchlist
- `tests/test_news_pulse_watchlist.py`
  - scoring pulse + agregare din news state
- `tests/test_watchlist_quick_ai_parser.py`
  - validare parser Quick AI + fallback
- `tests/test_watchlist_reorder_safety.py`
  - reorder fără duplicate + păstrare tickere
- `tests/test_watchlist_api_smoke.py`
  - smoke endpoints watchlist + pulse + quick-ai
- `tests/unit/test_watchlist_ownership.py`
  - ownership manual, invalid symbols, tombstone si scanner read-only
- `tests/test_watchlist_management_frontend.py`
  - contracte UI pentru remove individual/bulk, dialog si cache autoritativ
- `tests_e2e/test_quality_tabs.py`
  - confirmare DELETE reala in browser, cu request interceptat
