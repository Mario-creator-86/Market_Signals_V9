# Implementarea îmbunătățirilor Pre-Earnings

Scop: listă strictă, analiză verificabilă și evaluare corectă LONG/SHORT. Online rămâne pe pauză. Nu eliminăm funcții și nu promitem o precizie nevalidată.

| Pas | Stare | Criteriu |
|---|---|---|
| I01 Plan și inventar | DONE | Citite calculul scorului, selecția și registrul predicțiilor. |
| I02 Prag explicit al scorului | DONE | Configurabil; aplicat eligibilității și selecției. Implicit 60, limita existentă a benzii C, ca politică de selecție, nu prag statistic validat. Lista poate fi goală. |
| I03 Dovezi imuabile | DONE | Prima evaluare și prima intrare în shortlist păstrează componente, AI, surse, timestamp și praguri; propagare la outcome fără rescriere retrospectivă. |
| I04 Dosar și UI | DONE | Pragul și explicația selecției afișate. Dosar de verificare pe rând: porți, dovezi înghețate, argumente opuse, contradicții, lipsuri. Toate secțiunile existente păstrate. |
| I05 Validare istoric și Capital | IN PROGRESS | 39 apeluri Top reverificate pe bare daily; 23 trec pragul, 22 fără split, 12 direcții corecte. Raport și CSV în history_verification_2026-09-07. Rămân validarea tuturor evenimentelor, migrarea istoricului și reconcilierea P/L. |
| I06 Analiză și model candidat | TODO | Compară componentele pe aceleași evenimente, LONG/SHORT separat; variante în paralel fără ordine suplimentare. |
| I07 Validare prospectivă | TODO | Walk-forward și holdout, benchmark, acoperire, costuri; promovare numai pe dovezi. |

Fiecare etapă se închide cu teste și lista fișierelor modificate. Modificările de cod testate pe disc nu înseamnă că procesul local existent a fost repornit.

## Raport de implementare — prima etapă

- `agents/pre_earnings_agent.py`: `top_prediction_min_trader_score` implicit 60, configurabil și prin `PRE_EARNINGS_TOP_PREDICTION_MIN_TRADER_SCORE`, normalizat 0–100; motiv explicit `trader_score_below_threshold`. Prag aplicat și defensiv selecției; metadate de politică în ranking.
- `core/pre_earnings_evidence.py`: snapshots separate `first_direction_evidence` și `top_direction_evidence`; copii independente, valori nefinite convertite în null, știri limitate la 12 dovezi. Rândurile vechi primesc eticheta `legacy_first_observed`, fără a inventa o captură istorică. Înregistrările settled vechi nu au fost rescrise.
- `frontend/app.js`: pragul backendului, explicația că toate porțile sunt obligatorii și lista poate avea zero candidați; încrederea AI este explicit necalibrată.
- `tests/unit/test_pre_earnings_selection_evidence.py`: prag inclusiv, respingere fără completarea artificială a listei, păstrarea porților, înghețarea separată a evaluării/shortlistului, proveniență legacy și propagare până la outcome.
- Verificare: **506 passed**, 2902 deselected, un avertisment de depreciere, prin `pytest tests -k pre_earnings -q`; log în `implementation_pre_earnings_tests.log`. `node --check frontend/app.js`: cod 0. Nu este o rerulare a întregii suite și nu validează creșterea preciziei de trading.
- Backendul existent nu a fost repornit. Pentru verificarea în procesul activ este necesară reîncărcarea controlată a backendului și generarea unui snapshot nou; datele istorice existente nu sunt filtrate/rescrise arbitrar.

Următorul pas: I04, dosarul verificabil care citește componentele și sursele existente, apoi I05 înainte de antrenarea sau promovarea unor ponderi direcționale noi.

## Raport de implementare — etapa I04 (dosar de verificare)

Scop etapei: pentru fiecare rând Pre-Earnings, traderul vede ce susține decizia, ce o contrazice, ce date lipsesc și ce dovezi au fost înghețate la momentul deciziei — fără a elimina nicio secțiune existentă din UI.

- `core/pre_earnings_evidence.py`: funcție pură `build_verification_dossier(item, config)` care observă rândul așa cum este servit și produce: `gates` (eligibilitate, Trader Score vs. minim, benzi, blockers cu text uman din `BLOCKER_TEXT`), `opposing` (ținte de analiști și consens împotriva apelului, sentiment eToro contra, verdict peer read-through opus, reacții istorice recente de semn opus, istoricul apelurilor noastre pe ticker sub 50%), `contradictions` (AI vs. structură, AI vs. final, date de eveniment conflictuale la surse, știri vs. calendar, poziție deschisă de sens opus, direcție purtată doar de LLM), `gaps` (gap risk nescoreat, sesiune necunoscută, lipsă istoric EPS, lipsă analiști, acoperire sub prag, știri sub praguri, decizie doar din știri, verdict AI învechit față de știri, eveniment deja raportat, avertismente de calitate știri). `summarize_evidence()` compactează un snapshot înghețat (calitatea capturii, momentul, direcția, scorul + breakdown, pragul, AI, numărul de știri). Nimic nu este inventat: fiecare intrare citează un câmp prezent sau absențele lui; `probability_calibrated` rămâne false.
- `agents/pre_earnings_agent.py`: `get_verification_dossier(symbol)` — cale de servire read-only: copie profundă a itemului din state (state-ul nu este mutat niciodată de citire), resincronizare idempotentă pe copie cu aceeași logică ca tabela servită, apoi dosarul. `_lookup_frozen_evidence(symbol, event)` citește din registrul `open_predictions` cheia exactă `symbol|event`, cu fallback la cel mai apropiat eveniment al aceluiași simbol din fereastra de 14 zile folosită și de registrul AI; intrările sunt copiate sub `_feedback_guard()` și nu sunt rescrise.
- `api/main.py`: `GET /api/pre-earnings/dossier?symbol=...` — endpoint la cerere pentru rândul selectat; nu inițializează agentul (`get_pre_earnings_agent_if_ready`), 404 pentru simbol absent, 503 cât timp agentul nu e gata, `Cache-Control: no-store`. Ales în locul atașării în `get_state` pentru a păstra contractul de latență al poll-ului principal (dosarul se calculează numai pentru un simbol, la selecție).
- `frontend/app.js`: `API.getPreEarningsDossier` (cache TTL 30s per simbol), secțiunea **Verification Dossier** adăugată în `renderPreEarningsDetails` după „Top Relevant News": starea porților, snapshoturile „First evaluation (frozen)" și „Shortlist entry (frozen)" cu eticheta `frozen at decision` / `legacy snapshot`, listele „Opposing evidence", „Contradictions", „Missing / thin data", și nota de imuabilitate + încredere AI necalibrată. Încărcare asincronă cu gardă `data-dossier-symbol` (un răspuns întârziat pentru un rând anterior nu poate suprascrie dosarul curent). Toate secțiunile existente rămân; nimic nu a fost eliminat. `index.html`: bump `app.js?v=38.29`.
- Teste: `tests/unit/test_pre_earnings_verification_dossier.py` (12 teste: argumente opuse LONG/SHORT, contradicții, lipsuri, rând curat fără intrări, maparea blockers, serializare JSON finită, sumarul evidenței legacy, calea de servire cu dovezi înghețate + non-mutație, simbol necunoscut) și `tests/test_pre_earnings_premium_frontend.py::test_verification_dossier_is_wired_end_to_end_and_additive` (endpoint, apel frontend, gardă anti-stale, ordinea secțiunilor, păstrarea tuturor secțiunilor existente).
- Verificare: **519 passed**, 2902 deselected, un avertisment de depreciere, prin `pytest tests -k pre_earnings -q`; `node --check frontend/app.js`: cod 0. Nu este o rerulare a întregii suite și nu validează o creștere a preciziei de trading.
- Procesul local de pe portul 8003 rulează în continuare codul vechi (endpointul nou răspunde 404): verificarea în procesul activ necesită aceeași reîncărcare controlată ca la etapa I02–I03. Snapshoturile `legacy_first_observed` existente nu au fost rescrise.

Următorul pas: I05 — validarea eveniment → preț de referință → split/ajustare → rezultat și reconcilierea cu Capital, înainte de orice antrenare sau promovare de ponderi direcționale noi.

## Reaudit și corecții — 7 septembrie 2026

- Corectate timeout-ul lockului, asocierea greșită a dovezilor între evenimente, lipsa accesului la dovezile settled și eligibilitatea păstrată după eșec de recalculare.
- Dosarul/API/cache-ul sunt acum legate de simbol + eveniment. Test Node pentru răspunsuri întârziate și răspuns cu eveniment diferit. Versiune frontend 38.30.
- Evaluarea viitoare folosește bare daily; refuză sesiunea necunoscută, ziua evenimentului absentă și referințe separate de peste patru zile. Formula de producție reproduce independent toate cele 39 de seturi de prețuri descărcate.
- [Raport complet și fiecare candidat](history_verification_2026-09-07/REAUDIT_REPORT.md); [CSV cu toate cele 39 de apeluri](history_verification_2026-09-07/historical_calls.csv). Snapshoturi/hashuri și config de selecție arhivate; fără rescrierea feedbackului live.
- Recalculare curentă: 14 eligibili mecanic, Top 10 afișat. CLGN necesită confirmarea evenimentului, deci numărul nu reprezintă 14 earnings validate independent.
- Nu există replay integral al politicii noi: lipsesc intrările istorice complete. 12/22 = 54,5%, fără dovadă statistică de avantaj. Nu schimbați ponderile pe baza acestui eșantion.
- Verificare runtime: endpointul existent răspunde 200, dar ignoră parametrul evenimentului și întoarce dosarul GME din septembrie inclusiv pentru o dată solicitată în ianuarie 2027. Rulează o versiune anterioară corecțiilor curente; vezi running_backend_check.json. Nu a fost repornit de acest audit.
- Verificare după repornire (aceeași zi, sesiune ulterioară): stack repornit controlat pe codul reauditat (595 passed pe `pre_earnings/replay/reaudit` înainte de repornire). GME fără dată → 200 legat de evenimentul din septembrie; GME cu `event_utc=2027-01-20` → 404 (nu se mai returnează dosarul din septembrie). mlflow relansat de autostart, `GET /health` → 200.

## Instrument de replay point-in-time (2026-09-07)

La cererea proprietarului: un test care trece evenimentele din săptămâna trecută prin proces, dă acces doar la știrile disponibile până la publicare, apoi compară cu rezultatul real. Instrument: `scripts/replay_pre_earnings_week.py`, read-only (nu trimite ordine, nu rescrie feedback, nu pornește engine-ul). Două niveluri pe fiecare eveniment:

- **stored** — ce a spus sistemul ÎNAINTE de publicare, din registrul de feedback (bias structurat + verdict AI stocate la înregistrare). Fără look-ahead prin construcție; acoperă toate cele 155 evenimente.
- **replay** — modelul CURENT rulat pe știri tăiate la momentul evenimentului: `published_at ≤ event`, iar rândurile din ledger cer suplimentar `observed_at ≤ event` (când a văzut pipeline-ul articolul — un backfill târziu nu poate trece drept dovadă pre-eveniment). Consens de analiști, istoric EPS, eToro și gap risk NU sunt reconstituibile point-in-time și rămân absenți (N/A în promptul de producție). Promovarea prin `PreEarningsAgent._ollama_enrich_item` — același prompt și parser ca în producție.

Avertizări raportate, nu ascunse: modelul poate purta cunoștințe post-eveniment din antrenare pentru tickere cunoscute; 13/59 verdicturi replay s-au auto-declarat „already reported" (contaminare post-eveniment în știri sau din memorie) și au fost forțate NEUTRAL — ies din direcțional, nu se numără drept abstinență informată.

### Rezultate săptămâna 2026-08-31 → 2026-09-06 (155 evenimente, toate cu mișcare reală)

| Nivel | Apeluri direcționale | Corecte | Precizie | Wilson 95% (descriptiv) | Reper mereu-LONG |
|---|---:|---:|---:|---|---:|
| Bias structurat stocat (pre-eveniment) | 92 | 47 | 51,09% | 41,0–61,1% | 48,91% |
| Verdict AI stocat (pre-eveniment) | 120 | 58 | 48,33% | 39,6–57,2% | 50,83% |
| Replay model curent, știri tăiate la event | 33 | 17 | 51,52% | 35,2–67,5% | 54,55% |

Acoperire știri pre-eveniment: 96/155 evenimente fără niciun articol rămas în magazii (rotație), 24 cu 1–2, 35 cu ≥3. Acordul dintre verdictul AI stocat și replay-ul modelului curent: 57,58% (n=33). Erori apel AI: 0. Raport complet per eveniment în `replay_week_2026-08-31_2026-09-07.json`.

Citire onestă: o singură săptămână, eșantioane mici, intervale uriașe — nimic nu e statistic concluzient. Ce spune totuși: în săptămâna analizată nici sistemul stocat, nici replay-ul nu au bătut reperul mereu-LONG; verdictul AI afișat atunci a fost sub reper; iar modelul curent e în dezacord cu ce s-a afișat atunci în ~42% din cazurile comparabile. Exact de asta pasul următor rămâne I05 (validarea prețurilor/evenimentelor) și acumularea de săptămâni, nu tuning de praguri.

### Eșantion mare: 6 luni (07.03 → 07.09.2026, 3.194 evenimente)

La cererea proprietarului s-a extins replay-ul la 6 luni. Două rulări:

1. **Replay 6 luni, nivelurile stored** (`replay_6months_2026-03-07_2026-09-07.json`, fără apel AI). Populația e inegală pe luni: mai–iunie practic goale (3+3 evenimente — bucla de outcome a fost moartă până la fix-ul din 02.09), iulie 974, august 1.562, septembrie 143. Verdicturile AI stocate există efectiv doar aug–sep (1.364 rânduri).

| Nivel | Apeluri direcționale | Corecte | Precizie | Wilson 95% | Reper mereu-LONG |
|---|---:|---:|---:|---|---:|
| Bias structurat stocat (6 luni) | 2.087 | 1.130 | 54,14% | 52,0–56,3% | 55,82% |
| Verdict AI stocat (aug–sep) | 854 | 459 | 53,75% | 50,4–57,1% | 55,74% |
| Top-10 (freeze la intrare) | 39 | 20 | 51,28% | 36,2–66,1% | 48,72% |

Stabilitate lunară a bias-ului stocat vs. reper: aprilie 52,1% vs 65,0% (sub reper); iulie 54,8% vs 54,7%; august 54,3% vs 55,9%; septembrie 50,6% vs 49,4%. Nicio lună nu demonstrează avantaj față de mereu-LONG; limita superioară Wilson (56,3%) abia atinge reperul. Anomalie cunoscută prezentă, nu ștearsă: CLGN cu mișcare „+859%" — cazul deja semnalat de audit pentru validarea de prețuri (I05).

2. **Replay cu AI pe era cu știri** (11.08 → 07.09, `replay_ledger_era_2026-08-11_2026-09-07.json`): 344 evenimente, 81 apel AI, 0 erori. Chiar în era ledger-ului, 263/344 (76%) evenimente nu au niciun articol pre-eveniment în magazii.

| Nivel | Apeluri direcționale | Corecte | Precizie | Wilson 95% | Reper mereu-LONG |
|---|---:|---:|---:|---|---:|
| Bias structurat stocat | 209 | 113 | 54,07% | 47,3–60,7% | 53,59% |
| Verdict AI stocat | 236 | 122 | 51,69% | 45,3–58,0% | 50,85% |
| Replay model curent (știri tăiate) | 49 | 25 | 51,02% | 37,5–64,4% | 51,02% |

Acord stoc vs. replay: 55,56% (n=45).

**Concluzie la eșantion mare:** pe ~2.000 de apeluri direcționale pe 6 luni, scorul structurat stocat și verdictul AI stocat NU au demonstrat avantaj față de reperul mereu-LONG (ambele ușor sub), iar replay-ul modelului curent pe știri tăiate e identic cu reperul (51,02% = 51,02%). Top-10 e peste reper (51,28% vs 48,72%) dar pe 39 de cazuri — nesemnificativ. Nu se promovează și nu se tunează nimic pe aceste numere; blocajul real e acoperirea de știri pre-eveniment (76–83% din evenimente fără nicio știre), deci acumularea de date validate (I05) și fixarea prețurilor rămân înaintea oricărui tuning.

### Rerulare

```text
.venv\Scripts\python.exe -X utf8 scripts/replay_pre_earnings_week.py --week-start 2026-08-31 --week-end 2026-09-07 --min-news 1 --resume
.venv\Scripts\python.exe -X utf8 scripts/replay_pre_earnings_week.py --week-start 2026-03-07 --week-end 2026-09-07 --no-ai
.venv\Scripts\python.exe -X utf8 scripts/replay_pre_earnings_week.py --week-start 2026-08-11 --week-end 2026-09-07 --min-news 1 --resume
```

Rapoarte: `replay_week_2026-08-31_2026-09-07.json`, `replay_6months_2026-03-07_2026-09-07.json`, `replay_ledger_era_2026-08-11_2026-09-07.json` (+ logurile `replay_*_run.log`) — toate în `docs/product_audit_2026-09-06/`. `--resume` reia o rulare întreruptă (checkpoint la fiecare 5 evenimente); `--no-ai` rulează doar nivelurile stocate, fără apeluri Ollama. Teste: `tests/unit/test_replay_pre_earnings_week.py` (10 teste hermetice, fără Ollama). Instrumentul nu schimbă nicio politică; nu se tunează praguri pe aceste eșantioane.
