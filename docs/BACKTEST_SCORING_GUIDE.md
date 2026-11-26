# 📊 Ghid Complet: Sistem de Backtest și Scoring

## 🎯 Prezentare Generală

Sistemul de backtest analizează performanța semnalelor de trading pe date istorice.
Fiecare semnal primește un **scor de la 0 la 100** bazat pe mai mulți indicatori tehnici.

**ACTUALIZARE V3 (Quant):** Include metrics avansate (R-Multiples, Profit Factor), logică strictă de execuție și parametrizare specifică pe strategie.

---

## 🔢 Cum se calculează SCORUL (0-100 puncte)

Scorul total = suma a 6 componente:

| Componentă | Puncte Max | Ce măsoară |
|------------|-----------|------------|
| **MTF Alignment** | 30 | Câte timeframe-uri sunt aliniate cu direcția trade-ului |
| **RSI Zone** | 20 | RSI-ul este în zona optimă pentru tipul de trade |
| **MACD Momentum** | 20 | Direcția și puterea MACD |
| **VWAP Distance** | 12 | Cât de aproape e prețul de VWAP |
| **EMA Distance** | 12 | Cât de aproape e prețul de EMA20 |
| **Price Action** | 6 | Confirmări din lumânări (pattern-uri) |

---

## ⚙️ Logica de Backtest (V3 Quant Realism)

### 1. Intrare Realistă & Anti-Lookahead
- **Regula de Aur**: Semnalele bazate pe prețuri de *Close* ale lumânării `i` sunt executate la **Open** al lumânării `i+1`.
- **MTF Logic**: Timeframe-urile superioare (1h, 4h) sunt "shiftate" pentru a se asigura că la momentul deciziei nu accesăm date din viitor (ex: trendul de 4h este fixat la începutul intervalului).

### 2. Risk Management Dinamic (Strategy Specific)
TP și SL sunt calculate dinamic în funcție de volatilitate (ATR) și specificul strategiei:

| Strategie | Stop Loss (Multiplicator ATR) | Take Profit (Multiplicator ATR) | Descriere |
|-----------|-------------------------------|---------------------------------|-----------|
| **PULLBACK** | **2.0x** | **3.0x** | Trend following clasic (R:R 1:1.5) |
| **BREAKOUT** | **1.5x** | **4.0x** | Stop strâns la eșec, target mare la succes |
| **CONTRA** | **1.0x** | **2.0x** | Scalping agresiv, invalidare rapidă |

### 3. Ieșire Conservatoare
- Dacă într-o lumânare prețul atinge atât TP cât și SL, se consideră **SL hit** (worst-case).
- Se aplică **Slippage** la ieșire (negativ pentru Stop Loss, neutru/negativ pentru TP).

### 4. Costuri Totale
- **Spread**: 2 bps (0.02%)
- **Slippage**: 1 bps (0.01%)
- **Comisioane**: 5 bps (0.05%)

---

## 📊 Metrice Avansate (R-Multiples)

Pentru a evalua corect performanța independent de mărimea contului, folosim **R-Multiples**:

1.  **Ce este R?**: Rismul inițial asumat per trade (Distanța Entry - SL).
2.  **R-Multiple**: Rezultatul trade-ului exprimat în unități de risc.
    *   Exemple:
        *   Pierdere la Stop Loss = **-1.0 R**
        *   Câștig la Take Profit = **+1.5 R** (pentru Pullback)
        *   Break-even = **0.0 R**

### Metrice Cheie Raportate:

*   **Win Rate**: Procentul de hit-uri.
*   **Expectancy (R)**: Valoarea medie așteptată a unui trade în unități R.
    *   Formula: `Media(Toate R-Multiples)`
    *   *Obiectiv*: > 0.2R per trade.
*   **Profit Factor**: Raportul `Gross Profit / Gross Loss`.
    *   *Obiectiv*: > 1.5
*   **Max Drawdown**: Scăderea maximă procentuală a equity-ului simulat.

---

## 🛡️ Profile de Risc și Filtre Suplimentare

| Profil | Scor Minim | Descriere |
|--------|-----------|-----------|
| **PRO** | 65 | Cel mai permisiv |
| **MEDIUM** | 72 | Echilibrat - calitate vs cantitate |
| **STRONG** | 88 | Strict - doar semnale de înaltă calitate |

**Filtre Extra (PRO Mode):**
- Volume > 80% din medie
- ADX > 20 (Trend Check)

---

## ✅ Rezumat Simplificat

```
1. CALCUL SCOR (0-100) -> Dacă > PRAG
       ↓
2. SETARE TP/SL (Specific Strategiei)
       Ex: Breakout -> SL=1.5xATR, TP=4.0xATR
       ↓
3. VALIDARE INTRABAR (Anti-Lookahead)
       Intrare la NEXT BAR OPEN
       ↓
4. SIMULARE
       Aplică Slippage/Comisioane
       Verifică Worst-Case Exit
       ↓
5. REZULTAT
       Calculează R-Multiple (ex: +2.5R)
       Actualizează Max Drawdown
```
