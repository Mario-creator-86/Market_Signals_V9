from agents.news_tab_agent import get_news_tab_agent, NEWS_BACKUP_PATH
import os
import json

def test_backup():
    print("Testing News Backup System...")
    agent = get_news_tab_agent()
    
    # Force a save (even if empty) to create file
    print("Forcing backup save...")
    agent._save_backup()
    
    if os.path.exists(NEWS_BACKUP_PATH):
        print(f"✅ Backup file created: {NEWS_BACKUP_PATH}")
        with open(NEWS_BACKUP_PATH, 'r') as f:
            data = json.load(f)
            print(f"Backup contains {len(data.get('market_news', []))} market news items.")
    else:
        print("❌ Backup file NOT found!")

if __name__ == "__main__":
    test_backup()
