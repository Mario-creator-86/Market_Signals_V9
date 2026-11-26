# Mutarea Market Signals V9 online — punctul de continuare

**Regulă actuală: păstrăm toate funcțiile și taburile, inclusiv Pre-Earnings, Agents, Capital și AI Cockpit ca spații distincte.** [PRODUCT_PRESERVATION.md](PRODUCT_PRESERVATION.md) documentează traseele verificate și are prioritate față de propunerile inițiale de simplificare.

**Stare la 6 septembrie 2026: plan pregătit; migrarea SaaS NU a început.** Nu au fost create conturi cloud, abonamente Stripe, conturi utilizator sau infrastructură publică în această etapă. Decizie actualizată: un singur abonament de 10 USD/lună, cu toate funcțiile client incluse.

Obiectiv: cont individual cu email/parolă, un abonament lunar de 10 USD, date personale izolate și o arhitectură care poate fi extinsă și testată până la milioane de conturi. Capacitatea nu este demonstrată de un desen sau de numărul de conturi pe care îl permite baza de date.

## Citește în această ordine

1. [PLAN.md](PLAN.md) — obiectiv, etape, decizii și gate-uri de lansare.
2. [PACKAGES.md](PACKAGES.md) și [PACKAGE_DRAFT.json](PACKAGE_DRAFT.json) — oferta unică decisă; limitele de consum se validează înainte de vânzare.
3. [ARCHITECTURE.md](ARCHITECTURE.md) — arhitectura țintă, date, API, auth și plăți.
4. [CAPACITY.md](CAPACITY.md) — ce înseamnă „milioane”, încărcare și cost.
5. [STATUS.md](STATUS.md) — registrul lizibil al tuturor pașilor.
6. [TASKS.json](TASKS.json) — sursa de adevăr pentru status, dependențe, subpași și dovezi.
7. [HANDOFF.md](HANDOFF.md) — instrucțiunile exacte pentru AI-ul care continuă.
8. [PROGRESS.md](PROGRESS.md) — ce s-a făcut efectiv; actualizare obligatorie după fiecare pas.

## Primul pas de implementare

**S00 — stabilirea baseline-ului executabil.** Există modificări locale de re-audit care nu au commit. Nu folosi reset/clean și nu le suprascrie. Verifică [BASELINE.json](BASELINE.json), HEAD-ul actual și schimbările altor agenți înainte de orice editare.

Cei 50 de pași S00–S49 (150 subpași) sunt toți TODO în momentul predării. Planificarea făcută acum nu înseamnă că S00 sau autentificarea sunt implementate. Prioritatea este S00 → fundația de izolare/identitate → pipeline și cote → facturare → staging → pilot → lansare → extindere.

## Registru și verificare

După editarea TASKS.json rulează, din rădăcina proiectului:

```text
python docs/online_migration/render_status.py
python docs/online_migration/validate_plan.py
```

Nu marca DONE fără toate subacțiunile, criteriile de acceptare, raportul și rezultatele verificărilor. Dacă o sesiune se termină la jumătate, păstrează IN_PROGRESS și scrie exact subpasul următor. Niciun AI nu trebuie să presupună progres din afirmații precum „aproape gata”.

## Produs, design și atragere

- [Design și decizii](DESIGN_AND_DECISIONS.md)
- [Strategie comercială la 10 USD](GROWTH.md)

S37–S49 se execută după dependențe, nu după numărul ID-ului; Pre-Earnings și automatizarea multi-agent sunt obligatorii înainte de pilot.
