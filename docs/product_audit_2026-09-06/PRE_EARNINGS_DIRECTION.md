# Pre-Earnings: precizia LONG/SHORT și pașii de îmbunătățire

Completare: [idei Sidekick adaptate funcțiilor existente](SIDEKICK_LESSONS.md), cu surse oficiale și separarea capabilităților declarate de precizia încă nedemonstrată.

## Evidence Lab — 15 septembrie 2026

Sistemul are acum o buclă de învățare și verificare, nu doar o formulă euristică. Modulul `core/pre_earnings_learning.py` evaluează fiecare afirmație a produsului walk-forward: fiecare lună este notată doar cu reguli și modele antrenate pe evenimente decontate anterior. O afirmație primește `EDGE_PROVEN` numai dacă bate **toate** reperele naive pe aceleași evenimente (partea majoritară anterioară și „mereu LONG”), cu test de semn unilateral p < 0,05. Pentru mărimea gapului, criteriul este intervalul bootstrap 95% al îmbunătățirii Brier față de rata de bază anterioară.

Rezultat pe 3.548 rezultate decontate, dintre care 3.422 validate. Excluse: 121 sesiuni necunoscute, 3 fără mișcare realizată, 2 mișcări peste 100% pentru verificare manuală (inclusiv CLGN). Raport complet: [learning_report_2026-09-15.json](learning_report_2026-09-15.json).

| Afirmație | Evenimente | Precizie | Cel mai bun reper | Verdict |
|---|---:|---:|---:|---|
| Scor direcțional euristic | 2.812 | 55,3% | 56,4% (mereu LONG) | NO_EDGE |
| Verdict AI LONG/SHORT | 773 | 54,7% | 56,0% | NO_EDGE |
| Direcția Top 10 | 38 | 52,6% | 47,4% | INSUFFICIENT_EVIDENCE |
| Model învățat (reantrenat walk-forward) | 2.993 | 52,7% | 56,4% | NO_EDGE |
| Probabilitate gap mare (actuală) | 3.069 | Brier 0,203 | 0,163 (rata de bază) | NO_EDGE |
| Probabilitate gap calibrată walk-forward | 3.069 | Brier 0,188 | 0,163 | NO_EDGE |

Concluzie: **nicio predicție actuală nu bate un reper naiv în afara eșantionului.** Corelația fiecărui input stocat cu direcția este sub 0,03. Ajustarea ponderilor pe aceleași inputuri nu poate crea avantaj. Probabilitatea de gap mare ordonează slab evenimentele (AUC 0,54) și este necalibrată. Un prim test fără corecție de reper a arătat fals scorul euristic drept avantaj față de reperul majoritar lunar; reperul „mereu LONG” a fost adăugat tocmai pentru a bloca această capcană.

Ce funcționează acum în produs:

- **Model shadow.** După fiecare ciclu de decontare se reantrenează un model logistic pentru direcție și o calibrare izotonică pentru gap. Predicțiile lui se înregistrează pe fiecare predicție deschisă înainte de eveniment, se îngheață după începerea evenimentului și se copiază în rezultatul decontat. Nu tranzacționează și nu schimbă selecția.
- **Dovadă prospectivă.** Afirmațiile „live shadow” se evaluează separat. Doar acest istoric, înregistrat înainte de evenimente, poate justifica promovarea unui model.
- **Istoric păstrat.** Rezultatele eliminate de plafonul de 4.000 din magazinul live se arhivează în `pre_earnings_outcomes_archive.jsonl`, iar învățarea citește arhiva plus magazinul live.
- **Citirea știrilor, măsurată.** Fiecare predicție înregistrează câte articole și surse a citit AI-ul. Laboratorul compară separat verdictele AI după minimum 3 articole cu cele pe știri puține. Rezultatele istorice nu au aceste câmpuri, deci ambele afirmații pornesc de la zero evenimente și se completează pe măsură ce evenimentele noi se decontează; nu se ghicește nimic retroactiv.
- **Transparență.** `GET /api/pre-earnings/learning-report` și panoul „Evidence Lab” din Track Record arată verdictele. Top 10 afișează explicit când avantajul direcțional nu este dovedit.

### PE04 început: profit realizat în paper trading

Registrul de decizii conține profitul net al fiecărei poziții paper închise, după comisioanele și slippage-ul estimate. Nu include funding și nici execuții reale reconciliate. `GET /api/evidence/paper-performance` și secțiunea „Realized paper trading” din Evidence Lab arată rezultatul pe fiecare strategie.

| Strategie | Tranzacții | Net USD | Rată de câștig | Profit factor | Verdict |
|---|---:|---:|---:|---:|---|
| Toate strategiile | 276 | −86,79 | 43,5% | 0,89 | UNPROVEN |
| Scanner AI SHORT | 176 | −23,17 | 44,9% | 0,95 | UNPROVEN |
| Scanner AI LONG | 86 | −24,25 | 45,4% | 0,90 | UNPROVEN |
| Pre-Earnings AI LONG | 13 | −37,43 | 15,4% | 0,28 | INSUFFICIENT_EVIDENCE |

Perioada: 27 august – 8 septembrie 2026. O strategie devine `PROFITABLE_PROVEN` numai dacă intervalul bootstrap al profitului mediu pe tranzacție este peste zero, cu minimum 30 de tranzacții. Pentru mai multe strategii, intervalul de verdict este ajustat Bonferroni. Concluzie: **paper trading nu este încă profitabil**, iar aceasta este referința pe care orice avantaj viitor trebuie să o depășească.

### PE07 început: informații noi înainte de eveniment

Explorare pe aceleași 3.422 evenimente, cu bare zilnice Yahoo pentru 1.587 simboluri și SPY:

| Semnal testat | Rezultat |
|---|---|
| Reacția peerilor din aceeași industrie deja raportați (3/7/14 zile) | 53% vs 57% mereu LONG. Fără avantaj. |
| Reacția companiilor din același sector | circa 49,5%. Fără avantaj. |
| Momentum 5 sau 20 zile relativ la SPY | 45–46%. Fără avantaj. |
| Contrarian: inversarea unei mișcări de 10%+ în 5 zile vs SPY | 56,9% pe 489 evenimente vs 50,3% mereu LONG, p = 0,032 |

Ultimul rezultat a apărut după 12 variante încercate. Corectat pentru numărul de încercări, **nu este semnificativ**. Nu a fost promovat. Regula exactă este pre-înregistrată în `core/pre_event_features.py` (15 septembrie 2026) și se evaluează doar pe evenimentele capturate după această dată. Perioada de explorare nu poate fi folosită ca dovadă.

Laboratorul aplică acum corecția Holm-Bonferroni pe toate afirmațiile direcționale testate, astfel încât un „câștig” apărut întâmplător dintr-un set mare de teste nu poate fi afișat drept avantaj dovedit.

Informații capturate acum înainte de fiecare eveniment, fără date plătite:

- **Revizuiri ale estimărilor EPS** (Yahoo `eps_trend` și `eps_revisions`): schimbarea consensului pe 7/30/90 zile, revizuiri în sus și în jos, număr de analiști. Câmpul `eps_estimate_revisions_30d` era marcat până acum „indisponibil”.
- **Randament relativ la SPY** pe 5 și 20 de sesiuni încheiate, pentru regula contrarian pre-înregistrată.

Captura se face în afara lock-ului de feedback, pentru cele mai apropiate evenimente din 7 zile, cu maximum 40 de simboluri pe ciclu. Valorile se îngheață după începerea evenimentului și ajung în rezultatul decontat. Laboratorul le evaluează automat ca afirmații separate.

Stare actualizată a pașilor de mai jos: PE05 parțial (aceleași evenimente, euristic vs AI vs Top 10 vs model, cu repere). PE06 realizat în regim shadow. PE09 realizat ca walk-forward lunar; holdoutul prospectiv este istoricul shadow. PE12 în curs: rularea în paralel este activă, promovarea cere `EDGE_PROVEN` pe istoricul shadow. PE07 rămâne pasul decisiv: avantajul poate veni doar din informații noi înainte de eveniment (revizuiri EPS, reacția peerilor deja raportați, performanță relativă pre-event), testate automat în acest laborator.

## Decizie de produs — 7 septembrie 2026

### Clarificare obligatorie a proprietarului: selecție strictă

Proprietarul nu dorește toate companiile în lista de recomandări. Păstrăm filtrarea și pragurile; îmbunătățim analiza care decide cine le trece. Universul larg este procesat în fundal, iar lista principală arată numai candidații eligibili. Arhiva de audit poate păstra respingerile, fără a aglomera selecția. „Top 10” este un maxim, nu o obligație de a umple zece locuri.

Inspecția `_prediction_score_details` și `_build_top_predictions` arată:

- Există porți pentru eveniment verificat, direcție, intensitatea biasului, acoperire, disponibilitatea gapului, analiza AI și știri. Valorile implicite din funcție includ bias minim 20 și coverage minim 45; acestea nu sunt prezentate ca valori active fără verificarea configurației.
- Trader Score combină direcție 22%, coverage 18%, confidence 14%, gap 12%, acord AI 14%, istoric ticker 8%, știri 7%, urgență 5%, apoi penalizări. Aceste ponderi clasifică oportunitățile; valoarea lor predictivă direcțională nu este demonstrată de formulă.
- `_build_top_predictions` filtrează eligibilitatea și fereastra temporală, sortează după Trader Score și limitează numărul. În această funcție nu există un prag minim separat pentru Trader Score. Benzile A/B/C/WATCH sunt definite în calculul scorului, dar banda nu este o condiție de admitere în acea selecție. Această observație nu anulează porțile distincte ale execuției.

Soluția candidată trebuie să păstreze patru etape verificabile:

1. Porți obligatorii pentru date, eveniment și posibilitatea de execuție; punctele din alte componente nu pot compensa un eșec aici.
2. Analiză aprofundată numai pentru candidații care trec prefiltrarea, cu surse distincte, schimbări de estimări, context de preț și verificarea ambelor direcții. Refolosește cache-urile și datele deja existente.
3. Scor de avantaj direcțional evaluat separat pentru LONG/SHORT. Verifică aportul fiecărei componente pe aceleași evenimente, inclusiv dependența dintre sentiment, scorul structurat și acordul AI. Același articol sau aceeași opinie nu trebuie tratate drept confirmări independente.
4. Admitere numai peste pragul direcțional validat, urmată de clasarea oportunității și limitarea numărului. Dacă nu trece nimeni, lista rămâne goală. Pragurile se aleg pe train/validation cu constrângere de acoperire utilă și se verifică pe holdout, nu prin mărirea arbitrară a numerelor actuale.

Nu au fost modificate încă ponderile sau pragurile active. Următorul livrabil de implementare este instrumentarea contribuțiilor și snapshoturilor necesare unei comparații reproductibile între selecția actuală și cea candidată. Datele istorice nevalidate și cele 39 de cazuri Top 10 nu justifică promovarea imediată a unei noi politici.

La cererea proprietarului, lucrul pentru lansarea online este pus pe pauză. Prioritatea este corectitudinea produsului local și valoarea deciziilor. Nu se elimină Pre-Earnings, SHORT, agenții sau alte funcții. Nicio pondere de trading nu a fost schimbată în această investigație.

## Ce am măsurat efectiv

Sursă: `data/pre_earnings_feedback.json`, 3.544 evenimente salvate, zero chei duplicate simbol/zi în snapshotul analizat. Raportul JSON reține calea, SHA-256 și momentul citirii. Nu este o verificare independentă a prețurilor furnizorului și nu reprezintă P/L realizat.

| Populație | Corecte / direcționale nonzero | Precizie | Reper mereu LONG pe aceleași cazuri |
|---|---:|---:|---:|
| Top 10, direcția păstrată la includere | 20 / 39 | 51,28% | 48,72% |
| Scorul direcțional salvat, toate versiunile | 1.151 / 2.131 | 54,01% | 55,42% |
| Scorul cu versiunea two_stage_evidence_weighted_v2 | 146 / 274 | 53,28% | 53,65% |
| Verdictul AI salvat | 459 / 854 | 53,75% | 55,74% |

Top 10: 36 LONG, 3 SHORT; LONG 18/36, SHORT 2/3. Intervalul Wilson descriptiv de 95% pentru 20/39 este aproximativ 36,2%–66,1%, presupunând independență; evenimentele din aceeași zi/sector pot încălca ipoteza. Cele trei SHORT nu permit concluzia că SHORT este superior. În istoricul general, SHORT are 192/414 = 46,38%, iar LONG 959/1.717 = 55,85%. Populațiile AI și generale diferă: diferențele de procent nu demonstrează incremental că AI ajută sau dăunează.

Neutralitatea, predicțiile fără direcție și mișcările zero sunt excluse explicit din rata direcțională și numărate separat în JSON. Nu au fost selectate retrospectiv praguri câștigătoare. Reperul mereu LONG este un control statistic, nu o recomandare de tranzacționare.

## Ce explică sursa și ce rămâne ipoteză

1. **Recalibrarea nu optimizează direct direcția.** `_build_group_calibration_model` compară `predicted_prob_high_gap` cu `realized_high_gap`, pe sector/capitalizare. Un model al amplitudinii nu demonstrează un avantaj LONG/SHORT.
2. **Scorul direcției este euristic.** `compute_direction_bias_score` combină componente cu ponderi prestabilite: istoric EPS, target/rating analiști, calls/puts, sentiment, reacții istorice și retail. `analyst_revision` este numele unei componente calculate din target upside și recommendationMean; nu măsoară în acea formulă schimbarea estimărilor EPS în timp. Trebuie testat un semnal real de revizuire, nu presupus că există.
3. **Alegerea LONG domină selecția.** Observată în 36/39 Top 10. Cauza nu este demonstrată numai de distribuție; trebuie descompuse contribuțiile scorurilor și criteriile shortlistului pe aceleași evenimente.
4. **Încrederea declarată de AI nu este o probabilitate validată.** `_synchronize_direction_decision` combină sentimentul și verdictul AI cu scorul structurat. Calibrarea empirică a încrederii trebuie măsurată separat și pe predicții autentice anterioare evenimentului.
5. **Calitatea țintei încă limitează analiza.** Există 121 sesiuni necunoscute, 111 rânduri marcate `ai_cache_backfill`, două mișcări absolute peste 100% și opt Top 10 înregistrate în aceeași zi sau după ziua indicată de eveniment. Acestea sunt semnale de verificare, nu dovezi automate de corupere sau look-ahead. Ora 00:00 poate fi doar un placeholder; publicarea exactă și sesiunea trebuie verificate.
6. **Direcția gapului nu este profitul tranzacției.** Feedbackul folosește prețuri de referință înainte/după eveniment. Pozițiile pot avea alt moment de intrare/ieșire, costuri și stopuri. Este necesară legarea fiecărei poziții Capital de predicția care a produs-o.

## Îmbunătățiri propuse, în ordinea implementării

| Pas | Stare | Livrabil și criteriu de verificare |
|---|---|---|
| PE01 | DONE | Diagnostic reproductibil al ratelor, reperelor, abținerilor, duplicatelor și anomaliilor. Script și două teste automate trecute. |
| PE02 | TODO | Validare eveniment → preț de referință → split/ajustare → rezultat. Păstrează rândul original și marchează motivul excluderii din setul validat; nu șterge pierderi sau valori extreme arbitrar. Verifică prioritar CLGN și sesiunile necunoscute. |
| PE03 | TODO | Inventariază snapshoturile existente și completează dovada point-in-time: momentul deciziei, componente brute, surse/timestamps, versiune model/prompt, direcție, eligibilitate, motive. Snapshotul evaluat trebuie să fie imuabil și anterior publicării reale. Backfillul se raportează separat. |
| PE04 | TODO | Reconciliere cu Capital: precizie gap, precizie de la recomandare la intrare, profit net, câștig/pierdere medie, drawdown, costuri și număr de tranzacții. Separă eroarea direcției de eroarea execuției/ieșirii. |
| PE05 | TODO | Diagnostic pe aceleași evenimente: structură singură, AI singur, combinație, acord/dezacord, LONG/SHORT, sesiune și versiune. Grupurile mici rămân descriptive; nu selecta cele mai bune retrospectiv pentru producție. |
| PE06 | TODO | Construiește ținta direcțională și un model de referință simplu, calibrat. Compară separat modele LONG și SHORT sau o clasificare UP/DOWN/FLAT; justifică empiric selecția. Păstrează modelul de amplitudine distinct. |
| PE07 | TODO | Testează incremental informații disponibile înainte de eveniment: revizuiri EPS/revenue și dispersia lor, guidance anterioară, performanța pre-event relativă la sector, reacții istorice la surprize, rezultate ale peerilor deja publicate. Nu folosi EPS actual al evenimentului ca feature pre-earnings. |
| PE08 | TODO | Opțiuni: numai dacă există sursă/licență/acoperire, testează IV/implied move/skew pe scadența relevantă, cu timestamp și lichiditate. Nu interpreta simplul calls/puts drept dovadă automată de cumpărare direcțională. Lipsă = lipsă. |
| PE09 | TODO | Validare cronologică walk-forward: antrenează pe trecut, calibrează pe o fereastră separată, testează pe viitor; separă pe eveniment și evită suprapunerea orizonturilor. Păstrează un holdout neatins; înregistrează numărul de variante încercate. |
| PE10 | TODO | Testează posibilitatea de a nu tranzacționa când avantajul este insuficient. Arată câte oportunități sunt eliminate și impactul asupra randamentului net, nu doar win rate. Pragurile se aleg pe train/validation, nu pe holdout. Nu dezactiva SHORT pe baza analizei actuale. |
| PE11 | TODO | UI: afișează distinct risc de mișcare mare, direcție, încredere empiric calibrată dacă există, mărimea eșantionului, acord AI/structură, surse și explicația refuzului. Toate analizele rămân accesibile. |
| PE12 | TODO | Rulează candidatul în paralel, fără ordine suplimentare, pe evenimente noi. Promovează numai după îmbunătățire OOS față de modelul curent și repere, după costuri, cu stabilitate pe perioade și acoperire utilă. Păstrează rollback și versiunea precedentă. |

Nu promitem 65% sau 70% precizie. Un procent mai mare poate proveni dintr-un eșantion mic, din alegerea retrospectivă a filtrelor sau din eliminarea aproape tuturor tranzacțiilor. Se urmăresc simultan avantajul față de reper, probabilități calibrate, rezultat net și acoperire. Riscul alegerii unui backtest prea bine adaptat istoricului este documentat în [Bailey et al., The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf).

## Rerulare

Din rădăcina proiectului:

```text
.venv\Scripts\python.exe -X utf8 scripts/audit_pre_earnings_direction.py --feedback data/pre_earnings_feedback.json --output docs/product_audit_2026-09-06/pre_earnings_direction_diagnostic.json
.venv\Scripts\python.exe -X utf8 -m pytest tests/unit/test_pre_earnings_direction_diagnostic.py -q
```

Scriptul citește un snapshot al fișierului și scrie numai raportul indicat. Nu pornește engine-ul, nu rescrie feedbackul, nu ajustează scoruri și nu trimite ordine. Nu este încă un backtest OOS și nu dovedește îmbunătățirea predicției. Prima etapă realizată este măsurarea reproductibilă a problemei.
