
import os
import sys
import json
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.news_tab_agent import get_news_tab_agent

def verify_tickers():
    agent = get_news_tab_agent()
    
    test_tickers = ["BROCCOLI714USDT", "JASMYUSDT", "BABYUSDT", "BTC-USD", "GC=F", "SI=F"]
    print(f"🔍 Testing news fetch for: {test_tickers}")
    
    for ticker in test_tickers:
        print(f"\n--- Ticker: {ticker} ---")
        articles = agent._fetch_ticker_news(ticker, test_tickers, limit=10, days_back=7)
        print(f"Found {len(articles)} articles.")
        for a in articles[:3]:
            print(f" - [{a.published_at}] {a.title[:60]}...")
            
    print("\n✅ Verification complete.")

if __name__ == "__main__":
    verify_tickers()
