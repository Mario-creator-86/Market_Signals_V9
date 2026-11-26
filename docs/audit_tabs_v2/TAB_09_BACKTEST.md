# AUDIT FORENSIC V2: TAB 9 — STRATEGY BACKTESTING LAB (`#view-backtest`)

**Data audit:** 2026-09-05
**Metodă:** trasare câmp-cu-câmp UI → payload → endpoint → producător backend → sursă de date; verificare live GET-only (5 cereri curl către http://127.0.0.1:8003: `history?limit=3`, `history?include_trades=false`, `oos-validation/latest`, `research-validation/latest`); fără POST, fără browser. Dovezi salvate doar în `$TMP`.
**Commit-uri verificate:** `1eedcfc` (HEAD), `c7edafc` (fix payoff_ratio), `7f26fd2` (fixuri tab-audit, CSS 1.6), `d4dd784` (config shim + cache key TP/SL).

---

## 1. VERDICT GENERAL

| Tab | Selector | Status funcțional | Verdict |
| :--- | :--- | :--- | :--- |
| TAB 09 | `#view-backtest` | OPERAȚIONAL (rulări, istoric, walk-forward, OOS) | **BINE, CU DEFECTE**: 2×P1 deschise (moștenite runda 1), 6×P2 noi, 8×P3 |

**Rezumat:** Lanțul principal de date este real și onest: UI → `POST /api/backtest/run|walk-forward` (`api/main.py:3783/3803`) → `api/actions_service.py:run_backtest:1776 / run_walk_forward_backtest:1456` → `core/quick_backtester.py` → `core/backtester.py` (Yahoo/Binance) + `core/trading_costs.py`. Matematica KPI verificată live pe payload real este corectă (win_rate, payoff, expectancy, PF — toate re-calculabile din avg_win_r/avg_loss_r). Totuși, **5 zone de randare consumă chei pe care backendul nu le produce niciodată** (histograma R-multiple, KPI-urile sidebar, grila KPI a vizualizării standard, jurnalul de tranzacții pentru recorduri standard/faithful, gate-ul de evidence în matricea de 12 combo) — randări moarte sau false, nu date fake din backend.

---

## 2. TABEL CÂMP-CU-CÂMP (UI → producător)

Legendă status: **OK** = lanț complet verificat; **DEAD-RENDER** = UI citește o cheie inexistentă în payload; **FAKE** = se afișează o valoare ce nu derivă din date; **PARȚIAL**; **P3** = cosmetic/inconsistență.

### 2.1 Panou parametri (`frontend/index.html:322-496`)

| Element UI | Handler (app.js) | Endpoint | Producător backend | Status |
| :--- | :--- | :--- | :--- | OK/Defect |
| `#bt-ticker`, `#bt-compare-tickers`, `#bt-days` (+butoane 7/14/30/90 → `App.setBacktestDays:17052`), `#bt-train-days`, `#bt-test-days`, `#bt-custom-tp/sl`, `#bt-variant-mode`, `#bt-orb-entry` | `App.runBacktest:17347`, `App.runWalkForward:17676` | `POST /api/backtest/run` (`main.py:3783`), `POST /api/backtest/walk-forward` (`main.py:3803`) | `run_backtest` (`actions_service.py:1776`), `run_walk_forward_backtest` (`:1456`) | OK (modele `BacktestRequest`/`WalkForwardRequest` `main.py:1085/1096` conțin toate cheile trimise) |
| `#bt-risk` = OPENING_RANGE | `runBacktest:17423` | `POST run` (`trade_mode=ORB_FVG`) | `run_orb_fvg_backtest` (`core/orb_fvg_backtester.py:389`); crypto respins onest (`actions_service.py:1867-1871`) | OK |
| `#bt-risk` = FAITHFUL | `runBacktest:17510` (timeout 180s) | `POST run` (`risk_mode=FAITHFUL`) | `core/strategy_backtester.backtest` + adaptor `actions_service.py:1913-1920` | **P1 deschis** (vezi §4.1) |
| `#bt-risk` = ALL | `runBacktest:17574` | `POST run` (`compare_all=true`) | `_get_strategy_comparison` (`actions_service.py:1304`) | **P1 deschis** (vezi §4.2) |
| Presets `#bt-presets` | `saveBacktestPreset:17143` | — (localStorage) | — | OK |
| `#bt-last-time` | `renderBacktestResults:18110` + `viewBacktest:17961` | — | — | OK |

### 2.2 Tab Overview — mod comparație (`renderBacktestResults:18239-18437`)

| Câmp randat | Cheie payload | Producător | Status |
| :--- | :--- | :--- | :--- |
| Banner "Compared Strategies for {ticker} ({days} days)" | `ticker`, `days` | `run_backtest:1848-1854` | OK |
| Sortare "Score" | `_score` calculat FE: `win_rate_trades + expectancy_r` (`app.js:18244`) | — (client-side) | P3: NU este `_composite_score`-ul backend (`actions_service.py:1450`: exp×80+wr×0.6+(pf−1)×10−dd×0.3)×confidence; același etichetă "Score", două formule diferite |
| Coloană Strategy | `ticker`·`trade_mode` | `actions_service.py:1363-1365` | OK |
| Coloană Risk / Quality `[A][B][C][D]` | `risk_mode` re-etichetat (`app.js:18301-18310`: STRONG→A, MEDIUM→B, RELAX→C, PRO→D) | `actions_service.py:1363` | P3: gradul e doar re-labeling-ul risk mode-ului, nu o măsurătoare de calitate |
| Coloană WR (Trades) | `win_rate_trades` | `quick_backtester.py:280` (net_pnl>0 / total) | OK (live: 50.0 = 1/2) |
| Coloană Exp (R) | `expectancy_r` | `quick_backtester.py:172` = wr·avgW − lossR·\|avgL\| | OK (live: 0.4687 = 0.5·1.9718 − 0.5·1.0345) |
| Coloană Payoff | `payoff_ratio`; `null → 'N/A'` (`app.js:18291`) | `quick_backtester.py:159/187`; None la 0 pierderi | OK — c7edafc confirmat live (RELAX BREAKOUT 1W/0L → `payoff=None` în payload, UI afișează N/A) |
| Coloană Total | `total_signals` | `quick_backtester.py:275` (wins+losses) | OK |
| KPI bloc expandat: PF / MAX DD / WR / AVG WIN / AVG LOSS | `profit_factor` (null→N/A ✓), `max_drawdown_pct`, `avg_win_r`, `avg_loss_r` | `quick_backtester.py:183-191`, `actions_service.py:1372-1377` | OK |
| Bloc "Evidence status" + scor "N/A" la sample insuficient | `r.evidence.sufficient` (`app.js:18286`) | **doar FAITHFUL atașează evidence** (`runBacktest:17557`, payload live: `{'sample_size':3,'minimum_sample':30,'sufficient':False}`); `_get_strategy_comparison:1362-1381` NU produce `evidence` | **DEAD-RENDER (P2-D)** pentru matricea de 12 combo — gate-ul nu se poate activa niciodată acolo |
| Banner Top Strategy: SCORE/WR/EXPECTANCY/PF/TOTAL TRADES | `best._score`, `win_rate_trades`, `expectancy_r`, `profit_factor`, `total_signals` | idem + `app.js:18393-18418`; PF null→'N/A' ✓ | OK |
| **KPI-uri sidebar** (`BacktestUI.updateKPIs:512-526` → `#kpi-pf`, `#kpi-dd`, `#kpi-wr`, `#kpi-exp`, `#kpi-payoff`) | `profit_factor`, `max_drawdown_pct`, `win_rate_trades`, `expectancy_r`, `payoff_ratio` | apelat din `app.js:18422`; **ID-urile nu există nicăieri în `frontend/index.html`** (grep `kpi-pf` → doar `app.js:520`) | **DEAD-RENDER (P2-A)** — setterul no-op tăcut (`if (el)`), 0 efect vizibil |
| Trades tab pe strategie | `r.trades` = `signals` din engine | `core/backtester.py` semnale: `{type, entry, exit_price, result, net_pnl, qty, sl, tp, commission, ...}` — **fără `entry_time`/`exit_time`/`pnl_pct`/`r_multiple`** (verificat live pe payload) | **P2-E**: Entry/Exit Date mereu `-`; PnL% recalculat client din entry/exit (brut, ignoră `net_pnl`/comision — P3); badge-ul R (`app.js:18202`) nu se randează niciodată |

### 2.3 Tab Overview — vizualizarea standard (`else` la `app.js:18438-18568`)

Atingibilă doar prin `viewBacktest(index)` pe recorduri de istoric (rulările proaspete sunt re-ambalate în mod comparație la `17361-17666`). Record standard salvat = `run_backtest:1927-1938+2033` cu chei top-level `{mode, days, risk_mode, trade_mode, variant_mode, total_wins, total_losses, win_pct, loss_pct, signals, timestamp}` — **fără** `payoff_ratio/expectancy_r/max_drawdown/avg_win_r/avg_loss_r/profit_factor` top-level.

| Câmp randat | Cheie citită | Realitate payload | Status |
| :--- | :--- | :--- | :--- |
| WIN RATE | `data.win_pct` | există pentru standard | OK; dar record **faithful** nu are `win_pct` (are `win_rate`) → randează literal **"undefined%"** (`app.js:18453`) — **P2-B** |
| PAYOFF RATIO | `data.payoff_ratio \|\| 0` (`:18460`) | absent în payload standard → mereu **0** | **DEAD-RENDER (P2-B)** |
| EXPECTANCY | `data.expectancy_r \|\| 0` (`:18467`) | absent → mereu **0R** | **DEAD-RENDER (P2-B)** |
| MAX DRAWDOWN | `data.max_drawdown \|\| 0` cu prefix "-" (`:18474`) | absent (și la faithful cheia e `max_drawdown_pct`) → mereu **-0%** | **DEAD-RENDER (P2-B)** |
| Avg Win / Avg Loss | `data.avg_win_r/avg_loss_r` (`:18482-18483`) | absente standard → 0R | **DEAD-RENDER (P2-B)**; pe faithful sunt OK |
| Profit Factor | `data.profit_factor == null → 'N/A'` (`:18484`) | absent standard → mereu **N/A** (mascare onestă dar moartă) | DEAD-RENDER (P2-B) |
| Total Trades | `total_wins+total_losses` | există | OK |
| **Histogramă R-Multiple Distribution** (`:18494-18527`) | `t.r_multiple` | `core/backtester.py` **nu emite `r_multiple`** (grep: singurii producători sunt `orb_fvg_backtester.py:382/340` și `backtest_optimizer.py:220`); pentru recorduri standard `data.signals` = dicționare per-ticker (nu trade-uri) → toate valorile 0 | **FAKE (P2-C)**: o singură bară la 0.0R cu înălțimea = #tickere; funcționează corect doar pentru recorduri ORB din istoric |
| Rând unic "Active Strategy" — coloană Score | `data.win_pct` (`:18556`) | win rate afișat sub eticheta "Score" | P3 |
| Trades tab (standard record) | `data.signals` trecut prin `buildTradesTable` (`:18441-18443`) | dicționare per-ticker, nu trade-uri → rânduri false: Type `-`, Entry 0.00, PnL `0%`, Status `OPEN` (`:18134-18152`) | **P2-E** (rânduri fabricate) |
| Trades tab (faithful record) | `data.trades` = `<int>` → gardat → `[]`; `trade_list` (3 trade-uri reale, cu entry_time/exit_time/r) **nu e consumat nicăieri** în `viewBacktest` | **P2-E**: "No signals." deși există 3 tranzacții în payload (verificat live) |

### 2.4 Walk-Forward (`renderWalkForwardResults:17989-18101` ← `run_walk_forward_backtest` `actions_service.py:1456-1634`)

| Câmp | Cheie | Producător | Status |
| :--- | :--- | :--- | :--- |
| WINDOWS / skipped | `summary.windows` | `:1624` | OK (`skipped_windows` produs dar nerandat — acceptabil) |
| WIN RATE (TRADES) | `summary.win_rate_trades` | `:1609-1610` = wins/(wins+losses) | OK — matematică verificată |
| AVG EXPECTANCY / AVG TRAIN SCORE / AVG TEST SCORE | `summary.avg_*` | `:1611-1613` | OK |
| TOTAL TRADES | `summary.total_trades` | `:1601` (`closed_trades or trade_count` — fallback funcțional, `closed_trades` mereu 0 din `_summarize_bt_result:1413` deoarece `quick_backtest` nu returnează cheia) | OK (dar fallback-ul maschează un câmp mort — P3) |
| Strategii pe fereastră | `summary.strategy_counts` | `:1606-1607` | OK |
| Rânduri fereastră: Test Window | `test_start/test_end` | `:1572-1575` | P3: separatorul randat e literal **"?"** (`app.js:18019` `${startDate} ? ${endDate}`) |
| Selected Strategy / Score | `best_strategy.risk_mode/trade_mode/train_score/test_score` | `:1576-1581` (test = `_composite_score`) | OK |
| WR / Exp (R) / DD | `test_summary.*` | `_summarize_bt_result:1405-1423` | OK |
| **PF** | `test.profit_factor !== undefined ? Number(...).toFixed(2) : '-'` (`app.js:18011`) | `Number(null) = 0` → afișează **"0.00"** pentru ferestre fără pierderi, în loc de N/A | **P2-F**: fixul c7edafc (corect în backend) este mascat înapoi la 0.00 în UI walk-forward |
| Trades tab | mesaj static "does not expose detailed trade log" | onest | OK |

### 2.5 Strict OOS Audit / Research Ledger (`renderStrictOosAudit:17816-17943` ← GET `main.py:3835/3852`)

| Câmp | Status |
| :--- | :--- |
| Toate câmpurile numerice trec prin `number()/percent()` cu fallback 'N/A' (`:17829-17836`) | OK — robust la null |
| Live: ambele endpoint-uri → `{"status":"no_reports","reports":[], ...}` | OK — starea goală randată cu placeholder onest (`:17851-17864`) |
| Proveniență SHA-256, praguri, capital model, limitări | consumate cu fallback-uri (`:17915-17920`); nerandabile acum (fără artifact) | OK |

### 2.6 Istoric (`UI.renderBacktestHistory:22167-22200` ← GET `main.py:3824` ← `get_backtest_history:276` ← `data/backtest_results.json`, `save_backtest_result:246-270`, insert(0), cap 50)

| Câmp | Status |
| :--- | :--- |
| ticker / timestamp / days | OK (toate recordurile live le au) |
| Badge mod: `comparison→Strategies`, `orb_fvg→ORB FVG`, altfel `Standard` (`:22179-22181`) | **P3**: recordurile `faithful` primesc badge-ul **"Standard"** (greșit) |
| `viewBacktest:17945` | OK; gardurile trades-int de la 7f26fd2 împiedică crash-ul |
| `diffLastBacktest:17249` | folosește `win_pct/total_wins/total_losses` — există doar pe recorduri standard; pe comparison/faithful diferența e mereu 0 | P3 |

### 2.7 Sursa ultimă de date (verificată prin cod, conform rundei 1)

Stocuri → Yahoo Finance (`core/backtester.py:_fetch_stock_historical`), crypto → Binance `/api/v3/klines` (`_fetch_crypto_historical`), FAITHFUL → Binance 1m+HTF (`core/strategy_backtester.py:75-120`), costuri → `core/trading_costs.py:resolve_trading_cost_model`. Niciun producător sintetic de KPI găsit.

---

## 3. VERIFICAREA MATEMATICII PE PAYLOAD LIVE (200 OK)

Sursa: `GET /api/backtest/history?limit=3` (record AAPL 5d comparison, 2026-09-05T01:45:18; BTC-USD faithful 7d).

| Măsurătoare | Valoare UI/payload | Recalcul | Verdict |
| :--- | :--- | :--- | :--- |
| AAPL MEDIUM/PULLBACK wr_trades | 50.0 | 1/2 = 50.0 | OK |
| AAPL payoff_ratio | 1.9061 | 1.9718 / 1.0345 = 1.9058 | OK |
| AAPL expectancy_r | 0.4687 | 0.5·1.9718 − 0.5·1.0345 = 0.4687 | OK |
| AAPL profit_factor | 1.91 | gross_profit/gross_loss = 1.9718/1.0345 | OK |
| BTC faithful win_rate | 66.7 | 2/3 | OK |
| BTC faithful expectancy_r | 0.665 | (2/3)·1.53 − (1/3)·1.066 = 0.6647 | OK (diferă de `avg_R`=0.705 = media `r_net`; `expectancy_r` e cel corect) |
| 12 combo AAPL | DISTINCT (0–3 trade-uri, WR 0/33.3/50/100, exp −0.12…+2.12) | — | **d4dd784 confirmat live**: combo-urile nu mai sunt identice |

---

## 4. STATUS FIXURI Runda 1

### 4.1 REDESCHIS — P1: FAITHFUL ~253s > timeout UI 180s
`frontend/app.js:17525` (`timeout: 180000`) neschimbat; `core/strategy_backtester.py` paginează 1m + HTF (`:96-116`, `sleep(0.05)`/pagină, `expected_bars/1000` pagini + interogări HTF `:226-227`). Nu există niciun mecanism de reiaere/progress. La depășire: `Request timed out after 180000ms` → catch → **"Connection error."** înșelător (`app.js:17670`). **Rămâne deschis.**

### 4.2 REDESCHIS — P1: compare_all crypto — 12 combo × re-fetch, fără `df=`
`_get_strategy_comparison` (`actions_service.py:1351-1360`) apelează `quick_backtest` **fără `df=`** — spre deosebire de walk-forward care face fetch o singură dată și îl reutilizează (`:1486`, `:1534`, `:1562`). Fiecare combo re-paginează Binance (până la 30 cereri/combo ⇒ ~360 cereri). `_data_cache` din `quick_backtester.py:385-405` e folosit doar de `fetch_backtest_data`, care nu e apelat aici. **Rămâne deschis.**

### 4.3 REZOLVAT — payoff_ratio None (c7edafc)
Diff confirmat: `actions_service.py:1419` — `round(result["payoff_ratio"],2) if result.get("payoff_ratio") is not None else None`. Live: combo-uri cu 0 pierderi returnează `payoff=None, profit_factor=None`; tabelul comparație randează `N/A` (`app.js:18291/18317`). **Consistent.** (Atenție: mascat înapoi la "0.00" în walk-forward — P2-F mai jos.)

### 4.4 REZOLVAT — crash `viewBacktest` pe recorduri faithful (`trades:<int>`)
3 garduri prezente: `app.js:17219-17220` (export CSV), `18441-18442` (randare standard), `18495-18496` (histogramă). Payload live `trades=3 (int)` — nu mai crăpează.

### 4.5 PARȚIAL — fix CSS grid (7f26fd2, `styles_backtest.css?v=1.6`)
Regula `.bt-results-container .wl-row { display:grid; }` (`frontend/styles_backtest.css:382-387`, cache-bust `frontend/main.css:25`) acoperă **doar** `#backtest-results` (`index.html:530`). **`#bt-strategies-list` (index.html:538) și `#bt-trades-list` (index.html:543) NU au clasa și NU există nicio regulă `.wl-row { display:grid }` globală** (grep: singura regulă `.wl-row` plasmă e cea scoped). Rezultat: matricea de 12 combo (rânduri la `app.js:18365`), journal-ul de trade-uri (`:18194`) și lista walk-forward duplicată în tab-ul Strategies (`:18081-18095`) își stack-ează celulele vertical, în timp ce header-ele (`.wl-table-header`, `styles_watchlist.css:469`) rămân pe un singur rând → decalaj header/rânduri. **Fix incomplet — P2-G.**

### 4.6 REZOLVAT — 12 combo-uri distincte live
Config shim + cache key cu TP/SL (`quick_backtester.py:247-261`, `_ConfigShim`) + record istoric live AAPL cu 12 rezultate distincte (§3). Închis.

---

## 5. DEFECTE (consolidat)

| ID | Severitate | Descriere | Dovezi |
| :--- | :--- | :--- | :--- |
| D-1 | **P1** | FAITHFUL crypto: rulare reală ~253s vs timeout UI 180s; eroarea e mascată în "Connection error." | `app.js:17525,17668-17670`; `core/strategy_backtester.py:96-116,226` |
| D-2 | **P1** | Compare All (crypto): 12 × re-fetch Binance (fără reutilizare `df=`) → nu se întoarce în timeout; multi-ticker (`#bt-compare-tickers`, `app.js:17383-17421`) agravează (1 POST/ticker) | `actions_service.py:1351-1360` vs `:1486,1534,1562`; `quick_backtester.py:374-410` |
| D-3 | **P2** | `BacktestUI.updateKPIs` — dead render total: `#kpi-pf/#kpi-dd/#kpi-wr/#kpi-exp/#kpi-payoff` nu există în `index.html` | `app.js:512-526,18422`; grep `kpi-pf` în `index.html` = 0 rezultate |
| D-4 | **P2** | Grila KPI a vizualizării standard citește 6 chei top-level inexistente (`payoff_ratio`, `expectancy_r`, `max_drawdown`, `avg_win_r`, `avg_loss_r`, `profit_factor`) → mereu 0/0R/-0%/N/A; pe recorduri faithful `win_pct` lipsă → **"undefined%"** | producător: `actions_service.py:1927-1938`; consumator: `app.js:18452-18485` |
| D-5 | **P2** | Histograma "R-Multiple Distribution" — FAKE: `r_multiple` nu e emis de `core/backtester.py`; pentru recorduri standard se hartoară `signals` (dicționare per-ticker) → o singură bară 0R cu înălțimea #tickere; funcțional doar pentru recorduri ORB | `app.js:18494-18527`; producător doar `orb_fvg_backtester.py:296,340,382` |
| D-6 | **P2** | Jurnalul de tranzacții moarte la reîncărcarea din istoric: (a) recorduri standard → `signals[]` (dicționare ticker) randate ca trade-uri false "OPEN 0%"; (b) recorduri faithful → `trade_list` (cu timestamp-uri reale) niciodată citit → "No signals."; (c) trade-urile engine nu au `entry_time/exit_time` → coloanele Entry/Exit Date mereu `-`; (d) badge-ul R niciodată randat | `app.js:18134-18207,18440-18443`; payload live (chei trade fără timp/r_multiple) |
| D-7 | **P2** | Walk-Forward: PF `null` → afișat **"0.00"** (`Number(null)`), în loc de N/A — maschează fixul c7edafc | `app.js:18011`; `actions_service.py:1416` |
| D-8 | **P2** | Fix CSS grid parțial: tab-urile Strategies/Trades (matrix + journal) rămân fără `display:grid` | `styles_backtest.css:382-387`; `index.html:530,538,543` |
| D-9 | P3 | Gate-ul "Evidence status"/scor N/A mort pentru matricea de 12 combo (backend nu atașează `evidence` decât la FAITHFUL) | `app.js:18286,18333`; `actions_service.py:1362-1381` |
| D-10 | P3 | Două definiții diferite pentru "Score": FE `wr+exp` (comparație) vs backend `_composite_score` (walk-forward) | `app.js:18244`; `actions_service.py:1450` |
| D-11 | P3 | PnL% afișat e brut (din entry/exit), ignoră `net_pnl`/comision prezente în payload | `app.js:18142-18154` |
| D-12 | P3 | Badge istoric: recorduri faithful etichetate "Standard"; `Compare vs Last` inoperabil pe comparison/faithful (diferențe mereu 0) | `app.js:22179-22181,17256-17267` |
| D-13 | P3 | `equity_curve` produs de ORB (`orb_fvg_backtester.py:600-610`) nu are niciun consumator UI în Tab 9 | grep `equity_curve` în `app.js` → doar Tab Capital (`:2710`) |
| D-14 | P3 | Cosmetice: separator "?" între datele ferestrei WF (`app.js:18019`); "Score" = win_pct în rândul standard (`:18556`); prefix "-" la DD doar în vizualizarea standard (`:18474`); CSV comparație fără coloana payoff (`:17198-17216`) | — |

**Total: 2×P1 (deschise din runda 1), 6×P2 (D-3…D-8), 6×P3.**

---

## 6. RECOMANDĂRI (prioritizat)

1. **D-2 (P1):** în `_get_strategy_comparison`, faceți un singur `fetch_backtest_data(ticker, is_crypto, days, ui_interval)` și pasați `df=` către toate cele 12 `quick_backtest` (identic cu walk-forward). Reduce 360 → 30 cereri Binance.
2. **D-1 (P1):** timeout FAITHFUL la 300s + mesaj de eroare dedicat ("replay still running, try fewer days") sau `days` max redus pentru FAITHFUL (ex. ≤3 zile) până la optimizarea paginării.
3. **D-3:** fie eliminați `BacktestUI.updateKPIs` + apelul din `18422`, fie adăugați cele 5 carduri KPI în sidebar (`index.html`) — acum codul mort sugerează o funcționalitate inexistentă.
4. **D-4:** în `run_backtest`, promovați KPI-urile agregate ale primului semnal la top-level al payload-ului standard (payoff_ratio, expectancy_r, max_drawdown_pct, avg_win_r, avg_loss_r, profit_factor) sau faceți FE să citească din `signals[0]`; adăugați `win_pct` în adaptorul FAITHFUL (`actions_service.py:1913-1920`).
5. **D-5:** emiteți `r_multiple` per trade din `core/backtester.py` (net_pnl/risk, exact ca în `_compute_trade_kpis`) sau calculul R pe client din `net_pnl/(|entry−sl|·qty)`; altfel eliminați histograma.
6. **D-6:** (a) `viewBacktest`: pentru recorduri standard folosiți `signals[i].trades`; (b) pentru faithful mapați `trade_list` → format `buildTradesTable` (același mapping există deja în `runBacktest:17533-17542`).
7. **D-7:** în `renderWalkForwardResults`, tratați `null` explicit: `test.profit_factor == null ? 'N/A' : Number(...).toFixed(2)`.
8. **D-8:** lărgiți regula CSS la `.bt-results-area .wl-row` (acoperă toate 3 containerele) — `.bt-results-area` este comun (`index.html:517`).
9. **D-9/D-13:** atașați `evidence` (sample_size) și rândurilor de comparație din `_get_strategy_comparison`; randare `equity_curve` ORB sau eliminare din payload.

---

## 7. CONCLUZIE

Lanțul de date Tab 9 este autentic (fără valori fabricate în backend) și matematic corect acolo unde cheile există; fix-urile rundei 1 pentru `payoff_ratio`, crash-ul `trades:<int>` și combo-urile identice sunt confirmate live. Cea mai importantă constatare nouă este de altă natură: **grupa de randări "analitice" ale UI-ului (histograma R, KPI-urile sidebar, grila vizualizării standard, jurnalul la reîncărcare din istoric) consumă chei pe care backendul nu le produce** — userul vede 0/N/A sau bară falsă la 0R acolo unde crede că citește analytics. Ambele P1 din runda 1 (FAITHFUL timeout, compare_all crypto) rămân nerezolvate în cod.
