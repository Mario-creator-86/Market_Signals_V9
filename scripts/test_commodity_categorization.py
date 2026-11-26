"""
Script to verify commodity news categorization.
Runs a market news fetch and checks if GOLD/SILVER articles are correctly categorized.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.news_tab_agent import get_news_tab_agent

def test_commodity_categorization():
    print("🚀 Testing Commodity News Categorization...")
    
    agent = get_news_tab_agent()
    
    # Fetch market news
    print("📡 Fetching market news...")
    articles = agent._fetch_market_news()
    
    print(f"📊 Total market news fetched: {len(articles)}")
    
    gold_articles = [a for a in articles if a.ticker == "GOLD"]
    silver_articles = [a for a in articles if a.ticker == "SILVER"]
    oil_articles = [a for a in articles if a.ticker == "CL=F"]
    market_articles = [a for a in articles if a.is_market_wide]
    
    print(f"🥇 GOLD articles: {len(gold_articles)}")
    print(f"🥈 SILVER articles: {len(silver_articles)}")
    print(f"🛢️ OIL articles: {len(oil_articles)}")
    print(f"📰 MARKET articles: {len(market_articles)}")
    
    if gold_articles:
        print("\n--- Sample GOLD articles ---")
        for a in gold_articles[:3]:
            print(f"  • {a.title[:60]}...")
    
    # Check for false negatives (articles with "gold" in title that went to market)
    false_negatives = [a for a in market_articles if "gold" in a.title.lower() and a.ticker is None]
    if false_negatives:
        print(f"\n⚠️ Possible false negatives (gold in title but under MARKET): {len(false_negatives)}")
        for a in false_negatives[:3]:
            print(f"  • {a.title[:60]}...")
    else:
        print("\n✅ No false negatives detected!")

if __name__ == "__main__":
    test_commodity_categorization()
