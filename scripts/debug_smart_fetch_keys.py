
import json
import os

NEWS_BACKUP_PATH = "news_backup.json"

def check():
    if not os.path.exists(NEWS_BACKUP_PATH):
        print(f"File {NEWS_BACKUP_PATH} not found.")
        return
        
    try:
        with open(NEWS_BACKUP_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        ticker_news = data.get("ticker_news", {})
        count = data.get("total_articles", 0)
        
        print(f"Total articles in backup: {count}")
        print(f"Tickers in 'ticker_news' ({len(ticker_news)}):")
        
        sorted_keys = sorted(ticker_news.keys())
        for k in sorted_keys:
            print(f" - {k}: {len(ticker_news[k])} articles")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check()
