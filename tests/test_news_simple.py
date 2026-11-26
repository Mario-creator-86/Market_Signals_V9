"""Simple news availability test (manual)."""
import os
import sys
from datetime import datetime

import pytest
import requests
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Test tickers from watchlist
TICKERS = ["MARA", "RIOT", "PINS", "CLSK", "AAPL"]


def main() -> None:
    print("=" * 80)
    print("News Availability Test")
    print("=" * 80)

    for ticker in TICKERS:
        print(f"\n\nTicker: {ticker}")
        print("-" * 80)

        # Yahoo News
        try:
            stock = yf.Ticker(ticker)
            yahoo_news = stock.news
            print(f"Yahoo: {len(yahoo_news)} articles")
            if yahoo_news:
                latest = yahoo_news[0]
                pub_ts = latest.get("providerPublishTime", 0)
                pub_date = datetime.fromtimestamp(pub_ts).strftime("%Y-%m-%d %H:%M") if pub_ts else "N/A"
                print(f"  Latest: [{pub_date}] {latest.get('title', 'N/A')[:60]}")
        except Exception as e:
            print(f"Yahoo ERROR: {e}")

        # FMP News
        try:
            fmp_key = os.getenv("FMP_API_KEY", "your_key_here")
            url = f"https://financialmodelingprep.com/api/v3/stock_news?tickers={ticker}&limit=3&apikey={fmp_key}"
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                fmp_news = r.json()
                print(f"FMP: {len(fmp_news)} articles")
                if fmp_news:
                    latest = fmp_news[0]
                    print(f"  Latest: [{latest.get('publishedDate', 'N/A')}] {latest.get('title', 'N/A')[:60]}")
            else:
                print(f"FMP ERROR: {r.status_code}")
        except Exception as e:
            print(f"FMP ERROR: {e}")

    print("\n" + "=" * 80)
    print("Test Complete")
    print("=" * 80)


def test_news_simple_manual():
    pytest.skip("Manual news availability check; run this module directly.")


if __name__ == "__main__":
    main()
