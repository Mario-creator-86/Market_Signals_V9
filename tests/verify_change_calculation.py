
import requests

def test_finnhub(ticker):
    print(f"\n--- Testing Finnhub: {ticker} ---")
    key = "d4uosl9r01qnm7pnaj0gd4uosl9r01qnm7pnaj10"
    url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={key}"
    try:
        res = requests.get(url, timeout=10)
        data = res.json()
        print(f"Data: {data}")
        if data and 'c' in data and data['c'] > 0:
            price = float(data['c'])
            prev = float(data['pc'])
            change_pct_api = float(data['dp']) # 'dp' is percent change
            
            print(f"Price: {price}")
            print(f"Prev: {prev}")
            print(f"API Pct: {change_pct_api}%")
            
            if prev > 0:
                calc = ((price - prev)/prev)*100
                print(f"Calc Pct: {calc:.4f}%")
        else:
            print("❌ Invalid/Empty Data")
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    test_finnhub("CIFR")
    test_finnhub("AAPL")
