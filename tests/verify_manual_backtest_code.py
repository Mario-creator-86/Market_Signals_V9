import sys
import os
import pandas as pd
from unittest.mock import MagicMock

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.quick_backtester import QuickBacktester
from core.config import ConfigManager

def verify_manual_backtest_logic():
    print("🚀 Starting Manual Backtest Code Verification")
    
    # Initialize
    config = ConfigManager()
    qb = QuickBacktester(config)
    
    ticker = "BTC-USD"
    custom_tp = 10.0
    custom_sl = 5.0
    
    print(f"🔹 Testing {ticker} with Custom TP={custom_tp}%, Custom SL={custom_sl}%")
    
    # We need to actually fetch data or mock it. 
    # To be safe and deterministic, let's mock the data fetching part so we don't rely on Yahoo Finance API availability
    # fast and reliable.
    
    # Create dummy OHLCV data
    # We need > 50 rows for start_idx check
    # Generate 60 rows.
    # Rows 0-54: Flat
    # Row 55: Entry triggered
    # Row 56: Hits TP (High jumps)
    # Row 57: Hits SL (Low drops) - for the SHORT test
    
    opens = [100.0] * 60
    highs = [100.0] * 60
    lows = [100.0] * 60
    closes = [100.0] * 60
    vols = [1000] * 60
    
    # Win Scenario (LONG): Day 56 hits TP +10% (110)
    highs[56] = 112.0 # Trigger TP
    closes[56] = 110.0
    
    # Loss Scenario (SHORT): Day 57 hits SL -5% (Short from 100, SL at 105)
    # Price rises to 112 (Day 56) is fine for SHORT... wait.
    # If we enter SHORT on Day 55 (price 100), SL is 105.
    # Day 56 high is 112. It will hit SL immediately. 
    # That is what we want for the Loss test.
    
    data = {
        'open': opens,
        'high': highs,
        'low':  lows,
        'close': closes,
        'volume': vols
    }
    df = pd.DataFrame(data)
    df.index = pd.date_range(start='2024-01-01', periods=60)
    # Reset index to get 'timestamp' column which quick_backtester expects
    df = df.reset_index().rename(columns={'index': 'timestamp'})
    
    # Mock data fetching
    qb.fetch_backtest_data = MagicMock(return_value=(df, "crypto"))
    
    # Mock signal generation to force trades
    # We want to force a BUY signal on Day 1
    # The backtester normally runs strategies. We can bypass this by testing the `_calculate_exits` logic directly 
    # OR by mocking `_run_strategy` to return a signal.
    
    # Let's test `quick_backtest` but mock the inner strategy runner to return our test signals
    # Actually, quick_backtest is complex. 
    # A cleaner approach for "Unit Testing" the feature is to instantiate QuickBacktester 
    # and override the signals it processes.
    
    # BUT `quick_backtest` generates signals internally.
    # Let's try to pass parameters and let it run, but since we mock data, we need a strategy that triggers.
    
    # simplified: We will rely on the fact that we passed custom_tp_pct/sl_pct to the method
    # and verify they are stored in the result or used.
    
    # Let's inspect `_calculate_exits` directly? No, better to run `quick_backtest` and see results.
    # We need a strategy that will buy.
    
    # Mocking internal methods to force entry
    # We need to patch methods on the INSTANCE of qb, or the class if we can't reach the instance methods easily.
    # Since we have the instance 'qb', we can try patching attributes if they were methods... 
    # but `quick_backtest` calls `self._check_gate`.
    
    # Let's use `unittest.mock.patch.object` context manager around the call.
    from unittest.mock import patch
    
    # Create a dummy ScoreResult
    class DummyScore:
        is_valid = True
        total_score = 90
        direction = "LONG" # Will trigger BUY
    
    class DummyRowContext:
        atr = 1.0 # Set ATR to 1.0 for simplicity
        
    with patch.object(qb, '_check_gate', return_value=(True, "")), \
         patch.object(qb, '_build_row_context', return_value=DummyRowContext()), \
         patch('core.quick_backtester.compute_signal_score', return_value=DummyScore()), \
         patch.object(qb, '_calculate_resampled_trends_fixed', return_value={}):
         
        # Run Backtest for BUY
        print("   Running BUY test...")
        results = qb.quick_backtest(
            ticker=ticker,
            days=30,
            risk_mode="MEDIUM",
            trade_mode="PULLBACK",
            custom_tp_pct=custom_tp,
            custom_sl_pct=custom_sl
        )
    
    trades = results.get('trades', []) # Note: key might be 'signals' in the result dict based on inspection
    # Inspecting quick_backtester.py: results["signals"] contains the trade records.
    trades = results.get('signals', [])
    
    print(f"   Generated {len(trades)} trades")
    
    if not trades:
        print("   ❌ FAILED: No trades generated to verify.")
        return

    trade = trades[0]
    entry = trade['entry_price']
    exit_price = trade['exit_price']
    # Calculate PnL pct manually for verification
    pnl_pct = (exit_price - entry) / entry * 100
    
    print(f"   Trade 1: Entry={entry}, Exit={exit_price}, Calc PnL={pnl_pct:.2f}%")
    print(f"   Exit Reason: {trade.get('exit_reason')}")
    
    # Verification
    # TP is 10%. Entry 100. Target 110.
    # High on day 1 is 100. High on day 2 is 112. It should exit at 110 or close to it on gap?
    # QuickBacktester logic usually checks High >= TP.
    
    expected_pnl = 10.0
    
    if abs(pnl_pct - expected_pnl) < 0.5: # Allow small slippage logic
        print(f"   ✅ SUCCESS: Trade hit Custom TP of {custom_tp}%")
    else:
        print(f"   ❌ FAILED: Trade PnL {pnl_pct}% did not match expected {custom_tp}%")

    print("-" * 30)
    
    # Test SHORT with SL
    print(f"🔹 Testing SHORT with Custom SL={custom_sl}%")
    
    # Change DummyScore direction to SHORT
    DummyScore.direction = "SHORT"
    
    with patch.object(qb, '_check_gate', return_value=(True, "")), \
         patch.object(qb, '_build_row_context', return_value=DummyRowContext()), \
         patch('core.quick_backtester.compute_signal_score', return_value=DummyScore()), \
         patch.object(qb, '_calculate_resampled_trends_fixed', return_value={}):
         
        results = qb.quick_backtest(
            ticker=ticker,
            days=30,
            risk_mode="MEDIUM",
            trade_mode="PULLBACK",
            custom_tp_pct=custom_tp,
            custom_sl_pct=custom_sl
        )
    
    trades = results.get('signals', [])
    if trades:
        trade = trades[0]
        # Calculate PnL pct manually if needed, or check net_ret
        # trade['net_ret'] is decimal (e.g. 0.10). pnl_pct usually is not in trade record directly in this dict structure?
        # Let's check keys in quick_backtester.py: 
        # trade_rec = { ... "net_ret": ..., "exit_price": ... }
        
        exit_price = trade['exit_price']
        entry = trade['entry_price']
        
        # Short PnL = (Entry - Exit) / Entry
        pnl = (entry - exit_price) / entry * 100
        
        print(f"   Trade 2 (SHORT): Entry={entry}, Exit={exit_price}, Calc PnL={pnl:.2f}%")
        
        # Expected Loss is -5% (SL hit). 
        # But wait, SL logic: SL hit means we exited at SL price.
        # Entry 100. Custom SL 5% means SL price = 105.
        # High was 112, so we definitely hit 105.
        # So exit should be approx 105.
        # PnL = (100 - 105)/100 = -5%.
        
        if abs(pnl - (-custom_sl)) < 0.5:
             print(f"   ✅ SUCCESS: Trade hit Custom SL of {custom_sl}%")
        else:
             print(f"   ❌ FAILED: Trade PnL {pnl:.2f}% did not match expected SL {-custom_sl}%")

if __name__ == "__main__":
    verify_manual_backtest_logic()
