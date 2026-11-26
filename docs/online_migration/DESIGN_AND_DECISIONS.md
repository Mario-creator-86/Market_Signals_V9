# Design și decizii mai bine fundamentate

**Regulă actuală: păstrăm toate funcțiile și taburile, inclusiv Pre-Earnings, Agents, Capital și AI Cockpit ca spații distincte.** [PRODUCT_PRESERVATION.md](PRODUCT_PRESERVATION.md) documentează traseele verificate și are prioritate față de propunerile inițiale de simplificare.

Stare: propunere inclusă în plan, nu UI implementat. S37–S44 sunt integrați cu S19/S25. Puncte inspectate: frontend/index.html, cockpit.js, chart_workspace.js și mapping-ul celor 13 taburi. Redactarea nu reprezintă un nou audit vizual complet.

## D01 — Acasă personal

Patru zone: piața acum; evenimente care afectează lista; oportunități explicate; schimbări de la ultima vizită. Primele trei elemente de investigat sunt o limită de prezentare, cu acces la restul listei. Nu forțăm trei oportunități dacă datele nu susțin niciuna.

Timestamp, sesiune și sursă pentru fiecare element. Diferențe calculate din snapshotul ultimei vizite, nu noutăți inventate de AI. „Nicio schimbare importantă” este valid. Rezumat comun calculat o singură dată, apoi selectat pentru watchlistul utilizatorului.

## D02 — Navigație care păstrează specializarea

Cele 13 taburi rămân destinații distincte. Pre-Earnings, Agents și Capital au acces direct, la fel ca AI Cockpit și instrumentele de cercetare. O bară laterală cu grupuri și favorite poate ordona destinațiile fără a le elimina. Pe mobil folosim un selector accesibil și scurtături fixate de utilizator. Căutare globală, deep links și revenire la filtre. Acceptare: inventar complet înainte/după, fără funcții pierdute.

## D03 — Cardul unei oportunități

1. Simbol, denumire, preț cu unitate, sursă, sesiune și timestamp.
2. Motivul apariției și orizontul scenariului.
3. Dovezi cu acces direct la datele de origine.
4. Contraargumente, riscuri și condiția de invalidare.
5. Date lipsă, vechi sau contradictorii.
6. Acțiuni: Vezi dovezile, Compară, Creează alertă, Salvează scenariul, Testează în paper.

Faptul, calculul și interpretarea AI au etichete diferite. Exemplele educaționale nu intră în listele live. Un scor mare nu devine automat indicație de cumpărare.

## D04 — Semantica scorurilor

| Indicator | Înseamnă | Nu înseamnă |
|---|---|---|
| Calitatea datelor | Completitudine, prospețime, proveniență | Probabilitate de randament |
| Scorul strategiei | Criterii îndeplinite conform metodei | 85/100 = 85% succes |
| Istoric comparabil | Rezultate, eșantion, perioadă, costuri | Validarea automată a următorului caz |
| Interpretare AI | Sinteză cu surse și limite | Încredere calibrată prin tonul sigur |

Probabilitățile necesită eveniment-țintă, orizont, calibrare și validare separată. Fără acestea afișăm scor descriptiv, nu procent de succes. Lipsa datelor devine N/A cu explicație, nu zero aparent măsurat. [Microsoft — capacități și limite AI](https://www.microsoft.com/en-us/research/?p=564561)

## D05 — Pagina simbolului și comparații

Grafic cu evenimente, preț/sesiune, știri, scenarii favorabile și nefavorabile, invalidare, alerte și istoric. Comparație între 2–3 simboluri pe aceeași perioadă; randamente normalizate și monedă clară. Datele necomparabile sunt etichetate. Prețurile din sesiuni diferite nu sunt egalizate artificial.

Explicăm de ce apare rezultatul în lista personală și permitem ajustarea relevanței. Personalizarea folosește preferințele declarate; nu deducem toleranța financiară la risc din clickuri.

## D06 — Jurnal verificabil

Extindem Decision Ledger și registrele existente, fără duplicare. Snapshot cu informațiile disponibile atunci, versiunea strategiei, orizont și invalidare. Notele ulterioare sunt revizii vizibile. Evaluăm toate scenariile eligibile, inclusiv cele nereușite și imposibil de evaluat. Separăm paper/backtest/real; lansarea nu are execuție reală.

Raportul include eșantion, perioadă, benchmark și costuri simulate. Fără selecție retrospectivă a orizontului favorabil, rescrierea istoricului sau date viitoare. Afirmațiile despre calitatea predictivă necesită validare distinctă.

## D07 — Identitate vizuală

Fundaluri neutre, o culoare principală, culori semantice cu text/simbol, tipografie lizibilă și spațiere consecventă. Densitate ajustabilă; teme deschisă/închisă verificate dacă sunt implementate. Tabele cu antete fixe, coloane configurabile și unități consecvente. Pe mobil: sumar prioritar și acces explicit la restul datelor.

Stări obligatorii: loading, empty, unavailable, stale, error/retry, quota reached, subscription expired. Tastatură, focus vizibil, nume accesibile, contrast și mișcare redusă. Fără animații sau urgență artificială care presează tranzacționarea.

## D08 — Ajutor și notificări

Onboarding opțional: obiectiv de utilizare, 5 simboluri și prima alertă. Ajutor lângă sarcină, glosar și exemple scurte. [NN/g — Contextual help](https://www.nngroup.com/articles/onboarding-tutorials/)

Digest zilnic/săptămânal ales de utilizator, quiet hours, timezone, snooze, grupare și deduplicare. Invalidarea scenariului este la fel de vizibilă ca apariția sa. Analiza concentrării/corelației în portofoliul paper este idee ulterioară ce cere metodă validată, nu funcție promisă la lansare.

## Validare

S37 produce mapping/prototipuri; S38–S42 implementează; S43 demonstrează oferta; S44 verifică sarcini cu minimum 5 participanți recrutați autorizat. Țintă inițială: 4/5 găsesc sursa și riscul, compară, creează alertă și anulează fără ajutor. Confuzia scor/probabilitate și prețul neclar sunt blocaje de lansare. Raportăm timpul, erorile și intervențiile moderatorului și retestăm schimbările afectate.

Un eșantion mic identifică probleme de utilizabilitate, nu demonstrează creșterea conversiei. Înțelegerea riscurilor și reducerea erorilor sunt măsurate separat de profitul simulat; designul atractiv nu dovedește rezultate financiare mai bune.
