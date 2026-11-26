
import logging
import pandas as pd
import yfinance as yf
import requests
import time

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ScannerTest")

def get_yahoo_top_movers():
    print("🔍 Fetching Yahoo Top Movers...")
    headers = {'User-Agent': 'Mozilla/5.0'}
    symbols = set()
    
    endpoints = [
        "day_gainers", "day_losers", "most_actives"
    ]
    
    for ep in endpoints:
        try:
            url = f"https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds={ep}&count=25"
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                data = res.json()
                quotes = data.get('finance', {}).get('result', [{}])[0].get('quotes', [])
                count = 0
                for q in quotes:
                    sym = q.get('symbol', '')
                    if sym and '.' not in sym and not any(c.isdigit() for c in sym):
                        symbols.add(sym)
                        count += 1
                print(f"  - {ep}: Found {count} valid symbols")
            else:
                print(f"  - {ep}: Failed ({res.status_code})")
        except Exception as e:
            print(f"  - {ep}: Error {e}")
            
    return list(symbols)[:30]

def download_chunked(tickers, chunk_size=5):
    print(f"\n⬇️ Downloading {len(tickers)} tickers in chunks of {chunk_size}...")
    all_data = []
    
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i + chunk_size]
        print(f"  - Chunk {i//chunk_size + 1}: {chunk}")
        try:
            d = yf.download(chunk, period="2d", progress=False, threads=False)
            if not d.empty:
                all_data.append(d)
        except Exception as e:
            print(f"    Error: {e}")
            
    if not all_data:
        return pd.DataFrame()
        
    try:
        # If headers are MultiIndex, concat axis=1 works to merge columns
        combined = pd.concat(all_data, axis=1)
        return combined
    except Exception as e:
        print(f"  Concatenation error: {e}")
        return pd.DataFrame()

def process_data(data, threshold=0.5):
    print(f"\n📊 Processing Data (Threshold: {threshold}%)...")
    results = []
    
    # Check structure
    if isinstance(data.columns, pd.MultiIndex):
        print("  - Detected MultiIndex columns")
        tickers = data['Close'].columns
    else:
        print("  - Detected Single Index columns (Single ticker?)")
        # In newer yfinance, even single ticker might be multiindex depending on auto_adjust
        # or it might be just 'Close'
        return []

    for sym in tickers:
        try:
            closes = data['Close'][sym].dropna()
            if len(closes) < 2:
                continue
            
            current = float(closes.iloc[-1])
            prev = float(closes.iloc[-2])
            
            if prev > 0:
                change = ((current - prev) / prev) * 100
                if abs(change) >= threshold:
                    results.append((sym, change))
        except Exception as e:
            pass
            
    results.sort(key=lambda x: abs(x[1]), reverse=True)
    return results

# === MAIN Execution ===
print("--- TEST START ---")
screener_syms = get_yahoo_top_movers()
print(f"Total Unique Symbols: {len(screener_syms)}")

if screener_syms:
    df = download_chunked(screener_syms)
    if not df.empty:
        # print(df.head())
        hits = process_data(df, threshold=0.5)
        print(f"\n✅ Found {len(hits)} stocks matching > 0.5%:")
        for sym, chg in hits[:10]:
            print(f"  {sym}: {chg:.2f}%")
    else:
        print("❌ DataFrame Empty")
else:
    print("❌ No symbols found")

print("--- TEST END ---")
