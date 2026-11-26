# Plan de migrare online

**Regulă actuală: păstrăm toate funcțiile și taburile, inclusiv Pre-Earnings, Agents, Capital și AI Cockpit ca spații distincte.** [PRODUCT_PRESERVATION.md](PRODUCT_PRESERVATION.md) documentează traseele verificate și are prioritate față de propunerile inițiale de simplificare.

## Obiectiv și limite

Transformăm aplicația locală într-un SaaS de analiză a pieței și paper trading, cu utilizatori separați și un singur abonament de 10 USD/lună cu toate funcțiile client incluse. În această sesiune livrăm planul și mecanismul de continuare, nu implementarea. Prețul și moneda sunt decise de proprietar; nu se reintroduc niveluri sau funcții cu plată suplimentară.

Prima versiune publică include research, alerte și simulare. Execuția cu bani reali, păstrarea cheilor de broker și administrarea banilor clienților sunt o etapă distinctă, cu design și evaluare specifice; nu sunt activate prin simpla mutare online. Niciun abonament nu promite câștiguri. O funcție pe care o vindem trebuie să existe, să fie verificată și să aibă drepturi comerciale pentru datele distribuite.

Ținta strategică este de ordinul milioanelor de conturi înregistrate. Construim întâi pentru un pilot și creștem pe praguri măsurabile. Nu cumpărăm infrastructură pentru milioane înainte să avem utilizatori, dar evităm alegerile care ar impune o rescriere completă la primul val de clienți.

## Ce există acum — verificat în sursă

| Există | Implicație pentru migrare |
|---|---|
| FastAPI, HTML/JS și 13 taburi | Se pot păstra incremental; nu este necesară o rescriere totală de frontend înainte de pilot. |
| Login care schimbă cheia globală de operator într-o sesiune HttpOnly, în api/main.py | Este autentificare de operator, nu cont personal cu parolă și roluri de client. |
| ConfigManager și fișiere comune watchlist/config/capital/state | Trebuie separate datele personale de setările platformei; nu se adaugă doar un câmp user_id într-un fișier comun. |
| SQLiteKVStore singleton și alte baze/fișiere locale | Nu sunt baza de producție pentru milioane de utilizatori și scrieri concurente. |
| Engine global/subprocess și stare comună | Motorul devine serviciu comun; nu se pornește o copie per utilizator sau per replică API. |
| Dockerfile și compose.production.yml cu volume persistente | Sunt o bază de containerizare pentru o instalare; nu demonstrează multi-tenancy sau scalare orizontală. |
| Re-audit și teste locale | Sunt baseline tehnic. Nu reprezintă validare a pieței, teste de milioane de utilizatori sau audit SaaS independent. |

Nu am identificat în punctele de intrare inspectate un sistem SaaS complet de utilizatori, facturare și izolare. S00 trebuie să completeze inventarul întregului repository înainte de a decide ce se refolosește. Starea git este în BASELINE.json; nu modifica runtime-ul personal pentru a construi demonstrații SaaS.

## Etapele de lucru

| Etapă | Pași | Livrabil care permite continuarea |
|---|---|---|
| A — clarificare tehnică și comercială | S00–S03 | Baseline, registru de drepturi, economie la 10 USD și ADR. Contractele comerciale pot rămâne în lucru, dar blochează lansarea feedurilor respective. |
| B — date și identitate | S04–S08 | Două conturi cu date izolate, sesiuni sigure și API care nu expune administrarea. |
| C — servicii de produs | S09–S14 | Pipeline comun, joburi, paper trading, alerte și AI cu bugete. |
| D — abonamente și experiență | S15–S23 | Checkout test, webhooks, abonament unic, UI, streaming, suport și observabilitate. Dependențele din TASKS.json stabilesc ordinea exactă. |
| E — pregătire operațională | S24–S29 | Staging reproducibil, import sigur, review de securitate, teste de încărcare și restaurare. |
| F — validare și lansare | S30–S32 | Pilot real la prețul decis, lansare progresivă și retenție măsurată. |
| G — capacitate | S33–S36 | 10k → 100k → 1M → 3M conturi, fiecare cu raport de test și cost. |

Sarcinile nu sunt presupuse a încăpea într-o singură sesiune AI. Fiecare are trei subpași persistabili și criterii de acceptare. Durata depinde de integrarea datelor și de complexitatea migrării; niciun termen de lansare nu este garantat înainte de S00–S03. Contractele, pilotul real și review-ul independent au timpi care nu pot fi înlocuiți prin generare de cod.

## Gate-uri de lansare

**G-IDENTITY, înainte de accesul unui utilizator extern:** signup/login/recovery; A nu poate vedea B prin API, cache, export, stream sau worker; rutele de operator sunt inaccesibile public; logurile nu conțin secrete.

**G-STAGING:** plăți numai în sandbox; abonamentul unic în toate stările sale și toate taburile testate; joburile nu blochează plățile; migrare și rollback verificate; datele personale ale proprietarului nu sunt expuse.

**G-PUBLIC, înainte de prima încasare:** S01/S02 și S25–S30 complete, 10 USD/lună verificat în catalog/checkout/factură, entitate și taxe finalizate, drepturi asupra datelor, termeni și privacy verificate, zero P0/P1 de securitate/facturare, review independent, restaurare testată, suport și buget operațional. Simpla adăugare a unui disclaimer nu decide clasificarea unui serviciu de recomandări financiare. [Context ESMA](https://www.esma.europa.eu/press-news/esma-news/requirements-when-posting-investments-recommendations-social-media)

**G-SCALE:** capacitatea fiecărui prag se declară numai pentru configurația și profilul de sarcină testate, cu cost și cote de furnizor aprobate. Un milion de rânduri în users nu dovedește un milion de utilizatori simultani.

## Decizii ale proprietarului — nu blochează redactarea sau implementarea locală

| ID | Decizie | Când devine necesară |
|---|---|---|
| D01 | Entitatea comercială și țările în care se vinde | Înainte de contracte/plăți live. Nu presupunem că domiciliul stabilește jurisdicția firmei. |
| D02 | Buget cloud, date, AI și suport; praguri de oprire | Înainte de resurse plătite și apoi la fiecare prag de scalare. |
| D03 | Preț DECIS: 10 USD/lună, un abonament. Rămân taxele inclusive, trial/refund/grace | Înainte de Stripe live; test prices rămân etichetate strict sandbox. |
| D04 | Domeniu și nume public de produs | Înainte de branding final și publicare. |
| D05 | Contractele feedurilor și modelul de date permis | Înainte ca acele date să fie distribuite utilizatorilor externi. |
| D06 | Furnizor cloud/IdP/payment și acces la conturile lor | ADR propune AWS/Cognito/Stripe; configurarea live folosește conturi ale proprietarului. |

AI-ul continuă lucrul independent care nu depinde de o decizie lipsă. Nu schimbă prețul de 10 USD și nu inventează, o cheie API, un contract, clienți de test reali sau o aprobare. Nu contactează oameni și nu cumpără servicii doar pentru că apar într-un plan.

## Regula de raportare cerută de proprietar

După fiecare pas sau subpas executat: scrie ce s-a schimbat, fișierele, verificările rulate și rezultatele, ce a rămas, dependențele și următoarea acțiune concretă. Actualizează TASKS.json, STATUS.md și PROGRESS.md. Raportul pasului se salvează în evidence/Sxx/REPORT.md. Folosește numai statusuri TODO, IN_PROGRESS, BLOCKED, DONE; o propunere nu este DONE.

Un pas este DONE doar când toate acțiunile sunt DONE, dependențele sunt DONE, criteriile au dovezi și raportul există. Dacă intervine alt AI în aceleași fișiere, recitește diferențele și păstrează schimbările; nu reinstala peste ele un snapshot vechi.

## Extinderea planului de produs

S37–S44: design, explicații, pagină simbol, registre existente, digest, demo și validare UX. S47–S49: Pre-Earnings distinct, agenți personali cu execuție paper și verificare integrală candidat → decizie → risc → execuție → rezultat. S19/S25 depind explicit de acestea. S45–S46: experimente comerciale după lansare. Toate rămân TODO până la implementare și dovezi.
