import sys
import os
import json
import logging
from datetime import datetime, timezone

# Configure logging
logging.basicConfig(level=logging.ERROR) # Only errors to keep output clean
sys.path.append(os.getcwd())

from agents.news_tab_agent import get_news_tab_agent

def verify_final():
    print("--- 🔍 FINAL FEED VERIFICATION ---")
    
    # 1. Load Watchlist
    try:
        with open("watchlist.json", 'r') as f:
            watchlist = json.load(f)
            stocks = watchlist.get("stocks", [])
            crypto = watchlist.get("crypto", [])
            tickers = stocks + crypto
            print(f"📋 Watchlist: {len(stocks)} Stocks, {len(crypto)} Crypto. Total: {len(tickers)}")
    except Exception as e:
        print(f"❌ Error loading watchlist: {e}")
        return

    # 2. Refresh News
    print("🔄 Fetching fresh news from API (FMP, Yahoo, etc.)...")
    agent = get_news_tab_agent()
    # Force clear to be sure
    agent.clear_cache()
    
    # FETCH
    state = agent.refresh_all_news(tickers)
    
    # 3. Simulate Frontend Merge & Sort
    all_articles = []
    
    # Add Market News
    m_news = state.get("market_news", [])
    print(f"📊 Market News items: {len(m_news)}")
    all_articles.extend(m_news)
    
    # Add Ticker News
    t_news_map = state.get("ticker_news", {})
    t_count = 0
    for t, items in t_news_map.items():
        all_articles.extend(items)
        t_count += len(items)
    print(f"📈 Ticker News items: {t_count}")
    
    # Sort (Newest First)
    # published_at is ISO string in dict
    all_articles.sort(key=lambda x: x.get("published_at", ""), reverse=True)
    
    # 4. PRINT TOP 10 Results
    print("\n📰 **PREVIEW: TOP 10 NEWS ON FEED**")
    print("="*60)
    for i, article in enumerate(all_articles[:10]):
        pub = article.get("published_at_formatted", "") or article.get("published_at", "")
        print(f"{i+1}. [{article.get('ticker') or 'MARKET'}] {pub} | {article.get('source')}")
        print(f"    {article.get('title')[:80]}...")
    print("="*60)
    
    # Check if Watchlist items are present in Top 5
    top_tickers = [a.get('ticker') for a in all_articles[:5] if a.get('ticker')]
    if top_tickers:
        print(f"✅ SUCCESS: Watchlist tickers found in top news: {set(top_tickers)}")
    else:
        print("⚠️ WARNING: No watchlist tickers in Top 5 (Market news might be newer).")

if __name__ == "__main__":
    verify_final()
