import json
import os

def check_file(path):
    if not os.path.exists(path):
        print(f"{path}: Not Found")
        return 0
    
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        if isinstance(data, list):
            print(f"{path}: Is a LIST with {len(data)} items.")
            # Verify if items look like news
            if data and isinstance(data[0], dict) and "title" in data[0]:
                print(f"{path}: Seems to be a list of {len(data)} news items!")
                return len(data)
            return 0
            
        news = data.get("news", {})
        if isinstance(news, list):
            print(f"{path}: 'news' field is a LIST with {len(news)} items!")
            return len(news)
            
        market = news.get("market_news", [])
        print(f"{path}: {len(market)} market news found.")
        return len(market)
    except Exception as e:
        print(f"{path}: Error {e}")
        return 0

print("--- State News Count ---")
c1 = check_file("state.json")
c2 = check_file("frontend/state.json")
# c3 = check_file("state_debug.json") # Broken
