# News Cockpit V3

## Scope
Upgrade complet pentru News Tab la modelul „trader-grade news cockpit”:
- layout 3 coloane (Filters / Feed / Insight Panel)
- dedup cross-source cu fingerprint + `also_reported_by`
- `action_score` separat de `impact_score`
- micro-summary Ollama top-N (HIGH/CRITICAL) + cache
- deep analysis on-demand cu schema extinsă (`trade_setups`, `unknowns`)
- suppression persistent (`news_suppression.json`)
- market narrative cache-uit
- history async cu progres

## Model NewsArticle (nou)
În `agents/news_tab_agent.py`:
- `action_score: int`
- `story_fingerprint: str`
- `also_reported_by: list[str]`
- `ai_micro_summary: object | null`
- `ai_deep_analysis: object | null`

Backward compatibility:
- `from_dict` oferă default pentru câmpuri lipsă (state vechi).

## Persistență

### News state
- `news_backup.json` include acum câmpurile noi din articol.

### Suppression
- fișier nou: `news_suppression.json`
- structură:
  - `keywords: []`
  - `sources: []`
  - `ticker_topics: { TICKER: [] }`

### AI store
`core/analysis_store.py` a fost extins la schema v2:
- `by_url`
- `by_article` (listă de înregistrări key-uite by `article_id + timestamp + model`)
- `narratives`

Compatibilitate legacy:
- URL-based store vechi este migrat automat.

## Endpoint-uri noi / extinse

În `api/main.py`:
- `GET /api/news-tab/article/{article_id}`
- `GET /api/news-tab/narrative`
- `GET /api/news-tab/suppression`
- `POST /api/news-tab/suppression`
- `POST /api/news-tab/history/start`
- `GET /api/news-tab/history/progress/{job_id}`

Extinderi:
- `GET /api/news-tab/export`:
  - `include_tldr`
  - `include_action_score`
  - `include_time_horizon`
- `POST /api/news/analyze`:
  - acceptă `article_id`, `url`, `force_refresh`, `prompt_type`
  - cache lookup by `article_id`
  - deep JSON include `trade_setups` + `unknowns`

## UI
În `frontend/index.html`, `frontend/styles_news.css`, `frontend/app.js`:
- 3 coloane: left filters, center stream, right insight
- breaking pinned (`max 5`)
- stream mode `single` / `group by ticker`
- filtre:
  - search
  - time window `24h/3d/7d/30d/90d`
  - impact toggles
  - category chips
  - hide duplicates / only actionable / only watchlist
- suppression actions în Insight Panel
- history fetch async 30/60/90 cu progress bar
- export opțional cu TL;DR, action_score, time_horizon

## Config
`config.json` și `core/config.py`:
- `news.news_refresh_minutes = 5`
- `news.actionability_threshold = 60`
- `news.auto_summarize_top_n = 10`
- `news.narrative_ttl_seconds = 900`
- `news.hide_duplicates_default = true`
