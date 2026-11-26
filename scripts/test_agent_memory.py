
import os
import sys
import logging
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.news_tab_agent import get_news_tab_agent

# Configure logging to see the Smart Fetch messages
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def test_memory():
    print("🚀 Initializing NewsTabAgent...")
    # This should trigger _load_backup from absolute path
    agent = get_news_tab_agent()
    
    ticker = "BTC-USD"
    cached = agent._state.ticker_news.get(ticker, [])
    print(f"📊 Memory check for {ticker}: {len(cached)} items found.")
    
    if len(cached) > 0:
        print(f"✅ SUCCESS: {ticker} has items. Smart Fetch should do Light Update.")
    else:
        print(f"❌ FAILURE: {ticker} has NO items. Smart Fetch will do Deep Fetch.")
    
    # Test one that definitely isn't there
    ticker2 = "NONEXISTENT_TICKER"
    cached2 = agent._state.ticker_news.get(ticker2, [])
    print(f"📊 Memory check for {ticker2}: {len(cached2)} items found.")

if __name__ == "__main__":
    test_memory()
