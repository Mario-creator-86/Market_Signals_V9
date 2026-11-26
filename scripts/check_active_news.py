import json
import os
import sys
from datetime import datetime

# Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist.json")
NEWS_BACKUP_PATH = os.path.join(BASE_DIR, "news_backup.json")

def load_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ Error loading {os.path.basename(path)}: {e}")
        return None

def main():
    print(f"🔍 Checking News for Watchlist...")
    print(f"📂 Data Source: {NEWS_BACKUP_PATH}\n")

    # 1. Load Watchlist
    wl_data = load_json(WATCHLIST_PATH)
    if not wl_data:
        print("❌ Watchlist not found.")
        return

    # Extract clean list of tickers
    tickers = []
    if isinstance(wl_data, dict):
        for key in ['stocks', 'crypto', 'forex', 'yahoo', 'binance']:
            if key in wl_data:
                tickers.extend(wl_data[key])
    elif isinstance(wl_data, list):
        tickers = wl_data
    
    tickers = list(set([t.upper().strip() for t in tickers if t]))
    print(f"📋 Watchlist ({len(tickers)} tickers): {', '.join(tickers[:5])}...")

    # 2. Load News
    news_data = load_json(NEWS_BACKUP_PATH)
    if not news_data:
        print("❌ News backup not found.")
        return

    # 3. Analyze News
    market_news = news_data.get('market_news', [])
    breaking_news = news_data.get('breaking_news', [])
    ticker_news_map = news_data.get('ticker_news', {})

    print(f"🗞️  Total News Database: {news_data.get('total_articles', 0)} articles")
    print("-" * 50)
    
    # --- COLLECT ALL NEWS ---
    all_collected_articles = []
    found_count = 0

    # 1. Market News (Top 20 most recent)
    market_news.sort(key=lambda x: x.get('published_at', ''), reverse=True)
    for art in market_news[:20]:
         art['_type'] = 'MARKET'
         art['_display_ticker'] = 'MARKET'
         all_collected_articles.append(art)

    # 2. Ticker News (Top 2 per ticker)
    for ticker in tickers:
        articles = ticker_news_map.get(ticker, [])
        if articles:
            found_count += 1
            # Sort newest first
            articles.sort(key=lambda x: x.get('published_at', ''), reverse=True)
            
            # Take top 2
            for art in articles[:2]:
                art['_type'] = 'TICKER'
                art['_display_ticker'] = ticker
                all_collected_articles.append(art)
    
    # --- SORT & DISPLAY ---
    print(f"📉 COMBINED NEWS TIMELINE ({len(all_collected_articles)} articles, Newest First):")
    
    # Sort ALL collected articles by date
    all_collected_articles.sort(key=lambda x: x.get('published_at', ''), reverse=True)
    
    for i, art in enumerate(all_collected_articles):
        title = art.get('title', 'No Title')
        raw_date = art.get('published_at', 'Unknown Date')
        source = art.get('source', 'Unknown')
        ticker = art.get('_display_ticker', '???')
        
        # Consistent Formatting
        date_str = raw_date
        try:
             # Clean format: YYYY-MM-DD HH:MM
             dt = datetime.fromisoformat(raw_date.replace('Z', '+00:00'))
             date_str = dt.strftime('%Y-%m-%d %H:%M')
        except:
             date_str = str(raw_date)[:16] 

        print(f"   {i+1}. [{date_str}] {ticker:<8} : {title} ({source})")
    
    print("-" * 50)
    print(f"✅ Found news for {found_count} out of {len(tickers)} tickers.")
    
    # 4. Show Breaking News
    if breaking_news:
        print(f"\n🚨 BREAKING NEWS ({len(breaking_news)}):")
        for art in breaking_news:
             bd = art.get('published_at', '')
             try:
                 dt = datetime.fromisoformat(bd.replace('Z', '+00:00'))
                 bd = dt.strftime('%Y-%m-%d %H:%M')
             except: pass
             print(f"   🔥 [{bd}] {art.get('title', '')}")

if __name__ == "__main__":
    main()
