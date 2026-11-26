import sys
import os
import json
import logging
from datetime import datetime, timezone

# Configure logging
logging.basicConfig(level=logging.INFO)
sys.path.append(os.getcwd())

from agents.news_tab_agent import get_news_tab_agent

def verify_feed():
    print("--- Verifying News Feed Content ---")
    
    # 1. Load Watchlist
    tickers = []
    try:
        with open("watchlist.json", 'r') as f:
            watchlist = json.load(f)
            stocks = watchlist.get("stocks", [])
            crypto = watchlist.get("crypto", [])
            tickers = stocks + crypto
            print(f"Loaded {len(tickers)} tickers from watchlist (Stocks: {len(stocks)}, Crypto: {len(crypto)})")
    except Exception as e:
        print(f"Error loading watchlist: {e}")
        return

    # 2. Initialize Agent
    agent = get_news_tab_agent()
    agent.clear_cache() # Start fresh
    
    # 3. Refresh News
    print(f"Refreshing news for tickers: {tickers[:5]}...")
    state = agent.refresh_all_news(tickers)
    
    # 4. Analyze Results
    market_news = state.get("market_news", [])
    ticker_news = state.get("ticker_news", {})
    
    print(f"\nMarket News Count: {len(market_news)}")
    if market_news:
        top = market_news[0]
        print(f"Top Market News: {top['title']} ({top['published_at']})")
        
    print(f"\nTicker News Keys: {list(ticker_news.keys())}")
    
    # Check AAPL specifically
    if "AAPL" in ticker_news:
        aapl_news = ticker_news["AAPL"]
        print(f"AAPL News Count: {len(aapl_news)}")
        if aapl_news:
            top = aapl_news[0]
            print(f"Top AAPL News: {top['title']} ({top['published_at']})")
    else:
        print("❌ AAPL news NOT found in results.")

    # Check MARA
    if "MARA" in ticker_news:
        mara = ticker_news["MARA"]
        if mara:
             print(f"Top MARA News: {mara[0]['title']} ({mara[0]['published_at']})")

    # Check Crypto
    if "BTC-USD" in tickers:
        # Agent normalizes BTC-USD to BTCUSD usually
        btc_key = "BTC-USD"
        if "BTCUSD" in ticker_news: btc_key = "BTCUSD"
        
        if btc_key in ticker_news:
            print(f"BTC News Count: {len(ticker_news[btc_key])}")
            print(f"Top BTC News: {ticker_news[btc_key][0]['title']}")
        else:
             print("❌ BTC news NOT found.")

if __name__ == "__main__":
    verify_feed()
