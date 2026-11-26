# design: "V5 Quant Engine Implementation"

## 1. Exit Management (Task A)
Support flexible exit modes per strategy.

**Logic:**
```python
def calculate_exits(entry_price, direction, atr, params):
    # params: { mode: "ATR"|"FIXED"|"HYBRID", sl_mult, tp_mult, sl_pct, tp_pct }
    
    sl_dist = 0
    tp_dist = 0
    
    if params["mode"] == "ATR":
        sl_dist = atr * params["sl_mult"]
        tp_dist = atr * params["tp_mult"]
        
    elif params["mode"] == "FIXED":
        sl_dist = entry_price * params["sl_pct"]
        tp_dist = entry_price * params["tp_pct"]
        
    elif params["mode"] == "HYBRID":
        # Max protection
        sl_dist = max(entry_price * params["sl_pct"], atr * params["sl_mult"])
        tp_dist = max(entry_price * params["tp_pct"], atr * params["tp_mult"])

    if direction == "BUY":
        return entry_price - sl_dist, entry_price + tp_dist
    else:
        return entry_price + sl_dist, entry_price - tp_dist
```

## 2. Entry Gate (Task B)
Hard filters applied before scoring.

**Pseudocode:**
```python
def check_gate(row, last_trade_context):
    # 1. Regime (ADX)
    if mode in ["PULLBACK", "BREAKOUT"] and row.adx < 20:
        return False, "weak_trend_adx"
        
    # 2. Volatility
    atr_pct = row.atr / row.close
    if atr_pct < 0.0005 or atr_pct > 0.05:
        return False, "volatility_extremes"
        
    # 3. Cooldown
    if bars_since_last_exit < 10:
        return False, "cooldown"
        
    # 4. One Signal Per Swing (Swing Reset)
    # Don't buy again unless price retraced X * ATR from last High since entry
    if last_trade_context and last_trade_context["type"] == "BUY":
        current_drawdown = (last_trade_context["highest_high"] - row.close)
        if current_drawdown < (0.5 * row.atr):
            return False, "no_swing_reset"

    return True, "pass"
```

## 3. Position Management (Task C)
Strict concurrency control.

**Rules:**
- **Max Open Positions**: 1
- **Reverse**: If `new_dir != old_dir` AND `new_score >= reverse_threshold` (e.g. 80).
- **Replace**: If `new_dir == old_dir` AND `new_score >= old_score + 12`.

**Execution:**
- Close old position at `Open(i+1)`.
- Open new position at `Open(i+1)` (Swap) or `Open(i+2)` (Conservative). *We will use Swap (same candle) for efficiency if logic allows, else Conservative.*

## 4. Configuration (JSON)

```json
{
  "exit_config": {
    "mode": "ATR", 
    "hybrid_sl_pct": 0.02
  },
  "gate_config": {
    "adx_min": 20,
    "cooldown_bars": 10,
    "swing_retrace_atr": 0.5
  },
  "position_config": {
    "max_positions": 1,
    "reverse_threshold": 80,
    "replace_delta": 12
  }
}
```

## 5. Validation Checklist (Task D)

- [ ] **Anti-Lookahead**: Verify `mtf_trends` are shifted by 1 timeframe unit.
- [ ] **Execution**: Verify trade entry price equals `Open[entry_index]`, not `Close[signal_index]`.
- [ ] **Stress Test**: Run with `slippage = 5bps` (5x normal) and check if strategy survives.
- [ ] **Gate Stats**: Ensure >30% of signals are rejected (showing gates are working).
