# Reaudit Pre-Earnings — 7 septembrie 2026

## Verdict

Implementarea a fost verificată și corectată, dar performanța predictivă nu este validată. Nu este justificată afirmația că sistemul este acum precis sau complet verificat.

## Selecția curentă

Snapshot 2026-09-07T16:11:53.328188+00:00; evaluare 2026-09-07T16:20:10.787445+00:00. 281 rânduri analizate, 14 eligibile, 10 afișate în Top. Prag 60/100.

Acestea sunt rezultate ale filtrelor pe datele salvate, nu confirmări independente ale tuturor evenimentelor. CLGN este eligibil tehnic, însă earnings-ul atribuit datei de 8 septembrie nu este confirmat de pagina IR verificată. Nu îl considerăm candidat verificat pentru earnings.

| Ticker | Direcție | Scor | Data din calendar | În Top 10 |
|---|---|---:|---|---|
| GME | LONG | 70 | 2026-09-08 | Da |
| CASY | LONG | 69 | 2026-09-08 | Da |
| CHWY | LONG | 68 | 2026-09-09 | Da |
| ORCL | LONG | 68 | 2026-09-10 | Da |
| AVAV | LONG | 67 | 2026-09-09 | Da |
| LOVE | LONG | 67 | 2026-09-10 | Da |
| CLGN | SHORT | 66 | 2026-09-08 | Da |
| SIG | LONG | 64 | 2026-09-09 | Da |
| M | LONG | 64 | 2026-09-10 | Da |
| AIR | LONG | 64 | 2026-09-22 | Da |
| BRZE | LONG | 62 | 2026-09-08 | Nu |
| NKE | LONG | 62 | 2026-10-01 | Nu |
| COST | LONG | 61 | 2026-09-24 | Nu |
| NAVN | LONG | 60 | 2026-09-09 | Nu |

## Test istoric real: ce poate și ce nu poate demonstra

Arhiva are 3544 rezultate și 39 recomandări Top cu direcția/scorul salvate. 23 trec pragul de 60, aplicat retrospectiv scorului salvat. Nu am recalculat știrile sau AI-ul istoric cu informațiile de astăzi și nu am ales pragul după rezultate.

Snapshoturile noi first_direction_evidence/top_direction_evidence lipsesc din toate rezultatele și predicțiile deschise ale acestei arhive. Prin urmare nu putem reconstrui integral filtrele actuale la data fiecărei decizii. Testul este o verificare a prețurilor și o analiză a pragului pe recomandările arhivate; nu un backtest integral al politicii noi.

Prețuri descărcate de la Yahoo Finance prin yfinance, daily, auto_adjust=False, actions=True. BMO: închiderea ședinței anterioare → deschiderea din ziua evenimentului. AMC: închiderea zilei evenimentului → deschiderea ședinței următoare. Istoricul daily Yahoo este ajustat pentru split; nu aplicăm încă o dată raportul splitului. Nu sunt randamente ale unor ordine executate, nu includ costuri, slippage sau taxe de împrumut.

| Cohortă | Corecte / total | Rată |
|---|---:|---:|
| Top arhivat, rezultate salvate | 20/39 | 51.28% |
| Prag nou, rezultate salvate | 12/23 | 52.17% |
| Prag nou, prețuri reverificate, fără split | 12/22 | 54.55% |
| Mereu LONG, aceeași cohortă fără split | 11/22 | 50.00% |

Pentru 12/22, intervalul Wilson 95% este aproximativ 34.7%–73.1%. LONG: 11/21; SHORT: 1/1. Nu există suficientă evidență pentru performanța SHORT sau pentru un avantaj statistic. Identitatea și ora exactă a fiecărui earnings nu au fost verificate independent pentru toate cele 39 de rânduri.

## Fiecare candidat istoric care trece pragul

Mișcarea reprezintă variația acțiunii, inclusiv pentru SHORT; semnul negativ confirmă un SHORT. CLGN este prezentat separat în tabel, dar exclus din cohorta fără split.

| Ticker | Data | Sesiune | Semnal | Scor salvat | Gap daily verificat | Direcție corectă | Observație |
|---|---|---|---|---:|---:|---|---|
| A | 2026-08-26 | AMC | LONG | 64 | +2.75% | Da | Preț verificat |
| OKTA | 2026-08-26 | AMC | LONG | 67 | +23.61% | Da | Preț verificat |
| BBWI | 2026-08-26 | BMO | LONG | 67 | +3.87% | Da | Preț verificat |
| NVDA | 2026-08-26 | AMC | LONG | 73 | +6.30% | Da | Preț verificat |
| P | 2026-08-26 | AMC | LONG | 64 | +1.62% | Da | Preț verificat |
| DG | 2026-08-27 | BMO | LONG | 60 | +4.12% | Da | Preț verificat |
| BBY | 2026-08-27 | BMO | LONG | 61 | -7.82% | Nu | Preț verificat |
| S | 2026-08-27 | AMC | SHORT | 64 | -5.37% | Da | Preț verificat |
| AFRM | 2026-08-27 | AMC | LONG | 62 | +10.98% | Da | Preț verificat |
| NIO | 2026-09-01 | BMO | LONG | 62 | -3.55% | Nu | Preț verificat |
| PANW | 2026-09-01 | AMC | LONG | 62 | -4.44% | Nu | Preț verificat |
| DELL | 2026-09-01 | AMC | LONG | 72 | +8.72% | Da | Preț verificat |
| CRDO | 2026-09-01 | AMC | LONG | 62 | -8.71% | Nu | Preț verificat |
| AVGO | 2026-09-02 | AMC | LONG | 60 | -4.22% | Nu | Preț verificat |
| NTAP | 2026-09-02 | AMC | LONG | 64 | -10.41% | Nu | Preț verificat |
| HPE | 2026-09-02 | AMC | LONG | 65 | -8.16% | Nu | Preț verificat |
| VSXY | 2026-09-03 | BMO | LONG | 60 | -9.37% | Nu | Preț verificat |
| PL | 2026-09-03 | AMC | LONG | 61 | +7.03% | Da | Preț verificat |
| LULU | 2026-09-03 | AMC | LONG | 68 | -19.40% | Nu | Preț verificat |
| GWRE | 2026-09-03 | AMC | LONG | 64 | -17.41% | Nu | Preț verificat |
| NX | 2026-09-03 | AMC | LONG | 63 | +9.97% | Da | Preț verificat |
| BRC | 2026-09-03 | BMO | LONG | 64 | +2.77% | Da | Preț verificat |
| CLGN | 2026-09-04 | BMO | SHORT | 67 | -2.93% | Da | EXCLUS: split, earnings neconfirmat |

## Probleme găsite și corectate în cod

1. Timeout pe lock: nu se mai citește starea fără lock și nu se mai eliberează un lock neobținut; endpoint-ul întoarce 503 cu Retry-After.
2. Dovezile înghețate sunt asociate exclusiv evenimentului exact, cu normalizarea fusului orar; nu se mai împrumută alt eveniment aflat la până la 14 zile distanță. Sunt căutate și rezultatele deja încheiate.
3. Eșecul recalculării dosarului blochează eligibilitatea în răspuns în loc să păstreze o eligibilitate veche.
4. UI/API/cache folosesc identitatea evenimentului. Răspunsurile întârziate ale aceluiași ticker nu mai suprascriu alt eveniment.
5. Evaluarea viitoare a gapului folosește loaderul daily existent, cu cache și buget de API, în locul barelor intraday. Sesiunea necunoscută, ziua absentă și distanța >4 zile între referințe sunt refuzate.
6. Formula de producție a fost comparată cu formula independentă pe toate cele 39 seturi reale de prețuri: True.

## Anomalii și limitări rămase

- CLGN: arhiva salvată +859,420277%; barele daily 3,410000 → 3,309999 arată -2,932555%. Emitentul confirmă reverse split 1:10 la deschiderea din 4 septembrie. Nu tratăm diferența drept un profit demonstrat și nu reclasificăm automat evenimentul ca earnings.
- GIII își schimbă semnul rezultatului față de arhivă (+0,84% → -0,53%), iar CIEN (-0,02% → +0,09%). Ambele au scor 59 și nu intră în cohorta de 23.
- Istoricul live nu a fost rescris: procesul existent deține starea în memorie și poate suprascrie modificări externe. Calculele și excluderile corectate sunt în acest raport/CSV. Statisticile vechi din aplicație necesită o migrare controlată a rezultatelor cu proveniență.
- Nu a fost repornit procesul existent care rulează botul. Testele rulează cu stocare izolată. Codul nou trebuie încărcat la restart pentru a intra în procesul local.
- Datele noi trebuie să captureze toate intrările necesare unui replay. Snapshoturile de evidență actuale sunt utile pentru explicații, dar nu reprezintă încă un set complet de antrenare point-in-time.

## Surse primare verificate

- [CollPlant: reverse split, efectiv 4 septembrie](https://ir.collplant.com/news-events/press-releases/detail/203/collplant-announces-reverse-share-split).
- [CollPlant: registrul comunicatelor](https://ir.collplant.com/news-events/press-releases): comunicările din 3–4 septembrie privesc achiziția/consiliul, fără confirmarea unui earnings în datele analizate.
- [NVIDIA: rezultate 26 august 2026](https://investor.nvidia.com/news/press-release-details/2026/NVIDIA-Announces-Financial-Results-for-Second-Quarter-Fiscal-2027/default.aspx).
- [SentinelOne: anunț anterior, 27 august după închidere](https://investors.sentinelone.com/press-releases/news-details/2026/SentinelOne-Announces-Date-of-Fiscal-Second-Quarter-2027-Financial-Results-Conference-Call-and-Participation-in-Upcoming-Investor-Conference/default.aspx).
- [Dell: rezultate 1 septembrie 2026](https://investors.delltechnologies.com/news-releases/news-release-details/dell-technologies-delivers-second-quarter-fiscal-2027-financial).
- [Lululemon: rezultate 3 septembrie 2026](https://corporate.lululemon.com/newsroom/press-releases/2026/09-03-2026-210528733).

## Reproducere și continuare

`python -X utf8 scripts/verify_pre_earnings_history.py --output-dir docs/product_audit_2026-09-06/history_verification_2026-09-07`

Reutilizează snapshoturile și prețurile arhivate. Pentru o evaluare nouă utilizați alt director și `--fetch`. Hashurile intrărilor sunt în history_verification.json; toate cele 39 de apeluri în historical_calls.csv, iar barele în provider_bars.json.

Pașii următori: validarea datei/release-ului cu sursă primară și nivel de încredere explicit; migrare versionată a rezultatelor legacy; snapshoturi complete înainte de publicare; evaluare prospectivă LONG/SHORT, cu baseline și intervale de încredere. Nu modificați ponderile după aceste 22 de rezultate.
