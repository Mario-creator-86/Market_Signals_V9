# 🔍 AUDIT CRIMINALISTIC COMPLET — TAB 12: SMART ALERTS (`#view-alerts`)
**Data Auditului:** 2026-09-04  
**Auditor:** Senior Quantitative Systems Engineer, Full-Stack Software Architect & UX/UI Expert  
**Țintă:** TAB 12 — SMART ALERTS (`#view-alerts` / `#modal-add-alert`)  
**Verdict Operațional Global:** ⚠️ **CRITIC DEFICITAR — PERSISTENȚĂ ȘI IPC RUPTE (SIMULARE / INCOMPLET)**

---

## 1. REVERSE DATA FLOW (UI ➔ SURSĂ & LANȚ DE EVALUARE)

### 1.1 Cartografiere Completă Element UI ➔ Controller ➔ API ➔ Backend ➔ Sursă

| Componentă UI (DOM ID / Clasă) | Controller JS (`frontend/app.js`) | Ruta API (`api/main.py`) | Handler Backend (`api/actions_service.py` & `core/`) | Sursă de Adevăr & Mecanism Stocare |
| :--- | :--- | :--- | :--- | :--- |
| **Tab Navigation Item** `[data-testid="nav-alerts"]` | `ViewRouter.navigate('alerts')` (L24285) | — | — | Activează `#view-alerts`, apelează `UI.renderAlerts()` |
| **Header Tab** `#view-alerts .page-header` | Static HTML (L731-741) | — | — | Icon `fa-bell`, titlu "Smart Alerts", subtitlu |
| **Buton Create Alert** `.btn-auth` | `UI.showAddAlertModal()` (L23520) | — | — | Afișează `#modal-add-alert` (`display: flex`) |
| **Modal Creare** `#modal-add-alert` | `UI.hideAddAlertModal()` (L23528), `Actions.saveAlert()` (L23593) | `POST /api/alerts` | `api_create_alert` (L1291) ➔ `create_alert()` (L2368) ➔ `AlertManager.add_alert()` | Instanță `core.alerts:AlertManager`. Stocare țintă: `watchlist.json` (BUG!) |
| **Input Simbol** `#alert-ticker` | Preluat în `Actions.saveAlert()` | Payload JSON: `ticker` | `Alert.ticker` (format string uppercase) | Fără validare față de watchlist activ |
| **Select Tip Condiție** `#alert-type` | Preluat în `Actions.saveAlert()` | Payload JSON: `type` | `Alert.alert_type` (`PRICE_UP`, `PRICE_DOWN`, `VOLATILITY`) | Enum `core.alerts:AlertType` |
| **Input Prag Valoare** `#alert-target` | Preluat în `Actions.saveAlert()` | Payload JSON: `target` (float) | `Alert.target_value` | Număr pozitiv |
| **Grilă / Tabel Alerte** `#alerts-list-container` | `UI.renderAlerts()` (L23536) | `GET /api/alerts` | `api_get_alerts` (L1281) ➔ `get_alerts()` (L2356) ➔ `AlertManager.get_alerts()` | Returnează lista JSON a alertelor |
| **Badge Status Alertă** `.badge-win` / `.badge-loss` | `UI.renderAlerts()` (L23566) | — | Evaluat din proprietatea booleană `alert.active` | Verde (`Active`) dacă `true`, Roșu (`Triggered/Off`) dacă `false` |
| **Buton Toggle Activ/Inactiv** `button[onclick^="Actions.toggleAlert"]` | `Actions.toggleAlert(id)` (L23623) | `POST /api/alerts/toggle` | `api_toggle_alert` (L1320) ➔ `toggle_alert()` (L2398) ➔ `AlertManager.toggle_alert()` | Comută `alert.is_active` între True și False |
| **Buton Ștergere Alertă** `button[onclick^="Actions.deleteAlert"]` | `Actions.deleteAlert(id)` (L23633) | `DELETE /api/alerts/{id}` | `api_delete_alert` (L1305) ➔ `delete_alert()` (L2383) ➔ `AlertManager.remove_alert()` | Elimină alerta din listă și apelează salvare |
| **Notificare Audio & Toast la Declanșare** | `App.updateUI` hook (L24312-24336) | `GET /api/state` | `core/engine.py:run_scan()` populează `snapshot["alerts_triggered"]` | Sunet `Activity_Sound.wav?v=7.2` prin `AudioSystem.playSignalSound()` + `UI.showToast()` |

---

## 2. DEFECTE FORENSICE CRITICE DE ARHITECTURĂ & PERSISTENȚĂ

Auditul criminalistic a executat teste live cu interpretorul Python și `fastapi.testclient.TestClient`, identificând 5 defecte structurale de nivel SEVER:

### 🔴 DEFECTUL 1: Pierderea Totală a Alertelor la Salvare (Data-Dropping Persistence Bug)
* **Fișiere afectate:** [`core/alerts.py:L61-64`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/core/alerts.py#L61-L64), [`core/config.py:L1916-1950`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/core/config.py#L1916-L1950)
* **Mecanismul Eșecului:**
  În `core/alerts.py`:
  ```python
  def _save_alerts(self):
      """Save alerts to watchlist.json via ConfigManager"""
      self.config.watchlist["alerts"] = [a.to_dict() for a in self.alerts]
      self.config._save_watchlist()
  ```
  Însă în `core/config.py`, metoda `_save_watchlist()` reconstruiește explicit dicționarul salvat pe disc:
  ```python
  data = {
      "stocks": stocks,
      "crypto": crypto,
      "hot_list": hot_list,
      "order": order,
      "settings": clean_settings,
      "scanner_exclusions": scanner_exclusions,
      "schema_version": 2,
  }
  _write_json_atomic(WATCHLIST_PATH, data, indent=2, ensure_ascii=False)
  ```
  **Cheia `"alerts"` este pur și simplu omisă!** În plus, `self._watchlist` este resetat doar cu `stocks` și `crypto`, ștergând și din memoria procesului cheia `"alerts"`.
* **Dovadă Test Live Executat:**
  ```bash
  python -c "from core.alerts import AlertManager, AlertType; from core.config import ConfigManager; cm1 = ConfigManager(); am1 = AlertManager(cm1); am1.add_alert('BTC-USD', AlertType.PRICE_UP, 100000.0); print('AM1:', [a.to_dict() for a in am1.alerts]); cm2 = ConfigManager(); am2 = AlertManager(cm2); print('AM2:', [a.to_dict() for a in am2.alerts])"
  ```
  **Rezultat:**
  ```text
  AM1: [{'id': 'dfc456eb', 'ticker': 'BTC-USD', 'type': 'PRICE_UP', 'target': 100000.0, 'active': True, ...}]
  AM2: []
  ```
  **Consecință:** La orice instanțiere sau restart al backend-ului, toate alertele create dispar instantaneu.

---

### 🔴 DEFECTUL 2: Decuplare Procesuală Totală în Modul Subprocess (IPC Rupt)
* **Fișiere afectate:** [`api/engine_service.py:L22, L192-205`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/api/engine_service.py#L22), [`api/actions_service.py:L2356-2415`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/api/actions_service.py#L2356-L2415), [`core/engine.py:L1178`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/core/engine.py#L1178)
* **Mecanismul Eșecului:**
  1. În configurația standard de producție, motorul rulează ca proces separat: `_ENGINE_MODE = "subprocess"`.
  2. Funcția `get_engine()` din `api/engine_service.py` conține garda:
     ```python
     if _ENGINE_MODE != "thread":
         return None
     ```
  3. În consecință, `get_engine()` returnează ÎNTOTDEAUNA `None` pentru rutele API (`/api/alerts`).
  4. API-ul apelează fallback-ul: creează o instanță efemeră `AlertManager(ConfigManager())` în procesul FastAPI. Din cauza Defectului 1, aceasta nici măcar nu salvează pe disc.
  5. Subprocesul `MarketEngine` își instanțiază propriul `AlertManager` la pornire și **nu reîncarcă niciodată alertele de pe disc sau printr-un canal IPC**.
  6. **Consecință Catastrofală:** În regim de rulare standard (subprocess), **nicio alertă adăugată din interfața utilizator nu ajunge vreodată la motorul de scanare!** Alertele nu se evaluează și nu se declanșează niciodată.

---

### 🔴 DEFECTUL 3: Mitul Tabelei SQLite `alerts` din `market_signals.db`
* **Referință Documentație Precedentă:** În `docs/AUDIT_COMPLET_UI_TO_SOURCE_2026-09-04.md:L447` s-a consemnat:
  *"Sursă de Adevăr: SQLite market_signals.db tabela de alerte, evaluată periodic de AlertsWatcher"*.
* **Realitatea Criminalistică din Cod:**
  - Căutarea în întregul workspace după `CREATE TABLE.*alert` sau referințe SQLite la alerte returnează **0 rezultate**.
  - Nu există niciun modul `api/alerts_service.py` și niciun `core/alerts_watcher.py` (acestea au fost consemnate eronat în trecut).
  - Implementarea reală rezidă exclusiv în `core/alerts.py` (134 linii de cod) și este cuplată defectuos la `watchlist.json`.

---

### 🔴 DEFECTUL 4: Evaluare Oarbă la Simbolurile din Afara Watchlist-ului
* **Fișiere afectate:** [`core/alerts.py:L98-109`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/core/alerts.py#L98-L109), [`core/engine.py:L914, L1178`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/core/engine.py#L914)
* **Mecanismul Eșecului:**
  - Evaluarea alertelor folosește strict dicționarul:
    ```python
    price_data = live_prices.get(alert.ticker)
    if not price_data:
        continue
    ```
  - `live_prices` este alimentat din `snapshot["tickers"]`, care la rândul său filtrează strict tickerii din lista activă:
    `filtered_tickers = {k: v for k, v in prev_tickers.items() if k in current_watchlist}`.
  - Modalul de creare (`#modal-add-alert`) permite utilizatorului să introducă orice simbol (ex. `TSLA`, `AMD`, `SOL-USD`).
  - Dacă utilizatorul creează o alertă pentru un activ care **nu se află în Watchlist-ul activ scanat**, motorul nu descarcă niciodată prețul pentru acel activ.
  - **Consecință:** Alerta este marcată vizual ca "Active", dar nu va fi evaluată niciodată în istorie. Utilizatorul are falsa iluzie că este monitorizat activul.

---

### 🔴 DEFECTUL 5: Inexistența Canalelor Externe (Telegram, Discord) & Notificări Fantomă
* **Constatare Criminalistică:**
  - Documentațiile și formularele preconizate menționau integrări Telegram / Discord.
  - În realitate, nu există nicio linie de cod de webhook Discord, bot Telegram, email sau SMS în `core/alerts.py` sau `api/main.py`.
  - Singurul mecanism de notificare este:
    1. Trimiterea unui eveniment în `snapshot["alerts_triggered"]` pentru **un singur ciclu de scanare** (5-60 secunde).
    2. Frontend-ul `app.js` verifică la polling dacă `alerts_triggered` conține elemente noi.
    3. Dacă utilizatorul are tab-ul deschis exact în acele secunde, rulează `AudioSystem.playSignalSound()` (`Activity_Sound.wav`) și apare un toast verde `UI.showToast()`.
    4. Dacă utilizatorul are browserul închis, este pe alt tab sau deschide aplicația 2 minute mai târziu: notificarea s-a pierdut irevocabil.

---

## 3. LANȚUL DE EVALUARE & PROBE LIVE

```mermaid
flowchart TD
    UI[Frontend: User inputs Ticker, Type, Target] -->|Actions.saveAlert| API_POST["POST /api/alerts"]
    API_POST --> ActionsSvc["api/actions_service.py: create_alert()"]
    
    ActionsSvc --> CheckEngine{"get_engine()"}
    CheckEngine -->|Subprocess Mode: None| TransientAM["Instantiate Transient AlertManager"]
    CheckEngine -->|Thread Mode: Active| InProcEngine["engine.alert_manager.add_alert()"]
    
    TransientAM --> BugSave["_save_alerts() -> Config._save_watchlist()"]
    BugSave -->|CRITICAL BUG: 'alerts' dropped!| DiskFail["watchlist.json on disk: ALERTS LOST!"]
    
    InProcEngine --> MemoryList["engine.alert_manager.alerts (RAM)"]
    InProcEngine --> BugSave
    
    subgraph Engine_Scan_Loop ["core/engine.py: run_scan() (Interval: 5s - 60s)"]
        LiveFeed["Fetch Live Prices: Binance API (Crypto) / yfinance (Stocks)"]
        FeedFail{"Price Fetch Success?"}
        LiveFeed --> FeedFail
        FeedFail -->|Yes| UpdateSnap["snapshot['tickers']"]
        FeedFail -->|Fail / Timeout| CacheSnap["Fallback to prev_state (Cached Price)"]
        
        UpdateSnap --> CheckActiveAlerts["alert_manager.check_alerts(snapshot['tickers'])"]
        CacheSnap --> CheckActiveAlerts
        
        CheckActiveAlerts --> CheckWatchlist{"Ticker in Watchlist?"}
        CheckWatchlist -->|No| NeverEval["Silently Ignored (continue)"]
        CheckWatchlist -->|Yes| EvalCondition{"Condition Met?"}
        
        EvalCondition -->|PRICE_UP: price >= target| Triggered["Trigger Alert!"]
        EvalCondition -->|PRICE_DOWN: price <= target| Triggered
        EvalCondition -->|VOLATILITY: abs(pct) >= target| Triggered
        EvalCondition -->|No| NextScan["Wait for Next Cycle"]
        
        Triggered --> Deactivate["alert.is_active = False (One-shot)"]
        Triggered --> StateTrigger["snapshot['alerts_triggered'].append(...)"]
        StateTrigger --> WatchlistEvent["Append to watchlist_events"]
        StateTrigger --> LogBuffer["Append to log_buffer"]
    end
    
    StateTrigger -->|GET /api/state| PollUI["Frontend App.updateUI"]
    PollUI --> Toast["UI.showToast('ALERT: ...')"]
    PollUI --> Audio["AudioSystem.playSignalSound('Activity_Sound.wav')"]
```

### 3.1 Dovadă Probe Live cu FastAPI TestClient
Rularea testului de regresie pe rutele active `/api/alerts`:
```python
from fastapi.testclient import TestClient
from api.main import app

client = TestClient(app)
r1 = client.get("/api/alerts")
# Status: 200, Body: []

r2 = client.post("/api/alerts", json={"ticker": "NVDA", "type": "PRICE_DOWN", "target": 100.0})
# Status: 200, Body: {'status': 'ok', 'alert': {'id': '1e0c2c4a', ...}}

r3 = client.post("/api/alerts/toggle", json={"alert_id": "1e0c2c4a"})
# Status: 404, Body: {'detail': 'Alert not found'} (DEOARECE S-A PIERDUT LA SALVARE)

r4 = client.delete("/api/alerts/1e0c2c4a")
# Status: 404, Body: {'detail': 'Alert not found'}
```
**Concluzie criminalistică:** Rutele API de interacțiune eșuează cu 404 imediat după creare din cauza lipsei de persistență pe disc.

---

## 4. UI / UX & ERGONOMIE

### 4.1 Layout Vizual & Structură
- **Container Principal:** `#view-alerts` ocupă un spațiu curat, centrat, cu lățime maximă de 1400px și padding proporționat (20px 32px).
- **Tabelul de Alerte:** Folosește containerul `.premium-table-container` cu margini rotunjite și stil modern tip Dark Surface.
- **Cardul de Creare (Modal):** Lățime fixă 450px, bine structurat vizual, cu fundal modal-overlay întunecat.

### 4.2 Anomalii Ergonomice & Erori de Randare
1. **Bug-ul Etichetei de Prag (Label Static):**
   - Câmpul `#alert-target-label` este definit static ca `Target Price ($)`.
   - Când utilizatorul comută la `VOLATILITY` (Volatility Spike (%)), eticheta rămâne în continuare `Target Price ($)`, inducând în eroare utilizatorul dacă trebuie să introducă procente (ex. `5`) sau dolari.
2. **Bug-ul Prefixului Valutar în Tabel:**
   - În `frontend/app.js:L23564`:
     ```javascript
     <td>$${UI.formatPrice(a.target)}</td>
     ```
   - Chiar dacă alerta este de tip `VOLATILITY` cu valoarea `5.00` (reprezentând o mișcare de 5%), tabelul o randează absurd ca:
     **`$5.00`** în loc de **`5.00%`**.
3. **Absența Completă a Istoricului de Declanșare:**
   - Când o alertă se declanșează, singura schimbare vizuală este trecerea badge-ului din verde (`Active`) în roșu (`Triggered/Off`).
   - Tabelul **NU** afișează data/ora declanșării (`triggered_at`), prețul exact la care s-a declanșat (`current_price`) sau variația față de țintă.
   - Nu există o secțiune separată de arhivă "Triggered History".
4. **Lipsa Funcției de Editare (Edit Threshold):**
   - Nu există buton de editare. Dacă traderul vrea să modifice un prag de la $250 la $255, este forțat să șteargă alerta și să reintroducă toate datele de la zero.
5. **Lipsa Autocomplete și Validare:**
   - Câmpul `#alert-ticker` este un simplu text input. Nu există validare în timp real dacă simbolul este valid, dacă este criptomonedă sau acțiune și dacă face parte din lista monitorizată activ.

---

## 5. BENCHMARK COMPARATIV CU INDUSTRIA

| Criteriu de Evaluare | TradingView Alerts | Investing.com Price Alerts | Koyfin Notifications | Market Signals V9 (TAB 12) |
| :--- | :--- | :--- | :--- | :--- |
| **Condiții Suportate** | Preț, indicatori (RSI, MACD), canale, benzi, scripturi Pine personalizate | Preț (peste/sub), variație procentuală zilnică | Praguri preț, indicatori macro, ținte de consens analiști | Preț (> / <), Variație 24h (pseudo-volatilitate). *Indicatori/Trend: Lipsă* |
| **Canale Notificare** | Pop-up Web, Sunet personalizat, Webhook, Email, SMS, Push mobil | Push mobil, Email, Web Notification | Email zilnic, In-App Notification Center, Push mobil | Sunet local (`Activity_Sound.wav`) și Toast Web. *Email/Webhook/Telegram/Discord: LIPSĂ* |
| **Integrare Grafică** | Linii orizontale interactive pe grafic cu drag-and-drop | Alert line statică pe grafic | Notificare integrată în instrumente | Complet decuplat de graficul din tab-ul Cockpit / Chart Workspace |
| **Gestiune Declanșare** | "Only Once" sau "Every Time" cu setare Cooldown / Frecvență | "Once" sau "Recurring" | Setări recurente avansate | Strict One-Shot (fără recurență, fără cooldown configurabil) |
| **Jurnal Declanșări** | Log complet cu timestamp milisecunde, preț trigger și abatere | Istoric dedicat în panoul de utilizator | Panou de activitate cu istoric 90 zile | Fără istoric de declanșare; evenimentul se pierde după un ciclu |
| **Persistență & Fiabilitate** | Cloud redundant de înaltă frecvență | Server-side alert cluster | Cloud enterprise-grade | **Bug critic de persistență: se pierde la salvare pe disc / proces decuplat** |

---

## 6. ANALIZĂ SWOT A MODULULUI

### Puncte Forte (Strengths)
1. **Design Curat și Minimalist:** Structură DOM simplă, lizibilă, fără încărcare cognitivă inutilă.
2. **Infrastructură Audio Existentă:** Sistemul de sunet (`AudioSystem.playSignalSound`) folosește un fișier audio dedicat (`Activity_Sound.wav?v=7.2`) și include mecanism de auto-unlock conform politicilor browserelor moderne.
3. **Interfață Rapidă:** Comutare instantanee (toggle on/off) și ștergere fără latențe vizuale.

### Puncte Slabe (Weaknesses)
1. **Ruperea Completă a Stocării și IPC:** Alertele nu se salvează pe disc din cauza filtrării din `core/config.py:_save_watchlist()`. În modul standard de operare (`subprocess`), motorul nu știe de existența alertelor din API.
2. **Deficit de Documentare Honestă:** Documentele anterioare declarau prezența tabelei SQLite și a canalelor Telegram/Discord, când în realitate nu există nici măcar codul stuf pentru ele.
3. **Absența Indicatorilor Tehnici:** Alertele sunt exclusiv numerice simple pe preț; lipsesc alertele pe RSI, MACD, schimbare de trend, confluență sau semnale generate de agenții inteligenți V9.
4. **Bug-uri Vizuale Imediate:** Prefixul valutar `$` afișat pentru pragurile procentuale de volatilitate.

### Oportunități (Opportunities)
1. **Migrare pe SQLite ACID:** Crearea tabelei `smart_alerts` în `data/market_signals.db` cu coloane complete (`id`, `ticker`, `alert_type`, `target_value`, `is_active`, `created_at`, `triggered_at`, `trigger_price`).
2. **Integrare Chart Workspace:** Desenarea automată a liniilor de alertă pe graficul TradingView Lightweight Charts existent în V9.
3. **Smart Agent Alerts:** Notificări avansate bazate pe inteligența colectivă a agenților (ex. "Alertă când Confluența depășește 85%" sau "Alertă la spargerea unui Fair Value Gap").
4. **Integrare Webhooks Reale:** Adăugarea unui dispatcher asincron pentru Telegram Bot și Discord Webhooks.

### Amenințări (Threats)
1. **Risc de Execuție Financiară pentru Trader:** Dacă un utilizator setează o alertă pentru un nivel critic de Stop Loss sau Take Profit, bazându-se pe sistem, iar alerta este pierdută în mod silențios, utilizatorul poate suferi pierderi de capital severe.
2. **Pierderea Încrederii în Platformă:** Comportamentul de ștergere automată a alertelor la restart sau incapacitatea de a declanșa compromite statutul de terminal profesional al Market Signals V9.

---

## 7. PLAN CONCRET DE REMEDIERE & ARHITECTURĂ PROPUSĂ

### Pasul 1: Corectarea Imediată a Persistenței în `core/config.py`
În fișierul `core/config.py`, în cadrul metodei `_save_watchlist()` (L1937):
```python
# Adăugarea conservării alertelor în payload-ul JSON
alerts = self._watchlist.get("alerts", [])
data = {
    "stocks": stocks,
    "crypto": crypto,
    "hot_list": hot_list,
    "order": order,
    "alerts": alerts,  # <-- FIX OBLIGATORIU
    "settings": clean_settings,
    "scanner_exclusions": scanner_exclusions,
    "schema_version": 2,
}
```

### Pasul 2: Comunicație IPC sau Reîncărcare Activă în `MarketEngine`
În bucla de scanare din `core/engine.py:run_scan()`:
```python
# Reîncărcarea alertelor dacă fișierul a fost modificat
self.alert_manager._load_alerts()
```

### Pasul 3: Corectarea Randării UI în `frontend/app.js`
În `UI.renderAlerts` (L23564):
```javascript
// Afișare condiționată a unității de măsură
const targetDisplay = a.type === 'VOLATILITY' 
    ? `${Number(a.target).toFixed(2)}%` 
    : `$${UI.formatPrice(a.target)}`;
```
Și adăugarea listener-ului pe `#alert-type` în `#modal-add-alert`:
```javascript
document.getElementById('alert-type')?.addEventListener('change', (e) => {
    const label = document.getElementById('alert-target-label');
    if (label) label.textContent = e.target.value === 'VOLATILITY' ? 'Target Volatility Spike (%)' : 'Target Price ($)';
});
```

---
*Raport redactat și certificat criminalistic conform standardelor de audit tehnic V9.*
