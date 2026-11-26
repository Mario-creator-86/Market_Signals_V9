
import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.news_tab_agent import get_news_tab_agent, NewsCategory

def reanalyze():
    print("🚀 Starting re-analysis of existing news...")
    
    agent = get_news_tab_agent()
    
    # The agent already loaded the backup in __init__
    with agent._cache_lock:
        state = agent._state
        
        all_articles = []
        all_articles.extend(state.market_news)
        all_articles.extend(state.breaking_news)
        for ticker, articles in state.ticker_news.items():
            all_articles.extend(articles)
            
        total = len(all_articles)
        print(f"📊 Total articles to re-analyze: {total}")
        
        if total == 0:
            print("❌ No articles found in state. Check news_backup.json.")
            return

        breaking_before = len(state.breaking_news)
        
        # Re-analyze each
        for i, article in enumerate(all_articles):
            # We need to reset is_breaking and category to let them be re-detected
            # BUT _analyze_article does this internally.
            agent._analyze_article(article)
            
            if (i + 1) % 100 == 0:
                print(f"  Processed {i+1}/{total}...")

        # Re-organize state based on new tags
        agent._organize_state(all_articles)
        
        breaking_after = len(state.breaking_news)
        print(f"✅ Re-analysis complete!")
        print(f"🔥 Breaking News: {breaking_before} -> {breaking_after}")
        
        # Save updated backup
        agent._save_backup()
        print("💾 Backup updated on disk.")

if __name__ == "__main__":
    reanalyze()
