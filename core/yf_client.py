"""Yahoo Finance Data Client - Replaces FMP"""
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import time

from core.data_quality import get_health_dashboard, QualifiedValue

class YahooFinanceClient:
    def __init__(self):
        self.rate_limit_delay = 0.2
        
    def get_stock_data(self, symbol, period="1d", interval="5m"):
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period=period, interval=interval)
            time.sleep(self.rate_limit_delay)
            # Strip timezone to prevent "Cannot compare tz-naive and tz-aware timestamps" errors
            if df is not None and not df.empty:
                if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None:
                    df.index = df.index.tz_convert(None)
                get_health_dashboard().register("yf_client_stock_data", QualifiedValue.verified(df, "yfinance_history"))
            else:
                get_health_dashboard().register("yf_client_stock_data", QualifiedValue.unavailable("yfinance_history", f"Empty response for {symbol}"))
            return df
        except Exception as e:
            print(f"Error fetching {symbol}: {e}")
            get_health_dashboard().register("yf_client_stock_data", QualifiedValue.unavailable("yfinance_history", str(e)))
            return None
    
    def get_crypto_data(self, symbol, period="1d", interval="5m"):
        if "-" not in symbol:
            symbol = f"{symbol}-USD"
        return self.get_stock_data(symbol, period, interval)
    
    def get_ticker_info(self, symbol):
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            time.sleep(self.rate_limit_delay)
            if info:
                get_health_dashboard().register("yf_client_ticker_info", QualifiedValue.verified(info, "yfinance_info"))
            else:
                get_health_dashboard().register("yf_client_ticker_info", QualifiedValue.unavailable("yfinance_info", f"Empty info for {symbol}"))
            return info
        except Exception as e:
            print(f"Error fetching info for {symbol}: {e}")
            get_health_dashboard().register("yf_client_ticker_info", QualifiedValue.unavailable("yfinance_info", str(e)))
            return {}

yf_client = YahooFinanceClient()
