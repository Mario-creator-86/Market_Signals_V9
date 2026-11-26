# Audit de produs și optimizări — 6–7 septembrie 2026

**Prioritate actualizată:** online pus pe pauză la cererea proprietarului; ne concentrăm pe funcționalitatea locală și [precizia LONG/SHORT Pre-Earnings](PRE_EARNINGS_DIRECTION.md). Diagnostic PE01 executat; îmbunătățirea statistică a modelului nu este încă demonstrată.

**Stare: ÎN CURS.** Acest registru nu certifică fiecare câmp, endpoint sau flux. Inspecția unui tab nu înseamnă audit exhaustiv. Rapoartele precedente sunt puncte de pornire; constatările de mai jos au dovezi proprii. Toate cele 13 taburi și funcțiile existente sunt păstrate.

## Cerințe păstrate

- Pre-Earnings rămâne un spațiu separat, cu calendar, Top 10, Pulse, dosare și rezultate istorice.
- Agenții AI, controalele de risc, automatizarea paper, Capital, Decision Ledger și Research Factory rămân în produs.
- Planul online are un singur abonament de **10 USD/lună**, cu toate funcțiile pentru client. Limitele de consum necesită măsurători; nu sunt promisiuni de utilizare nelimitată.
- Arhitectura SaaS este planificată, nu implementată. S00–S49 sunt TODO; vezi [planul](../online_migration/README.md) și [regula de conservare](../online_migration/PRODUCT_PRESERVATION.md).

## Reparații demonstrate

| ID | Problemă și efect | Corecție | Dovadă |
|---|---|---|---|
| A01 | Aliasul `interval` putea fi ignorat de valoarea implicită `timeframe`; cererea de 30 minute folosea 15 minute; 4h returna bare orare. Indicatorii puteau descrie alt orizont. | `core/stock_data_client.py`: precedență explicită, interval 30m corect, agregare OHLCV 4h pe sesiune locală. | Teste pentru alias, 30m și agregare 4h în `test_product_audit_regressions.py`. |
| A02 | Prețuri zero/negative puteau produce rezultate istorice de gap imposibile. | `agents/pre_earnings_agent.py`: respinge ambele prețuri nepozitive înainte de calcul. | Patru cazuri de regresie. Nu reprezintă validarea anomaliei CLGN. |
| A03 | Cache-ul de backtest nu detecta corectarea unei lumânări anterioare dacă ultima închidere rămânea identică. | `core/quick_backtester.py`: amprentă pentru conținutul și schema întregului DataFrame. | Corectarea unui high anterior schimbă cheia; copia identică o păstrează. |
| A04 | Comparația celor 12 combinații descărca repetat istoricul. | `api/actions_service.py`: un singur set de date comun, transmis celor 12 simulări. | Test: 1 fetch, 12 rulări cu același snapshot. |
| A05 | Modurile de risc/comerț schimbau procentele TP/SL, dar acestea nu erau folosite în modul ATR. | Multiplicatorii existenți se aplică și distanțelor ATR. Nu au fost inventate strategii noi de intrare. | Tranzacții simulate: STRONG modifică distanța țintei cu 1,5× și a stopului cu 1,25×. |
| A06 | Argumentul explicit `exit_mode` era ignorat. | Modul ATR/FIXED_PCT este transmis motorului și inclus în identitatea cache-ului. | Aceleași bare produc ținta procentuală cerută în FIXED_PCT și ținta ATR în ATR. |
| A07 | Modificarea configurației RSI putea reutiliza tranzacțiile vechi din cache. | Amprentă pentru configurația efectivă de semnal și variante; snapshot separat pentru variante. | După schimbarea pragului RSI, aceleași bare nu mai reutilizează intrările anterioare. |
| A08 | Cockpit eticheta un interval de preț observat drept „Expected move”. | `api/cockpit_service.py`, `frontend/cockpit.js`: câmp și etichetă `Range proxy`, explicație explicită; aliasul API vechi rămâne compatibil. | Test de proveniență și zero valid; contractul frontend actualizat. |
| A09 | Hot List transforma valori lipsă în 0,00%; coloana comparativă afișa edge în locul mediei istorice; un istoric incomplet putea ascunde cardul. | `frontend/app.js`: lipsă = N/A, media istorică în coloana ei, istoric incomplet tolerat. | Test Node pe funcția reală de randare: null, zero real, medie diferită de edge, sumar absent, gap absent. |
| A10 | Trade Radar rotunjea nivelurile la 4 zecimale înainte de publicarea rezultatului. Pentru active ieftine, ținta/stopul se puteau confunda cu intrarea sau cu zero. | `agents/trade_radar_agent.py`: păstrează precizia numerică în payload. | Îmbogățirea reală a candidatului testată la 0,00000073; 0,00073; 0,0021; 100. Nivelurile păstrează raportul calculat. |

Fișiere de teste: [regresii Python](../../tests/unit/test_product_audit_regressions.py), [regresii Hot List](../../tests/frontend/test_hotlist_missing_data.cjs), [contract Cockpit](../../tests/test_cockpit_frontend_contract.py).

## Ce funcționează bine și trebuie păstrat

- Separarea recomandării de permisiunea efectivă de deschidere: Cockpit explică eligibilitatea și porțile de execuție; Trade Radar afișează blocarea când lipsesc indicatorii.
- Registrul de decizii și evidența pozițiilor paper oferă deja baza unui produs explicabil. Trebuie extinse și reconciliate, nu recreate în paralel.
- Există controale distincte pentru risc și administrarea ieșirilor. Planul online păstrează capacitate pentru protejarea pozițiilor chiar la atingerea cotelor de analiză.
- Pre-Earnings are profunzime reală: selecție, surse, AI, context de eveniment, istoric. Prima pagină necesită o ierarhie mai clară, fără eliminarea filtrelor sau dosarelor.
- Suita existentă acoperă numeroase regresii. Totuși, testele generale nu au detectat inițial modurile ATR inerte și valorile null convertite în zero: sunt necesare teste pe rezultatele efective, nu numai pe etichete.

## Acoperire UI și investigații rămase

| Tab | Verificat până acum | Următoarea verificare necesară |
|---|---|---|
| AI Cockpit | Overview și workspaces inspectate; traseul range proxy corectat; risc analizat în sursă. | Fiecare KPI până la registrul său; reconcilierea sumelor și a snapshoturilor; risk/confidence. |
| Watchlist | Inspecție inițială; traseul intervalelor istorice reparat. | Matrice per coloană, dosar și preț, cu sursă și timestamp. |
| Scanner | Inspecție inițială a fluxului. | Grupuri, identitatea simbolurilor, preț/sesiune și clasificarea movers. |
| News | Articole și categorii inspectate; riscuri observate în știri sociale. | Verificarea titlu + corp + sursă înainte de validarea fiecărui tag/catalizator. |
| Trade Radar | Date încărcate, 34 candidați într-un snapshot, blocări și vechime vizibile; precizie reparată. | Toate detaliile, sursele prețurilor, formatarea activelor foarte ieftine, reîmbogățirea datelor vechi. |
| Premarket | 8 candidați într-un snapshot, WATCH, Pulse, prețuri, scoruri și staleness inspectate. | Reconcilierea Pulse TOP LONG/SHORT cu eligibilitatea WATCH și sesiunile; verificarea catalizatorilor. |
| Pre-Earnings | Inspecție aprofundată UI/sursă; gap și intervale reparate. | Anomalia CLGN, ajustări corporate actions, proveniența fiecărui outcome și toate filtrele. |
| Hot List | Carduri și Market Heat inspectate; lipsa datelor, istoricul și comparația reparate. | Discrepanțe între categoria movers și semnul variației; sursa expected move; toate acțiunile dosarului. |
| Backtest | Parametri, variante și istoric UI inspectate; motorul quick testat și optimizat. | Paritate cu strategiile de producție, STRICT/CUSTOM, interval UI, erori/insuficiență date, FAITHFUL și walk-forward. |
| Calendar | Grid septembrie–noiembrie inspectat. | Confirmat vs estimat, sursă per eveniment, timezone, actual/forecast/previous și detalii pe zi. |
| Agents | Inventar și controale inspectate; pipeline și porți analizate. | Fiecare agent, frecvențe, timeout, failure mode, apeluri AI reale și parcursul complet al unei decizii. |
| Alerts | Stare goală, formular și rutare Telegram inspectate; reparațiile anterioare sunt în rapoartele vechi. | CRUD izolat, trigger, deduplicare și retry. Niciun mesaj extern trimis în acest audit. |
| Capital | Poziții, expunere și ledger inspectate; legături de execuție analizate. | Reconciliere per poziție/închidere, costuri, funding, preț curent și grafice. |

Numerele UI sunt observații la un moment dat, nu rezultate garantate și nu probe de rentabilitate.

## Probleme deschise — nu sunt declarate rezolvate

1. **P1 — Identitatea strategiilor quick.** Intrările rămân RSI/MACD; PULLBACK/BREAKOUT/CONTRA modifică profilul ieșirilor. `variant_mode` este încă returnat ca metadată fără aplicare distinctă în acest motor. Câmpul nou `simulation_scope` explică limitele la nivel de rezultat; trebuie propagat în UI și separat de replay-ul strategiei live. Urmărește `QuickBacktester.quick_backtest`, `Backtester.run_backtest`, `_get_strategy_comparison` și walk-forward.
2. **P1 — CLGN +859,4% în istoric.** Nu concluziona că este performanță reală sau că A02 îl repară. Verifică cele două bare, sesiunea BMO/AMC, spliturile/ajustările furnizorului și simbolul înainte de orice recalculare a istoricului. Nu s-a șters sau rescris feedbackul.
3. **P1 — Active Risk.** `api/dashboard_service.py:_compute_dashboard_strategy_health` însumează `position_size_pct`, cu fallback `risk_pct_used`, plafonat la 100. Aceste mărimi nu sunt automat echivalente cu expunerea portofoliului sau riscul la stop. UI numără semnale din alt snapshot. Reconciliere necesară înainte de schimbarea modelului de confidence, care consumă aceeași metrică.
4. **P1 — Știri/catalizatori.** Premarket derivă taguri regex din toate articolele; AI Pulse poate avea un alt moment al analizei. Etichete observate par discutabile, dar corpul și proveniența completă nu au fost încă verificate. Nu elimina automat știrile sau agenții.
5. **P1 — Date insuficiente / erori de backtest.** Motorul poate întoarce un rezultat gol; wrapperul trebuie auditat pentru a nu confunda imposibilitatea simulării cu zero tranzacții valide. Nu înlocui lipsa datelor cu performanță zero.
6. **P2 — Claritate și conversie.** Mai puține filtre înaintea rezultatelor, surse/timestamp lângă valori, explicație „de ce eligibil / de ce blocat”, demonstrație completă Pre-Earnings → AI → risc → paper → rezultat. Fără ascunderea funcțiilor și fără promisiuni de profit.

## Protocol de continuare

1. Citește acest registru și starea Git; păstrează schimbările altor sesiuni. Folosește matricele `docs/audit_tabs_v2` ca listă de câmpuri, nu ca dovadă că verificările sunt încă valabile.
2. Pentru fiecare constatare: UI → API → transformare → cache → furnizor/ledger, cu unitate, timestamp, ajustări și tratarea lipsei datelor.
3. Reproduce defectul într-un test izolat înainte de corecție; verifică efectul asupra valorilor și deciziilor. Nu apela tranzacții sau notificări externe pentru un test.
4. Completează câte un rând de audit pentru fiecare card/câmp și notează explicit ce nu s-a verificat. Această primă trecere nu înlocuiește matricea exhaustivă.
5. Închide mai întâi problemele P1 de mai sus, apoi parcurge restul modalurilor și acțiunilor. Validează tot separat de implementarea SaaS.

## Validare și limitări operaționale

- Baseline: `pytest_baseline.log` — 3371 passed, 31 skipped.
- Primele corecții: `pytest_after.log` — 3380 passed, 31 skipped.
- Reproducere moduri/cache: `backtest_before_2026-09-07.log` — 3 failed, 9 passed înainte de reparație.
- Rularea intermediară `pytest_after_2026-09-07.log` a avut 3383 passed și un contract care încă cerea eticheta veche „Expected move”. Contractul a fost actualizat pentru noua semantică; rerularea țintită a trecut.
- `targeted_latest_2026-09-07.log`: 51 passed, inclusiv cele patru cazuri de precizie Radar.
- `frontend_2026-09-07.log`: sintaxă JS, regresii Hot List și regresii chart — cod 0.
- `pytest_final_2026-09-07.log`: **3384 passed, 31 skipped, 2 warnings**, 177,12 secunde. Testele Radar de precizie au fost adăugate ulterior colectării acestei rulări; dovada lor separată este logul țintit de mai sus. Nu se adună cele 51 de teste țintite la totalul suitei: cele mai multe se suprapun.
- `final_targeted_2026-09-07.log`: validarea planului a trecut — 50 sarcini, 150 acțiuni, un pachet, zero pași SaaS DONE.
- Testele E2E opt-in sunt omise de configurația existentă; două avertismente de depreciere existau în suita generală. Nu este validare de scalare, securitate SaaS sau rentabilitate.
- Procesul local existent nu a fost repornit de acest audit. Modificările Python sunt pe disc și testate în procese izolate; reîncărcarea backendului și a cache-urilor trebuie verificată înainte de a considera UI existent actualizat. Nu s-a publicat aplicația și nu s-au modificat conturi cloud/plăți.
