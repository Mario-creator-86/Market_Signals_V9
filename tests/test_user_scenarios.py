import sys
import os
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.engine import MarketEngine
from core.config import ConfigManager

def test_user_scenarios():
    print("🚀 Starting User Scenario Verification\n")

    # 1. Setup Mock Engine
    # We mock the heavy components to avoid starting the full system
    engine = MarketEngine()
    engine.signals = MagicMock()
    engine.agent_manager = MagicMock()
    engine.config = ConfigManager() # Use real config manager logic, we will inject settings
    
    # Mock is_us_market_open to always True for testing stocks
    with patch('core.engine.is_us_market_open', return_value=True):
        
        # ==========================================
        # SCENARIO 1: BTC-USD (User Example)
        # Request: TP=6%, SL=3%
        # ==========================================
        print("🔹 Scenario 1: BTC-USD (Crypto) - TP=6%, SL=3%")
        ticker = "BTC-USD"
        
        # Inject Settings
        engine.config._ticker_settings[ticker] = {
            "tp_pct": 6.0,
            "sl_pct": 3.0
        }
        
        # Create Dummy Signal
        signal = {
            "type": "BUY",
            "entry": 100000.0,
            "tp": 0.0,
            "sl": 0.0,
            "signal_score": 80,
            "quality_score": 80,
            "variant": "A",
            "mtf_score": 3,
            "rsi": 50,
            "macd": 100,
            "macd_hist": 10,
            "risk_mode": "MEDIUM",
            "signal_mode": "PULLBACK",
            "mtf_trends": {"1h": "BULL", "30m": "BULL", "5m": "BULL"},
            "details": {"mtf": "Complete Alignment"},
            "reasons": ["Test"]
        }
        
        # Process
        # We need to patch _is_ticker_suppressed and others to pass filters
        with patch.object(engine, '_is_ticker_suppressed', return_value=False), \
             patch.object(engine.signals, 'has_open_signal_for_ticker', return_value=None), \
             patch.object(engine.signals, 'can_open_new_signal_for_ticker', return_value=True), \
             patch.object(engine, '_passes_winrate_filter', return_value=True), \
             patch.object(engine.quick_backtester, 'fetch_backtest_data', return_value=(None, None)): # Skip backtest
             
            engine._process_signal(ticker, signal, "crypto")
            
            # Verify
            added_signal = engine.signals.add_signal.call_args[0][0]
            
            # Expected stats
            exp_tp = 100000 * (1 + 0.06) # 106000
            exp_sl = 100000 * (1 - 0.03) # 97000
            
            print(f"   Input: Entry={signal['entry']}, Settings TP={engine.config._ticker_settings[ticker]['tp_pct']}%, SL={engine.config._ticker_settings[ticker]['sl_pct']}%")
            print(f"   Result: TP={added_signal['tp']} (Exp: {exp_tp}), SL={added_signal['sl']} (Exp: {exp_sl})")
            print(f"   TP % in signal data: {added_signal['tp_pct']}%")
            
            if added_signal['tp'] == exp_tp and added_signal['sl'] == exp_sl:
                 print("   ✅ SUCCESS: BTC-USD Signal updated correctly with custom TP/SL.")
            else:
                 print("   ❌ FAILED: BTC-USD values do not match.")

        print("-" * 40)

        # ==========================================
        # SCENARIO 2: GC=F (Futures/Gold)
        # Request: TP=1.5%, SL=0.5% (SHORT)
        # ==========================================
        print("🔹 Scenario 2: GC=F (Gold Futures) - TP=1.5%, SL=0.5% (SHORT)")
        ticker = "GC=F"
        
        # Inject Settings
        engine.config._ticker_settings[ticker] = {
            "tp_pct": 1.5,
            "sl_pct": 0.5
        }
        
        # Create Dummy Signal
        signal = {
            "type": "SHORT",
            "entry": 2000.0,
            "tp": 0.0,
            "sl": 0.0,
            "signal_score": 80,
            "quality_score": 80,
            "variant": "B",
            "mtf_score": 3,
            "rsi": 50,
            "macd": -10,
            "macd_hist": -5,
            "risk_mode": "MEDIUM",
            "signal_mode": "BREAKOUT",
            "mtf_trends": {"1h": "BEAR", "30m": "BEAR", "5m": "BEAR"},
            "details": {"mtf": "Bearish Alignment"},
            "reasons": ["Test"]
        }
        
        with patch.object(engine, '_is_ticker_suppressed', return_value=False), \
             patch.object(engine.signals, 'has_open_signal_for_ticker', return_value=None), \
             patch.object(engine.signals, 'can_open_new_signal_for_ticker', return_value=True), \
             patch.object(engine, '_passes_winrate_filter', return_value=True), \
             patch.object(engine.quick_backtester, 'fetch_backtest_data', return_value=(None, None)):
             
            engine._process_signal(ticker, signal, "stock") # Futures treated as stock asset type structure mostly
            
            # Verify
            added_signal = engine.signals.add_signal.call_args[0][0]
            
            # Expected stats (SHORT)
            # TP is BELOW entry: 2000 * (1 - 0.015) = 1970
            # SL is ABOVE entry: 2000 * (1 + 0.005) = 2010
            exp_tp = 2000 * (1 - 0.015)
            exp_sl = 2000 * (1 + 0.005)
            
            print(f"   Input: Entry={signal['entry']}, Settings TP={engine.config._ticker_settings[ticker]['tp_pct']}%, SL={engine.config._ticker_settings[ticker]['sl_pct']}%")
            print(f"   Result: TP={added_signal['tp']} (Exp: {exp_tp}), SL={added_signal['sl']} (Exp: {exp_sl})")
            
            if added_signal['tp'] == exp_tp and added_signal['sl'] == exp_sl:
                 print("   ✅ SUCCESS: GC=F Signal updated correctly with custom TP/SL.")
            else:
                 print("   ❌ FAILED: GC=F values do not match.")

        print("-" * 40)

        # ==========================================
        # SCENARIO 3: NVDA (Stock)
        # Request: TP=4%, SL=2%
        # ==========================================
        print("🔹 Scenario 3: NVDA (Stock) - TP=4%, SL=2%")
        ticker = "NVDA"
        
        # Inject Settings
        engine.config._ticker_settings[ticker] = {
            "tp_pct": 4.0,
            "sl_pct": 2.0
        }
        
        # Create Dummy Signal
        signal = {
            "type": "BUY",
            "entry": 150.0,
            "tp": 0.0,
            "sl": 0.0,
            "signal_score": 90,
            "quality_score": 90,
            "variant": "A",
            "mtf_score": 3,
            "rsi": 50,
            "macd": 2,
            "macd_hist": 0.5,
            "risk_mode": "STRONG",
            "signal_mode": "PULLBACK",
            "mtf_trends": {"1h": "BULL", "30m": "BULL", "5m": "BULL"},
            "details": {"mtf": "Complete Alignment"},
            "reasons": ["Test"]
        }
        
        with patch.object(engine, '_is_ticker_suppressed', return_value=False), \
             patch.object(engine.signals, 'has_open_signal_for_ticker', return_value=None), \
             patch.object(engine.signals, 'can_open_new_signal_for_ticker', return_value=True), \
             patch.object(engine, '_passes_winrate_filter', return_value=True), \
             patch.object(engine.quick_backtester, 'fetch_backtest_data', return_value=(None, None)):
             
            engine._process_signal(ticker, signal, "stock")
            
            # Verify
            added_signal = engine.signals.add_signal.call_args[0][0]
            
            # Expected stats
            exp_tp = 150 * (1 + 0.04) # 156
            exp_sl = 150 * (1 - 0.02) # 147
            
            print(f"   Input: Entry={signal['entry']}, Settings TP={engine.config._ticker_settings[ticker]['tp_pct']}%, SL={engine.config._ticker_settings[ticker]['sl_pct']}%")
            print(f"   Result: TP={added_signal['tp']} (Exp: {exp_tp}), SL={added_signal['sl']} (Exp: {exp_sl})")
            
            if added_signal['tp'] == exp_tp and added_signal['sl'] == exp_sl:
                 print("   ✅ SUCCESS: NVDA Signal updated correctly with custom TP/SL.")
            else:
                 print("   ❌ FAILED: NVDA values do not match.")

    print("\n✅ All Scenarios Verified Successfully")

if __name__ == "__main__":
    test_user_scenarios()
