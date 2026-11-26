# 🔍 AUDIT CRIMINALISTICĂ V2 — TAB 12: SMART ALERTS (`#view-alerts`)
**Data auditului:** 2026-09-05 (runda 2, post-fix `7f26fd2` + `c7edafc`)
**Metodă:** trasare câmp-cu-câmp UI → API → backend → sursă de adevăr; verificare live GET-ONLY pe `http://127.0.0.1:8003`; analiza căii de cod pentru CRUD (fără creare alerte live, conform constrângeri).
**Server live:** `running:true` (subprocess), `/api/health` = ok.

## VERDICT OPERAȚIONAL GLOBAL: ⚠️ **PARȚIAL REPARAT — PERSISTENȚA FUNCȚIONEAZĂ, DAR MOTORUL (SUBPROCESS) NU VEDE NICIODATĂ ALERTELE CREATE DUPĂ PORNIREA LUI (P1 RĂMÂNE)**

Rezumat:
- **Reparat din runda 1:** persistența cheii `"alerts"` în `watchlist.json` (load+save), CRUD-ul API (404-after-create eliminat), toast+sunet la declanșare (`alerts_triggered` expus în `/api/state`, consumat în core loop), eticheta `%` pentru VOLATILITY (tabel + modal), cardul Telegram (nou, onest).
- **Rămâne deschis (P1):** în modul de producție `MS_ENGINE_MODE=subprocess` (impus implicit în `api/main.py:53`, `run_server.py:98`, `compose.production.yml:40`), API-ul scrie alertele pe disc, dar **subprocesul motorului nu reîncarcă niciodată lista de alerte după pornire** → alerta creată din UI nu se declanșează niciodată cât timp motorul rulează, iar la prima declanșare a altei alerte este **ștearsă silentios de pe disc** de scrierea stocării motorului. Toggle/Delete din UI de asemenea nu ajung la motorul care rulează.
- **Rămân deschise (P2/P3):** alerte pe simboluri din afara watchlist-ului („Active" perpetuu, niciodată evaluate), lipsa validării tipului/țintei la creare, lipsa istoricului de declanșare în UI, „Volatility Spike" = de fapt variație 24h absolută.

---

## 1. TABEL CÂMP-CU-CÂMP (UI → SURSĂ)

Legendă verdict: **OK** = lanț complet și onest; **STALE** = date corecte dar fereastra de viață e scurtă; **MISCALIBRATED** = metrica nu corespunde etichetei; **DEAD-BACKEND** = produsul backend nu ajunge niciodată la efectul promis.

| # | Element UI (fișier:linie) | Frontend producer (app.js) | Payload / API | Backend producer (fișier:linie) | Sursa de adevăr | Verdict |
|---|---|---|---|---|---|---|
| 1 | Header tab: icon `fa-bell`, titlu „Smart Alerts", subtitlu (`index.html:731-745`) | static | — | — | static | OK |
| 2 | Buton „Create Alert" (`index.html:742`) | `UI.showAddAlertModal()` (`app.js:23710-23728`) | — | — | — | OK |
| 3 | Modal `#modal-add-alert` (`index.html:2473-2512`) | `UI.hideAddAlertModal()` (`app.js:23730-23736`) | — | — | — | OK |
| 4 | Input `#alert-ticker` (`index.html:2484-2485`) | `Actions.saveAlert()` L23900: `trim().toUpperCase()` | POST `/api/alerts` `{ticker}` (`app.js:1497-1505`) | `AlertRequest.ticker: str` (`main.py:1126`) → `create_alert` (`actions_service.py:2368-2381`) → `AlertManager.add_alert` (`alerts.py:65-69`) | `watchlist.json["alerts"]` (fixat, vezi §3) | OK persistență / **P2** fără validare vs watchlist |
| 5 | Select `#alert-type` — PRICE_UP / PRICE_DOWN / VOLATILITY (`index.html:2489-2493`) | `typeInput.value` (L23901) | `{type}` | `AlertRequest.type: str` **nevalidat** (`main.py:1125-1128`) → `Alert.alert_type` (`alerts.py:15-18`) | constante `AlertType` (`alerts.py:9-12`) | OK pentru cele 3 valori; **P3** tip arbitrar acceptat, alertă mută |
| 6 | Input `#alert-target` (`index.html:2497-2498`) | `parseFloat(target)` (`app.js:1501`) | `{target: float}` | `AlertRequest.target: float` fără constrângeri (`main.py:1128`) → `Alert.target_value` | număr | OK; **P3** țintă ≤ 0 / negativă acceptată (PRICE_UP declanșează instant) |
| 7 | Etichetă `#alert-target-label` (`index.html:2496`) | sincronizare pe change: „Spike Threshold (%)" vs „Target Price ($)" (`app.js:23716-23727`) | — | — | — | **OK (fix runda 1 confirmat)** |
| 8 | Notă „trigger once then deactivate" (`index.html:2501-2507`) | — | — | `alert.is_active = False` la trigger (`alerts.py:123`) | — | OK (onest, one-shot real) |
| 9 | Tabel `#alerts-list-container` — randare (`app.js:23738-23792`) | `API.getAlerts()` → GET `/api/alerts` (`app.js:1492-1496`) | `[]` / listă dict | `api_get_alerts` (`main.py:1321-1329`) → `get_alerts` (`actions_service.py:2356-2366`, fallback `AlertManager(ConfigManager())` proaspăt pe request) → `AlertManager.get_alerts` (`alerts.py:87-90`) | `watchlist.json["alerts"]` | OK (CRUD API + persistență reparate; dovadă live + hermetică) |
| 10 | Celulă Ticker `a.ticker` (`app.js:23764`) | — | cheie `ticker` (`alerts.py:26`) | `Alert.to_dict` (`alerts.py:23-31`) | `watchlist.json` | OK |
| 11 | Celulă Tip `a.type.replace('_',' ')` (`app.js:23765`) | — | cheie `type` (`alerts.py:27`) | idem | idem | OK |
| 12 | Celulă Target — `%` pentru VOLATILITY/PCT/PERCENT, altfel `$` (`app.js:23766`) | — | cheie `target` (`alerts.py:28`) | idem; VOLATILITY compară `abs(change_pct)` 24h vs țintă (`alerts.py:115-120`) | `snapshot["tickers"][t].change_pct` (engine) | **OK afișare (fix runda 1)**; **MISCALIBRATED semantic** — „Volatility Spike" este de fapt variația absolută 24h, nu un spike de volatilitate |
| 13 | Badge Status `Active` / `Triggered/Off` din `a.active` (`app.js:23768-23770`) | — | cheie `active` (`alerts.py:29`) | `is_active` comutat la trigger (`alerts.py:123`) sau toggle (`alerts.py:79-85`) | `watchlist.json` | OK randare; **P3** badge-ul nu distinge „oprit de utilizator" de „declanșat" |
| 14 | Buton Toggle (`app.js:23774-23776` → `Actions.toggleAlert` L23925-23933) | POST `/api/alerts/toggle` `{alert_id}` (`app.js:1506-1514`) | — | `api_toggle_alert` (`main.py:1360-1373`) → `toggle_alert` (`actions_service.py:2398-2415`) → `AlertManager.toggle_alert` (`alerts.py:79-85`) + `_save_alerts` | `watchlist.json` | OK în procesul API; **P1 corolar:** motorul care rulează nu vede comutarea (lista sa in-memorie e înghețată) |
| 15 | Buton Ștergere (`app.js:23777-23779` → `Actions.deleteAlert` L23935-23945) | DELETE `/api/alerts/{id}` (`app.js:1515-1521`) | — | `api_delete_alert` (`main.py:1345-1358`) → `delete_alert` (`actions_service.py:2383-2396`) → `remove_alert` (`alerts.py:71-77`) | `watchlist.json` | OK în procesul API; **P1 corolar:** alerta ștearsă poate încă declanșa în motorul curent |
| 16 | Feed declanșări: toast + sunet | `UI.processTriggeredAlerts(state)` chemat în core loop (`app.js:20699-20703`), toast L24627, sunet `AudioSystem.playSignalSound()` L24630, dedup L24622-24624 | GET `/api/state` cheie `alerts_triggered[-10:]` | `api/state_service.py:3209` (lean) și `:3247` (full) din `state_data["alerts_triggered"]` | motor: `snapshot["alerts_triggered"].extend(...)` (`engine.py:1178-1182`), salvat prin `save_state_payload` (`engine.py:1216` → `state_persistence.py:346-353`, înlocuire completă) | **OK (fix runda 1 (a) confirmat)** — toast ajunge la utilizator; **STALE:** cheia trăiește exact un ciclu de scanare (~10s, `engine.py:926-939` snapshot nou fără cheie → la scanul următor dispare); fără istoric durabil |
| 17 | Telegram: badge `#tg-state-badge` (`index.html:765-768`; randare `app.js:23810-23825`) | `UI.loadTelegramDelivery()` (`app.js:23795-23830`), chemat la activarea view-ului (`app.js:24598, 24610`) | GET `/api/alerts/telegram` (`app.js:1522-1526`) | `api_get_telegram_settings` (`main.py:1383-1393`) → `get_telegram_settings` (`telegram_service.py:55-81`) | `config.json["telegram"]` (live: `runtime/config.json`) + env `TELEGRAM_BOT_TOKEN` | **OK — onest:** live a returnat `enabled:false, configured:false, bot_token_masked:"", has_env_token:false` → badge „Not configured" |
| 18 | Checkbox master `#tg-enabled` (`index.html:778-780`) | `s.enabled` (L23800); salvare payload L23839 | POST `/api/alerts/telegram` | `save_telegram_settings` (`telegram_service.py:84-123`) → `config.update_values` atomic | `config.json["telegram"].enabled` | OK; `enabled` din răspuns = `enabled && token && chat_id` (`telegram_service.py:75`) — nu poți părea activ fără credențiale |
| 19 | Input `#tg-token` (`index.html:786-787`) | niciodată pre-populat; placeholder cu token mascat (L23802-23804); „gol = păstrează" (L23848-23849) | POST `bot_token` (opțional) | `telegram_service.py:100-103` — token gol nu suprascrie; GET nu returnează raw | `config.json["telegram"].bot_token` | OK (token nu se echoed; mascare `telegram_service.py:77`) |
| 20 | Input `#tg-chat-id` (`index.html:792-793`) | `s.chat_id` (L23801) | POST `chat_id` | `telegram_service.py:104-105` | `config.json["telegram"].chat_id` | OK |
| 21 | Route checkboxes alerts/signals/capital/news (`index.html:799-813`) | `routes.news !== false` etc. (L23805-23809); payload L23841-23846 | POST `routes` | validare pe `ROUTE_KEYS` + merge (`telegram_service.py:38, 106-114`) | `config.json["telegram"].routes` | OK; producătorii celor 4 rute există: alerts=`engine.py:1206`, signals=`core/execution/broker.py:233`, capital=`broker.py:249`, news=`agents/news_tab_agent.py:1503`; livrarea e gate-uită pe `enabled` + rută (`telegram_service.py:210-214`) |
| 22 | Buton „Save Settings" (`index.html:817-819` → `Actions.saveTelegramDelivery` `app.js:23832-23866`) | — | POST `/api/alerts/telegram` | `main.py:1396-1413` | `config.json` | OK |
| 23 | Buton „Send Test Message" (`index.html:820-822` → `Actions.testTelegramDelivery` `app.js:23868-23892`) | salvează întâi formularul (L23875), apoi POST `/api/alerts/telegram/test` (`app.js:1539-1550`) | `{}` | `test_telegram` (`telegram_service.py:168-177`) → `_send_message` (`:126-165`); neconfigurat → `detail:"Telegram is not configured (bot_token/chat_id missing)"` (`:133-134`) | HTTP real către `api.telegram.org` | OK — calea de eroare „not configured" e curată (cod + mesaj commit E2E live); netestat live conform constrângerii GET-only |
| 24 | Empty state listă (`app.js:23744-23746`; placeholder static `index.html:751-752`) | — | — | — | — | OK |
| 25 | Istoric declanșări în tabel (dată/oră, preț la trigger) | **NU EXISTĂ** în `renderAlerts` | `alerts_triggered` disponibil în `/api/state` dar nerenderat în tab | `current_price`+`timestamp` există în payload-ul de trigger (`alerts.py:124-128`) | — | **P3 ( DEAD-RENDER pentru datele deja produse)** — runda 1 §4.2.3 rămasă deschisă |

---

## 2. DEFECTE (cu dovezi)

### 🔴 P1 — Motorul subprocess nu reîncarcă alertele; alertele create din UI nu se declanșează și sunt șterse de prima declanșare a altei alerte
**Lanțul defectului:**
1. Producția rulează `MS_ENGINE_MODE=subprocess` (`api/main.py:53` setdefault, `run_server.py:98`, `compose.production.yml:40`; `api/engine_service.py:22`). În acest mod `get_engine()` returnează mereu `None` (`engine_service.py:192-196`) → tot CRUD-ul API trece prin `AlertManager(ConfigManager())` efemer în procesul FastAPI (`actions_service.py:2361-2415`) și **persistă corect** pe disc (fixat).
2. Motorul își instanțiază propriul `AlertManager` o singură dată la pornire: `core/engine.py:487` → `AlertManager._load_alerts()` (`core/alerts.py:50-56`) — **singurul apel** `_load_alerts` din repo.
3. Bucla `run_scan` reîmprospătează `config._watchlist` de pe disc (`engine.py:841` → `reload_watchlist_if_changed`, `config.py:1706-1710`) dar **nu reconstruiește niciodată `alert_manager.alerts`**; `check_alerts` (`engine.py:1178`) evaluează lista înghețată la startup.
4. Consecință A: alerta creată din UI după pornirea motorului nu se declanșează niciodată (rămâne „Active" în UI, evaluare zero).
5. Consecință B (pierdere de date): la prima declanșare a oricărei alerte pe care motorul o cunoaște, `_save_alerts` (`alerts.py:58-63`, apelat la `:130-131`) suprascrie `config.watchlist["alerts"]` cu lista sa veche și scrie pe disc → **alerta creată din UI este ștearsă silentios din `watchlist.json`**.
6. Corolare: Toggle OFF (`alerts.py:79-85`) și Delete (`alerts.py:71-77`) făcute din UI nu sunt respectate de motorul curent (alerta ștearsă/oprită poate încă declanșa până la restart).
**Dovadă live:** `/api/engine/status` → `{"running":true,"initialized":true}` — motorul rulează chiar acum în regimul defect; `git log` confirmă că niciun commit de la `c7edafc` nu atinge `core/alerts.py`/`core/engine.py` în această privință.
**Cerința minimă de reparare:** în `run_scan`, după `self.config.reload_watchlist_if_changed()` (`engine.py:841`), apel `self.alert_manager._load_alerts()` (idee: doar la schimbarea mtime-ului watchlist-ului, pentru a evita distrugerea stării `is_active` la trigger între salvare și reload).

### 🟠 P2 — Alerte pe simboluri din afara watchlist-ului: „Active" perpetuu, niciodată evaluate (runda 1, Defectul 4 — nereparat)
- Modalul acceptă orice simbol (`index.html:2484`, placeholder „e.g. BTC-USD or AAPL").
- Nicio validare client (`app.js:23904-23907` verifică doar ne-gol) și nici server (`main.py:1125-1128`).
- Evaluarea sare silentios peste simbolurile fără preț: `alerts.py:102-104`; `snapshot["tickers"]` conține doar watchlistul activ filtrat (`engine.py:911-914`).
- Efect: utilizatorul vede „Active", iar alerta nu va fi evaluată vreodată. (Corectată vizual doar în măsura în care declanșarea e imposibilă — nu există niciun indiciu în UI.)

### 🟡 P3 — Defecte minore (toate cu dovezi)
1. **Tip alertă nevalidat la creare** (runda 1 P3 — nereparat): `AlertRequest.type: str` (`main.py:1125-1128`); un tip necunoscut (ex. postare manuală în JSON) creează o alertă care nu intră pe niciun branch în `check_alerts` (`alerts.py:110-120`) — alertă mută, fără eroare.
2. **Țintă fără constrângeri:** `target: float` acceptă valori ≤ 0; `PRICE_UP` cu țintă negativă declanșează instant la primul scan.
3. **Fără istoric de declanșare în UI:** `current_price` + `timestamp` sunt produse (`alerts.py:124-128`) și expuse ~10s prin `alerts_triggered` (`state_service.py:3209/3247`), dar tab-ul nu le randează nicăieri (doar toast efemer). Runda 1 §4.2.3 deschisă.
4. **„Volatility Spike (%)" este de fapt |variație 24h|** (`alerts.py:115-120` folosește `change_pct`) — MISCALIBRATED față de etichetă; prefixul `$`→`%` e reparat, semantica nu.
5. **Badge de statut ambiguu:** „Triggered/Off" acoperă atât declanșare, cât și oprire manuală (`app.js:23768-23770`); `created_at` (`alerts.py:30`) nu e afișat.
6. **Fără funcție de editare / autocomplete** (runda 1 §4.2.4-5 deschise): doar toggle+delete (`app.js:23772-23780`).
7. **`AlertManager._load_alerts` fără try/except** (`alerts.py:52-56`): un dicționar corupt în `watchlist.json["alerts"]` (cheie lipsă `ticker` → KeyError în `from_dict`, `alerts.py:33-44`) ar cadea `MarketEngine.__init__` (`engine.py:487`) la pornire; în API fallback-ul returnează `[]` silentios (`actions_service.py:2361-2366`).
8. **`API.addAlert` folosește `fetch` gol** fără timeout/cooldown (`app.js:1497-1505`) — inconsistent cu restul API-ului (`fetchWithTimeout`).
9. **`UI.seenManualActivity` crește nelimitat** (`app.js:2010`, add la `24623-24624`, fără epurare) — neglijabil operațional, dar e o scurgere de memorie teoretică.

---

## 3. STAREA ROUND 1 → RUNDA 2 (regresii / rezolvări)

| Item runda 1 (`docs/audit_tabs/TAB_12_ALERTS.md`) | Stare acum | Dovezi |
|---|---|---|
| Defect 1: cheia `alerts` aruncată la salvare (`config.py` vechi) | ✅ **REZOLVAT** (commit `c7edafc`) | `config.py:1651-1661` (load), `:1944-1955` (save păstrează), `:1699` (salvari nealertă nu o mai pierd); test hermetic `scripts/verify_fixes_2026_09_05.py:37-51` (add→save→reload→unrelated save, 24/24); live: `watchlist.alerts=[]` prezent în `/api/state` |
| Defect 1b: CRUD 404 imediat după creare | ✅ **REZOLVAT** pe cale de cod | fiecare request construiește `ConfigManager()` proaspăt care recitește discul (`config.py:1279-1313`, `self.load()` în `__init__`), deci create→get/toggle/delete în subprocess mode se văd reciproc prin `watchlist.json`; rutele există (`main.py:1321-1373`) |
| Defect 2: decuplarea subprocess (motorul nu află de alerte) | ⚠️ **PARȚIAL** — persistența e reparată, **canalul motor rămâne rupt (P1)** | vezi P1 mai sus; `engine.py:487` + absența oricărui `_load_alerts` în bucla de scan |
| Defect 3: mitul tabelei SQLite `alerts` | ✔ documentat corect între timp | persistența reală = `watchlist.json["alerts"]`; nu există tabelă SQLite (confirmat grep) |
| Defect 4: evaluare oarbă în afara watchlist | ❌ **DESCHIS (P2)** | vezi P2 |
| Defect 5: canale externe inexistente | ✅ **REZOLVAT** (commit `7f26fd2`) | `core/telegram_service.py` complet; hook-uri: `engine.py:1206` (alerts), `broker.py:233/249` (signals/capital), `agents/news_tab_agent.py:1503` (news); API GET/POST/test (`main.py:1383-1426`); card UI (`index.html:757-826`) |
| Fix (a): alertele declanșate nu ajungeau la nimeni | ✅ **REZOLVAT** | expunere `alerts_triggered[-10:]` lean+full (`state_service.py:3209/3247`); consumator live în core loop `app.js:20699-20703` → `processTriggeredAlerts` (`:24618-24633`) toast+sunet+dedup |
| Fix (b): VOLATILITY % + label | ✅ **REZOLVAT (afișare)** | tabel `%` condiționat `app.js:23766`; label sincron `app.js:23716-23727` |
| P3 validare tip la creare | ❌ **DESCHIS** | vezi P3.1 |

**Regresii noi introduse din runda 1:** nu am identificat regresii — `git log --oneline -10` arată doar `218009f` (tooling) și `1eedcfc` (docs) după `c7edafc`; niciunul nu atinge lanțul de alerte.

---

## 4. DOVEZI LIVE (GET-only, 6 cereri, întreme ≥1.2s)

| Cerere | Rezultat |
|---|---|
| `GET /api/alerts` | `200` `[]` (15ms) — lista e vidă |
| `GET /api/alerts/telegram` | `200` `{"enabled":false,"configured":false,"bot_token_masked":"","has_env_token":false,"chat_id":"","routes":{"news":true,"signals":true,"capital":true,"alerts":true}}` — cardul reflectă onest „Not configured" |
| `GET /api/state?lean=true&include_news=false&include_hotlist=false` | `200`; chei incl. `alerts_triggered:[]` (chiar și în lean — fix consumat de core loop) și `watchlist.alerts:[]` (cheia există în view-ul in-memorie) |
| `GET /api/engine/status` | `200` `{"running":true,"initialized":true,"signals_count":13}` — subprocess-ul motorului e activ → P1-ul e valabil în acest moment |
| `GET /api/health` | `200` `{"status":"ok",...}` |
| Citire locală read-only `runtime/watchlist.json` | cheia `alerts` **lipsește de pe disc** — fișierul a fost scris ultima dată `2026-09-05 00:15:37` (+0300), înainte de commit-ul `c7edafc` (22:02) → lipsa e explicabilă prin „nicio salvare de la fix"; cheia va apărea la prima salvare. `runtime/config.json` conține `telegram:{enabled:false, routes:{...toate true}, chat_id:""}` — coerent cu GET (fără bot_token stocat). Root `watchlist.json` (17 aug) e legacy, neusorat: `WATCHLIST_PATH = runtime/watchlist.json` (`config.py:45-47`, fără `MS_WATCHLIST_PATH` setat) |

**Notă de integritate:** /api/state lean include `alerts_triggered`, dar cheia trăiește un singur ciclu de scan (~10s, `engine.py:926-939` construiește snapshot nou fără cheie și `save_state_payload` face replace complet, `state_persistence.py:346-353`). Poll-ul UI (~5s, `refresh_interval_ms:5000`, `config.py:359`) acoperă fereastra, deci toast-ul funcționează în practică; istoricul durabil lipsește prin design.

---

## 5. RECOMANDĂRI (prioritate descrescătoare)

1. **P1:** reîncărcarea alertelor în motor la schimbarea mtime-ului watchlist-ului: în `run_scan`, după `engine.py:841`, apel `self.alert_manager._load_alerts()` condiționat de `reload_watchlist_if_changed` care a raportat schimbare; alternativ mută evaluarea direct pe `config.watchlist["alerts"]` printr-un singur proces (alertele în API, motorul doar le citește).
2. **P2:** la creare, validați ticker-ul față de watchlistul activ (`stocks+crypto`) în `create_alert` (`actions_service.py:2368`) și afișați eroare explicită în UI; opțional marcați alertele „ne-monitorizate" cu badge dedicat.
3. **P3:** validare enum pentru `type` (Literal["PRICE_UP","PRICE_DOWN","VOLATILITY"]) + `target > 0` în `AlertRequest`; împachetează `_load_alerts` în try/except per-element; randează un mic „Trigger history" (ultimele 10 din `alerts_triggered`) în tab; distinge badge-ul „Oprit manual" de „Declanșat"; redenumește condiția VOLATILITY în „24h Move %".
4. **Documentație:** actualizează `docs/audit_tabs/TAB_12_ALERTS.md` cu starea „parțial reparat" ca să nu inducă în eroare.

---
*Audit certificat conform standardului forensic V9, runda 2. Toate afirmațiile au dovezi file:linie sau payload live.*
