# Perf Baseline (Etapa 0)

Metoda:
- rulare locala cu `TEST_MODE=1`
- `fastapi.testclient` cu 20 request-uri/endpoint
- metrici: `min`, `avg`, `p95`, `max` (ms)

## Endpointuri cheie (10)

| Endpoint | min | avg | p95 | max |
|---|---:|---:|---:|---:|
| `/api/state?lean=true&include_news=false&include_hotlist=false` | 664.78 | 889.11 | 1048.27 | 3723.02 |
| `/api/state?lean=false&include_news=false&include_hotlist=true&hotlist_details=false` | 1928.18 | 2366.96 | 3809.04 | 7352.28 |
| `/api/dashboard` | 569.86 | 642.05 | 784.73 | 877.47 |
| `/api/news-tab/state` | 4.39 | 108.61 | 141.42 | 2037.98 |
| `/api/scanner/state` | 3.18 | 8.18 | 9.13 | 87.67 |
| `/api/premarket` | 3.37 | 8.36 | 11.05 | 84.37 |
| `/api/pre-earnings` | 3.14 | 17.77 | 18.71 | 278.49 |
| `/api/traderadar/state` | 3.22 | 5.10 | 5.55 | 29.54 |
| `/api/calendar/state` | 327.61 | 349.75 | 372.01 | 384.53 |
| `/api/watchlist` | 3.73 | 4.84 | 6.00 | 11.39 |

## Concluzii baseline
- `state` este in continuare principalul bottleneck.
- Endpointurile agent-state (`premarket`, `pre-earnings`, `scanner`, `traderadar`) sunt deja rapide cand servesc cache/snapshot.
- Prioritatea optimizarii ramane payload-ul Home (`state` + render cost in frontend).
