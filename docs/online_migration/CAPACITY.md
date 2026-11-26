# Capacitate, cost și dovada scalării

**Toate valorile de mai jos sunt ținte de test propuse, nu capacitatea măsurată a aplicației actuale.** În acest moment produsul a avut un singur utilizator local; nu există dovadă de încărcare SaaS.

## Profil de referință

Distincții obligatorii: conturi înregistrate, utilizatori activi lunar (MAU), utilizatori activi zilnic (DAU), conexiuni simultane, cereri HTTP/secundă, subscriptions pe simbol, evenimente/secundă și joburi/secundă. Cotele IdP se referă la operații de autentificare, nu doar la numărul de conturi. Costul identity poate depinde de MAU și funcții, nu de câți rânduri avem în users. [Cognito quotas](https://docs.aws.amazon.com/cognito/latest/developerguide/quotas.html), [Cognito pricing](https://aws.amazon.com/cognito/pricing/)

| Nivel | Conturi în DB | Conexiuni simultane țintă | HTTP RPS țintă | Etapă |
|---|---:|---:|---:|---|
| L0 | 100 | 20 | 10 | Pilot mic |
| L1 | 1.000 | 200 | 100 | Primele abonamente |
| L2 | 10.000 | 1.000 | 500 | S33 |
| L3 | 100.000 | 5.000 | 2.500 | S34 |
| L4 | 1.000.000 | 20.000 | 10.000 | S35 |
| L5 | 3.000.000 | 60.000 | 30.000 | S36 |

RPS-ul din tabel rezultă din ipoteza de stress 0,5 cereri HTTP/secundă per utilizator conectat, separat de streamuri. Nu toate cererile trebuie să ajungă în DB. Acest profil se înlocuiește cu distribuția reală după pilot. Pentru L4, ipoteza alternativă de 10% DAU înseamnă 100k persoane/zi; vârful de 20k simultan este o ipoteză exigentă pentru deschiderea pieței, nu o prognoză.

Exemplu de fan-out: 20k conexiuni × un batch la 5 secunde = 4k mesaje/secundă. La 1 KB/batch, payloadul este ~4 MB/secundă (~14,4 GB/oră de vârf), înainte de overhead. La 20 instrumente urmărite/conexiune avem 400k relații de abonare. Un quote comun nu trebuie cerut providerului de 400k ori. Dimensiunile batchurilor și rata reală depind de feed și licență.

Exemplu storage: un milion de conturi × 40 watchlist items în medie = 40 milioane rânduri. Testează indecșii, paginarea și ștergerea unui cont pe acest volum, nu pe 10 utilizatori. Nu instala automat câte un container, un cron sau un fișier per utilizator.

## SLO propuse pentru pilotul plătit

- API citiri cache: p95 ≤ 300 ms; scrieri obișnuite: p95 ≤ 700 ms, măsurate server-side cu dimensiunea payloadului documentată.
- Erori 5xx sub 0,5% la sarcina de referință; timeouturile furnizorilor nu trebuie să devină blocare globală.
- Acceptare job p95 ≤ 500 ms; timpul de așteptare și execuție au SLO separat per clasă. Un backtest greu nu promite finalizare în 500 ms.
- Propagare eveniment de piață de la ingestion la client p95 ≤ 2 s în profilul ales; aceasta nu promite că providerul oferă date nedecalate.
- Actualizare drepturi după evenimentul de plată valid p95 ≤ 60 s; reconciliere ratări ≤ 15 min, obiective care trebuie confirmate sub trafic.
- Disponibilitate lunară țintă 99,9% pentru serviciul plătit, cu dependențe și ferestre definite. Nu este un SLA oferit înainte de operare măsurată.
- RPO ≤ 15 min și RTO ≤ 60 min pentru început, validate prin restore drill. Stripe rămâne sursa externă pentru reconcilierea plăților; nu acceptăm dublarea debitărilor sau a ordinelor la recovery.
- Zero scurgeri cross-tenant, zero dublări de debitare/ordine și zero depășiri de quota în testele concurente.

## Scenarii obligatorii

1. Mix 80% citiri, 15% scrieri personale și 5% account/billing, apoi mix derivat din pilot.
2. Deschiderea pieței: login burst, simbol popular și multe alerte declanșate simultan.
3. Reconnect storm și clienți lenți; testează limitele conexiunilor, nu doar HTTP throughput.
4. Joburi AI/backtest până la cota comună a abonamentului, cu distribuție inegală și utilizator abuziv.
5. Duplicări și reordonări webhook, outage Stripe, IdP cu rate limit și val de resetări parole.
6. DB failover, replica lag, Redis restart, worker omorât în mijlocul jobului și provider cu 429.
7. Minimum 60 minute steady-state, burst 3× timp de 10 minute și 24 ore soak pentru L0/L1; la pragurile mari se dimensionează campania și se raportează durata reală.
8. Conturi sintetice distincte, seturi private distincte, instrumente distribuite; nu prezenta un benchmark pe un singur răspuns static drept performanță a produsului complet.

Nu face teste masive pe API-uri de furnizor neautorizate. Generatorul simulează fluxuri conforme contractului, iar câteva probe controlate validează integrarea reală. Raportul separă clar furnizor simulat de furnizor live. Cotele cloud și email se solicită din timp; autoscaling nu elimină limitele externe.

## Model de cost — fără prețuri inventate

Cost lunar = bază cloud + DB/storage/backups + cache/stream/egress + identitate/email + licențe date + AI personal + compute backtest + comisioane plăți + observabilitate + suport + taxe aplicabile. Egress-ul și drepturile datelor pot depăși costul serverelor.

Pentru abonamentul unic de 10 USD se măsoară: venit net după taxele relevante, cost mediu și p95 pentru AI/backtest/notificări, parte alocată din costul comun, marjă de contribuție și costul utilizatorilor foarte activi. Un milion de conturi gratuite și inactive are o economie diferită de un milion de abonați activi.

S02 produce trei scenarii: consum 25% din cote, consum mediu observat și consum 100% din cote. Prețul rămâne 10 USD. Consumul contractual trebuie susținut prin cache, calcul comun și cote comune transparente validate înainte de vânzare; dacă nu este posibil, se blochează lansarea și se raportează costul, fără scumpire sau eliminare ascunsă a funcțiilor. Nu ne bazăm pe ideea că nimeni nu va folosi ce a cumpărat.

Bugetele rămân decizie D02. Înainte de fiecare deployment plătit se salvează o estimare cu tarife verificate în ziua respectivă, regiune, mărimi instanțe și marjă de rezervă. La milioane de utilizatori sunt necesare oferte comerciale și cote confirmate; un număr generic de euro/lună în acest document ar fi înșelător.

## După un prag depășit

Optimizează interogări/cache și separă workloaduri înainte de sharding. Read replicas sunt pentru analytics/citiri tolerante la lag, nu pentru autoritatea de abonament sau bani. Celulele/partiționarea se introduc când profilul arată necesitatea. Fiecare celulă trebuie să aibă identitate de routing stabilă, limite de incident și procedură de recovery. Multi-region active-active pentru ledger/plăți nu se adaugă fără un design de consistență verificat.
