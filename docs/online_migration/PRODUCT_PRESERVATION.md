# Păstrarea produsului și îmbunătățirea fluxurilor reale

**Instrucțiune prioritară a proprietarului:** nu ștergem funcții și nu reducem complexitatea funcțională. Pre-Earnings, Agents, Capital, AI Cockpit, Research Factory și celelalte taburi se păstrează. Orice propunere anterioară de înlocuire cu cinci pagini generice este retrasă. Gruparea vizuală opțională nu elimină taburi, controale personale sau acces direct. Abonamentul unic rămâne 10 USD/lună.

## Ce am verificat în proiect

Inspecție în sursă și în UI local pe 6 septembrie 2026: AI Cockpit, Pre-Earnings, Agents și Capital. Observațiile runtime sunt fotografii de moment, nu certificare completă a datelor sau performanței.

| Componentă | Dovezi în sursă și UI | Ce păstrăm și dezvoltăm |
|---|---|---|
| Pre-Earnings | agents/pre_earnings_agent.py; core/pre_earnings/{score,decision,continuous}.py; api/main.py /api/pre-earnings și /track-record; UI Top 10, Pulse, dosar, peer read-through, istoric | Spațiu separat; legături între predicție, eligibilitate, execuție și rezultat |
| Agenți de analiză | Market Scanner, Sentiment, Trade Radar, Premarket, Pre-Earnings, News, Calendar în frontend/agents.js și api/agents_service.py | Identitate și contribuție explicită pentru fiecare rol; nu toate sunt LLM-uri și nu toate execută ordine |
| Evaluarea AI | core/ai/decision_engine.py; pipeline.py; critic.py; core/agent_manager.py on_new_signal | Decizie, validări, critic și motive de acceptare/respingere |
| Dezbatere | core/ai/debate.py; apelul condițional din core/agent_manager.py | Bull/Bear și sinteza managerului, când au rulat; indisponibil nu devine consens inventat |
| Execuție | agents/execution_agent.py process_signal / _process_paper_signal; RiskAgent și capital_entry_guard | Automatizare personală paper, configurație de risc și controale de oprire |
| Capital | core/capital_manager.py, inclusiv AI exit review; frontend/app.js | Poziții, equity, marjă, costuri, ieșiri și istoric; etichetare research versus Capital |
| Decizii și cercetare | core/decision_ledger/; api/cockpit_service.py _ledger_decision_rows; Strategy Lab și Research Factory în UI | Migrare/extindere a registrelor existente, nu baze paralele cu istoric duplicat |
| Mentenanță | Coding Agent, Critic & Watchdog, UI Inspector apar în flota locală | Se păstrează în administrarea platformei; utilizatorul client nu modifică sursa globală |

Ramura live din ExecutionAgent.process_signal raportează explicit că tranzacționarea live nu este conectată și returnează False. Paper trading și analiza AI sunt funcții reale ale proiectului; integrarea brokerului pentru bani reali nu se prezintă ca implementată. Nu deducem performanța unei strategii din existența agenților sau din teste unitare.

## Propunerea corectată de design

1. **Pre-Earnings separat:** subtaburi Pregătire, Top 10/Pulse, Dosar, Istoric. Filtrele avansate rămân disponibile; primul ecran arată și candidați, nu numai configurare. Stări: în analiză, urmărit, în afara ferestrei, eligibil, respins cu motiv, poziție deschisă, rezultat evaluat. Același decision_id/event_id leagă etapele.
2. **Agents separat:** echipa mea de analiză și execuție paper, cu rol, intrare, rezultat, ultima activitate și decizia afectată. Fluxul vizual arată ordinea efectivă din trace; nu presupune că toți agenții rulează pentru fiecare candidat. Criticul tranzacțiilor și watchdogul de cod sunt roluri distincte.
3. **Capital separat:** poziții active și decizii prioritare, apoi equity/istoric detaliat. Pentru fiecare poziție: cine a propus-o, sursa, ce risc a fost acceptat, ce condiții de ieșire există și ce a declanșat închiderea. Nu dublăm explicațiile deja existente; le legăm coerent.
4. **Cockpit păstrat:** sumarul activității agenților, oportunităților, riscului și Capitalului; acces direct la Strategy Lab, Decision Ledger și Research Factory personale. Rezumatul personal se adaugă acestui cockpit.
5. **Celelalte taburi păstrate:** Watchlist, Scanner, News, Trade Radar, Premarket, Hot List, Backtest, Calendar și Alerts rămân destinații proprii, cu consistență vizuală și legături între ele.

## Valoare comercială potrivită produsului

Mesaj propus: **„Urmărește cum o echipă de agenți analizează oportunități, verifică riscul și gestionează tranzacții simulate — cu fiecare decizie explicată. Pre-Earnings, cercetare și toate instrumentele incluse la 10 USD/lună.”** Se publică după validarea fluxului oferit clientului.

Demo-ul principal urmărește un caz complet: eveniment earnings → analiză → argumente pro/contra când există → risc → execuție paper sau refuz → rezultat. Un al doilea exemplu arată de ce botul NU tranzacționează. Rezultatele nefavorabile și lipsa datelor rămân vizibile.

Diferențiator de dezvoltat: comparație între politici/strategii pe același set de date, aceeași perioadă și aceleași costuri, cu rolurile agenților afișate. Nu inventăm clasamente de „cel mai bun AI” și nu atribuim aceluiași trade întregul P/L fiecărui agent. Beneficiul fiecărui rol cere experiment separat, nu simplu număr de aprobări.

## Migrare SaaS fără pierderi de funcții

Analiza comună poate fi partajată dacă licențele permit. Politicile personale, activarea botului, starea agenților pentru cont, deciziile, ordinele, pozițiile și ieșirile sunt izolate per workspace. Oprirea botului unui client nu oprește motorul global sau protecția ieșirilor altui cont.

Benchmarkul S02 include întregul ciclu autonom: candidat, AI, critic, dezbatere condițională, risc, monitorizare și ieșire. Nu estimăm costul doar pentru un raport AI. Rezervăm capacitate pentru protejarea/închiderea pozițiilor paper deja deschise, inclusiv când cota de cercetare e epuizată sau abonamentul expiră; politica exactă trebuie documentată și testată înainte de publicare.

## Observații care cer verificare înainte de promovare

În UI au apărut randament realizat CLGN +859,4% și un interval de încredere cu 100% din numai 2 cazuri. Acestea sunt observații, nu erori de calcul deja demonstrate. Auditul trebuie să urmărească prețuri/splituri, identitatea evenimentului și numitorii înainte de utilizarea lor comercială. Nu se ascunde un rezultat doar pentru că arată rău, dar nici nu se promovează un rezultat neverificat.

S47 păstrează și îmbunătățește Pre-Earnings; S48 migrează echipa de agenți/automatizarea personală; S49 verifică traseul complet și istoricul. S19/S25 depind de acești pași. Registrul distinct al auditului produsului consemnează problemele reale și remedierile, fără a marca implementarea SaaS ca terminată.
