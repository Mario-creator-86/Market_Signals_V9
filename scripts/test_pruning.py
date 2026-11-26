
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

def test_pruning():
    print("🚀 Initializing NewsTabAgent...")
    agent = get_news_tab_agent()
    
    # 1. Manually add a "fake" ticker to memory
    ticker = "TEMPORARY_TEST_TICKER"
    dummy_article = NewsArticle(
        id="test_id",
        title="Test Article",
        url="http://test.com",
        source="Test",
        published_at=datetime.now(timezone.utc),
        ticker=ticker
    )
    
    with agent._cache_lock:
        agent._state.ticker_news[ticker] = [dummy_article]
        print(f"✅ Added {ticker} to memory.")
    
    # 2. Call refresh_all_news WITH a list that does NOT include it
    print(f"🔄 Refreshing news for a list EXCLUDING {ticker}...")
    # We pass an empty list or some real ones, but NOT the temporary one
    agent.refresh_all_news(["AAPL", "BTC-USD"])
    
    # 3. Check if it was pruned
    with agent._cache_lock:
        if ticker not in agent._state.ticker_news:
            print(f"🎉 SUCCESS: {ticker} was pruned from memory!")
        else:
            print(f"❌ FAILURE: {ticker} is still in memory.")

if __name__ == "__main__":
    test_pruning()
