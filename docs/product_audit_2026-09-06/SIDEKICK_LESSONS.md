# Idei din Sidekick pentru produsul local

Analiză documentară, 7 septembrie 2026. Nu s-a testat un cont Sidekick și nu s-a implementat un nou asistent. Online rămâne pe pauză. Aceste idei completează PE02–PE12 din PRE_EARNINGS_DIRECTION.md.

## Ce documentează furnizorul

Sidekick folosește contextul graficelor și datele platformei pentru analiză și pentru operații precum scanări și alerte. Descrierea include rapoarte financiare, documente SEC și transcrieri earnings. Acestea sunt capabilități declarate, nu dovezi că predicțiile direcționale au un avantaj statistic. [Pagina produsului](https://trendspider.com/product/sidekick-ai-trading-assistant)

Deep Research separă analiza pe simboluri în conversații individuale și sintetizează rezultatele în conversația principală. Documentația notează că modificarea ulterioară a unui sub-chat nu recalculează automat concluzia principală. [Documentație Deep Research](https://help.trendspider.com/kb/sidekick-ai/sidekick-deep-research)

Ghidul diferențiază cercetarea și folosirea instrumentelor de executarea tranzacțiilor: Sidekick nu execută direct trades și nu creează direct strategii/bots. [Ghid oficial](https://help.trendspider.com/kb/sidekick-ai/sidekick-prompting-guide-1)

În paginile consultate nu am identificat un istoric complet de predicții earnings LONG/SHORT, cu timestamp anterior evenimentului, eșantion, benchmark și performanță OOS. Nu deducem precizia din demonstrații sau prezentare.

## Adaptări recomandate pentru Market Signals V9

| Prioritate | Adaptare propusă | Bază existentă și validare |
|---|---|---|
| 1 | Un dosar de dovezi pentru fiecare eveniment: estimări, revizuiri, guidance anterioară, mișcare pre-event, peers, surse și timestamp. | Extinde datele Pre-Earnings existente; leagă de PE02/PE03. Fiecare concluzie trebuie să indice o dovadă și ce informație lipsește. |
| 1 | Comparație vizibilă între teza LONG și SHORT, cu argumentele care le-ar invalida. | Există `core/ai/debate.py` cu Bull/Bear/Research Manager pentru entry review. Nu presupune că rulează pentru fiecare candidat Pre-Earnings. Verifică integrarea și compară efectul cu/fără dezbatere pe aceleași evenimente. |
| 1 | Separă ce spune scorul structurat, ce spune AI-ul și decizia finală. | `_synchronize_direction_decision` expune deja `structured_direction_label`, `decision_alignment` și scorul final. UI trebuie să explice contribuția și conflictul; un acord între agenți nu devine automat o probabilitate. |
| 1 | Reanaliză când apare o informație relevantă și invalidarea concluziilor dependente. | Există mecanisme de refresh și peer read-through. Verifică legăturile, snapshoturile și cache-urile; schimbarea unei dovezi trebuie să marcheze concluzia veche ca depășită, nu să rescrie predicția evaluată. |
| 2 | Butoane contextuale: „De ce LONG?”, „Cazul SHORT”, „Ce s-a schimbat?”, „Compară cu peers”, „De ce am pierdut?”. | Citește datele și registrele existente; nu reface calculele financiare în text generat. Atașează simbolul, evenimentul și orizontul corecte. |
| 2 | Evaluare după eveniment: separă eroarea direcției, eroarea datelor și eroarea execuției. | Extinde Decision Ledger și reconcilierea Capital din PE04. O explicație generată trebuie verificată contra predicției originale și tranzacției. |
| 3 | Panou conversațional comun, accesibil din taburile existente. | Păstrează cele 13 destinații. Instrumentele primesc parametri validați; explicarea, propunerea unei acțiuni și executarea au stări separate. Porțile de risc existente rămân autoritatea pentru ordine. |

## Exemplu de rezultat propus pentru Pre-Earnings

În dosarul unui simbol: dovezi favorabile LONG; dovezi favorabile SHORT; contradicții; date lipsă; verdict sau abținere; condiții de invalidare; momentul analizei; sursele. Probabilitatea UP/DOWN se afișează numai dacă un model calibrat o furnizează. Încrederea verbală a LLM-ului și votul agenților nu substituie calibrarea.

Pentru a testa utilitatea, îngheață același set de evenimente și compară modelul actual cu varianta care primește dosarul suplimentar. Înregistrează dinainte criteriile: precizie față de benchmark, rezultat net, acoperire, cost și latență. Folosește date viitoare neutilizate la alegerea variantei. O interfață mai clară se poate valida separat prin corectitudinea explicațiilor și timpul necesar pentru a găsi dovezile.

**Ordine de lucru:** întâi PE02–PE05 (adevărul datelor și diagnosticul), apoi dosar și evaluarea contribuției componentelor; panoul conversațional vine după ce poate explica rezultate verificabile. Nu se pretinde că aceste propuneri au crescut deja precizia.
