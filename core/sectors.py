# core/sectors.py - Sector mapping for tickere

from typing import Dict

# Mapping ticker -> sector
TICKER_SECTORS: Dict[str, str] = {
    # === CRYPTO MINERS ===
    "CLSK": "CryptoMiner",
    "RIOT": "CryptoMiner",
    "MARA": "CryptoMiner",
    "CIFR": "CryptoMiner",
    "HUT": "CryptoMiner",
    "BITF": "CryptoMiner",
    "IREN": "CryptoMiner",
    "CORZ": "CryptoMiner",
    "BTBT": "CryptoMiner",
    "ARBK": "CryptoMiner",
    "WULF": "CryptoMiner",
    "DMGI": "CryptoMiner",
    
    # === TECHNOLOGY ===
    "GOOGL": "Technology",
    "MSFT": "Technology",
    "AAPL": "Technology",
    "META": "Technology",
    "TSLA": "Technology",
    "CRM": "Technology",
    "ORCL": "Technology",
    "ADBE": "Technology",
    "NFLX": "Technology",
    "OKTA": "Technology",
    "SNOW": "Technology",
    "PLTR": "Technology",
    "CRWD": "Technology",
    "PANW": "Technology",
    "NET": "Technology",
    "ZS": "Technology",
    "DDOG": "Technology",
    "MDB": "Technology",
    "BZ": "Technology",
    
    # === SEMICONDUCTORS ===
    "NVDA": "Semiconductors",
    "AMD": "Semiconductors",
    "INTC": "Semiconductors",
    "TSM": "Semiconductors",
    "AVGO": "Semiconductors",
    "QCOM": "Semiconductors",
    "TXN": "Semiconductors",
    "MU": "Semiconductors",
    "AMAT": "Semiconductors",
    "LRCX": "Semiconductors",
    "KLAC": "Semiconductors",
    "MRVL": "Semiconductors",
    
    # === MATERIALS ===
    "SGML": "Materials",
    "FCX": "Materials",
    "NEM": "Materials",
    "GOLD": "Materials",
    "AA": "Materials",
    "X": "Materials",
    "CLF": "Materials",
    "MT": "Materials",
    "VALE": "Materials",
    "BHP": "Materials",
    "RIO": "Materials",
    "SCCO": "Materials",
    
    # === ENERGY ===
    "XOM": "Energy",
    "CVX": "Energy",
    "COP": "Energy",
    "SLB": "Energy",
    "EOG": "Energy",
    "MPC": "Energy",
    "PSX": "Energy",
    "VLO": "Energy",
    "OXY": "Energy",
    "HAL": "Energy",
    "DVN": "Energy",
    "FANG": "Energy",
    
    # === FINANCIALS ===
    "JPM": "Financials",
    "BAC": "Financials",
    "WFC": "Financials",
    "C": "Financials",
    "GS": "Financials",
    "MS": "Financials",
    "BLK": "Financials",
    "SCHW": "Financials",
    "AXP": "Financials",
    "USB": "Financials",
    "PNC": "Financials",
    "TFC": "Financials",
    
    # === HEALTHCARE ===
    "UNH": "Healthcare",
    "JNJ": "Healthcare",
    "PFE": "Healthcare",
    "ABBV": "Healthcare",
    "TMO": "Healthcare",
    "ABT": "Healthcare",
    "DHR": "Healthcare",
    "MRK": "Healthcare",
    "BMY": "Healthcare",
    "AMGN": "Healthcare",
    "GILD": "Healthcare",
    "CVS": "Healthcare",
    "BHVN": "Healthcare",
    "GENB": "Healthcare",
    
    # === CONSUMER ===
    "AMZN": "Consumer",
    "HD": "Consumer",
    "MCD": "Consumer",
    "NKE": "Consumer",
    "SBUX": "Consumer",
    "TGT": "Consumer",
    "LOW": "Consumer",
    "COST": "Consumer",
    "WMT": "Consumer",
    "DIS": "Consumer",
    "BABA": "Consumer",
    "JD": "Consumer",
    
    # === CRYPTO (Main Coins) ===
    "BTC-USD": "Crypto",
    "ETH-USD": "Crypto",
    "BNB-USD": "Crypto",
    "SOL-USD": "Crypto",
    "XRP-USD": "Crypto",
    "ADA-USD": "Crypto",
    "DOGE-USD": "Crypto",
    "AVAX-USD": "Crypto",
    "DOT-USD": "Crypto",
    "MATIC-USD": "Crypto",
    "LINK-USD": "Crypto",
    "UNI-USD": "Crypto",
    
    # === CRYPTO (Alt Coins) ===
    "EGLD-USD": "Crypto",
    "TRX-USD": "Crypto",
    "ATOM-USD": "Crypto",
    "LTC-USD": "Crypto",
    "BCH-USD": "Crypto",
    "ALGO-USD": "Crypto",
    "VET-USD": "Crypto",
    "FIL-USD": "Crypto",
}


def get_sector(ticker: str) -> str:
    """
    Get sector for a ticker.
    - Uses explicit mapping first (TICKER_SECTORS)
    - Falls back to heuristics for crypto tickers
    - Returns 'Other' if not found
    """
    t = (ticker or "").upper().strip()

    # 1) Exact mapping (highest priority)
    if t in TICKER_SECTORS:
        return TICKER_SECTORS[t]

    # 2) Heuristic: any -USD / -USDT / USDT tickers are crypto (Binance/Yahoo style)
    # Examples: BTC-USD, MAGIC-USD, JUV-USD, BTCUSDT, ETHUSDT
    if t.endswith("-USD") or t.endswith("-USDT") or t.endswith("USDT") or t.endswith("-USDC"):
        return "Crypto"

    return "Other"



def get_tickers_by_sector(sector: str) -> list[str]:
    """
    Get all tickers in a specific sector.
    
    Args:
        sector: Sector name (e.g., 'CryptoMiner')
    
    Returns:
        List of ticker symbols in that sector
    """
    return [ticker for ticker, sec in TICKER_SECTORS.items() if sec == sector]


def get_all_sectors() -> list[str]:
    """Get list of all unique sectors."""
    return sorted(set(TICKER_SECTORS.values()))
