# Audit forensics v2 — TAB 7: PRE-EARNINGS (`#view-preearnings` + secțiunea „Pre-Earnings decision matrix" din Cockpit)

**Data audit:** 2026-09-05 (runda 2, post-commit `e9eda27`)
**Metodă:** trasare completă UI → payload → endpoint → producător backend → sursă finală; verificare live GET-only pe `http://127.0.0.1:8003` (4 cereri: `/api/cockpit`, `/api/pre-earnings`, `/api/pre-earnings/track-record`, `/api/pre-earnings/refresh-status`); recomputare independentă a scorurilor pe 9 rânduri live; `git show e9eda27`, `2a3ebce`, `c7edafc`, `git log -12`. Fără POST, fără browser, fără URL-uri externe. Fără modificări de cod.

---

## VERDICT GENERAL: **PASS CU OBSERVAȚII — 0×P1, 2×P2, 5×P3**

Tabul este, în ansamblu, **onest cantitativ și complet conectat**: fiecare câmp randat are un producător backend real și o sursă finală identificabilă; nu am găsit niciun câmp FAKE sau DEAD-RENDER pe traseul principal. Matematica blend-ului **Structured (65%) + LLM (35%)** a fost **recomputată independent pe date live și se potrivește exact** (9/9 rânduri). Noul component **c6 (eToro retail positioning) este real cablat în scor**, nu doar în prompt — 104/279 itemi au `components.retail_positioning` în payload-ul live ȘI în cache-ul disk `data/pre_earnings.json`, iar formula (skew × damping-rank) a fost verificată punctual (GRFS: rank 573 → support 1.0 → c6=100.0; AIV: rank 5832 → c6=95.42; CURR: rank 4569 → c6=98.5775 — toate conforme cu `agents/pre_earnings_agent.py:701-714`).

**Cele 2 probleme P2 sunt ambele pe traseul eToro:**
1. Cardul UI „eToro Retail Sentiment" **multiply-ează cu 100 un procent deja pe scară 0–100** → afișează „10000% Long / 0% Short" pe toate cele 104 nume (bug de scară, verificat live + contradicție internă cu bara din widget-ul de decizie care afișează corect).
2. **Calibrare c6**: valorile live `buy_pct` sunt saturate (toate în {0, 93..100}, mediană c6 = 99) → componenta „cel mai mic nudge prin design" acționează de fapt ca **o înclinare bullish sistemică**: +11.5 puncte medii pe scorul structured (max +34), LONG-ward pe 95/104 nume. Semantica câmpului `buyHoldingPct` de la eToro e discutabilă și trebuie verificată.

Restul tab-ului: gates, Top-10, pulse, track-record, awaiting-result, strip „Reported", filtre, freshness — toate OK sau cu observații minore P3.

---

## 1. Re-verificare raport runda 1 (`docs/audit_tabs/TAB_07_PRE_EARNINGS.md`)

| # | Constatare runda 1 | Stare în runda 2 | Dovezi |
|---|---|---|---|
| R1-1 | **eToro context colectat dar invizibil în UI** | **REZOLVAT (parțial prin 7f26fd2, complet prin e9eda27)**. Card „eToro Retail Sentiment" randat în panoul de detalii (`frontend/app.js:15338-15371`): holders, popularity rank, buy/sell, bid/ask, bară buy/sell, max 2 headline-uri comunitare, notă sursă. Bara factor „Retail positioning" în widget-ul „How the final signal was blended" (`frontend/app.js:15479-15490`, adăugat de e9eda27). **DAR**: cardul are bug-ul de scară P2-1 (×100). | `git log -S "eToro Retail Sentiment"` → 7f26fd2; `git show e9eda27` → factor bar |
| R1-2 | Implied move/IV — onest „N/A", fără feed opțiuni plătit | **NEMODIFICAT / OK**. `components.options` = None pe 279/279 itemi live; `iv_rv_ratio` = None pe 279/279 → Cockpit afișează „N/A · unavailable" cu tooltip onest (`frontend/cockpit.js:235`). Range proxy rămâne fallback-ul real. | payload live; `cockpit.js:233-235` |
| R1-3 | Range proxy în loc de IV — anti-halie, tooltip corect | **OK**. Tooltip-ul „not options-implied volatility" prezent (`frontend/app.js:14982`, `16061`). | cod |
| R1-4 | IPO-uri / lipsă istoric → INSUFFICIENT_DATA, coadă „NEEDS COVERAGE" | **OK, verificat live**: OPTH — `direction_label=INSUFFICIENT_DATA`, GapRisk N/A, secțiunea „NEEDS COVERAGE / DATE UNAVAILABLE" (`frontend/app.js:15852-15854, 16249`). DLNG: `news_only_call=true`, badge `NEWS_ONLY_CALL`, gap_risk=None (nu 0). | payload live |
| R1-5 | Date conflict → CALENDAR_MISMATCH, blocare execuție | **OK (cod)**. `flags.calendar_news_mismatch` penalizează trader score (`agents/pre_earnings_agent.py:6752-6753`); `hasVerifiedSchedule()` exclude badge-urile CALENDAR_MISMATCH din listele prioritare (`frontend/app.js:15837-15844`). | cod |
| R1-6 | **P3 runda 1: banner PAUSED — dead path** | **ÎNCĂ DESCHIS (P3-1)**. Condiția `st === 'PAUSED' && disabled` (`frontend/app.js:15770-15777`) nu poate deveni niciodată adevărată: niciun producător backend nu emite `status.state="PAUSED"` (grep `"PAUSED"` pe `agents/` + `api/` → 0 rezultate; singurele valori „state" sunt `STALE/FRESH` pentru `ranking`, la `agents/pre_earnings_agent.py:1056,7148`). Banner-ul moare în continuare. | grep live |
| R1-7 | Recomandare runda 1: „surface eToro in details panel" | **IMPLEMENTAT** (vezi R1-1). | cod + live |
| R1-8 | Top-10 „trader score · not probability" disclaimer | **OK**. Disclaimere prezente atât în Top-10 (`frontend/app.js:15578, 15599`), cât și în coloana „Final Blended Signal" (`frontend/app.js:16156`). | cod |

**Regresii vs runda 1:** nu am găsit niciuna. Fix-ul de overflow `2a3ebce` este prezent și corect (vezi §6).

---

## 2. Tabel câmp-cu-câmp — COCKPIT „Pre-Earnings decision matrix" (12 coloane, `renderEarnings`)

Endpoint: **GET `/api/cockpit`** → `sections.upcoming_earnings`. Producător: `api/cockpit_service.py` — `_pre_earnings_row()` (`:276-367`), selecție/sortare/cap 20 (`:1678-1713, 1863-1892`), `paper_auto_trace` (`:1755-1824`). Sursă finală: **`data/pre_earnings.json`** citit prin `_read_pre_earnings_cache()` (`:172-242`) → agent `PreEarningsAgent.get_state()` (`agents/pre_earnings_agent.py:2553-2672`). Proba live: state=SUCCESS, 279 candidați activi, 20 afișați, freshness FRESH (age 43s), 12 reported today.

| Câmp (coloană) | Frontend file:line | Payload key | Backend file:line | Sursă finală | Verdict |
|---|---|---|---|---|---|
| Ticker | `frontend/cockpit.js:228` | `ticker` | `cockpit_service.py:326` | `pre_earnings.json items[].symbol` | **OK** |
| Earnings date + schedule_status | `cockpit.js:229` | `earnings_date`, `schedule_status` | `:327, 1696-1705` (DATE_VERIFIED/ESTIMATED/UNAVAILABLE/PAST) | `items[].earnings_time_utc` (Yahoo/Finnhub/Nasdaq scraper, marcat verificat/estimat) | **OK** — live: 261 dated, 155 verified, 18 unavailable |
| BMO / AMC | `cockpit.js:230` | `session` | `:328` | `items[].session` (calendar) | **OK** |
| Score (+score_kind) | `cockpit.js:231` | `score`, `score_kind` | `:290-294, 334-335` (trader_score → pre_earnings_score fallback; ambele etichetate „NOT_PROBABILITY") | `_prediction_score_details` `agents/pre_earnings_agent.py:6598-6775` (ponderi 0.22/0.18/0.14/0.12/0.14/0.08/0.07/0.05 + penalități) | **OK** |
| Data confidence + coverage | `cockpit.js:232` | `data_confidence`, `coverage_score` | `:336-337` | `compute_data_confidence` / `coverage_contributors` (agent) | **OK** |
| Expected move | `cockpit.js:233` | `expected_move_pct` (fallback `range_proxy_pct`) | `:295-297, 338` | range proxy din High-Low 20 sesiuni (`StockDataClient`); onest None | **OK** |
| Historical move + n | `cockpit.js:234` | `historical_move_pct`, `historical_sample_size` | `:298-300, 340-341` | `historical_median_move_pct` din rapoarte istorice reale | **OK** — 202/279 itemi au valoare; None → „N/A" |
| IV / RV | `cockpit.js:235` | `iv_rv_ratio`, `iv_rv_state` | `:302-305, 342-343` (fallback `options_snapshot.iv_rv_ratio`) | **fără feed opțiuni** → None întotdeauna | **DEAD-BACKEND ONEST** (P3-3, cunoscut din runda 1) — UI afișează „N/A · unavailable" |
| Bias + bias_score | `cockpit.js:236` | `bias`, `bias_score` | `:287-289, 308, 344-345` (`final_direction_bias_score` → `direction_bias_score`) | blend 0.65 structured + 0.35 LLM (`agents/pre_earnings_agent.py:9411-9414`) | **OK** — verificat live pe 20/20 rânduri (bias ↔ bias_score ↔ blockers coerente) |
| Strategy | `cockpit.js:237` | `strategy` | `:309, 346` | derivat din `recommendation_eligible` + direcție | **OK** |
| AI decision (+conf, model, age) | `cockpit.js:238` | `ai_decision`, `ai_confidence`, `ai_model`, `ai_generated_at` | `:285, 347-350` | `ai_decision` din cache-ul LLM (Ollama/cloud) | **OK** — live: 171/279 ai_reviewed |
| Recommendation / Paper Auto + gates | `cockpit.js:239-253` | `recommendation_status`, `paper_auto_trace.*`, `autonomous_prefilter.*` | `:307-315, 316-323, 351-367` + trace `:1755-1824` | gates reale din `core/autonomous_candidates.py:260-470` (min_gap 65, min_bias 20, min_coverage 50, min_score 78, fereastră 2h, track-record direcțional) + mod/kill-switch/readiness/sesiune | **OK** — live: toate rândurile au `regular_session_required`+`outside_pre_earnings_window` (sâmbătă) — corect |
| Note (schedule_counts, refresh_policy, source_freshness) | `cockpit.js:255-258` | `schedule_counts`, `refresh_policy`, `source_freshness` | `cockpit_service.py:1869-1891, 97-112` | `pre_earnings.json status/ranking/calendar_universe` | **OK** — toate cheile consumate există live |

**Gates afișate în panoul „Verify gates":** `metrics`/`thresholds`/`policies`/`stages`/`blockers` — toate prezente și coerente cu payload-ul (`cockpit.js:241-253`). Observație P3-4: când `gap_risk` e None, `metrics.gap_risk` ajunge 0 (`core/autonomous_candidates.py:266,456`) → panoul arată „Gap-risk 0 / min 37" pentru DLNG, în loc de N/A.

**Atenție la gates-documentație:** gates-urile menționate în brief („conf>=0.6, gap 60-89 → size ×0.5, gap>=60 → block") **NU există în cod sub forma aceasta**. Gates-ule reale: `pre_earnings_min_gap_risk` (default 65), `pre_earnings_min_abs_bias` (20), `pre_earnings_min_coverage_score` (50), `pre_earnings_min_score` (78) + fereastra de intrare 2h + `recommendation_blockers` + `ENTRY_WINDOW_OPEN / WAITING_FOR_ENTRY_WINDOW / BLOCKED_OPPOSITE_OPEN_POSITION / RESEARCH_RANKING_FUTURE_EVENT` (`agents/pre_earnings_agent.py:2700-2779`). Documentație drift (P3-6, doar doc).

### 2b. Carduri înrudite din Cockpit alimentate de pre-earnings
- „Best current opportunities" include surse `PRE_EARNINGS` (`cockpit_service.py:466-494`) — **OK**.
- „Sentiment shifts" (`renderSentiment`, `cockpit.js:265-272` ← `_sentiment_section` `cockpit_service.py:569-587`, sursă `dashboard_market_narrative`): `prior_tone`/`shift` sunt mereu None iar `shift_state=UNAVAILABLE` — pill-ul afișează onest „UNAVAILABLE". **OK (onest)**.
- „Major news" (`renderNews`, `cockpit.js:274-281` ← `_major_news` `cockpit_service.py:546`, din dashboard news bounded) — **OK**.

---

## 3. Tabel câmp-cu-câmp — TAB 7 complet (`#view-preearnings`)

Endpoint principal: **GET `/api/pre-earnings`** (`api/main.py:7659-7702`, cache endpoint 60s → agent `get_state()`). Auxiliar: **GET `/api/pre-earnings/track-record`** (`api/main.py:8045`). Încărcare: `UI.loadPreEarningsData` (`frontend/app.js:13911-13950`, cache client 30s) + `UI.loadPreEarningsTrackRecord` (`:13957-13990`). Markup: `frontend/index.html:1343-1620` (stat cards `:1364-1385`, filtre `:1398+`), stylesheet `styles_preearnings.css?v=1.18` (`index.html:1342`), `app.js?v=38.26` (`index.html:2731` — versionat corect după e9eda27).

| Câmp | Frontend file:line | Payload key | Backend file:line | Sursă finală | Verdict |
|---|---|---|---|---|---|
| Stat „Analyzed Candidates" | `app.js:15897` | `items[]` filtrate ALREADY_REPORTED | `get_state:2604-2626` | `pre_earnings.json items[]` | **OK** (live 279) |
| Stat „High Gap Risk" (≥70) | `app.js:15898` | `gap_risk` | agent `:5882-5884` | istoric moves + contribuitori determiniști | **OK** |
| Stat „Bullish Bias" | `app.js:15899` | `direction_label` | blend `:9428-9440` | blend 65/35 | **OK** |
| Stat „Avg Confidence" | `app.js:15902-15911` | `confidence_score` | agent | medie doar peste itemi cu scor | **OK** |
| Stat „N/A Risk" | `app.js:15900` | `gap_risk_level/gap_risk_unavailable` | agent `:5883-5888` | istoric lipsă | **OK** |
| Reported strip | `app.js:15951-16003`; markup `index.html:1392-1395` | `reported_today` + badge `ALREADY_REPORTED` | `get_state:2588-2633`, `AlreadyReportedDetector` | news headlines | **OK** — live 12 (SSL, MEI, ALMU, CAL, CPRT…) |
| Filtre (Scope/Quality/View) + count | `app.js:14747-14833, 15845-15849`; `index.html:1417+` | client-side pe `items` | — | — | **OK** |
| Top-10 board (rank, side, trader score, bias, gap, news, event truth, execution badges, AI chip, rationale) | `app.js:15514-15600` | `top_predictions[].*` | `_build_top_predictions` `agents/pre_earnings_agent.py:6797-6887` (sortare `:6820-6835`, limit 10 `:6836`) | items + `recommendation_eligible` + `active_position_context` | **OK** — live: 10/10 rânduri complete; toate `WAITING_FOR_ENTRY_WINDOW` (weekend) — corect; badges „Entry window open / Waiting / conflict" mapate la `execution_state` (`app.js:15563-15568` ← agent `:2728-2737`) |
| Top-10 stale banner | `app.js:15519-15520, 15591-15592` | `ranking.state`, `flags.recommendations_stale` | agent `:1056` | snapshot age | **OK** (live: nu e stări) |
| Pulse (current long/short, sentiment, istoric, conficțe) | `app.js:15603-15746` | `pulse.*`, `current_top_long/short` | agent (pulse history) | LLM pulse separat | **OK** |
| Matrix: secțiuni 4 (HIGH GAP/LONG/SHORT/NEEDS COVERAGE) | `app.js:15852-15887, 16245-16250` | client-side | — | — | **OK**; cap 15 + „Show all" la coverage (`:16214-16219`) |
| Coloana Ticker (+sesiune, dată UTC, ~exp move) | `app.js:16144-16147, 16050-16062` | `symbol`, `session`, `earnings_time_utc`, `range_proxy_pct`/`components.range_proxy_move` | agent `:5869-5870, 5923-5924` | range proxy | **OK** (formatter UTC comun `:14708`) |
| Coloana GapRisk | `app.js:16148-16154` | `gap_risk` (+N/A honest) | agent `:5882-5891` | istoric real | **OK** |
| Coloana Final Blended Signal | `app.js:16155-16160` | `final_direction_bias_score` | `_synchronize_direction_decision` `:9342-9440` | **blend 0.65/0.35** | **OK** — recompus independent: 9/9 exact |
| Coloana Structured vs LLM (badge, reason, reactions, AI chip, peer chip) | `app.js:16161-16167, 16084-16130` | `direction_label`, `why`, `recent_reactions`, `ai_decision`, `llm_direction_score`, `peer_readthrough` | agent `:5919, 9436, 5940` | news + LLM + peer read-through | **OK** |
| Coloana EPS Beat Rate | `app.js:16019-16027` | `beat_rate_historical/beats/samples/source/unavailable_reason` | agent `:5913-5917` | Yahoo EPS pairs reale | **OK** (live 205/279 au rate; restul „no EPS pairs" onest) |
| Coloana Analyst | `app.js:16084-16091, 16169-16180` | `analyst_target_upside_pct/recommendation/count` | agent `:5908-5912` | yfinance targetMean/recMean | **OK** (live 155/96/233) |
| Coloana Data Coverage | `app.js:16181-16185` | `coverage_score`, `coverage_contributors` | agent `:5949-5951` | 7 contribuitori ponderați | **OK** |
| Detalii: header, Refresh Symbol/AI, Updated | `app.js:14943-15262` | `item.*`, `updated_utc` | agent `:5959+` (`updated_utc: as_of_utc`) | — | **OK** |
| Detalii: Opportunity Score | `app.js:15265-15269` | `opportunity_score` | agent `:5952` (`_pe_score_result`) | `core/pre_earnings/score.py:105-116` (0.65·gap + 0.35·|bias|)·coverage | **OK** |
| Detalii: „How the final signal was blended" + factori | `app.js:15128-15159` | `structured_direction_bias_score`, `ai_decision.weight`, `llm_direction_score`, `components.*` | agent `:725-728, 9411-9144` | — | **OK** (+ factor c6 nou `:15479-15490`) |
| Detalii: LLM View / Already reported | `app.js:15061-15126` | `ai_decision.used/verdict/confidence/sentiment/rationale/news_articles_read`, `flags.already_reported_evidence` | agent (LLM cache) | Ollama/cloud | **OK** |
| Detalii: Analysis KPIs (GapRisk/Bias/Coverage/Range/Hist/Beat) | `app.js:15275-15291` | ca mai sus | ca mai sus | — | **OK** |
| Detalii: Sentiment Breakdown (analyst + news bars) | `app.js:15293-15303` | `components.analyst_revision/news_ai` | agent `:744-748` | yfinance + news sentiment | **OK** |
| Detalii: Earnings History & Track Record | `app.js:15305-15333` | `recent_reactions`, `symbol_track_record` | agent `:5919-5922` | Yahoo + feedback settled | **OK** |
| Detalii: Peer read-through card | `app.js:15186-15240` | `peer_readthrough` | agent `:5934-5940` | peer-euri settle-uite | **OK** (live 154/279) |
| Detalii: **eToro Retail Sentiment card** | `app.js:15338-15371` | `etoro_context.holders/popularity_rank/buy_pct/sell_pct/bid/ask/social_headlines` | `_fetch_etoro_context` `agents/pre_earnings_agent.py:5128-5161` | `core/etoro_client.py` (search + rates + feed) | **P2-1 — scară ×100 greșită** (vezi §5) |
| Detalii: Rationale / What to watch / Top news | `app.js:15019-15041, 15374-15390` | `why`, `what_to_watch`, `top_news` | agent `:5958-5959` | news scanner | **OK** |
| Track record panel (overall/per verdict/AI bands/istoric zilnic) | `app.js:14046-14330` | `overall`, `per_direction/per_verdict`, `per_ai_verdict`, `per_ai_band`, `outcomes_recent`, `ai_summary` | endpoint `api/main.py:8045` | `data/pre_earnings_feedback.json` | **OK** (live: sample 3543, overall 54%, per_ai 1379) |
| **Top-10 settled table** (`.pe-tr-top10-scroll`) | `app.js:14263-14269` | `top_predictions_history[]` (day_rank, trader_score, realized_signed_move_pct, hit) | endpoint track-record | feedback JSON | **OK** mecanic; P3-2 pe valori extreme |
| **Awaiting-result block** | `app.js:14256-14261, 14270` | `top_predictions_pending[]`, `top_metrics.pending` | idem | idem | **OK** — live: 39 settled · 28 awaiting, conturi coerente |
| Ținere scroll la re-render | `app.js:16239-16255, 14457` | — | — | — | **OK** |
| PAUSED banner | `app.js:15770-15777` | `status.state` | **niciun producător** | — | **P3-1 DEAD-RENDER** (moștenit din runda 1) |

---

## 4. Verificarea matematicii scorului (recomputat pe payload live)

Formula structurată (`agents/pre_earnings_agent.py:716-728`): medie ponderată doar pe componentele disponibile, **ponderi relative** (împărțire la `available_weight`), apoi mapare `legacy = (x−50)×2`. Formula finală (`:9411-9414`): `final = (1−w)·structured + w·llm`, `w=ollama_decision_weight=0.35` (config `:1111`, clamp 0–0.6 `:9399`).

Rezultate recomputare (9 simboluri live, componente luate din `items[].components`, inclusiv c6):

| Simbol | est. structured | payload `structured_direction_bias_score` | final estimat | payload final | AI (verdict/conf/weight) |
|---|---|---|---|---|---|
| GRFS | +35 | +35 | +23 | +23 | NEUTRAL/0.30/0.35 |
| CURR | −4 | −4 | −11 | −11 | SHORT/—/0.35 |
| AIV | +20 | +20 | +13 | +13 | NEUTRAL/—/0.35 |
| DLNG | −30 | −30 | −29 | −29 | SHORT/—/0.35 |
| KEN | −16 | −16 | −22 | −22 | SHORT/—/0.35 |
| TTAN | +58 | +58 | +61 | +61 | LONG/0.55/0.35 |
| GME | +37 | +37 | (fără LLM) | +37 | ai_decision absent → fallback structured, corect |
| AVO | +46 | +46 | +40 | +40 | LONG/0.35/0.35 |
| DETX | 0 | 0 | 0 | 0 | NEUTRAL/—/0.35 |

**„Ponderile se renormalizează?"** Da, implicit și corect: ponderile (c1 0.25, c2 0.30, c3 0.25, c4 0.20, c5 0.12, **c6 0.10**) sunt relative — e9eda27 nu a modificat ponderile existente, doar a adăugat c6; cota efectivă a fiecărei componente = wᵢ/Σw disponibile (ex. GRFS: c1 25.8%, c2 30.9%, c4 20.6%, c5 12.4%, **c6 10.3%**). Comentariul din cod (`:722-724`) documentează explicit asta. **Blend-ul documentat Structured 65% + LLM 35% este neschimbat și corect.**

**c3 (options) este mort structural** — `calls_vs_puts_ratio` e mereu None (fără feed opțiuni), deci „25% options" nu participă niciodată (P3-3, onest în UI).

---

## 5. SECȚIUNE DEDICATĂ: componenta c6 „eToro retail positioning" (commit e9eda27) — audit end-to-end

### 5.1 Traseul complet (verificat)
1. **Sursă**: `core/etoro_client.py` — `search_instrument()` (`:270-336`): campuri `popularityUniques` → `popularity_rank`/`holders`, `buyHoldingPct` → `buy_pct`, `sellHoldingPct` → `sell_pct` (fallback `100−buy`), `peRatio-TTM`, `arabesqueESGTotal`, market cap, cache 30 zile disk.
2. **Orchestrare**: `PreEarningsAgent._fetch_etoro_context()` (`agents/pre_earnings_agent.py:5128-5161`) — meta + live bid/ask/spread + max 2 postări comunitare; **eșec/indisponibilitate → None** (`client.is_available()` verificat `:5135`), logging debug.
3. **Apel unic, înainte de scorare**: `_build_item_for_symbol` (`:5701-5714`) — `etoro_ctx` fetch o dată, refolosit la `:5981` pentru `item["etoro_context"]` (fără dublă apelare provider — fixul promis în commit este real).
4. **c6 în scor**: `compute_direction_bias_score(..., retail=etoro_ctx, retail_weight=cfg 0.10, retail_sign 1.0, rank_ceiling 20000)` (`:639-714`, apel `:5707-5714`); c6 intră în media ponderată la `:725`. **DA, c6 e în scor, nu doar în prompt.**
5. **Config/env**: `retail_positioning_weight/sign/support_rank_ceiling` (`:1152-1155`) + override-uri `PRE_EARNINGS_*` (`:1212-1214`).
6. **LLM prompt**: linia „Retail positioning (eToro crowd, CONTEXT ONLY — crowds are often contrarian into earnings): X% buy / Y% sell, popularity #N" construită `:10247-10257` și **inserată efectiv** în prompt la `:10368`. Verificat în diff e9eda27: bugetul de caractere pentru news nu e atins.
7. **UI**: factor „Retail positioning" în blend panel (`frontend/app.js:15479-15490`) + card „eToro Retail Sentiment" (`:15338-15371`, adăugat în 7f26fd2).
8. **Persistență**: `data/pre_earnings.json` (disk) conține aceleași 104 componente c6 ca API-ul live — sursă unică, fără divergență.

### 5.2 Verificare live (GET-only)
- **104/279** itemi au `etoro_context` complet (buy_pct prezent 104/104, bid/ask 104/104, social_headlines doar 21/104 — feed comunitar sărac, gestionat onest în UI).
- **104/104** au `components.retail_positioning` ≠ null → **c6 activ în scor, confirmat live**.
- **Formula, verificată pe 3 puncte**: GRFS buy=100/sell=0, rank=573 → support=min(1, 1.2−573/20000)=1.0 → c6=100.0 ✔; AIV rank=5832 → 95.42 ✔; CURR rank=4569 → 98.5775 ✔. Commit-ul a raportat SKIL c6=96.095 — aceeași formulă.
- **Blend**: recomputarea structurată cu c6 se potrivește cu payload-ul pe toate cele 104 rânduri eToro (campionat 9 detaliate mai sus).
- **Degrare grațioasă la absența eToro**: 175/279 itemi fără context → c6=None → exclus din medie (aceleași semantici None ca restul componentelor), fără card în detalii, fără factor în blend. KEN/FRGT/CHRN/VZLA verify. **OK.**

### 5.3 Defecte constatate pe traseul eToro

**P2-1 — Cardul UI afișează „10000% Long" (scară ×100 greșită).**
`frontend/app.js:15342-15343`: `buyPct = Math.round(Number(et.buy_pct) * 100)`. Backendul livrează buy_pct pe scară 0–100 (live: {0.0, 93, 94, 97, 98, 99, 100}; round-1 probe identic: buy 100.0/sell 0.0). Rezultat randat: **„Buy 10000%", „10000% Long / 0% Short", lățime bară 10000%** — pe toate cele 104 nume acoperite. Contradicție internă care probează bug-ul: **același payload** este afișat corect (fără ×100) în factorul din blend panel (`app.js:15484-15485`: `${Math.round(buy)}% buy / ${Math.round(sell)}% sell`). Fix: eliminați `*100` (și `*100` pentru sell) în card. Impact: pur UI, zero efect pe scor.

**P2-2 — Calibrare c6: saturare la „cumpărat" + semantica câmpului eToro neverificată.**
Live: **toate** valorile buy_pct sunt în {0.0, 93..100} — dispersie aproape zero; 10 nume au buy=0 **și** sell=0.0 (MIND, AREC, DXLG, PTN, VRA, ESP, SAR, NG, SXTC, IBIO → skew=0 → c6=50 exact). Distribuția c6 live: min 50, mediană **99**, p75 **100**. Efect măsurat pe scorul structured (recompus fără/ cu c6): **95/104 itemi împinși LONG-ward, medie +11.5 puncte, max +34.2 (SPCX −4.5 → +29.7; TORO/SNX/RBNE/CTRM/CALM 0 → +33.3)**. Pentru un component „cea mai mică nudge prin design", efectul real este o înclinare bullish sistemică aproape constantă pe numele acoperite. Două cauze posibile, ambele de verificat:
- (a) **Semantica greșită a câmpului**: `buyHoldingPct`/`sellHoldingPct` din răspunsul de search eToro (`core/etoro_client.py:301-302, 323-324`) nu pare a fi votul %-procente al deținătorilor (real retail sentiment variază tipic 40–80%; aici 93–100 sau 0/0). Posibil să fie altă metrică (de ex. ponderea poziției nete long). **Notă: nu am putut verifica răspunsul brut eToro (URL-uri externe interzise în acest audit)** — recomand verificare cu un dump raw.
- (b) Chiar dacă datele sunt corecte, `retail_positioning_sign=1.0` (momentum-crowd) amplifică un bias direcțional pe care documentatia proprie îl numește „crowds are often contrarian into earnings".
Dovezile de track-record propriu există deja (3543 sample-uri settle-uite, `data/pre_earnings_feedback.json`) — condiția promisă în commit pentru flip pe −1 este îndeplinită tehnic. Recomandare: măsurare A/B a hit-rate-ului pe componenta c6 înainte de a mări/învârți semnul; adăugarea unui test unitar care fixează scala buy_pct (0–100).

**Alte observații minore pe traseu (P3):**
- `holding_pct` („holdingPct") este colectat în client (`:322`) dar nu este pasat în `etoro_context` — neutilizat.
- `pe_ratio`/`esg_score` sunt colectate și livrate, dar nu sunt randate nicăieri în UI (informativ).
- `social_headlines` 21/104 — UI-ul degradează onest (secțiune omisă când lista e goală).
- Prompt-ul LLM primește linia retail doar la (re)analize post-deploy: verdicturile deja în cache (220 itemi, majoritatea generate înainte de e9eda27 09:49) nu conțineau linia — corect, nu e defect; se autorepară la următorul ciclu de re-analiză/news-change.

---

## 6. Verificarea fix-ului owner 2a3ebce (overflow Top-10 settled)

**PREZENT ȘI CORECT.**
- `frontend/styles_preearnings.css:557-570`: `.pe-tr-top10-scroll { max-height:430px; overflow-x:auto; overflow-y:auto; overscroll-behavior:contain; }` — declarațiile `overflow` lipsite înainte de fix sunt acum prezente, cu comentariu datat 2026-09-05 care explică exact cauzele din commit.
- `:571-580`: tabelul interior forțat `display:table; width:100%; max-height:none` + `white-space:nowrap` pe celule.
- Wrapper-ul interior al blocului „Awaiting result" (`<details>`) primește același tratament, max-height 380px (`:666-671`), iar `summary`-ul are `display:list-item` ca să nu fie clipit (`:647-656`).
- Markup-ul consumă corect wrapper-ul: settled la `frontend/app.js:14264`, pending la `:14270`.
- `index.html:1342` servește `styles_preearnings.css?v=1.18` (bump declarat în commit). Versionarea `app.js?v=38.26` (`:2731`) corespunde conținutului post-e9eda27 — fără risc de cache vechi.

---

## 7. Defecte (final)

| ID | Severitate | Descriere | Dovezi |
|---|---|---|---|
| **P2-1** | **P2** (UI, owner-vizibil) | Cardul „eToro Retail Sentiment" multiply-ează buy_pct/sell_pct (scară 0–100) cu 100 → „10000% Long / 0% Short" pe toate cele 104 nume acoperite; bară 10000%. Același payload e afișat corect în blend panel → inconsistență internă. | `frontend/app.js:15342-15343, 15361-15364` vs `:15484-15485`; live: buy_pct ∈ {0, 93..100} |
| **P2-2** | **P2** (MISCALIBRATED / semantica sursei) | c6 saturat (mediană 99, p75 100) → împinge sistemic LONG 95/104 nume, medie +11.5p, max +34p — contrazice intenția „smallest nudge"; semantica `buyHoldingPct` (93–100 / 0-0) suspectă, neverificabilă în acest audit; semnul contrarian (−1) așteaptă activare. | live payload 104 itemi; recompute; `core/etoro_client.py:301-324`; `agents/pre_earnings_agent.py:701-714` |
| **P3-1** | P3 (DEAD-RENDER, moștenit runda 1 — ÎNCĂ DESCHIS) | Banner „PAUSED" inaccesibil: nicio sursă backend nu emite `status.state='PAUSED'`. | `frontend/app.js:15770-15777`; grep `"PAUSED"` backend = 0 |
| **P3-2** | P3 (STALE/calitate date) | Mutări realizate extreme în track-record: `realized_signed_move_pct=+859.42%` (CLGN 2026-09-04, SHORT, hit=false) și „avg move 291.1%" pe SHORT — probabil prețuri neadjustate split/glitch; afișate brute. | `track_record.json` history[0]; `per_direction.SHORT.avg_realized_move=291.1` |
| **P3-3** | P3 (DEAD-BACKEND ONEST, cunoscut) | c3 „options 25%" nu participă niciodată (fără feed opțiuni); IV/RV mereu N/A. Documentat onest în UI. | `components.options=None` 279/279 live; `iv_rv_ratio=None` 279/279 |
| **P3-4** | P3 (onestitate minoră) | Panoul de gates din Cockpit arată „Gap-risk 0 / min 37" când gap_risk e None (ar trebui N/A). | `core/autonomous_candidates.py:266,456`; `frontend/cockpit.js:245`; live DLNG |
| **P3-5** | P3 (narrativ ușor învechit) | `why` (ex. GRFS „DirectionBias=34.74 from cached news sentiment and catalyst flags") nu reflectă componența actuală a scorului (acum include c1/c2/c5/c6 eToro); `bias_reasons`-urile trailing („Analyst revision proxy: 0.0") sunt moștenite. | live GRFS `why`/`bias_reasons` |
| **P3-6** | P3 (doc-only) | Gates-urile citate în documentația de referință („conf>=0.6, gap 60-89 → ×0.5, gap>=60 → block") nu există în cod; gates reale: min_gap 65 / min_bias 20 / min_coverage 50 / min_score 78 + fereastră 2h + blockers. | `core/autonomous_candidates.py:270-273, 407-439`; `agents/pre_earnings_agent.py:2700-2779` |

**Total: 0×P1, 2×P2, 6×P3** (din care 1 doar documentație).

---

## 8. Recomandări (prioritate)

1. **P2-1 fix (2 minute)**: în `frontend/app.js:15342-15343` eliminați `* 100` pentru `buyPct`/`sellPct` (scară deja 0–100); bump `styles`/`app.js` version.
2. **P2-2**: (a) dump raw `buyHoldingPct`/`sellHoldingPct` pentru 3-5 instrumente și confirmați semantica; (b) if valid, activați `PRE_EARNINGS_RETAIL_POSITIONING_SIGN=-1` doar după măsurarea hit-rate-ului c6 pe sample-uri settle-uite; (c) adăugați test unitar care pinscă scala 0–100 și clampa `support` pentru rank uri > ceiling.
3. **P3-1**: ștergeți condiția `'PAUSED'` din `app.js:15771` sau emiteți efectiv `status.state` din backend (ex. `PAUSED` când `disabled=true`) — acum banner-ul de agent disabled este afișat doar de header-ul de agent status, nu și de alerta dedicată.
4. **P3-2**: clamp/flag pe `realized_signed_move_pct` > prag (ex. ±50%) → recalc sau „split-adjusted?" în track record, ca HIT/MISS-ul și „avg move" să nu fie dominate de artefacte de preț.
5. **P3-4**: în `assess_pre_earnings_candidate`, când `gap_risk` lipsește, emiteți `metrics.gap_risk=null` și randati „N/A / min 37" în panoul de gates.
6. **P3-5**: regenerați `why`-ul după blend sau prefixați-l cu componența reală (include retail positioning).
7. Opțional: passați `holding_pct` și randati `pe_ratio` în cardul eToro (deja colectate, zero cost).

---

## 9. Dovezi live (rezumat)

- `GET /api/cockpit` → `sections.upcoming_earnings`: SUCCESS, count 279, 20 afișate; toate cheile consumate de `renderEarnings` prezente pe toate rândurile; freshness FRESH (age 43s); `regular_session_required` + `outside_pre_earnings_window` pe toate rândurile (sâmbătă 2026-09-05 16:10 EDT) — gates corecte; 1 rând ELIGIBLE (AVO) cu doar blocker-e de window/sesiune — coerent.
- `GET /api/pre-earnings` → 279 itemi, 12 reported_today, ranking two_stage_evidence_weighted_v2 (covered 260/260, deep 248, ai_reviewed 171); 104 itemi eToro cu c6; recompute blend 9/9 exact; news_only_call (DLNG) cu clamp și blockers coerente; OPTH → INSUFFICIENT_DATA.
- `GET /api/pre-earnings/track-record` → sample 3543, overall 54.0%, top_predictions settled 39 / pending 28 / hit_rate 51.3; toate cheile consumate de `renderPreEarningsTrackRecord` prezente.
- `GET /api/pre-earnings/refresh-status` → state_revision 1788639622358, last_success 20:20:22Z, next_run_in 600 — corespunde cu freshness-ul din cockpit.
- Disk `data/pre_earnings.json` (4.67 MB, 23:21) conține aceleași 104 componente c6 — cache coerent cu API-ul.
