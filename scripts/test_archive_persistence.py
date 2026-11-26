
import os
import sys
import logging
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.news_tab_agent import get_news_tab_agent, NewsArticle
from datetime import datetime, timezone

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def test_archive_persistence():
    print("🚀 Initializing NewsTabAgent...")
    agent = get_news_tab_agent()
    
    # 1. Manually add a "fake" ticker to memory
    ticker = "ARCHIVE_TEST_TICKER"
    dummy_article = NewsArticle(
        id="archive_id",
        title="Archive Article",
        url="http://archive.com",
        source="Archive",
        published_at=datetime.now(timezone.utc),
        ticker=ticker
    )
    
    with agent._cache_lock:
        agent._state.ticker_news[ticker] = [dummy_article]
        print(f"✅ Added {ticker} to memory.")
    
    # 2. Get state WITH filtering (simulating UI call)
    print(f"🔍 Getting filtered state (EXCLUDING {ticker})...")
    filtered_state = agent.get_news_tab_state(active_tickers=["AAPL", "BTC-USD"])
    
    if ticker not in filtered_state["ticker_news"]:
        print(f"🎉 SUCCESS: {ticker} is NOT in filtered UI state.")
    else:
        print(f"❌ FAILURE: {ticker} is still in filtered UI state.")
        
    # 3. Get state WITHOUT filtering (simulating Backup/Internal)
    print(f"🔍 Getting internal state...")
    internal_state = agent.get_news_tab_state()
    
    if ticker in internal_state["ticker_news"]:
        print(f"🎉 SUCCESS: {ticker} is STILL in internal memory/archive!")
    else:
        print(f"❌ FAILURE: {ticker} was deleted from memory!")
        
    # 4. Simulate a refresh cycle
    print(f"🔄 Simulating a refresh cycle with ONLY AAPL...")
    agent.refresh_all_news(["AAPL"])
    
    # 5. Check memory again
    with agent._cache_lock:
        if ticker in agent._state.ticker_news:
            print(f"🎉 SUCCESS: {ticker} survived a refresh cycle! Archive is safe.")
        else:
            print(f"❌ FAILURE: {ticker} was lost during refresh cycle.")

if __name__ == "__main__":
    test_archive_persistence()
