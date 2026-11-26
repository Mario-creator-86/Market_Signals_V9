
import logging
import sys
import os
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

from core.quick_backtester import QuickBacktester
from core.config import ConfigManager

# Setup basic logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger("Verifier")

def simulate_find_best_strategy(qb, ticker, is_crypto, days=7, df=None):
    """
    Simulates MarketEngine._find_best_strategy logic
    """
    risk_modes = ["STRONG", "MEDIUM", "RELAX"]
    trade_modes = ["PULLBACK", "BREAKOUT", "CONTRA"]
    
    best_result = None
    best_win_rate = -1
    
    print(f"\n--- Testing All Strategies for {ticker} ({days} days) ---")
    
    for risk_mode in risk_modes:
        for trade_mode in trade_modes:
            try:
                # Use the existing df (fetched once)
                result = qb.quick_backtest(
                    ticker=ticker,
                    risk_mode=risk_mode,
                    is_crypto=is_crypto,
                    days=days,
                    trade_mode=trade_mode,
                    df=df
                )
                
                if result["total_signals"] > 0:
                    win_rate = result["win_rate"]
                    print(f"[{risk_mode}/{trade_mode}]: WR {win_rate}% ({result['wins']}W/{result['losses']}L) - {result['total_signals']} signals")
                    
                    # Logic verification: strictly based on Win Rate, then Total Signals
                    if win_rate > best_win_rate or (
                        win_rate == best_win_rate and 
                        best_result and 
                        result["total_signals"] > best_result["total_signals"]
                    ):
                        best_win_rate = win_rate
                        best_result = {
                            "risk_mode": risk_mode,
                            "trade_mode": trade_mode,
                            "win_rate": win_rate,
                            "total_signals": result["total_signals"],
                            "wins": result["wins"],
                            "losses": result["losses"],
                        }
            except Exception as e:
                print(f"Error {risk_mode}/{trade_mode}: {e}")
                
    return best_result

def run_verification():
    print("Initializing QuickBacktester...")
    # Mock config
    cfg = ConfigManager()
    qb = QuickBacktester(cfg)
    
    ticker = "BTC-USD"
    is_crypto = True
    days = 7
    
    print(f"Fetching data for {ticker}...")
    # Fetch once
    df, warnings = qb.fetch_backtest_data(ticker, is_crypto, days)
    
    if df is None or df.empty:
        print("Failed to fetch data. Aborting.")
        return

    print(f"Data fetched: {len(df)} candles.")
    
    # Run simulation
    best = simulate_find_best_strategy(qb, ticker, is_crypto, days, df)
    
    print("\n--- RESULT ---")
    if best:
        print(f"🏆 BEST STRATEGY IDENTIFIED: {best['risk_mode']} / {best['trade_mode']}")
        print(f"   Win Rate: {best['win_rate']}%")
        print(f"   Signals:  {best['total_signals']}")
    else:
        print("No valid strategy found (no signals generated).")

if __name__ == "__main__":
    run_verification()
