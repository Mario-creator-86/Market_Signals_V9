import requests
import json
from datetime import datetime

try:
    print("📡 Querying http://localhost:8000/api/news-tab/state ...")
    resp = requests.get("http://localhost:8000/api/news-tab/state", timeout=10)
    
    if resp.status_code != 200:
        print(f"❌ Error: {resp.status_code} - {resp.text}")
        exit()
        
    data = resp.json()
    
    print("📡 Querying Watchlist...")
    wl_resp = requests.get("http://localhost:8000/api/watchlist", timeout=5)
    watchlist = []
    if wl_resp.status_code == 200:
        raw_wl = wl_resp.json()
        if raw_wl and isinstance(raw_wl[0], dict):
             watchlist = [item['ticker'] for item in raw_wl]
        else:
             watchlist = raw_wl
        print(f"📋 Watchlist ({len(watchlist)}): {watchlist}")
    
    print("\n📡 Querying News State...")
    resp = requests.get("http://localhost:8000/api/news-tab/state", timeout=10)
    data = resp.json()
    
    # Check Ticker News matches Watchlist
    print(f"\n🔍 Checking News for Watchlist Tickers:")
    found_any = False
    all_ticker_news = data.get("ticker_news", {})
    
    for t in watchlist:
        news_for_t = all_ticker_news.get(t, [])
        if news_for_t:
            found_any = True
            latest = news_for_t[0]
            print(f"   ✅ {t}: {len(news_for_t)} articles. Latest: {latest.get('published_at_formatted')} - '{latest.get('title')[:50]}...'")
        else:
            print(f"   ⚠️ {t}: 0 articles.")

    if not found_any:
        print("\n❌ NO NEWS found for ANY watchlist ticker.")
        
    # Check generic stats
    print(f"\n📊 Stats: {data.get('total_articles', 0)} total articles in system.")
        
except Exception as e:
    print(f"💥 Exception: {e}")
