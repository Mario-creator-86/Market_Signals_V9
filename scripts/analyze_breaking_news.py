
import json
from datetime import datetime, timezone, timedelta

NEWS_BACKUP_PATH = "news_backup.json"

def analyze():
    try:
        with open(NEWS_BACKUP_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        all_articles = []
        # Flatten articles
        all_articles.extend(data.get("market_news", []))
        all_articles.extend(data.get("breaking_news", []))
        for ticker, articles in data.get("ticker_news", {}).items():
            all_articles.extend(articles)
            
        print(f"Total articles: {len(all_articles)}")
        
        breaking_count = sum(1 for a in all_articles if a.get("is_breaking"))
        print(f"Already marked as breaking: {breaking_count}")
        
        breaking_keywords = ["breaking", "alert", "urgent", "just in", "hot", "important", "developing", "shock", "surge", "plunge"]
        
        candidates = []
        now = datetime.now(timezone.utc)
        
        for a in all_articles:
            if a.get("is_breaking"):
                continue
                
            text = f"{a.get('title', '')} {a.get('summary', '')}".lower()
            match = [kw for kw in breaking_keywords if kw in text]
            
            if match:
                pub_str = a.get("published_at", "")
                try:
                    pub = datetime.fromisoformat(pub_str)
                    age_h = (now - pub).total_seconds() / 3600
                except:
                    age_h = 999
                
                candidates.append({
                    "title": a.get("title"),
                    "age_h": round(age_h, 1),
                    "keywords": match
                })
        
        print(f"\nPotential candidates (matching keywords but NOT marked): {len(candidates)}")
        
        # Sort by age
        candidates.sort(key=lambda x: x['age_h'])
        
        print("\nTop 10 candidates by recency:")
        for c in candidates[:10]:
            print(f"- [{c['age_h']}h ago] | Keywords: {c['keywords']} | {c['title']}")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    analyze()
