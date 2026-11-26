# Starea implementării

Generat din TASKS.json. Modifică registrul JSON, apoi regenerează acest fișier.

DONE cere dovezi și verificări; lista nu certifică funcționarea aplicației.

| Pas | Fază | Stare | Dependențe |
|---|---|---|---|
| S00 — Stabilirea baseline-ului executabil | A | TODO | — |
| S01 — Drepturi comerciale, date și piețe permise | A | TODO | S00 |
| S02 — Abonament unic de 10 USD și economie per utilizator | A | TODO | S00 |
| S03 — Contracte de arhitectură și model de amenințări | A | TODO | S00 |
| S04 — PostgreSQL și migrații | B | TODO | S03 |
| S05 — Izolare workspace și RLS | B | TODO | S04 |
| S06 — Conturi, parolă și sesiuni | B | TODO | S03, S04 |
| S07 — Autorizare API și separare operator | B | TODO | S05, S06 |
| S08 — Migrarea datelor personale din fișiere globale | B | TODO | S05, S07 |
| S09 — Pipeline comun pentru date de piață | C | TODO | S03, S04 |
| S10 — Worker separat și coadă durabilă | C | TODO | S04, S05, S09 |
| S11 — Paper trading separat per utilizator | C | TODO | S07, S08, S10 |
| S12 — Alerte și notificări multi-user | C | TODO | S08, S09, S10 |
| S13 — Backtesting ca job cu buget | C | TODO | S10, S11 |
| S14 — AI gateway și bugete | C | TODO | S09, S10 |
| S15 — Catalog unic și drepturi de acces | D | TODO | S02, S07 |
| S16 — Stripe Checkout în sandbox | D | TODO | S06, S15 |
| S17 — Webhooks și reconciliere abonamente | D | TODO | S16 |
| S18 — Gestionare abonament și facturare | D | TODO | S17 |
| S19 — Experiență SaaS și cele 13 taburi | D | TODO | S08, S11, S12, S13, S14, S15, S18, S38, S39, S40, S41, S42, S47, S48, S49 |
| S20 — Actualizări live cu fan-out controlat | D | TODO | S07, S09, S12, S15 |
| S21 — Consolă operator și suport | D | TODO | S07, S17, S23 |
| S22 — Confidențialitate și lifecycle cont | D | TODO | S05, S06, S17 |
| S23 — Observabilitate și control cost | D | TODO | S03, S10, S15 |
| S24 — Infrastructură ca cod și CI/CD | E | TODO | S03, S04, S10 |
| S25 — Staging integrat și E2E | E | TODO | S18, S19, S20, S22, S23, S24, S43, S44 |
| S26 — Importul contului proprietarului | E | TODO | S08, S11, S13, S25 |
| S27 — Review de securitate și izolare | E | TODO | S21, S22, S25, S26 |
| S28 — Teste de încărcare și cost pilot | E | TODO | S23, S25 |
| S29 — Backup și recuperare | E | TODO | S24, S25, S26 |
| S30 — Pilot privat cu 20–50 utilizatori | F | TODO | S01, S02, S25, S27, S28, S29 |
| S31 — Lansare abonamente plătite | F | TODO | S30 |
| S32 — Validare comercială și retenție | F | TODO | S31 |
| S33 — Capacitate 10.000 conturi | G | TODO | S28, S31 |
| S34 — Capacitate 100.000 conturi | G | TODO | S32, S33 |
| S35 — Capacitate 1.000.000 conturi | G | TODO | S34 |
| S36 — Capacitate de ordinul milioanelor | G | TODO | S35 |
| S37 — Design system și păstrarea celor 13 taburi | UX | TODO | S00 |
| S38 — Acasă personal și activare rapidă | UX | TODO | S37, S08, S09 |
| S39 — Card explicabil și semantică verificată a scorurilor | UX | TODO | S37, S09, S14 |
| S40 — Pagina simbolului și comparații coerente | UX | TODO | S39, S08 |
| S41 — Jurnal de decizii și evaluare fără selecție convenabilă | UX | TODO | S39, S11, S13 |
| S42 — Digest relevant și alerte cu control asupra zgomotului | UX | TODO | S12, S38, S40 |
| S43 — Landing, demo și ofertă unică verificabilă | UX | TODO | S18, S38, S39 |
| S44 — Validare UX și instrumentarea conversiei | UX | TODO | S38, S39, S40, S41, S42, S43, S47, S48, S49 |
| S45 — Experimente de atragere cu buget măsurabil | GROWTH | TODO | S31, S32 |
| S46 — Recomandări voluntare și retenție pe valoare | GROWTH | TODO | S32, S45 |
| S47 — Pre-Earnings separat, păstrat integral | PRODUCT | TODO | S37, S39, S09 |
| S48 — Agenți personali și automatizare paper completă | PRODUCT | TODO | S11, S14, S15, S37 |
| S49 — Traseu complet și comparație reproductibilă | PRODUCT | TODO | S47, S48, S40, S41 |

## S00 — Stabilirea baseline-ului executabil

**Stare:** TODO · **Dependențe:** niciuna

**Puncte de intrare:** api/main.py; core/config.py; api/engine_service.py; docs/REAUDIT_FIXES_2026-09-06.md

### Subpași

- [ ] **S00.1 [TODO]** Recitește BASELINE și rapoartele de audit; compară HEAD și modificările locale.
- [ ] **S00.2 [TODO]** Salvează copie verificabilă a sursei/configurației necesare, cu secrete separat; stabilește ramura de lucru fără a pierde schimbările altui AI.
- [ ] **S00.3 [TODO]** Rulează testele existente în runtime izolat; inventariază endpointurile, fișierele globale, serviciile și joburile.

### Acceptare

- Inventar endpoint → date → global/personal/operator și politică de acces.
- Baseline reproductibil, hashuri, rezultate teste și limitări salvate; nicio modificare de date reale.

**Raport cerut:** `evidence/S00/REPORT.md`

**Următoarea acțiune:** Recitește BASELINE și rapoartele de audit; compară HEAD și modificările locale.

**Blocaje:** Niciun blocaj înregistrat.

## S01 — Drepturi comerciale, date și piețe permise

**Stare:** TODO · **Dependențe:** S00

**Puncte de intrare:** THIRD_PARTY_NOTICES.md; requirements.txt; core/content_fetcher.py; adapters furnizori

### Subpași

- [ ] **S01.1 [TODO]** Inventariază fiecare feed, articol, grafic embed, model AI și licență OSS.
- [ ] **S01.2 [TODO]** Obține sau documentează drepturile de afișare, cache, redistribuire, date derivate, profesioniști/non-profesioniști și limite per utilizator.
- [ ] **S01.3 [TODO]** Stabilește țările de lansare, clasificarea serviciului, privacy/termeni și criteriile care blochează vânzarea unei funcții.

### Acceptare

- Registru cu dovezi contractuale și clasificare permis/interzis/neconfirmat.
- Orice funcție fără drepturi rămâne dezactivată în producție; bibliotecă open-source nu înseamnă licență asupra datelor.

**Raport cerut:** `evidence/S01/REPORT.md`

**Următoarea acțiune:** Inventariază fiecare feed, articol, grafic embed, model AI și licență OSS.

**Blocaje:** Niciun blocaj înregistrat.

## S02 — Abonament unic de 10 USD și economie per utilizator

**Stare:** TODO · **Dependențe:** S00

**Puncte de intrare:** PACKAGES.md; PACKAGE_DRAFT.json; configurație nouă catalog

### Subpași

- [ ] **S02.1 [TODO]** Intervievează 10–15 potențiali utilizatori, numai după autorizarea contactării lor; pregătește scenariile și scriptul de interviu local.
- [ ] **S02.2 [TODO]** Benchmark pentru ciclul complet: candidat, AI, critic, dezbatere condițională, risc, monitorizare/ieșire și backtest; separă costul comun de cel personal la 10 USD.
- [ ] **S02.3 [TODO]** Validează la 10 USD/lună costul tuturor funcțiilor, limitele comune transparente și metricile pilotului; păstrează prețul decis.

### Acceptare

- Catalog versionat: un abonament, 1000 cenți USD/lună, toate funcțiile client incluse; fără consum nelimitat presupus.
- Model de cost mediu/p95 și consum integral la 10 USD; lansarea este blocată dacă oferta nu este sustenabilă, fără majorare automată de preț.

**Raport cerut:** `evidence/S02/REPORT.md`

**Următoarea acțiune:** Intervievează 10–15 potențiali utilizatori, numai după autorizarea contactării lor; pregătește scenariile și scriptul de interviu local.

**Blocaje:** Niciun blocaj înregistrat.

## S03 — Contracte de arhitectură și model de amenințări

**Stare:** TODO · **Dependențe:** S00

**Puncte de intrare:** api/main.py; compose.production.yml; deploy/; core/deployment_security.py

### Subpași

- [ ] **S03.1 [TODO]** Aprobă prin ADR implementarea de referință din ARCHITECTURE.md sau motivează alternativa.
- [ ] **S03.2 [TODO]** Desenează limitele public/user/operator/worker și fluxurile de identitate, facturare, joburi și date.
- [ ] **S03.3 [TODO]** Scrie contracte OpenAPI v1, clasificare date, SLO și scenarii de abuz/cross-tenant.

### Acceptare

- ADR include opțiuni, costuri, compromisuri și migrare.
- Nicio cale publică nu folosește cheia globală operator sau runtime-ul personal.

**Raport cerut:** `evidence/S03/REPORT.md`

**Următoarea acțiune:** Aprobă prin ADR implementarea de referință din ARCHITECTURE.md sau motivează alternativa.

**Blocaje:** Niciun blocaj înregistrat.

## S04 — PostgreSQL și migrații

**Stare:** TODO · **Dependențe:** S03

**Puncte de intrare:** core/sqlite_store.py; core/execution/store.py; viitor core/db/; migrations/

### Subpași

- [ ] **S04.1 [TODO]** Introdu repository interfaces și SQLAlchemy/Alembic sau alternativă motivată; fără acces SQL ad-hoc în UI handlers.
- [ ] **S04.2 [TODO]** Creează schema definită în ARCHITECTURE: identitate, workspace, membership, preferințe, abonamente, ledger, jobs, usage.
- [ ] **S04.3 [TODO]** Separă rolurile DB runtime/migrator/admin; testează upgrade de schemă și restaurare.

### Acceptare

- Migrare de la DB goală și versiunea anterioară reușită.
- Chei compuse și indecși pe workspace_id; runtime nu este owner/superuser/BYPASSRLS.

**Raport cerut:** `evidence/S04/REPORT.md`

**Următoarea acțiune:** Introdu repository interfaces și SQLAlchemy/Alembic sau alternativă motivată; fără acces SQL ad-hoc în UI handlers.

**Blocaje:** Niciun blocaj înregistrat.

## S05 — Izolare workspace și RLS

**Stare:** TODO · **Dependențe:** S04

**Puncte de intrare:** viitor core/tenancy/; core/db/; toate repository-urile personale

### Subpași

- [ ] **S05.1 [TODO]** Creează un workspace personal per cont și membership; identitatea se rezolvă server-side.
- [ ] **S05.2 [TODO]** Aplică RLS și context de tranzacție local pentru pool-ul de conexiuni; validează toate FK-urile tenant-scoped.
- [ ] **S05.3 [TODO]** Include spațiul de lucru în cache keys, căi de obiecte, evenimente și jobs; export/download cu autorizare la fiecare citire.

### Acceptare

- Utilizatorul A nu citește/modifică/exportă nimic din B, inclusiv prin job IDs și canale push.
- Pool-ul reutilizat, workerul, replica și storage-ul nu scurg context între utilizatori.

**Raport cerut:** `evidence/S05/REPORT.md`

**Următoarea acțiune:** Creează un workspace personal per cont și membership; identitatea se rezolvă server-side.

**Blocaje:** Niciun blocaj înregistrat.

## S06 — Conturi, parolă și sesiuni

**Stare:** TODO · **Dependențe:** S03, S04

**Puncte de intrare:** api/main.py: AuthLoginRequest/auth_login; viitor api/identity/; frontend/account/

### Subpași

- [ ] **S06.1 [TODO]** Configurează adaptor OIDC și IdP de referință în mediu de test; identitate internă UUID legată de issuer+sub.
- [ ] **S06.2 [TODO]** Implementează signup cu email/parolă, username public opțional, verificare email, login, reset parolă, logout/toate sesiunile și MFA.
- [ ] **S06.3 [TODO]** Folosește code flow cu PKCE/state/nonce și sesiuni HttpOnly Secure; protejează CSRF, replay, enumerare și brute force.

### Acceptare

- Fluxurile complete trec E2E; JWT invalid/expirat/issuer greșit/audience greșită sunt respinse.
- Parolele nu sunt stocate de aplicație și tokenurile nu apar în loguri/localStorage; MFA obligatoriu operator.

**Raport cerut:** `evidence/S06/REPORT.md`

**Următoarea acțiune:** Configurează adaptor OIDC și IdP de referință în mediu de test; identitate internă UUID legată de issuer+sub.

**Blocaje:** Niciun blocaj înregistrat.

## S07 — Autorizare API și separare operator

**Stare:** TODO · **Dependențe:** S05, S06

**Puncte de intrare:** api/main.py middleware; api/config_service.py; api/ai_config_service.py; frontend/cockpit.js

### Subpași

- [ ] **S07.1 [TODO]** Introdu dependencies CurrentUser/CurrentWorkspace și permisiuni server-side.
- [ ] **S07.2 [TODO]** Separă /api/v1 de /internal; mută engine control, config global, keys, agents admin și coding agent în control plane privat.
- [ ] **S07.3 [TODO]** Blochează implicit rutele legacy pe ingress-ul public; generează teste pentru inventarul complet de rute.

### Acceptare

- Niciun abonament nu oferă acces administrativ; UI ascuns fără protecție API nu este acceptat.
- Anonim, user A, user B, operator și worker testați pentru fiecare familie de endpointuri.

**Raport cerut:** `evidence/S07/REPORT.md`

**Următoarea acțiune:** Introdu dependencies CurrentUser/CurrentWorkspace și permisiuni server-side.

**Blocaje:** Niciun blocaj înregistrat.

## S08 — Migrarea datelor personale din fișiere globale

**Stare:** TODO · **Dependențe:** S05, S07

**Puncte de intrare:** core/config.py; core/alerts.py; core/ui_watchlist.py; api/actions_service.py; api/state_service.py

### Subpași

- [ ] **S08.1 [TODO]** Mută watchlist, alerte, preferințe și configurare personală în repository-uri pe workspace.
- [ ] **S08.2 [TODO]** Desparte get_shared_config de UserSettings; interzice setări personale în singleton global.
- [ ] **S08.3 [TODO]** Depreciază scrierile JSON/SQLite pentru SaaS; păstrează modul local prin adapter separat și explicit.

### Acceptare

- A și B au watchlisturi/alerte independente în API și UI.
- Niciun request SaaS nu modifică watchlist.json, config.json sau capital.json ale operatorului.

**Raport cerut:** `evidence/S08/REPORT.md`

**Următoarea acțiune:** Mută watchlist, alerte, preferințe și configurare personală în repository-uri pe workspace.

**Blocaje:** Niciun blocaj înregistrat.

## S09 — Pipeline comun pentru date de piață

**Stare:** TODO · **Dependențe:** S03, S04

**Puncte de intrare:** api/market_data_service.py; agents/; core/cache_manager.py; data adapters

### Subpași

- [ ] **S09.1 [TODO]** Separă instrumentul după asset_class/exchange/currency și calendarul de sesiuni.
- [ ] **S09.2 [TODO]** Colectează o singură dată per provider/instrument/interval; dedupe, single-flight, backoff, cache și provenance.
- [ ] **S09.3 [TODO]** Materializează snapshoturi comune și aplică drepturile de acces înainte de distribuție; persistență OHLC în storage potrivit.

### Acceptare

- Creșterea utilizatorilor nu multiplică liniar cererile către feed pentru aceleași simboluri.
- Timestamp sursă versus fetch, splits, sesiuni, null și fallback sunt testate; date neautorizate nu sunt publicate.

**Raport cerut:** `evidence/S09/REPORT.md`

**Următoarea acțiune:** Separă instrumentul după asset_class/exchange/currency și calendarul de sesiuni.

**Blocaje:** Niciun blocaj înregistrat.

## S10 — Worker separat și coadă durabilă

**Stare:** TODO · **Dependențe:** S04, S05, S09

**Puncte de intrare:** api/engine_service.py; core/engine.py; Dockerfile; viitor workers/

### Subpași

- [ ] **S10.1 [TODO]** Elimină pornirea motorului din fiecare instanță API; entrypoints API/ingestion/worker separate.
- [ ] **S10.2 [TODO]** Introdu SQS sau echivalent durabil, outbox DB, job idempotency, lease/heartbeat, retry, DLQ și anulare.
- [ ] **S10.3 [TODO]** Adaugă limite per workspace, priorități echitabile și clase de resurse pentru AI/backtest.

### Acceptare

- Două replici API nu pornesc două motoare sau două calcule identice.
- Worker oprit/restartat produce un singur rezultat și o singură debitare; coada nu pierde joburi.

**Raport cerut:** `evidence/S10/REPORT.md`

**Următoarea acțiune:** Elimină pornirea motorului din fiecare instanță API; entrypoints API/ingestion/worker separate.

**Blocaje:** Niciun blocaj înregistrat.

## S11 — Paper trading separat per utilizator

**Stare:** TODO · **Dependențe:** S07, S08, S10

**Puncte de intrare:** core/execution/; api/execution_service.py; core/execution_archive.py

### Subpași

- [ ] **S11.1 [TODO]** Mută conturile paper, pozițiile și evenimentele într-un ledger per workspace, cu sume Decimal.
- [ ] **S11.2 [TODO]** Introdu idempotency pentru ordine și tranzacții atomice de balanță/poziție/risc.
- [ ] **S11.3 [TODO]** Păstrează tradingul real și cheile broker dezactivate; automatizarea personală rămâne opt-in și limitată.

### Acceptare

- Nicio poziție sau balanță a operatorului nu este copiată la utilizatori noi.
- Retry concurent nu dublează ordine; balanțele se reconciliază; stop personal nu oprește motorul global.

**Raport cerut:** `evidence/S11/REPORT.md`

**Următoarea acțiune:** Mută conturile paper, pozițiile și evenimentele într-un ledger per workspace, cu sume Decimal.

**Blocaje:** Niciun blocaj înregistrat.

## S12 — Alerte și notificări multi-user

**Stare:** TODO · **Dependențe:** S08, S09, S10

**Puncte de intrare:** core/alerts.py; viitor notifications/; frontend/alerts

### Subpași

- [ ] **S12.1 [TODO]** Indexează alertele după instrument și evaluează doar simbolurile schimbate, nu N utilizatori × întregul univers.
- [ ] **S12.2 [TODO]** Folosește claim atomic, outbox și dedupe pentru alertă plus canal; aplică preferințe, quiet hours și dezabonare.
- [ ] **S12.3 [TODO]** Implementează in-app/email și web push ulterior; nu publica date personale în notificări comune.

### Acceptare

- Retry și consumatori concurenți nu dublează notificarea.
- Expirarea planului, ștergerea contului și schimbarea pragului invalidează evaluările învechite.

**Raport cerut:** `evidence/S12/REPORT.md`

**Următoarea acțiune:** Indexează alertele după instrument și evaluează doar simbolurile schimbate, nu N utilizatori × întregul univers.

**Blocaje:** Niciun blocaj înregistrat.

## S13 — Backtesting ca job cu buget

**Stare:** TODO · **Dependențe:** S10, S11

**Puncte de intrare:** api/actions_service.py; core/backtest_summary.py; core/strategy_backtester.py; frontend Backtest

### Subpași

- [ ] **S13.1 [TODO]** Transformă /run_backtest în acceptare 202 + job ID; polling/push separat.
- [ ] **S13.2 [TODO]** Versionează parametri, dataset, strategie și cost model; cache comun numai pentru rezultate deterministe fără date private.
- [ ] **S13.3 [TODO]** Impune limite CPU/RAM/timp, drepturi la date, cote pe plan și exporturi private.

### Acceptare

- R01–R03 rămân reparate; un backtest greu nu blochează login/plăți.
- Oprirea workerului, anularea și inputul invalid nu produc rezultat fals sau debitare dublă.

**Raport cerut:** `evidence/S13/REPORT.md`

**Următoarea acțiune:** Transformă /run_backtest în acceptare 202 + job ID; polling/push separat.

**Blocaje:** Niciun blocaj înregistrat.

## S14 — AI gateway și bugete

**Stare:** TODO · **Dependențe:** S09, S10

**Puncte de intrare:** core/ai/; api/ai_config_service.py; agents/pre_earnings_agent.py

### Subpași

- [ ] **S14.1 [TODO]** Separă analiza comună pe simbol de cererile personale; cache după model/prompt/data version.
- [ ] **S14.2 [TODO]** Adaugă rezervare atomică de credite, limite tokens/context/timp și reconciliere a costului real.
- [ ] **S14.3 [TODO]** Protejează împotriva prompt injection din știri, exfiltrării, tool calls neautorizate și cotelor globale depășite.

### Acceptare

- Niciun model nu poate schimba abonamente, roluri sau executa ordine prin text dintr-un articol.
- Cost per raport măsurat; indisponibilitatea AI este explicită și nu afectează citirea pieței.

**Raport cerut:** `evidence/S14/REPORT.md`

**Următoarea acțiune:** Separă analiza comună pe simbol de cererile personale; cache după model/prompt/data version.

**Blocaje:** Niciun blocaj înregistrat.

## S15 — Catalog unic și drepturi de acces

**Stare:** TODO · **Dependențe:** S02, S07

**Puncte de intrare:** PACKAGE_DRAFT.json; viitor core/billing/entitlements.py; api/v1/usage

### Subpași

- [ ] **S15.1 [TODO]** Implementează catalogul versionat independent de stripe_price_id.
- [ ] **S15.2 [TODO]** Verifică feature + quota server-side la enqueue, citire, export și abonarea la stream.
- [ ] **S15.3 [TODO]** Construiește usage ledger cu reserve/commit/release și reset după perioada de facturare, nu după ziua calendaristică.

### Acceptare

- Schimbarea planului în browser nu deblochează funcții.
- 100 cereri concurente nu depășesc cota; fiecare limită se vede în cont și are eroare clară.

**Raport cerut:** `evidence/S15/REPORT.md`

**Următoarea acțiune:** Implementează catalogul versionat independent de stripe_price_id.

**Blocaje:** Niciun blocaj înregistrat.

## S16 — Stripe Checkout în sandbox

**Stare:** TODO · **Dependențe:** S06, S15

**Puncte de intrare:** viitor api/billing/; frontend/pricing/; secrets sandbox

### Subpași

- [ ] **S16.1 [TODO]** Creează adaptor provider plăți, Customer legat de workspace și un Product și un Price de test la 1000 USD cents/lună, quantity=1.
- [ ] **S16.2 [TODO]** Folosește Checkout hosted pentru abonament lunar; Price ID provine din catalogul server-side.
- [ ] **S16.3 [TODO]** Adaugă idempotency și protecție împotriva abonamentelor duplicate; success URL este doar confirmare vizuală.

### Acceptare

- Niciun card nu traversează backendul propriu; prețul/moneda din client nu sunt autoritate.
- Test checkout reușit, abandonat, necesită autentificare suplimentară și checkout duplicat.

**Raport cerut:** `evidence/S16/REPORT.md`

**Următoarea acțiune:** Creează adaptor provider plăți, Customer legat de workspace și un Product și un Price de test la 1000 USD cents/lună, quantity=1.

**Blocaje:** Niciun blocaj înregistrat.

## S17 — Webhooks și reconciliere abonamente

**Stare:** TODO · **Dependențe:** S16

**Puncte de intrare:** viitor billing inbox/outbox, subscription state machine, worker reconciliation

### Subpași

- [ ] **S17.1 [TODO]** Verifică semnătura pe raw body; salvează evenimentul unic și confirmă rapid după persistare durabilă.
- [ ] **S17.2 [TODO]** Procesează asincron duplicări/dezordine și citește starea canonică de la Stripe; mapează trialing/active/past_due/unpaid/canceled/incomplete.
- [ ] **S17.3 [TODO]** Rulează reconciliere periodică; upgrade plătit înainte de acces, downgrade la termen, grace policy versionată și handling refund/dispute.

### Acceptare

- Webhook fals respins; 10 replay-uri nu acordă 10 cote.
- Eveniment vechi nu reactivează abonament anulat; cădere/recovery reconciliate fără acces pierdut sau gratuit nejustificat.

**Raport cerut:** `evidence/S17/REPORT.md`

**Următoarea acțiune:** Verifică semnătura pe raw body; salvează evenimentul unic și confirmă rapid după persistare durabilă.

**Blocaje:** Niciun blocaj înregistrat.

## S18 — Gestionare abonament și facturare

**Stare:** TODO · **Dependențe:** S17

**Puncte de intrare:** frontend/account/billing; api/billing/

### Subpași

- [ ] **S18.1 [TODO]** Integrează Customer Portal și ecranul plan/consum/următoarea plată.
- [ ] **S18.2 [TODO]** Implementează anulare la sfârșit de perioadă și reactivare, fără schimbare de pachet și fără ștergerea istoricului.
- [ ] **S18.3 [TODO]** Verifică taxe inclusive pentru totalul fix de 10 USD, facturi și drepturile consumatorilor înainte de live; moneda este USD.

### Acceptare

- Clientul își poate opri abonamentul și accesa facturile.
- Anularea păstrează accesul până la paid-through; după expirare se aplică retenția/exportul documentate, fără ștergere imediată.

**Raport cerut:** `evidence/S18/REPORT.md`

**Următoarea acțiune:** Integrează Customer Portal și ecranul plan/consum/următoarea plată.

**Blocaje:** Niciun blocaj înregistrat.

## S19 — Experiență SaaS și cele 13 taburi

**Stare:** TODO · **Dependențe:** S08, S11, S12, S13, S14, S15, S18, S38, S39, S40, S41, S42, S47, S48, S49

**Puncte de intrare:** frontend/app.js; frontend/cockpit.js; frontend/index.html; frontend/chart_workspace.js

### Subpași

- [ ] **S19.1 [TODO]** Adaugă landing, pricing cu oferta unică de 10 USD/lună, signup, onboarding și account center; păstrează funcțiile existente prin migrare incrementală.
- [ ] **S19.2 [TODO]** Mapează fiecare tab conform PACKAGES; elimină din UI client comenzile operator și terminal/coding-agent.
- [ ] **S19.3 [TODO]** Adaugă empty/loading/error/locked states, accesibilitate, mobile și i18n de bază; tur de activare.

### Acceptare

- Un utilizator nou își creează contul, plătește în sandbox, urmărește un simbol și creează o alertă fără ajutor.
- Matricea celor 13 taburi verificată în browser pentru abonamentul unic, toate stările de acces și rolurile.

**Raport cerut:** `evidence/S19/REPORT.md`

**Următoarea acțiune:** Adaugă landing, pricing cu oferta unică de 10 USD/lună, signup, onboarding și account center; păstrează funcțiile existente prin migrare incrementală.

**Blocaje:** Niciun blocaj înregistrat.

## S20 — Actualizări live cu fan-out controlat

**Stare:** TODO · **Dependențe:** S07, S09, S12, S15

**Puncte de intrare:** frontend API polling; viitor realtime service; Redis fan-out

### Subpași

- [ ] **S20.1 [TODO]** Înlocuiește pollingul agresiv cu SSE sau WebSocket pe subscriptions autorizate.
- [ ] **S20.2 [TODO]** Adaugă snapshot + delta, reconnect cursor, resync după goluri, heartbeat, backpressure și limite per conexiune.
- [ ] **S20.3 [TODO]** Separă canalele public-market de user-events și gatează fiecare abonare după licență/plan.

### Acceptare

- Reconnect storm nu multiplică fetchurile la provider; browser inactiv încetinește actualizările.
- Revocarea sesiunii/planului închide canalele protejate; un client lent nu blochează ceilalți.

**Raport cerut:** `evidence/S20/REPORT.md`

**Următoarea acțiune:** Înlocuiește pollingul agresiv cu SSE sau WebSocket pe subscriptions autorizate.

**Blocaje:** Niciun blocaj înregistrat.

## S21 — Consolă operator și suport

**Stare:** TODO · **Dependențe:** S07, S17, S23

**Puncte de intrare:** frontend Agents/Coding Agent; api/config_service.py; internal admin

### Subpași

- [ ] **S21.1 [TODO]** Mută agent health, management chei/modele, configurații globale și repair tooling în serviciu privat.
- [ ] **S21.2 [TODO]** Separă support/billing/operator și impune MFA, least privilege și audit append-only.
- [ ] **S21.3 [TODO]** Adaugă investigație după request/job/payment ID, cu date minime și acces temporar auditat.

### Acceptare

- Abonatul nu poate apela admin endpoints.
- Nicio parolă/card/cheie nu apare în UI suport sau loguri; schimbările administrative sunt trasabile.

**Raport cerut:** `evidence/S21/REPORT.md`

**Următoarea acțiune:** Mută agent health, management chei/modele, configurații globale și repair tooling în serviciu privat.

**Blocaje:** Niciun blocaj înregistrat.

## S22 — Confidențialitate și lifecycle cont

**Stare:** TODO · **Dependențe:** S05, S06, S17

**Puncte de intrare:** privacy service; storage; billing; audit logs

### Subpași

- [ ] **S22.1 [TODO]** Implementează export, ștergere, retenție și separarea obligațiilor de facturare de datele produsului.
- [ ] **S22.2 [TODO]** Revocă sesiuni și joburi la ștergere; include obiecte, cache, copii și politica pentru backupuri.
- [ ] **S22.3 [TODO]** Adaugă consimțământ separat pentru marketing și inventar procesatori/date către AI.

### Acceptare

- Exportul este complet pentru A și nu include B; ștergerea oprește accesul imediat.
- Retenția documentată este testabilă, inclusiv datele din backup și logurile obligatorii.

**Raport cerut:** `evidence/S22/REPORT.md`

**Următoarea acțiune:** Implementează export, ștergere, retenție și separarea obligațiilor de facturare de datele produsului.

**Blocaje:** Niciun blocaj înregistrat.

## S23 — Observabilitate și control cost

**Stare:** TODO · **Dependențe:** S03, S10, S15

**Puncte de intrare:** core/observability; API middleware; workers; cloud dashboards

### Subpași

- [ ] **S23.1 [TODO]** Adaugă logs structurate, traces și metrici fără PII/secrete.
- [ ] **S23.2 [TODO]** Măsoară latență, queue age, data freshness, errors, DB pool, cost/plan și provider quotas.
- [ ] **S23.3 [TODO]** Definește alarme și runbooks, circuit breakers și kill-switch pentru funcțiile scumpe, fără a bloca account/billing.

### Acceptare

- Un job lent se urmărește end-to-end; incidentul unei surse este vizibil în UI și alarme.
- Dashboard cost arată cheltuiala per plan și oprirea controlată la bugetul aprobat.

**Raport cerut:** `evidence/S23/REPORT.md`

**Următoarea acțiune:** Adaugă logs structurate, traces și metrici fără PII/secrete.

**Blocaje:** Niciun blocaj înregistrat.

## S24 — Infrastructură ca cod și CI/CD

**Stare:** TODO · **Dependențe:** S03, S04, S10

**Puncte de intrare:** Dockerfile; compose.production.yml; deploy/; .github/workflows/

### Subpași

- [ ] **S24.1 [TODO]** Construiește medii dev/staging/prod separate, rețea privată DB, TLS, secrets manager și roluri cloud minimale.
- [ ] **S24.2 [TODO]** Separă imagini API ușoară, ingestion și compute; scoate torch/mlflow din API dacă nu sunt necesare acolo.
- [ ] **S24.3 [TODO]** Pipeline build/test/scan/migration/canary/rollback, imagini immutable și backup înainte de schimbări incompatibile.

### Acceptare

- Staging se reconstruiește din IaC; nicio dependență de PC-ul proprietarului.
- API se scalează fără motor duplicat; rollback testat; datele și secretele nu intră în imagine.

**Raport cerut:** `evidence/S24/REPORT.md`

**Următoarea acțiune:** Construiește medii dev/staging/prod separate, rețea privată DB, TLS, secrets manager și roluri cloud minimale.

**Blocaje:** Niciun blocaj înregistrat.

## S25 — Staging integrat și E2E

**Stare:** TODO · **Dependențe:** S18, S19, S20, S22, S23, S24, S43, S44

**Puncte de intrare:** staging deployment; tests_e2e/; tests/frontend/

### Subpași

- [ ] **S25.1 [TODO]** Publică numai staging în domeniu separat, cu plăți test și date autorizate de test.
- [ ] **S25.2 [TODO]** Rulează matricea conturi × stări de abonament × roluri × taburi, inclusiv două sesiuni simultane și dispozitive mobile.
- [ ] **S25.3 [TODO]** Verifică mailuri test, checkout, webhook, expirare plan, quotas, joburi și account recovery.

### Acceptare

- Dovezi Playwright, contract tests și test payments pentru fiecare flux critic.
- Nu există mockuri ascunse sau TEST_MODE în buildul destinat live.

**Raport cerut:** `evidence/S25/REPORT.md`

**Următoarea acțiune:** Publică numai staging în domeniu separat, cu plăți test și date autorizate de test.

**Blocaje:** Niciun blocaj înregistrat.

## S26 — Importul contului proprietarului

**Stare:** TODO · **Dependențe:** S08, S11, S13, S25

**Puncte de intrare:** runtime JSON/SQLite; core/config.py; migration tools

### Subpași

- [ ] **S26.1 [TODO]** Fă backup și dry-run; separă setările operator de datele personale ale proprietarului.
- [ ] **S26.2 [TODO]** Importă watchlist/alerte/istoric/ledger numai într-un workspace explicit; preserve provenance și marchează date demo/test.
- [ ] **S26.3 [TODO]** Reconciliere counts/checksums/balanțe; comutare cu fereastră de scriere oprită și rollback documentat.

### Acceptare

- Import idempotent; reexecutarea nu dublează poziții sau alerte.
- Conturile noi încep goale; datele personale nu devin dataset public.

**Raport cerut:** `evidence/S26/REPORT.md`

**Următoarea acțiune:** Fă backup și dry-run; separă setările operator de datele personale ale proprietarului.

**Blocaje:** Niciun blocaj înregistrat.

## S27 — Review de securitate și izolare

**Stare:** TODO · **Dependențe:** S21, S22, S25, S26

**Puncte de intrare:** security test suite; threat model; independent review

### Subpași

- [ ] **S27.1 [TODO]** Execută atacuri IDOR/BOLA, CSRF, XSS, SSRF, webhooks false, job injection, upload/export și token replay.
- [ ] **S27.2 [TODO]** Testează cross-tenant pe DB/cache/objects/stream/workers și accesul operator.
- [ ] **S27.3 [TODO]** Obține review independent înainte de paid launch și documentează remedierea constatărilor critice.

### Acceptare

- Zero constatări P0/P1 deschise pe acces/date/plăți.
- Raport independent sau gate explicit neîndeplinit; AI nu se auto-certifică independent.

**Raport cerut:** `evidence/S27/REPORT.md`

**Următoarea acțiune:** Execută atacuri IDOR/BOLA, CSRF, XSS, SSRF, webhooks false, job injection, upload/export și token replay.

**Blocaje:** Niciun blocaj înregistrat.

## S28 — Teste de încărcare și cost pilot

**Stare:** TODO · **Dependențe:** S23, S25

**Puncte de intrare:** k6/Locust; staging; CAPACITY.md

### Subpași

- [ ] **S28.1 [TODO]** Rulează profilele CAPACITY pentru L0/L1: signup burst, market open, mixed reads/writes, jobs și webhooks.
- [ ] **S28.2 [TODO]** Adaugă test de durată, provider failure, slow clients și worker crash; validează quota auth/email.
- [ ] **S28.3 [TODO]** Măsoară cost total și per user/plan; compară cu bugetul ales, nu cu o presupunere de gratuitate.

### Acceptare

- SLO, zero încălcări tenant și buget îndeplinite pe sarcină reproductibilă.
- Raport conține hardware/număr replici/dataset/RPS/concurență/cost și limitările metodei.

**Raport cerut:** `evidence/S28/REPORT.md`

**Următoarea acțiune:** Rulează profilele CAPACITY pentru L0/L1: signup burst, market open, mixed reads/writes, jobs și webhooks.

**Blocaje:** Niciun blocaj înregistrat.

## S29 — Backup și recuperare

**Stare:** TODO · **Dependențe:** S24, S25, S26

**Puncte de intrare:** infra backup; operations runbooks

### Subpași

- [ ] **S29.1 [TODO]** Activează backup DB/PITR, versionare obiecte și criptare; definiție retenție.
- [ ] **S29.2 [TODO]** Restaurează într-un mediu separat și reconciliază ledger, abonamente, usage și jobs.
- [ ] **S29.3 [TODO]** Testează outage IdP/Stripe/provider/DB și revenirea fără dublări; confirmă RPO/RTO măsurate.

### Acceptare

- Restore drill reușit și timpi măsurați, nu doar backup configurat.
- Datele de plată se reconciliază cu Stripe după recovery și joburile nu sunt facturate de două ori.

**Raport cerut:** `evidence/S29/REPORT.md`

**Următoarea acțiune:** Activează backup DB/PITR, versionare obiecte și criptare; definiție retenție.

**Blocaje:** Niciun blocaj înregistrat.

## S30 — Pilot privat cu 20–50 utilizatori

**Stare:** TODO · **Dependențe:** S01, S02, S25, S27, S28, S29

**Puncte de intrare:** analytics privacy-conscious; feedback; support log

### Subpași

- [ ] **S30.1 [TODO]** Pregătește invitații și onboarding; expediază numai când proprietarul autorizează contactarea.
- [ ] **S30.2 [TODO]** Rulează pilot 2–4 săptămâni cu participanți reali, limite mici și suport; fără promisiuni de profit.
- [ ] **S30.3 [TODO]** Măsoară activarea, retenția, înțelegerea datelor, erorile, costurile și willingness-to-pay.

### Acceptare

- Raport de utilizare reală, separat de teste și conturi interne.
- Nicio eroare de izolare/facturare și pragurile convenite sunt atinse sau pilotul este extins.

**Raport cerut:** `evidence/S30/REPORT.md`

**Următoarea acțiune:** Pregătește invitații și onboarding; expediază numai când proprietarul autorizează contactarea.

**Blocaje:** Niciun blocaj înregistrat.

## S31 — Lansare abonamente plătite

**Stare:** TODO · **Dependențe:** S30

**Puncte de intrare:** production deployment; billing live; release checklist

### Subpași

- [ ] **S31.1 [TODO]** Verifică prețul fix de 10 USD/lună în landing, Checkout și factură; finalizează taxele, entitatea, contractele feed și bugetele.
- [ ] **S31.2 [TODO]** Configurează Stripe live separat, final review și canary 10 clienți → 100 → 1000.
- [ ] **S31.3 [TODO]** Verifică plăți/reînnoiri/anulări reale autorizate, suport, status page și posibilitatea opririi înscrierilor.

### Acceptare

- Toate gate-urile G-PUBLIC sunt îndeplinite și documentate.
- Încasările se reconciliază; un abonat poate recupera contul, utiliza planul și anula fără intervenție manuală.

**Raport cerut:** `evidence/S31/REPORT.md`

**Următoarea acțiune:** Verifică prețul fix de 10 USD/lună în landing, Checkout și factură; finalizează taxele, entitatea, contractele feed și bugetele.

**Blocaje:** Niciun blocaj înregistrat.

## S32 — Validare comercială și retenție

**Stare:** TODO · **Dependențe:** S31

**Puncte de intrare:** product analytics; billing reports

### Subpași

- [ ] **S32.1 [TODO]** Urmărește cohorts 30/60/90 zile, churn, suport și marjă după costuri.
- [ ] **S32.2 [TODO]** Experimentează onboarding, prezentarea dovezilor și mesaje de valoare cu grupuri controlate; prețul rămâne 10 USD și funcțiile sunt aceleași.
- [ ] **S32.3 [TODO]** Prioritizează problemele utilizatorilor înainte de funcții noi și estimează capacitatea pe trafic real.

### Acceptare

- Conversie, reînnoire și costuri verificate; motive reale de cumpărare și anulare documentate pentru abonamentul de 10 USD.
- Nu se raportează utilizatori testați/simulați ca utilizatori reali sau venituri fictive.

**Raport cerut:** `evidence/S32/REPORT.md`

**Următoarea acțiune:** Urmărește cohorts 30/60/90 zile, churn, suport și marjă după costuri.

**Blocaje:** Niciun blocaj înregistrat.

## S33 — Capacitate 10.000 conturi

**Stare:** TODO · **Dependențe:** S28, S31

**Puncte de intrare:** CAPACITY.md; cloud autoscaling; DB query plans

### Subpași

- [ ] **S33.1 [TODO]** Rulează L2, distribuție realistă de consum și simboluri; index tuning și pool limits.
- [ ] **S33.2 [TODO]** Autoscale după queue age/connections/RPS, nu doar CPU.
- [ ] **S33.3 [TODO]** Verifică fan-out, drepturi feed și cost la vârf; crește limitele de înscriere numai după gate.

### Acceptare

- Raport L2 trece SLO și cost; creșterea conturilor nu dublează calculele comune.
- Test recovery și plăți la market-open peak.

**Raport cerut:** `evidence/S33/REPORT.md`

**Următoarea acțiune:** Rulează L2, distribuție realistă de consum și simboluri; index tuning și pool limits.

**Blocaje:** Niciun blocaj înregistrat.

## S34 — Capacitate 100.000 conturi

**Stare:** TODO · **Dependențe:** S32, S33

**Puncte de intrare:** DB/storage/realtime/operations

### Subpași

- [ ] **S34.1 [TODO]** Rulează L3 și reconnect storm; replci read-only numai unde lagul este acceptabil.
- [ ] **S34.2 [TODO]** Separă hot/cold storage și servicii realtime; partiționează usage/ledger după dovezi.
- [ ] **S34.3 [TODO]** Exersează incidente și cote cloud/IdP/email/feed; optimizează costuri reale.

### Acceptare

- Raport L3 și restore drill la volumul respectiv.
- Autorizarea și billing rămân pe citiri consistente, nu pe replică întârziată.

**Raport cerut:** `evidence/S34/REPORT.md`

**Următoarea acțiune:** Rulează L3 și reconnect storm; replci read-only numai unde lagul este acceptabil.

**Blocaje:** Niciun blocaj înregistrat.

## S35 — Capacitate 1.000.000 conturi

**Stare:** TODO · **Dependențe:** S34

**Puncte de intrare:** CAPACITY.md; load generators distributed; vendor quotas

### Subpași

- [ ] **S35.1 [TODO]** Rulează L4: un milion rânduri user, 20k conexiuni și 10k RPS ipoteză de vârf.
- [ ] **S35.2 [TODO]** Benchmark separat signup/auth/recovery bursts și provider fan-out cu cote aprobate.
- [ ] **S35.3 [TODO]** Evaluează celule/sharding/regionalizare numai dacă profilele arată nevoie; cost și suport incluse.

### Acceptare

- Capacitatea este afirmată numai după un test reprezentativ și raport complet.
- Nu se confundă milionul de conturi cu un milion de conexiuni simultane.

**Raport cerut:** `evidence/S35/REPORT.md`

**Următoarea acțiune:** Rulează L4: un milion rânduri user, 20k conexiuni și 10k RPS ipoteză de vârf.

**Blocaje:** Niciun blocaj înregistrat.

## S36 — Capacitate de ordinul milioanelor

**Stare:** TODO · **Dependențe:** S35

**Puncte de intrare:** capacity cells; regional design; contracts/operations

### Subpași

- [ ] **S36.1 [TODO]** Rulează L5 pentru 3M conturi și 60k conexiuni; extinde pe regiuni/celule dacă măsurătorile justifică.
- [ ] **S36.2 [TODO]** Testează evacuarea unei celule, billing global fără split-brain și izolarea incidentelor.
- [ ] **S36.3 [TODO]** Actualizează limita declarată, bugetele, SLA și capacitatea de suport pe baza testelor.

### Acceptare

- Raport L5 cu 30k RPS țintă și 60k streams, plus cost/retention/DR.
- Pentru milioane simultan se deschide un proiect de capacitate nou; nu este declarat implicit realizat.

**Raport cerut:** `evidence/S36/REPORT.md`

**Următoarea acțiune:** Rulează L5 pentru 3M conturi și 60k conexiuni; extinde pe regiuni/celule dacă măsurătorile justifică.

**Blocaje:** Niciun blocaj înregistrat.

## S37 — Design system și păstrarea celor 13 taburi

**Stare:** TODO · **Dependențe:** S00

**Puncte de intrare:** frontend/index.html; frontend/styles_variables.css; frontend/styles_layout.css

### Subpași

- [ ] **S37.1 [TODO]** Inventariază și păstrează cele 13 taburi, subtaburile și fiecare funcție; Pre-Earnings, Agents, Capital și Cockpit rămân spații distincte cu acces direct.
- [ ] **S37.2 [TODO]** Definește tipografie, culori semantice, spațiere, densitate, focus, componente și stări; realizează prototipuri desktop/mobile conform DESIGN_AND_DECISIONS.md.
- [ ] **S37.3 [TODO]** Verifică traseele principale cu tastatura, contrastul și ecrane înguste; documentează diferențele și criteriile vizuale.

### Acceptare

- Mapping complet al celor 13 taburi, fără eliminarea funcțiilor client sau expunerea operatorului.
- Prototipuri și inventar de componente existente; fiecare alegere are scop și criteriu verificabil.

**Raport cerut:** `evidence/S37/REPORT.md`

**Următoarea acțiune:** Inventariază și păstrează cele 13 taburi, subtaburile și fiecare funcție; Pre-Earnings, Agents, Capital și Cockpit rămân spații distincte cu acces direct.

**Blocaje:** Niciun blocaj înregistrat.

## S38 — Acasă personal și activare rapidă

**Stare:** TODO · **Dependențe:** S37, S08, S09

**Puncte de intrare:** frontend/cockpit.js; frontend/app.js; viitor home/onboarding API

### Subpași

- [ ] **S38.1 [TODO]** Implementează piața acum, impactul asupra watchlistului, oportunități explicate și schimbări de la ultima vizită, cu stare fără noutăți.
- [ ] **S38.2 [TODO]** Construiește onboarding opțional: obiectiv, 5 simboluri, prima alertă; adaugă căutare globală și ajutor contextual.
- [ ] **S38.3 [TODO]** Măsoară timpul până la primul rezultat util și verifică date lipsă, vizite noi, preferințe și mobile fără recomandări fabricate.

### Acceptare

- Schimbările folosesc snapshotul vizitei utilizatorului și surse datate, nu regenerare arbitrară AI.
- Un cont nou poate ajunge la o listă personală și o alertă fără tur obligatoriu; evenimentele de activare sunt definite.

**Raport cerut:** `evidence/S38/REPORT.md`

**Următoarea acțiune:** Implementează piața acum, impactul asupra watchlistului, oportunități explicate și schimbări de la ultima vizită, cu stare fără noutăți.

**Blocaje:** Niciun blocaj înregistrat.

## S39 — Card explicabil și semantică verificată a scorurilor

**Stare:** TODO · **Dependențe:** S37, S09, S14

**Puncte de intrare:** frontend/cockpit.js; frontend/app.js; contracte backend scor/proveniență

### Subpași

- [ ] **S39.1 [TODO]** Definește contractul cardului: motiv, dovezi, contraargumente, invalidare, orizont, sursă și date lipsă; separă fapt/calcul/interpretare.
- [ ] **S39.2 [TODO]** Implementează afișarea separată a calității datelor, scorului strategiei, istoricului și interpretării AI în scanner, radar și liste.
- [ ] **S39.3 [TODO]** Verifică fiecare câmp până la sursă și cazurile de date vechi/conflictuale; nu afișa probabilități fără calibrare și orizont documentate.

### Acceptare

- Același simbol/scenariu are aceeași semantică între taburi; 85/100 nu este etichetat 85% șansă.
- Cardurile au dovezi accesibile, argumente contrare și stare insuficient-date; AI nu completează fapte lipsă.

**Raport cerut:** `evidence/S39/REPORT.md`

**Următoarea acțiune:** Definește contractul cardului: motiv, dovezi, contraargumente, invalidare, orizont, sursă și date lipsă; separă fapt/calcul/interpretare.

**Blocaje:** Niciun blocaj înregistrat.

## S40 — Pagina simbolului și comparații coerente

**Stare:** TODO · **Dependențe:** S39, S08

**Puncte de intrare:** frontend/chart_workspace.js; frontend/hotlist_dossier.js; viitor symbol workspace

### Subpași

- [ ] **S40.1 [TODO]** Unește graficul, prețul/sursa/sesiunea, știrile, evenimentele, scenariile și alertele în aceeași pagină accesibilă din toate listele.
- [ ] **S40.2 [TODO]** Adaugă comparație între 2–3 simboluri cu aceeași perioadă, monedă explicită, benchmark și semnalarea datelor incomparabile.
- [ ] **S40.3 [TODO]** Verifică deep links, back navigation, selecții salvate, mobil și consistența cotațiilor; păstrează atribuirea surselor.

### Acceptare

- Prețul din card și grafic este reconciliat prin timestamp, sesiune și sursă, nu egalizat artificial.
- Comparația nu amestecă randamente, prețuri absolute sau perioade diferite fără etichete explicite.

**Raport cerut:** `evidence/S40/REPORT.md`

**Următoarea acțiune:** Unește graficul, prețul/sursa/sesiunea, știrile, evenimentele, scenariile și alertele în aceeași pagină accesibilă din toate listele.

**Blocaje:** Niciun blocaj înregistrat.

## S41 — Jurnal de decizii și evaluare fără selecție convenabilă

**Stare:** TODO · **Dependențe:** S39, S11, S13

**Puncte de intrare:** viitor decision_journal schema/API; paper ledger; backtest reports

### Subpași

- [ ] **S41.1 [TODO]** Inventariază Decision Ledger și registrele existente; migrează și extinde snapshotul personal cu surse, versiune și invalidare, fără a crea istoric paralel.
- [ ] **S41.2 [TODO]** Evaluează rezultatul la orizontul stabilit, inclusiv cazuri nereușite, anulate și date lipsă; separă paper de real și include costuri simulate.
- [ ] **S41.3 [TODO]** Testează lipsa datelor viitoare, deduplicarea și exportul; afișează eșantionul, perioada și limitele, fără rată de succes din selecții favorabile.

### Acceptare

- Rezultatele sunt reproductibile din snapshot și metodologie; istoricul nu se rescrie după rezultat.
- Evaluarea UI măsoară înțelegerea riscului separat de profitul simulat; lipsa datelor nu devine rezultat pozitiv.

**Raport cerut:** `evidence/S41/REPORT.md`

**Următoarea acțiune:** Inventariază Decision Ledger și registrele existente; migrează și extinde snapshotul personal cu surse, versiune și invalidare, fără a crea istoric paralel.

**Blocaje:** Niciun blocaj înregistrat.

## S42 — Digest relevant și alerte cu control asupra zgomotului

**Stare:** TODO · **Dependențe:** S12, S38, S40

**Puncte de intrare:** api notifications; frontend alerts/preferences; shared digest worker

### Subpași

- [ ] **S42.1 [TODO]** Adaugă rezumatul personal zilnic/săptămânal și alerte pentru schimbarea scenariului, cu explicație și link la sursă.
- [ ] **S42.2 [TODO]** Implementează grupare, deduplicare, severitate, quiet hours, timezone, snooze și preferințe pe canal; separă marketingul de notificările de serviciu.
- [ ] **S42.3 [TODO]** Verifică livrări repetate, latență, schimbare fus orar și dezabonare; măsoară alerte utile versus ignorate/dezactivate.

### Acceptare

- Nicio alertă duplicată la retry; utilizatorul controlează frecvența și canalul.
- Emailul personal nu expune date altui cont; folosirea insightului comun nu generează AI separat la fiecare citire.

**Raport cerut:** `evidence/S42/REPORT.md`

**Următoarea acțiune:** Adaugă rezumatul personal zilnic/săptămânal și alerte pentru schimbarea scenariului, cu explicație și link la sursă.

**Blocaje:** Niciun blocaj înregistrat.

## S43 — Landing, demo și ofertă unică verificabilă

**Stare:** TODO · **Dependențe:** S18, S38, S39

**Puncte de intrare:** frontend/landing.html; pricing; demo fixtures/licensed snapshots; auth/billing

### Subpași

- [ ] **S43.1 [TODO]** Construiește landing cu problema, trei rezultate demonstrabile, capturi reale, exemplu explicabil, preț 10 USD/lună și FAQ fără promisiuni de profit.
- [ ] **S43.2 [TODO]** Pregătește demo fără cont cu date redistribuibile și datate; implementează evaluarea de 7 zile fără card numai după S02, cu limite comune și anti-abuz.
- [ ] **S43.3 [TODO]** Verifică traseul demo → signup → activare → checkout, totalul facturii, limitele afișate și anularea; nicio recenzie inventată sau numărătoare falsă.

### Acceptare

- Toate funcțiile client sunt incluse în aceeași ofertă; landing, catalog și checkout au 1000 cenți USD/lună.
- Demo nu expune runtime-ul proprietarului; funcțiile neimplementate nu sunt prezentate ca disponibile.

**Raport cerut:** `evidence/S43/REPORT.md`

**Următoarea acțiune:** Construiește landing cu problema, trei rezultate demonstrabile, capturi reale, exemplu explicabil, preț 10 USD/lună și FAQ fără promisiuni de profit.

**Blocaje:** Niciun blocaj înregistrat.

## S44 — Validare UX și instrumentarea conversiei

**Stare:** TODO · **Dependențe:** S38, S39, S40, S41, S42, S43, S47, S48, S49

**Puncte de intrare:** analytics events; frontend tests; evidence/S44/REPORT.md

### Subpași

- [ ] **S44.1 [TODO]** Definește evenimentele și denominatorii conform GROWTH.md; separă conturi reale, teste, boți, evaluări și plăți confirmate.
- [ ] **S44.2 [TODO]** Pregătește și execută cu minimum 5 participanți recrutați autorizat sarcinile găsire dovadă/risc, comparație, alertă și anulare; salvează probleme anonimizate.
- [ ] **S44.3 [TODO]** Corectează blocajele observate și repetă sarcinile afectate; verifică înțelegerea scorurilor și accesibilitatea pe desktop/mobile.

### Acceptare

- Țintă inițială: 4/5 finalizează sarcinile esențiale fără ajutor; orice confuzie critică scor/probabilitate sau preț este rezolvată.
- Raport cu timp, erori și limitele eșantionului; nu se afirmă creștere de conversie fără comparație validă.

**Raport cerut:** `evidence/S44/REPORT.md`

**Următoarea acțiune:** Definește evenimentele și denominatorii conform GROWTH.md; separă conturi reale, teste, boți, evaluări și plăți confirmate.

**Blocaje:** Niciun blocaj înregistrat.

## S45 — Experimente de atragere cu buget măsurabil

**Stare:** TODO · **Dependențe:** S31, S32

**Puncte de intrare:** GROWTH.md; content assets; analytics acquisition reports

### Subpași

- [ ] **S45.1 [TODO]** Pregătește ghiduri utile și demonstrații video din produs, cu surse și rezultate complete; publică doar în canale autorizate.
- [ ] **S45.2 [TODO]** Testează secvențial conținut educațional, comunități relevante și colaborări mici, cu costuri/atribuire; reclamele necesită buget separat.
- [ ] **S45.3 [TODO]** Compară abonați activați, reînnoiri, rambursări și CAC pe cohorte; oprește canalele care consumă buget fără retenție conform regulilor GROWTH.

### Acceptare

- Niciun mesaj către terți, reclamă plătită sau publicare fără autorizarea canalului și bugetului.
- Rezultate documentate cu numitor și interval, fără trafic sau venituri fictive; prețul nu se schimbă.

**Raport cerut:** `evidence/S45/REPORT.md`

**Următoarea acțiune:** Pregătește ghiduri utile și demonstrații video din produs, cu surse și rezultate complete; publică doar în canale autorizate.

**Blocaje:** Niciun blocaj înregistrat.

## S46 — Recomandări voluntare și retenție pe valoare

**Stare:** TODO · **Dependențe:** S32, S45

**Puncte de intrare:** sharing templates; account lifecycle; product analytics

### Subpași

- [ ] **S46.1 [TODO]** Pregătește partajarea opt-in a șabloanelor de analiză fără poziții personale sau date nelicențiate; orice stimulent rămâne propunere separată.
- [ ] **S46.2 [TODO]** Adaugă feedback după utilizare, raport lunar al activității utile și solicitare opțională a motivului anulării, fără blocarea anulării.
- [ ] **S46.3 [TODO]** Evaluează reînnoiri 30/60/90 zile, marja și reclamațiile; păstrează numai experimentele cu beneficii observate.

### Acceptare

- Linkurile partajate sunt revocabile și nu includ date private implicit; testimoniale numai reale și cu acord.
- Nicio schemă de upgrade sau cost suplimentar; raportul de valoare nu echivalează activitatea cu profit financiar.

**Raport cerut:** `evidence/S46/REPORT.md`

**Următoarea acțiune:** Pregătește partajarea opt-in a șabloanelor de analiză fără poziții personale sau date nelicențiate; orice stimulent rămâne propunere separată.

**Blocaje:** Niciun blocaj înregistrat.

## S47 — Pre-Earnings separat, păstrat integral

**Stare:** TODO · **Dependențe:** S37, S39, S09

**Puncte de intrare:** PRODUCT_PRESERVATION.md; core/agent_manager.py; agents/pre_earnings_agent.py; core/capital_manager.py; frontend/agents.js

### Subpași

- [ ] **S47.1 [TODO]** Mapează calendar, score, Top 10, Pulse, dosar, peer read-through, predicții și track record până la sursă; inventar înainte/după.
- [ ] **S47.2 [TODO]** Îmbunătățește pagina separată cu subtaburi și stare de eligibilitate; leagă event_id/decision_id de explicații și Capital fără a amesteca predicția cu execuția.
- [ ] **S47.3 [TODO]** Verifică identitatea evenimentelor, prețuri/splituri, ferestre, LONG/SHORT, denominatori și toate funcțiile vechi.

### Acceptare

- Nicio funcție Pre-Earnings pierdută; diferențele față de original sunt documentate.
- Istoricul și Top 10 au proveniență verificată; valori suspecte sunt investigate, nu folosite în marketing.

**Raport cerut:** `evidence/S47/REPORT.md`

**Următoarea acțiune:** Mapează calendar, score, Top 10, Pulse, dosar, peer read-through, predicții și track record până la sursă; inventar înainte/după.

**Blocaje:** Niciun blocaj înregistrat.

## S48 — Agenți personali și automatizare paper completă

**Stare:** TODO · **Dependențe:** S11, S14, S15, S37

**Puncte de intrare:** PRODUCT_PRESERVATION.md; core/agent_manager.py; agents/pre_earnings_agent.py; core/capital_manager.py; frontend/agents.js

### Subpași

- [ ] **S48.1 [TODO]** Mapează rolurile de analiză, AI/Critic/debate, Risk și Execution; separă politicile personale de mentenanța globală fără a elimina agenți.
- [ ] **S48.2 [TODO]** Implementează Agents separat, configurare de risc per workspace, activare/oprire personală și trace de execuție; păstrează controalele și protecția ieșirilor.
- [ ] **S48.3 [TODO]** Testează două conturi, retry, veto, AI indisponibil, stop bot, cote și expirare abonament în timpul pozițiilor deschise; rezervă resurse pentru ieșiri.

### Acceptare

- Oprirea/configurarea unui client nu schimbă botul ori politica altuia; ordinele sunt idempotente.
- Toate rolurile reale sunt explicate corect; nu se prezintă fiecare worker drept LLM sau trader independent.

**Raport cerut:** `evidence/S48/REPORT.md`

**Următoarea acțiune:** Mapează rolurile de analiză, AI/Critic/debate, Risk și Execution; separă politicile personale de mentenanța globală fără a elimina agenți.

**Blocaje:** Niciun blocaj înregistrat.

## S49 — Traseu complet și comparație reproductibilă

**Stare:** TODO · **Dependențe:** S47, S48, S40, S41

**Puncte de intrare:** PRODUCT_PRESERVATION.md; core/agent_manager.py; agents/pre_earnings_agent.py; core/capital_manager.py; frontend/agents.js

### Subpași

- [ ] **S49.1 [TODO]** Leagă dovezile candidatului, AI/Critic/debate, risc, ordin, poziție și rezultat prin identificatori și timp, inclusiv refuzurile și fallbackurile.
- [ ] **S49.2 [TODO]** Construiește comparație între politici/strategii în paper pe aceleași date și costuri; afișează eșantion, drawdown și P/L net fără atribuire falsă per agent.
- [ ] **S49.3 [TODO]** Verifică un caz acceptat, unul respins și o ieșire post-earnings din UI până la ledger; testează anti-lookahead și lipsa datelor.

### Acceptare

- Trace-ul reflectă ordinea efectivă și rolurile rulate; niciun consens inventat.
- Comparațiile și demo-ul păstrează rezultatele nereușite; toate funcțiile originale au verificare de regresie.

**Raport cerut:** `evidence/S49/REPORT.md`

**Următoarea acțiune:** Leagă dovezile candidatului, AI/Critic/debate, risc, ordin, poziție și rezultat prin identificatori și timp, inclusiv refuzurile și fallbackurile.

**Blocaje:** Niciun blocaj înregistrat.
