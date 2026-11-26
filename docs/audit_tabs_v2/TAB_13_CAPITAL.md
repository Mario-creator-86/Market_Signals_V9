# AUDIT CRIMINALISTIC V2 (ROUND 2): TAB 13 — CAPITAL & PAPER TRADING TERMINAL (`#view-capital`)

**Data:** 2026-09-05 (09-06 UTC)
**Metoda:** re-audit camp-cu-camp UI↔backend, cu verificare live pe `http://127.0.0.1:8003` (doar GET, 5 cereri curl, payload salvat in $TMP, nu in repo).
**Referinta Round 1:** `docs/audit_tabs/TAB_13_CAPITAL.md` (2026-09-04).
**Commituri verificate:** `git log --oneline -10` → `1eedcfc` (docs), `218009f`, `c7edafc` (fix audit-leak), `e9eda27`, `2a3ebce`, `e04cf46`, `7f26fd2`, `80180a2`, `2acbdd9`, `8bf97a2`. Niciun commit recent nu atinge caii capital intr-un mod regresiv.

---

## 1. VERDICT GENERAL

**SITA (AIGO) — TAB 13 ESTE FUNDAMENTAL SANATOS: 0 × P1, 0 × P2, 8 × P3.**

Toate cifrele monetare randate (equity, open PnL, exposure, win rate, raw cash, daily P/L cross-cockpit) s-au reconciliat **la cent** cu payload-ul live si cu lantul contabil SQLite/ledger. Corecturile Round 1 (GZip + cache TTL 10s) sunt **confirmate functionale in productie**. P3-ul din Round 1 privind `/api/capital/equity-curve` (endpoint neutilizat de UI si auto-inconsistent) **ramane deschis si a fost re-confirmat numeric live**.

Spre deosebire de Round 1 (care a raportat "reconciliere cockpit↔capital la cent"), auditul V2 arata ca reconcilierea la cent este valabila pentru **cash / marja / win rate / daily realized PnL**, dar NU pentru **equity si open PnL**, unde cockpit-ul foloseste alta baza (net vs. gross) — vezi P3-6. Sunt in plus **4 randari moarte** (elemente DOM care nu mai exista in `index.html`, dar codul le scrie in continuare) si o **grebla semantica** in palnul de gate-funnel.

### Matricea starii V2

| Zona | Stare V2 | Dovada live |
|---|---|---|
| Portfolio Cockpit (equity/PnL/exposure/win-rate) | OK | equity calculat 10019.98 = ultimul snapshot curba 10019.98 (diff −0.00) |
| GZip + TTL 10s (fix Round 1) | **CONFIRMAT FUNCTIONAL** | `content-encoding: gzip`, 1 765 564 B → 236 622 B (7.5×), MISS 2.05 s → HIT 0.037 s |
| Equity curve (SVG, 90 puncte, minute-keyed) | OK | ultimul punct = equity-ul calculat din pozitiile live |
| Jurnal tranzactii (ledger) | OK | lant `raw_balance` se inchide la balance 7917.384; 100/100 intrari scope `capital` |
| Win rate | OK (server-authoritative) | 46.7% / 227 closed; denumitor = pozitii inchise non-research |
| Cross-check cockpit (Tab 1) | OK partial | daily_realized_pnl −11.468067 = recomputare ledger 7 CLOSE azi (exact); win_rate/marja/cash = identice; **equity difera cu 1.70 (baza net vs gross)** |
| `/api/capital/equity-curve` | P3 (Round 1, inca deschis) | `mark_to_market_pnl: 0.0` cu 13 pozitii deschise; total_equity 10015.31 vs UI 10019.98 (−4.67); zero apeluri din `app.js` |
| Randari moarte | 4 × P3 | `#capital-balance-display`, `#capital-open-pnl-pct`, `#positions-last-sync`, intreg lantul Decisions (3 containere lipsa) |

---

## 2. TABELUL CAMP-CU-CAMP (UI → payload → endpoint → producator → sursa finala)

Legenda clasificare: **OK** = lant complet si verificat live; **DEAD-RENDER** = codul scrie intr-un element care nu exista in DOM; **DEAD-BACKEND** = backend produce date pe care nimic nu le consuma; **STALE/MISCALIBRATED** = date reale dar prezentare inselatoare.

Endpointulunic consumat de tab: `GET /api/state?include_news=false&include_hotlist=false&lean=true&include=capital&unchanged=false` (`frontend/app.js:653-692`, cheia cache `api_state:...:capital:...`, `api/main.py:1856`). Producator: `get_capital_state` → `_build_capital_payload` (`api/state_service.py:1466-1500`, `893-1461`).

| # | Camp UI (DOM) | Frontend | Cheie payload | Producator backend | Sursa ultimă | Clasificare |
|---|---|---|---|---|---|---|
| 1 | Portfolio Equity `#capital-equity-value` (`index.html:859`) | `app.js:2697-2704` — `balance + Σmargin_used + Σ(unrealized_pnl_gross ?? current_pnl)` | `capital.balance`, `capital.positions[].margin_used/.unrealized_pnl_gross/.current_pnl` | `state_service.py:893-1146` (hidratare preturi) + echivalent server `resolve_capital_equity` `core/capital_manager.py:50-90`, snapshot `_snapshot_daily_equity` `capital_manager.py:3646-3699` | Cash din CapitalManager (SQLite `market_signals.db` + oglinda JSON), marja din pozitii OPEN, MTM din preturi hidratate (state cache → `live_prices_cache.json` → SQLite `live_price_cache` → eToro bulk) | **OK** — live: 7917.38 + 2097.92 + 4.68 = **10019.98**; identic cu ultimul snapshot al curbei (diff −0.00) |
| 2 | Open P&L `#capital-open-pnl` (`index.html:863`) | `app.js:2642-2667` — Σ`(unrealized_pnl ?? current_pnl)` (NET), colorare verde/rosu | `positions[].unrealized_pnl`, `.current_pnl` | `_apply_live_pnl` `state_service.py:1056-1108` (gross − comision intrare − comision iesire estimat) | Pret curent hidratat + pret intrare din pozitie | **OK** — live: **+3.51**; verificat net ≤ gross pe toate cele 13 pozitii; 0 pozitii cu warning de comision lipsa |
| 3 | Open P&L % `#capital-open-pnl-pct` | `app.js:2663-2672` | — | — | — | **DEAD-RENDER** — elementul nu exista in `index.html` (grep 0 rezultate); pct-ul Open P&L nu se afiseaza niciodata |
| 4 | Balanta cash bruta `#capital-balance-display` | `app.js:2602-2603` | `capital.balance` | `state_service.py:1436` (`deployable_cash_balance`) | CapitalManager | **DEAD-RENDER** — element absent din markup; cash-ul brut nu mai este vizibil nic dasheri (doar equity) |
| 5 | Exposure `#capital-exposure-value` (`index.html:867`) | `app.js:2698-2707` — `Σmargin_used / equity × 100` | `positions[].margin_used` | acelasi ca #1; omolog server `cockpit_service.py:432-434` (`notional_exposure_pct`) | Marja blocata din pozitii | **OK** — live: **20.94%** == cockpit `notional_exposure_pct: 20.9409` |
| 6 | Win Rate `#capital-win-rate` (`index.html:871`) | `app.js:2674-2689` — prefera `capital.win_rate`/`total_closed_trades`; fallback istoric | `capital.win_rate`, `capital.total_closed_trades` | `state_service.py:1404-1410` — wins = pozitii CLOSE non-research cu `pnl > 0`; denumitor = total inchise non-research | Pozitii CLOSE din CapitalManager | **OK** — live: **46.7% / 227**; denumitor sanatos (break-even numarat ca pierdere, nu win). Fallback-ul frontend are denumitor diferit (doar fereastra 100 de evenimente) dar e inactiv atata timp cat serverul trimite valorile (P3-8) |
| 7 | Contoare pozitii `#capital-positions-count` / `#capital-research-count` (`index.html:977-978`) | `app.js:2615-2637` — filtre pe `status==='OPEN'` + `research_only`/`research_cohort` | `capital.positions[]` (+ `capital_positions`, `research_positions` neconsumate) | `state_service.py:1421-1427`, `707-722` | CapitalManager | **OK** — live: **13 capital / 0 research** == `position_summary` (total_open 13, paper_experiment_open 13) |
| 8 | Equity Curve SVG `#capital-equity-curve` (`index.html:876`) | `app.js:2710` → `_renderCapitalEquityCurve` `app.js:2936-3080`; scala `_buildCapitalEquityScale` 2909-2934; segmente drepte, timescale, guard ±1000% la reset | `capital.equity_curve[].{date,total_equity}` | `state_service.py:1454` → `cm.get_equity_curve(limit=90)` `capital_manager.py:991-1005` | SQLite `equity_history` (snapshot-uri la minut scrise de `_snapshot_daily_equity` + `_append_equity_snapshot`) | **OK** — live: 90 puncte (19:40→21:09 UTC); ultimul = 10019.98 = equity-ul calculat; KPI-urile cardului (latest/start/high/low/delta) deriva din acelasi array |
| 9 | Jurnal `#capital-history-body` (`index.html:902`): Data, Event/Scope, Details, Amount, Raw Cash + rand detaliu | `app.js:2838-2894`; detalii `app.js:7456+`, `7323+`; sortare desc dupa timestamp | `capital.ledger[]` (fallback `capital.history`) | `state_service.py` → `cm.get_ledger` (`capital_manager.py:1008+`, cu `_enrich_ledger_entry_unlocked`) | Ledger double-entry SQLite (OPEN_POSITION / RELEASE_MARGIN / CLOSE_POSITION + scope) | **OK** — live: 100 intrari, scope 100% `capital`; ultimul `raw_balance` = **7917.384** = balance-ul curent (lant inchis la micro-cent); tipuri: 36 CLOSE / 29 OPEN / 35 RELEASE |
| 10 | Export CSV | `app.js:24157` (`Actions.exportCapitalHistory`) — sanitizare anti-formula `= + - @` păstrata | `capital.ledger[]` | idem #9 | idem | **OK** (verificare code-level; nicio schimbare regresiva vs Round 1) |
| 11 | Order Terminal `#capital-open-*` (`index.html:913-942`) | `Actions.openCapitalPosition` → `submitCapitalPaperOrder` `app.js:24045/24078` → `API.openCapitalPosition` `app.js:1551` | body POST | `POST /api/capital/open-position` `api/main.py:1451` | CapitalManager + bid/ask live | **OK** (code-level; POST in afara scope-ului GET al auditului) |
| 12 | Autopilot status + butoane `#capital-autopilot-status`, `#capital-bot-start/stop`, `#capital-cycle-run` (`index.html:951-961`) | `app.js:3167-3196` | `opportunities.autonomous_enabled/bot_enabled/execution_agent_enabled/autonomous_candidates_enabled` | `state_service.py:697-699, 793-796` | `config.bot.enabled` + `config.agents.execution.*` | **OK** — toate cheile prezente in payload |
| 13 | Carduri semnal `#capital-opportunities` (ticker, score, chip decizie, entry/tp/sl, RSI/MACD/strategie/varsta, news, motiv, PAPER AUTO, learning chip) | `app.js:3198-3294` | `opportunities.signals[]` (toate cheile consumate prezente) | `_build_capital_opportunities` `state_service.py:427-637` (statusuri: open_capital/open_research/eligible/blocked/research_only/conflict/below_score/invalid_timestamp/expired; gating varsta `max_signal_age_seconds`) | `signals_state.json` (semnale OPEN) + `summarize_ticker_news_signals` + ticker learning | **OK** — live: 12 semnale, toate `open_capital`/paper experiment; toate cheile consumate de UI exista |
| 14 | Paln gate-funnel (Signals/Verified price/Faithful OOS/Realised edge/AI+News/Capital eligible) | `app.js:3333-3375` | `opportunities.gate_funnel` | `state_service.py:724-746` | `capital_entry_gate.checks` per semnal | **MISCALIBRATED (P3-5)** — live: `price_data_passed:13` dar `faithful_oos_passed:0, realized_edge_passed:0, news_ai_passed:0` si simultan `capital_eligible:13`. Cauza: etapele `not_evaluated` (experimente paper, gate `paper_autonomous_experiment`/`paper_only`) se numara ca 0, in timp ce `capital_eligible` numara statusul `open_capital`. Funnel-ul afiseaza o poveste auto-contradictorie (0/13 pe 3 etape, 13/13 la final) |
| 15 | Readiness card + scope positions | `app.js:3335-3366` | `opportunities.readiness`, `position_summary` | `state_service.py:765-791, 716-722` | pozitii + config | **OK** — live: `paper_experiment_open` cu motiv; 0 Capital / 13 Paper Auto / 0 Research |
| 16 | Primary blocker | `app.js:3376-3383` | `opportunities.blocker_summary` | `state_service.py:748-763` | gate-uri semnale | **OK** — live: gol → sectiune ascunsa |
| 17 | Ultimul ciclu | `app.js:3323-3331` | `opportunities.last_cycle` | inregistrarea ciclului autonom | runtime bot | **OK** — live: `no_eligible_candidates`, 8 candidati, 0 submisi |
| 18 | Scanner rows `.capital-scanner-row` (ticker, directie, change%, sursa, watchlist) | `app.js:3296-3313` | `opportunities.scanner[]` | `state_service.py:639-682` (`scanner_agent.get_state().hot_tickers`, guard stablecoin USDT/USDC → `X-USD`) | scanner agent (yahoo gainers/losers etc.) | **OK** — live: 12 randuri; toate cheile consumate (`ticker/raw_symbol/direction/change_pct/provider/in_watchlist`) prezente |
| 19 | Carduri pozitii `#capital-trade-cards`: ticker, badge LONG/SHORT, chips strategie/RESEARCH/PAPER AUTO/levier/score, Entry, Current + flash + chip LIVE/CACHE, Stop/Target + distante %, is-near-stop ≤1%, Size, R:R, PnL $/% net, deschidere | `app.js:2735-2836` (dirty-check prin semnatura); PnL `_calculateCapitalPositionPnl` `app.js:7520-7554` | `positions[].{entry_price,current_price,current_price_live,current_price_source,stop_loss_price,take_profit_price,usd_size,leverage,qty,direction,research_only,paper_capital_experiment,explainability,decision_context}` | hidratare `state_service.py:893-1146` + `_apply_explainability` 1110-1124 | preturi: state cache → `live_prices_cache.json` → SQLite `live_price_cache` → eToro bulk (filtru magnitudine ratio 30/0.03 `state_service.py:1013-1020`) → fallback entry_price | **OK** — live: 0 chei lipsa din cele 15 consumate; chip LIVE/CACHE alimentat de `current_price_live` (ferestre 45 s, `state_service.py:1043-1054`) |
| 20 | Explainability panel `#capital-explainability-panel` (`index.html:995`) | `UI.renderCapitalExplainabilityPanel` `app.js:9546+`; selectie sincronizata `app.js:2632`, `_syncCapitalExplainabilitySelection` | `positions[].explainability`, `decision_context` | `summarize_position_explainability` (`state_service.py:1110-1112`) | semnalul sursa + context executie | **OK** (code-level; cheile prezente in payload) |
| 21 | Modal Edit SL/TP `#capital-risk-modal` (`index.html:2541-2611`) | `Actions.saveCapitalRiskLevels` etc. | POST body | `POST /api/capital/update-position-levels` `api/main.py:1630` | CapitalManager | **OK** (code-level) |
| 22 | Tabel Decizii `#capital-decisions-body` + sumar `#capital-decision-summary` + `#capital-strategy-intelligence` | `renderCapitalDecisions` `app.js:3414-3446`; sumar `app.js:8864+`; inteligenta `app.js:8572+`; apelat din `app.js:2902-2903` si dupa fiecare actiune `app.js:23993/24006/24065/24500/24559` | `capital.decisions[]` (embedded) + `GET /api/capital/decisions` (`app.js:1606`, endpoint `api/main.py:1683`) | `_compact_trade_audit(get_trade_audit(limit=20))` `state_service.py:1461`; endpoint dedicat audit decisions `main.py:1683-1719` | trade audit log | **DEAD-RENDER + DEAD-BACKEND partial (P3-4)** — toate 3 containerele lipsesc din `index.html`; `renderCapitalDecisions` face `if (!body) return;` inainte de orice render. Datele se frecventeaza inutil (endpoint apelat la fiecare comanda); singurul efect viu ramane sincronizarea selectiei explainability prin `cachedState.capital_decisions` (`app.js:2632`) |
| 23 | `positions-last-sync` | `app.js:2712-2713` | — | — | — | **DEAD-RENDER** — element absent din `index.html` |
| 24 | Reset modal `#capital-reset-modal` (`index.html:2518-2538`) | `Actions.resetCapital` | POST | `POST /api/capital/reset` `api/main.py:1428` | CapitalManager (curata si `equity_history`) | **OK** (code-level) |

Chei payload **neconsumate de UI** (greutate moarta, mitigata de gzip — P3-7): `capital.accounting`, `capital.capital_positions`, `capital.research_positions`, `capital.raw_cash_balance`, `capital.history` (cand `ledger` exista), `refresh_interval_ms`.

---

## 3. VERIFICARI LIVE (DOVEZI, GET ONLY — 5 cereri)

1. **GZip + cache (fix Round 1):**
   - Fara `Accept-Encoding`: `HTTP 200`, `x-cache: MISS`, `time=2.05s`, 1 765 564 B (necompresat — curl nu advertisementa gzip; browserul da, deci UI-ul real primeste compresie).
   - Cu `--compressed`: **`content-encoding: gzip`**, `size_download=236 622` (reducere **7.5×**), `x-cache: HIT`, `time=0.037s`.
   - TTL: `api/main.py:1865` → `cache_ttl_seconds = 10.0 if include_flags == {"capital"} else 45.0`, cu stale-while-revalidate single-flight (`main.py:1955-2010`). **Confirmat.**
2. **Reconciliere aritmetica din payload** (script pe `$TMP/msv9_capital.json`):
   - `balance = 7917.384`, marja = 2097.92, MTM gross = +4.68, net = +3.51 → **equity UI = 10019.98**; `exposure = 20.94%`.
   - `equity_curve[-1].total_equity = 10019.98` → **diff = −0.00** fata de formula UI (oglinda exacta a `resolve_capital_equity`).
   - `win_rate = 46.7%`, `total_closed_trades = 227` (server-side; denumitor = inchise non-research).
   - Lant ledger: 100 intrari capital; ultimul `raw_balance = 7917.384` = balance curent; tipuri 36 CLOSE / 29 OPEN / 35 RELEASE_MARGIN.
   - `positions`: 13/13 OPEN, toate cheile consumate prezente; `net ≤ gross` pe toate; 0 warning comision lipsa.
3. **Cross-check cockpit (`GET /api/cockpit`, `api/cockpit_service.py:394-464`):**
   - `daily_realized_pnl = −11.468067` (7 tranzactii) == recomputarea mea din ledger: 7 × CLOSE azi, Σ `net_pnl` = **−11.47**. **Exact.**
   - `win_rate 46.7 = 46.7`, `closed_trades 227 = 227`, `open_margin 2097.92 = 2097.92`, `deployable_cash = raw_cash = balance`. **Identice.**
   - `estimated_equity = 10018.29` vs equity-ul tab-ului **10019.98** → **diff −1.70**: cockpit-ul aduna PnL **net** (`unrealized_pnl`, `cockpit_service.py:431-433`), tab-ul Capital aduna PnL **gross** (`unrealized_pnl_gross`). Baze diferite, ambele documentate in cod — dar doua cifre-si titlu diferite pentru "equity" pe doua tab-uri (P3-6).
4. **`GET /api/capital/equity-curve?limit=5` (Round 1 P3 — reconfirmat):**
   - `current.total_equity = 10015.31`, `mark_to_market_pnl = 0.0` cu **13 pozitii deschise** — endpointul foloseste `cm.get_positions()` nehidratat (`main.py:1726-1755`), deci MTM = 0 si equity-ul difera cu **−4.67** fata de UI.
   - **Zero referinte** in `frontend/app.js` la `/capital/equity-curve` — UI-ul consuma exclusiv `capital.equity_curve` embedded. Endpointul este viu dar inutilizat si auto-inconsistent.
5. **`GET /api/capital/decisions?limit=5`:** `records[5]` cu shape corect (`event_id/event_type/payload/position_id/signal_id/status/strategy_type/summary/ticker/timestamp`) — dar tabelul care l-ar afisa nu mai exista in DOM (P3-4).

---

## 4. DEFECTE (P1/P2/P3) CU DOVEZI

**P1 (blocant): 0.** **P2 (major): 0.**

| ID | Severitate | Defect | Dovezi |
|---|---|---|---|
| P3-1 | P3 | DEAD-RENDER `#capital-balance-display` — cash-ul brut nu se afiseaza niciodata; cod mortsilentios | `app.js:2602-2603`; grep `index.html` = 0 rezultate |
| P3-2 | P3 | DEAD-RENDER `#capital-open-pnl-pct` — procentul Open P&L nu se afiseaza niciodata | `app.js:2663-2672`; grep `index.html` = 0 |
| P3-3 | P3 | DEAD-RENDER `#positions-last-sync` — "Last update" nu se afiseaza | `app.js:2712-2713`; grep `index.html` = 0 |
| P3-4 | P3 | Lant Decisions mort: 3 containere lipsa (`#capital-decisions-body`, `#capital-decision-summary`, `#capital-strategy-intelligence`); backend produce `capital.decisions` (20) + endpointul dedicat este apelat dupa fiecare actiune de tradere, dar nimic nu randeaza; singura folosinta viua = sincronizarea selectiei explainability | `app.js:3414-3417` (`if (!body) return;`), `8864-8866`, `8572-8573`, `1606`, `2902-2903`, `23993/24006/24065/24500/24559`; live `GET /api/capital/decisions` → 200 cu 5 records |
| P3-5 | P3 | **Round 1 P3 — inca deschis.** `/api/capital/equity-curve` neutilizat de UI si auto-inconsistent: `current.mark_to_market_pnl = 0.0` cu 13 pozitii deschise; `total_equity = 10015.31` vs UI 10019.98 (−4.67), pentru ca foloseste pozitii nehidratate (`resolve_capital_equity(cm, open_positions)` pe `cm.get_positions()` fara preturi live) | `api/main.py:1722-1758`; grep `equity-curve` in `app.js` = 0 apeluri; payload live `$TMP/msv9_curve.json` |
| P3-6 | P3 | Divergenta cross-tab a bazei de evaluare: cockpit `estimated_equity` foloseste PnL net (`cockpit_service.py:428-433`), tab-ul Capital foloseste MTM gross (`capital_manager.py:62-84` + comment `app.js:2639-2641, 2691-2696`); live diff = 1.70 USD. Round 1 a afirmat "cockpit↔capital reconciliat la cent" — valabil pentru cash/marja/win-rate/daily PnL, dar nu pentru equity/open-PnL | payload live cockpit vs capital; `app.js:2644-2660` |
| P3-7 | P3 | Paln gate-funnel semantics: etapele `not_evaluated` se randeaza ca "0/13" (pare "picat"), in timp ce "Capital eligible 13/13" — pentru experimentele paper (gate `paper_autonomous_experiment`, status `paper_only`) povestea afisata este auto-contradictorie | `app.js:3333-3354`; `state_service.py:479-485, 724-746`; live: `gate_progress` = `{data: passed, backtest/realized_performance/news: not_evaluated}` pe toate cele 12 semnale |
| P3-8 | P3 | Fallback win-rate frontend are alt denumitor (doar CLOSE din fereastra ultimelor 100 evenimente) decat valoarea server-side (227 inchise totale); inactiv in prezent (serverul trimite intotdeauna `win_rate`/`total_closed_trades`), dar daca ar lipsi, ar afisa un win rate pe fereastra scurta etichetat ca global | `app.js:2683-2688` vs `state_service.py:1404-1410` |

**Note cosmetice (fara ID de defect):** drift de comentariu in `api/main.py:1857-1865` (povestea "4.5s" vs cod 10.0/45.0); docstring "daily equity snapshots" (`capital_manager.py:991`) in timp ce snapshot-urile sunt la minut (`minute_key` in payload).

---

## 5. STATUS REGRESII VS ROUND 1

| Element Round 1 | Status V2 |
|---|---|
| GZip middleware (`minimum_size=1024`) | **Prezent si functional** (`api/main.py:929-933`; live 7.5× compresie) |
| Cache TTL 10s pe `include=capital` | **Prezent** (`api/main.py:1865`), extins cu stale-while-revalidate + rebuild single-flight (`main.py:1955-2010`) — MISS 2.05s → HIT 0.037s |
| P3 equity-curve endpoint auto-inconsistent si neutilizat | **NERESOLVAT — reconfirmat numeric** (P3-5) |
| Reconciliere cockpit↔capital "la cent" | **Partial valabila**: exact pentru cash/marja/win-rate/daily realized PnL; difera 1.70 USD pentru equity (baza net vs gross — P3-6, nou-documentata) |
| Filtru magnitudine cotatii (ratio 30/0.03) | Prezent, neschimbat (`state_service.py:1013-1020`) |
| Formula equity (cash + marja + MTM gross) | Neschimbata si re-verified la cent fata de curba (`capital_manager.py:50-90, 3646-3699` vs `app.js:2691-2704`) |
| Anti-injection CSV | Prezent (`app.js:24157`) |
| Rezilienta preturi (state cache → live_prices_cache.json → SQLite → eToro bulk → entry fallback) | Neschimbata (`state_service.py:902-1041`); functia mentionata in Round 1 `_hydrate_capital_live_prices` a fost intre timp inline-uita in `_build_capital_payload` — acelasi comportament |
| `git log` pe cai capital | Nicio regresie: ultimele commituri care ating `api/main.py`/`state_service.py`/`capital_manager.py`/`app.js` sunt fix-uri de audit si feature-uri ne-conexe (alerts, pre-earnings, hotlist) |

---

## 6. RECOMANDARI (prioritate descrescatoare, toate P3)

1. **P3-4:** elimina lantul Decisions mort — fie reintroduci `#capital-decisions-body` in `index.html`, fie stergi `renderCapitalDecisions`/`renderCapitalDecisionSummary`/`renderCapitalStrategyIntelligence` + apelurile catre `GET /api/capital/decisions` (economie de request dupa fiecare comanda). Pastreaza doar `_syncCapitalExplainabilitySelection`.
2. **P3-5:** hidrateaza preturile in `/api/capital/equity-curve` (refoloseste lantul din `_build_capital_payload`) SAU sterge endpointul; alternativ, returneaza `current` doar cu datele existente in `equity_history` (fara snapshot sintetic "is_current" fals).
3. **P3-7:** in funnel, randeaza etapele `not_evaluated` ca "n/a" sau "—", nu ca 0, si/sau marcheaza explicit ca semnalele paper-only sari peste etapele de evidenta.
4. **P3-6:** uniformizeaza baza: afiseaza si in cockpit equity pe baza gross MTM (sau eticheteaza explicit "equity (net of est. exit fees)" in ambele locuri).
5. **P3-1/2/3:** sterge cele 3 seturi de linii moarte (`app.js:2602-2603`, `2663-2672`, `2712-2713`) sau reintrodu elementele in markup (cash-ul brut si % din Open PnL sunt utile traderului).
6. **P3-8:** cand cade fallback-ul de win rate, eticheteaza-l "win rate (ultimele N evenimente)".
7. Curatenie chei payload neconsumate (`accounting`, `capital_positions`, `research_positions`, `history` dublat de `ledger`) — opțional, doar dacă se dorește payload și mai mic.
8. Repara comentariul din `main.py:1857-1865` si docstringul din `capital_manager.py:991`.

---

## 7. CONCLUZIE

Tab-ul 13 Capital trece re-auditul V2 **fara niciun defect P1/P2**. Arhitectura contabila (double-entry ledger, marja rezervata/eliberata, comisioane entry/exit, MTM gross pentru equity si net pentru cardul de PnL) este coerenta si verificata la cent in productie. Fix-urile Round 1 (GZip, TTL) sunt active si masurabile. Restanta reala consta in **curatenie de UI** (4 randari moarte + lantul Decisions), **endpointul orfan equity-curve** (Round 1, nerezolvat), **semantica funnel-ului** pentru experimentele paper si **alinierea bazei net/gross** dintre cockpit si Capital.
