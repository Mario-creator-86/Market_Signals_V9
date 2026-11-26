# AUDIT ROUND 2 — 13 taburi + modal chart (2026-09-05, seara)

Metodă: 14 agenți paraleli (unul pe tab + unul pe modalul chart workspace),
lansați în valuri de 3 (limita de rate LLM), fiecare cu misiune exaustivă
„field-by-field": fiecare câmp randat trasat frontend file:line → cheie payload →
endpoint → producător backend file:line → sursă finală, clasificat
OK / DEAD-RENDER / DEAD-BACKEND / FAKE / STALE / MISCALIBRATED, cu dovadă live
(GET-only, ≤30 cereri/tab) și recheck al rundei 1. Rapoartele complete:
`docs/audit_tabs_v2/` (TAB_01..13 + MODAL_CHART_WORKSPACE).

Eșecuri de infrastructură la agenți (captcha verify failed / model request
failed): Tab 2 ×2, Tab 6 ×1, Tab 10 ×1 — toate rezolvate la retry.

## Verdict pe taburi (round 2)

| # | Tab | P1 | P2 | P3 | Cheie |
|---|-----|----|----|----|-------|
| 1 | AI Cockpit | 0 | 2 | 8 | Robustness randează `[Object Object]`; KPI „AI HEALTHY" contrazice ledgerul (30/30 `all_unavailable_blocked`); SWR fără max-age; aria-busy încă deschis |
| 2 | Watchlist | 1* | 2 | 6 | *P1 moștenit: CSS mobile <600px ascunde coloanele greșite; titluri știri ne-escapate în acordeon; `change_pct: null` coalescezat în „+0,00%" verde |
| 3 | Scanner | 0 | 1 | 8 | Cache-ul KuCoin movers ignora `limit` (50→15 rânduri); flash de preț mort pe Binance/Stocks; NFPUSDT verificat byte-cu-byte cu Binance |
| 4 | News | 0 | 3 | 6 | Impact/breaking dominat de zgomot social eToro (88% din CRITICAL); „Takeaway" AI 1/691 (SSRF allowlist fără etoro.com); payload 2,69MB, SWR mort |
| 5 | Trade Radar | 0 | 0 | 2 | Curat: 0 contradicții verdict-vs-move pe 50 rânduri; red-flag slug-uri + banner disabled lipsă |
| 6 | Premarket | **1** | 1 | 6 | **Fix-ul c7edafc era INERT în producție**: /api/premarket servește din disk-cache; blocul cu `agent.get_state()` rulează doar în test mode |
| 7 | Pre-Earnings | 0 | 2 | 6 | c6 eToro real cablat în scor (104/279); card UI multiply-ează buy_pct×100 („10000% Long"); buy_pct saturat → tilt LONG sistemic; gates din brief nu există în cod (doc drift) |
| 8 | Hot List | **1** | 3 | 8 | **Panoul FMP FAKE**: `fmp_data={}` hardcodat + `_details_loaded:True` → Piotroski „0/9" roșu inventat; fallback chain gol în config (fix c7edafc inert); momentum None cached 6h |
| 9 | Backtest | 2* | 6 | 6 | *P1 din runda 1 rămân (FAITHFUL 253s, compare_all re-fetch); KPI ids inexistente în DOM; istoricul citește 6 chei neemise; histograma citește `r_multiple` niciodată emis; jurnal mort din istoric |
| 10 | Calendar | 0 | 4 | 12 | Modalul economic = carcasă goală (0/518 Actual/EPS); banner FOMC dead-render; ore UTC ca locale; NVDA lipsă din grilă (data gap) |
| 11 | Agents | 0 | 0 | 5 | Compliant; `renderAgents` legacy mort în app.js toggle-uiește fără poarta 409 (risc latent) |
| 12 | Alerts | **1** | 1 | 9 | **Motorul subprocess nu reîncarcă niciodată alertele create după pornire** → nu se declanșează + șterse silențios de `_save_alerts`; alerte pe tickeri ne-urmați: „Active" perpetuu |
| 13 | Capital | 0 | 0 | 8 | Fundament sănătos, aritmetică la cent; cockpit vs capital MTM brut/net (−$1.70); equity-curve orfan; 4 randări moarte |
| M | Chart modal | **1** | 2 | 5 | **Order ticket: preț „Market" poate fi vechi de zile** (`cache:"stale"` din SQLite, frontend nu verifica); pipeline analyze-ticker (d96cda2) confirmat funcțional |

Total: **4 P1, ~20 P2, zeci de P3**. Zero regresii introduse de commit-urile
dintre runde (7f26fd2 → e9eda27 → c7edafc).

## Fix-uri aplicate în această sesiune (verificate, comitate)

### P1 (4)
1. **Premarket — traseu de servire** (`agents/premarket_scanner_agent.py`):
   logica de restaurare extrasă în `_apply_market_open_state()` și aplicată pe
   AMBELE trasee — `get_state()` ȘI `_persist_locked()` (fișierul disk
   `data/premarket.json` de care depinde producția). Lecție: testul hermetic
   anterior a trecut pentru că testa `get_state()` direct, nu traseul HTTP.
   Acum scriptul de verificare include și testul de persistență disk.
2. **Alerte — reload în motor** (`core/alerts.py` + `core/engine.py`):
   `reload_from_config()` apelat în `run_scan` după `reload_watchlist_if_changed`;
   `_load_alerts` hardenit per-item (JSON corupt nu mai cade motorul).
3. **Hot List — panou FMP onest** (`frontend/app.js` renderHotListFmpSection):
   payload gol → mesaj explicit „provider FMP retirat", nu Piotroski 0/9 roșu.
   Verificat în pagină: empty → mesaj onest; payload real → 6/9 normal.
4. **Chart modal — order ticket** (`frontend/chart_workspace.js`):
   `cache:"stale"` → refetch unic cu `refresh=true`; prețul stale nu mai
   ajunge niciodată în orderDraft.

### P2 (12+)
- **KuCoin movers cache limit-independent** (`api/market_data_service.py`) —
  cache ține lista sortată completă, slice per cerere. Live: `limit=15` →
  15+15, `limit=50` → 50+50 (înainte: 15+15 pentru ambele). Test unitar
  extins pentru compatibilitate cu cache-ul legacy.
- **SSRF allowlist + etoro.com** (`core/content_fetcher.py`) — deblochează
  „Takeaway" WIIM pentru ~47% din feed.
- **Card eToro ×100** (`frontend/app.js`) — „10000% Long" → „100% buy / 0%
  sell". Confirmat live în browser.
- **Momentum None cache 6h → retry 60s** (`api/state_service.py`).
- **Fallback chain derivat automat** (`core/ai/decision_engine.py`) — când
  `ai.fallback.chain` e gol (config.json actual), lanțul se construiește din
  ceilalți provideri configurați → fix-ul de dezbatere c7edafc devine live.
- **Watchlist: titluri știri esc()** + **`change_pct: null` → „—"** (merge +
  renderer). Confirmat live: 2 rânduri randează „—".
- **Walk-forward PF null → „-"** (app.js).
- **Trade Radar: label map red-flags + banner agent disabled** (app.js).
- **Alerte: 400 pentru tickeri din afara watchlist-ului** (actions_service +
  main.py). Live: POST NOTWATCHEDXYZ → 400.
- **Cockpit: pill Robustness din obiectul walk-forward** → MEASURED/WF ERROR.
- **Backtest**: agregate record-level în rezultatul standard (profit_factor,
  expectancy_r, payoff_ratio, max_drawdown, avg_win/loss_r — citite de
  vizualizarea istoric, înainte mereu 0/NA); `win_pct` pe faithful; jurnalul
  de tranzacții citește `trade_list` real (nu semnalele per-ticker ca
  trade-uri false „OPEN 0%") — ambele site-uri; CSS grid extins la Strategies
  + Trades; removed `BacktestUI.updateKPIs` (scria în id-uri inexistente).
- **Cache-bust**: app.js 38.27, cockpit.js 1.18, chart_workspace.js 5.2,
  styles_backtest.css 1.7, main.css 27.15.

## Verificare

- `py_compile` pe toate modulele atinse; **suite complet: 3343 passed**
  (1 fix la testul movers pentru cache legacy), 14 skipped.
- `scripts/verify_fixes_2026_09_05.py`: **27/27** (inclusiv noua verificare
  de persistență disk premarket).
- Server repornit; live: KuCoin movers limit-independente, CRUD alerte +
  persistență `alerts` în watchlist.json + 400 validare, /api/premarket cu
  `flags.market_open` pe traseul disk.
- Browser pass pe 6 suprafețe: versiuni încărcate, Watchlist „—", FMP onest,
  Cockpit fără [Object Object], eToro card corect, regula grid Backtest activă.

## Backlog (documentat, neblocant)

1. Backtest P1 perf: FAITHFUL 253s > 180s timeout (job async + polling) și
   `compare_all` re-folosire `df=` ca la walk-forward.
2. News: recalibrare impact pt surse sociale eToro (cap + tightening
   `_is_breaking`), SWR pe /api/news-tab/state, dedup cross-bucket.
3. Cockpit: sondă de health AI onestă (reachability ≠ generare — arată
   „HEALTHY" când ledgerul are 30/30 `all_unavailable_blocked`), max-age pe
   fereastra SWR + badge UI.
4. Calendar: producători pentru modalul economic (Actual/EPS — needs data
   source), etichete fus orar, `high_impact` post-merge; NVDA lipsă din grilă
   (data gap, de investigat).
5. Pre-Earnings: semantica `buyHoldingPct` (c6 saturat — de verificat pe dump
   raw eToro; eventual activare semn contrarian), doc drift pe gates (reale:
   min_gap 65 / bias 20 / coverage 50 / score 78 + fereastră 2h), anomalii
   split în track-record.
6. Chart modal: fallback eToro/StockData pt charts stocks; randarea
   annotațiilor cantitative V9 (pivots/BOS/liquidity — calculate dar never
   rendered).
7. Agents: eliminare `renderAgents` legacy din app.js; corectare „Last run:
   On demand"; catch 5xx per-panou.
8. Alerts: istoric declanșări persistent în UI; semantica „Volatility Spike".
9. Watchlist: CSS mobile <600px ascunde coloanele greșite; RSI/MACD
   DEAD-RENDER; trend-bar metrică fabricată client-side.
10. P3-uri per tab în `docs/audit_tabs_v2/TAB_*.md`.
