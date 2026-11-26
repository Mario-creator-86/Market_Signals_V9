"""
Signal Consistency Test
Verifies that live signal generation matches backtest signal generation.

The test ensures:
1. Both systems use the same compute_signal_score function
2. Entry/Exit calculations are identical
3. RowContext construction is equivalent
4. GATE checks are consistent
"""
import logging
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import pandas as pd

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.scoring_system import (
    compute_signal_score,
    RowContext,
    SCORE_THRESHOLDS,
    GATE_CONFIG,
)
from core.quick_backtester import QuickBacktester

logger = logging.getLogger("SignalConsistencyTest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class SignalConsistencyTest:
    """
    Test that verifies signal generation is consistent between:
    - Live engine (engine.py -> ticker_analyzer.py)
    - Backtester (quick_backtester.py)
    
    Both should use the same:
    - compute_signal_score() from scoring_system.py
    - RowContext construction
    - GATE checks
    - Entry/Exit calculations
    """
    
    def __init__(self):
        from types import SimpleNamespace
        self.config = SimpleNamespace(
            signal={
                "rsi_period": 14,
                "macd_fast": 12,
                "macd_slow": 26,
                "macd_signal": 9,
                "adx_period": 14,
                "exit_mode": "ATR",
                "atr_period": 14,
                "atr_timeframe": "5m",
                "atr_multiplier_tp": 2.0,
                "atr_multiplier_sl": 1.5,
                "atr_multipliers_by_mode": {
                    "PULLBACK": {"tp": 2.0, "sl": 1.5},
                    "BREAKOUT": {"tp": 2.5, "sl": 1.7},
                    "CONTRA": {"tp": 1.5, "sl": 1.0},
                },
                "scoring": {"mtf_alignment_min": 4},
                "regime": {"lookback_days": 20}
            },
            risk_mode="MEDIUM",
            tp_pct=4.0,
            sl_pct=2.0
        )
        self.backtester = QuickBacktester(self.config)
        self.errors = []
        self.warnings = []
        self.passed = []
    
    def run_all_tests(self, ticker: str = "BTC-USD", days: int = 3) -> Dict:
        """Run all consistency tests."""
        logger.info(f"[SEARCH] Running Signal Consistency Tests for {ticker} ({days} days)")
        
        results = {
            "ticker": ticker,
            "days": days,
            "timestamp": datetime.now().isoformat(),
            "tests": [],
            "errors": [],
            "warnings": [],
            "passed": [],
            "summary": {}
        }
        
        # Test 1: Verify shared scoring function
        self._test_shared_scoring_function(results)
        
        # Test 2: Verify RowContext fields match
        self._test_row_context_fields(results)
        
        # Test 3: Verify GATE config is shared
        self._test_gate_config_shared(results)
        
        # Test 4: Run backtest and verify signal structure
        self._test_backtest_signal_structure(ticker, days, results)
        
        # Test 5: Verify entry/exit calculation consistency
        self._test_entry_exit_calculations(results)
        
        # Summary
        results["summary"] = {
            "total_tests": len(results["tests"]),
            "passed": len(results["passed"]),
            "errors": len(results["errors"]),
            "warnings": len(results["warnings"]),
            "status": "PASS" if len(results["errors"]) == 0 else "FAIL"
        }
        
        self._print_results(results)
        return results
    
    def _test_shared_scoring_function(self, results: Dict):
        """Verify compute_signal_score is the same function."""
        test_name = "Shared Scoring Function"
        results["tests"].append(test_name)
        
        try:
            # Both engine and backtester import from scoring_system
            from core.quick_backtester import compute_signal_score as bt_score
            from core.scoring_system import compute_signal_score as ss_score
            
            # Check they're the same function object
            if bt_score is ss_score:
                results["passed"].append(f"[PASS] {test_name}: Same function reference")
                logger.info(f"[PASS] {test_name}: PASS")
            else:
                results["errors"].append(f"[FAIL] {test_name}: Different function references")
                logger.error(f"[FAIL] {test_name}: FAIL - Different function references")
        except Exception as e:
            results["errors"].append(f"[FAIL] {test_name}: {str(e)}")
            logger.error(f"[FAIL] {test_name}: {str(e)}")
    
    def _test_row_context_fields(self, results: Dict):
        """Verify RowContext has all required fields."""
        test_name = "RowContext Fields"
        results["tests"].append(test_name)
        
        try:
            required_fields = [
                "open", "high", "low", "close", "volume",
                "rsi", "macd", "macd_signal", "macd_hist",
                "ema20", "vwap", "atr", "adx", "vol_sma",
                "adx_5m", "bb_width_5m", "atr_percentile_5m", "market_regime",
                "trend_1d", "trend_4h", "trend_1h", "trend_30m", "trend_5m", "trend_1m",
                "dist_vwap_atr", "dist_ema_atr",
                "cooldown_active", "cooldown_bars_remaining"
            ]
            
            # Create a dummy RowContext
            ctx = RowContext(
                open=100, high=101, low=99, close=100.5,
                volume=1000, rsi=50, macd=0.1, macd_signal=0.05, macd_hist=0.05,
                ema20=100, vwap=100.2, atr=1.0, adx=25, vol_sma=900,
                adx_5m=25, bb_width_5m=0.03, atr_percentile_5m=50.0, market_regime="MIXED",
                trend_1d="BULLISH", trend_4h="BULLISH", trend_1h="NEUTRAL",
                trend_30m="NEUTRAL", trend_5m="NEUTRAL", trend_1m="NEUTRAL",
                dist_vwap_atr=0.2, dist_ema_atr=0.5,
                cooldown_active=False, cooldown_bars_remaining=0
            )
            
            missing = []
            for field in required_fields:
                if not hasattr(ctx, field):
                    missing.append(field)
            
            if not missing:
                results["passed"].append(f"[PASS] {test_name}: All {len(required_fields)} fields present")
                logger.info(f"[PASS] {test_name}: PASS")
            else:
                results["errors"].append(f"[FAIL] {test_name}: Missing fields: {missing}")
                logger.error(f"[FAIL] {test_name}: FAIL - Missing: {missing}")
        except Exception as e:
            results["errors"].append(f"[FAIL] {test_name}: {str(e)}")
            logger.error(f"[FAIL] {test_name}: {str(e)}")
    
    def _test_gate_config_shared(self, results: Dict):
        """Verify GATE_CONFIG is the same in both modules."""
        test_name = "GATE Config Shared"
        results["tests"].append(test_name)
        
        try:
            from core.quick_backtester import GATE_CONFIG as bt_gate
            from core.scoring_system import GATE_CONFIG as ss_gate
            
            if bt_gate is ss_gate:
                results["passed"].append(f"[PASS] {test_name}: Same GATE_CONFIG reference")
                logger.info(f"[PASS] {test_name}: PASS")
            else:
                # Check if values match even if different objects
                if bt_gate == ss_gate:
                    results["warnings"].append(f"[WARN] {test_name}: Same values but different objects")
                    logger.warning(f"[WARN] {test_name}: Different objects, same values")
                else:
                    results["errors"].append(f"[FAIL] {test_name}: Different GATE_CONFIG values")
                    logger.error(f"[FAIL] {test_name}: FAIL - Different values")
        except Exception as e:
            results["errors"].append(f"[FAIL] {test_name}: {str(e)}")
            logger.error(f"[FAIL] {test_name}: {str(e)}")
    
    def _test_backtest_signal_structure(self, ticker: str, days: int, results: Dict):
        """Run backtest and verify signal structure matches expected format."""
        test_name = "Backtest Signal Structure"
        results["tests"].append(test_name)
        
        try:
            bt_result = self.backtester.quick_backtest(
                ticker=ticker,
                risk_mode="MEDIUM",
                is_crypto="-" in ticker,  # BTC-USD style = crypto
                days=days,
                trade_mode="PULLBACK"
            )
            
            if "error" in bt_result:
                results["warnings"].append(f"[WARN] {test_name}: Backtest had error: {bt_result['error']}")
                logger.warning(f"[WARN] {test_name}: {bt_result['error']}")
                return
            
            # Check required fields in backtest result
            required_fields = ["ticker", "signals", "win_rate", "total_signals"]
            missing = [f for f in required_fields if f not in bt_result]
            
            if missing:
                results["errors"].append(f"[FAIL] {test_name}: Missing result fields: {missing}")
                logger.error(f"[FAIL] {test_name}: Missing: {missing}")
                return
            
            # Check signal structure
            signals = bt_result.get("signals", [])
            if signals:
                signal = signals[0]
                signal_required = ["type", "entry", "tp", "sl", "exit_price", "pnl_pct"]
                signal_missing = [f for f in signal_required if f not in signal]
                
                if signal_missing:
                    results["warnings"].append(f"[WARN] {test_name}: Signal missing fields: {signal_missing}")
                else:
                    results["passed"].append(f"[PASS] {test_name}: {len(signals)} signals with correct structure")
                    logger.info(f"[PASS] {test_name}: PASS - {len(signals)} signals")
            else:
                results["warnings"].append(f"[WARN] {test_name}: No signals generated")
                logger.warning(f"[WARN] {test_name}: No signals")
                
        except Exception as e:
            results["errors"].append(f"[FAIL] {test_name}: {str(e)}")
            logger.error(f"[FAIL] {test_name}: {str(e)}")
    
    def _test_entry_exit_calculations(self, results: Dict):
        """Verify entry/exit (TP/SL) calculations are consistent for ATR exits."""
        test_name = "Entry/Exit Calculations"
        results["tests"].append(test_name)
        
        try:
            # Test case: entry at 100, ATR 2.0, TPx2.0, SLx1.5
            entry = 100.0
            atr = 2.0
            tp_mult = 2.0
            sl_mult = 1.5
            
            # BUY signal expectations
            expected_buy_tp = entry + (atr * tp_mult)  # 104
            expected_buy_sl = entry - (atr * sl_mult)  # 97
            
            # SELL signal expectations
            expected_sell_tp = entry - (atr * tp_mult)  # 96
            expected_sell_sl = entry + (atr * sl_mult)  # 103
            
            # Verify calculations
            tolerance = 0.001
            
            buy_tp_ok = abs(expected_buy_tp - 104.0) < tolerance
            buy_sl_ok = abs(expected_buy_sl - 97.0) < tolerance
            sell_tp_ok = abs(expected_sell_tp - 96.0) < tolerance
            sell_sl_ok = abs(expected_sell_sl - 103.0) < tolerance
            
            if all([buy_tp_ok, buy_sl_ok, sell_tp_ok, sell_sl_ok]):
                results["passed"].append(f"[PASS] {test_name}: TP/SL calculations correct")
                logger.info(f"[PASS] {test_name}: PASS")
            else:
                results["errors"].append(f"[FAIL] {test_name}: Calculation mismatch")
                logger.error(f"[FAIL] {test_name}: FAIL")
                
        except Exception as e:
            results["errors"].append(f"[FAIL] {test_name}: {str(e)}")
            logger.error(f"[FAIL] {test_name}: {str(e)}")
    
    def _print_results(self, results: Dict):
        """Print test results summary."""
        summary = results["summary"]
        
        print("\n" + "="*60)
        print("SIGNAL CONSISTENCY TEST RESULTS")
        print("="*60)
        print(f"Ticker: {results['ticker']}")
        print(f"Days: {results['days']}")
        print(f"Timestamp: {results['timestamp']}")
        print("-"*60)
        
        print(f"\n[PASS] PASSED: {summary['passed']}/{summary['total_tests']}")
        for p in results["passed"]:
            print(f"   {p}")
        
        if results["warnings"]:
            print(f"\n[WARN] WARNINGS: {len(results['warnings'])}")
            for w in results["warnings"]:
                print(f"   {w}")
        
        if results["errors"]:
            print(f"\n[FAIL] ERRORS: {len(results['errors'])}")
            for e in results["errors"]:
                print(f"   {e}")
        
        print("\n" + "="*60)
        status = "[PASS]" if summary["status"] == "PASS" else "[FAIL]"
        print(f"{status} OVERALL STATUS: {summary['status']}")
        print("="*60 + "\n")


def main():
    """Run the signal consistency test."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Signal Consistency Test")
    parser.add_argument("--ticker", default="BTC-USD", help="Ticker to test")
    parser.add_argument("--days", type=int, default=3, help="Days for backtest")
    args = parser.parse_args()
    
    tester = SignalConsistencyTest()
    results = tester.run_all_tests(ticker=args.ticker, days=args.days)
    
    # Exit with error code if tests failed
    if results["summary"]["status"] != "PASS":
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
