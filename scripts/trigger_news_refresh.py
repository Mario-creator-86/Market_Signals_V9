import requests
import time

def trigger_refresh():
    url = "http://localhost:8000/api/news-tab/refresh"
    print(f"🚀 Triggering news refresh via {url}...")
    
    try:
        start = time.time()
        resp = requests.post(url, timeout=120)  # Long timeout as fetch takes time
        duration = time.time() - start
        
        if resp.status_code == 200:
            data = resp.json()
            market_news = len(data.get("state", {}).get("market_news", []))
            ticker_news_count = sum(len(v) for v in data.get("state", {}).get("ticker_news", {}).values())
            new_articles = len(data.get("new_articles", []))
            
            print(f"✅ Refresh SUCCESS in {duration:.1f}s")
            print(f"   Market News: {market_news}")
            print(f"   Ticker News Count: {ticker_news_count}")
            print(f"   New Articles Fetched: {new_articles}")
        else:
            print(f"❌ Refresh FAILED: {resp.status_code}")
            print(resp.text)
            
    except Exception as e:
        print(f"💥 Error: {e}")

if __name__ == "__main__":
    trigger_refresh()
