# Predare către următorul AI

**Regulă actuală: păstrăm toate funcțiile și taburile, inclusiv Pre-Earnings, Agents, Capital și AI Cockpit ca spații distincte.** [PRODUCT_PRESERVATION.md](PRODUCT_PRESERVATION.md) documentează traseele verificate și are prioritate față de propunerile inițiale de simplificare.

## Prompt de continuare

> Lucrează la Market Signals V9, în acest repository. Citește `docs/online_migration/README.md`, apoi PLAN, ARCHITECTURE, PACKAGES, CAPACITY, TASKS, STATUS și PROGRESS. Planul a fost redactat la 2026-09-06; nu presupune că aplicația SaaS a fost implementată. Verifică starea reală înainte de lucru. Păstrează modificările locale ale celorlalți agenți. Continuă primul pas disponibil din TASKS.json, începând cu S00 dacă nu există dovezi ulterioare. Implementează incremental, testează și raportează fiecare subpas. Actualizează TASKS.json și PROGRESS.md, salvează raportul și dovezile în evidence/Sxx/, apoi regenerează STATUS.md și rulează validate_plan.py. Nu declara un pas DONE fără dovezi și fără dependențe îndeplinite. Proprietarul a decis un singur abonament de 10 USD/lună cu toate funcțiile client incluse; citește și DESIGN_AND_DECISIONS.md și GROWTH.md. S37–S44 sunt dependențe de produs înainte de pilot, S45–S46 sunt experimente după lansare. Nu inventa prețuri, aprobări, rezultate de teste, drepturi asupra datelor sau capacitate pentru milioane de utilizatori. Planificarea nu autorizează cheltuieli, contactarea clienților ori publicarea serviciului. Execută lucrările locale autorizate și documentează deciziile externe necesare; continuă pașii independenți dacă un pas este blocat.

## Algoritm la fiecare sesiune

1. Citește instrucțiunile repository-ului, HEAD, statusul Git și rapoartele ultimelor sesiuni. Compară cu BASELINE.json; acesta este o fotografie istorică, nu o comandă de revenire la un commit.
2. Verifică dovezile ultimului pas DONE și orice IN_PROGRESS. Nu relua orbește lucrări deja făcute și nu considera un simplu checkbox dovadă de funcționare.
3. Alege un pas TODO cu toate dependențele DONE sau reia unul IN_PROGRESS. Dependențele explicite sunt autoritatea; numărul pasului nu impune singur ordinea. S21 depinde intenționat și de S23.
4. Marchează IN_PROGRESS, owner și started_at înainte de modificări. Completează acțiunea exactă în next_action.
5. Execută fiecare subpas în ordine. Notează fișierele, comenzile exacte, mediul, rezultatele și limitările. Salvează dovezi fără secrete sau date personale.
6. Verifică fiecare criteriu de acceptare prin rezultat observabil. Pentru pași de decizie/documentare, dovada poate fi un document aprobat și o verificare; nu inventa teste automate pentru aceștia.
7. Completează evidence/Sxx/REPORT.md folosind șablonul. În evidence ale fiecărei acțiuni pune căi relative la acest director de plan, către fișiere existente, de exemplu evidence/S00/REPORT.md. Raportul trebuie să indice separat dovada fiecărui subpas.
8. Marchează DONE numai când toate acțiunile, criteriile și dependențele sunt îndeplinite. Completează completed_at și test_results cu rezultate reale; pentru verificări manuale indică metoda și rezultatul. Validatorul verifică consistența registrului, nu corectitudinea produsului.
9. Adaugă o intrare cronologică în PROGRESS.md: realizat, verificat, probleme rămase, următorul pas. Rulează cele două scripturi din README și raportează utilizatorului progresul concret.
10. La întrerupere, lasă IN_PROGRESS și un next_action precis: fișier, funcție, comandă și problema de rezolvat. Pentru BLOCKED, completează blockers cu impedimentul real, ce dovadă există și ce îl deblochează. Nu ascunde un blocaj prin marcarea DONE.

Nu suprascrie jurnalele istorice și nu modifica BASELINE.json pentru a ascunde diferențe. Nu publica chei API, parole, configurații private sau exporturi ale utilizatorilor în dovezi. Copiile de date și secrete stau în afara documentației versionate, cu acces controlat.

## Primul rezultat cerut: S00

Un baseline reproductibil al codului actual, inventarul endpointurilor și clasificarea datelor, plus rezultate de teste obținute în mediu izolat. Testele istorice din raportul de reparații sunt context, nu înlocuiesc validarea baseline-ului nou. Nu porni engine-uri suplimentare pe datele locale reale.

## La lansare și scalare

Respectă porțile din PLAN.md. Publicarea plătită cere verificarea ofertei de 10 USD, drepturi comerciale și verificările de securitate, recuperare și capacitate. Milioane de conturi reprezintă o țintă arhitecturală: raportează separat conturi, utilizatori activi, conexiuni simultane și trafic măsurat. Nicio bifă din documentație nu echivalează cu un test de sarcină.
