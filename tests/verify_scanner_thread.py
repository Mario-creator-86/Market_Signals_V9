
import yfinance as yf
import time
import signal
import sys
import threading

def signal_handler(sig, frame):
    print("\n✅ SIGINT received! Process is responsive.")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

def run_download():
    print("🚀 Starting yf.download(threads=False)...")
    tickers = ["AAPL", "NVDA", "TSLA", "AMD", "MSFT", "GOOGL", "AMZN", "META", "NFLX", "INTC"]
    start = time.time()
    data = yf.download(tickers, period="2d", progress=False, threads=False)
    end = time.time()
    print(f"✅ Download complete in {end - start:.2f}s")
    if data.empty:
        print("⚠️ Data is empty (might be network issue, but didn't hang)")
    else:
        print(f"📊 Rows downloaded: {len(data)}")

print("⏳ Press Ctrl+C anytime to test responsiveness.")
t = threading.Thread(target=run_download)
t.start()
t.join()
print("🎉 Test Complete.")
