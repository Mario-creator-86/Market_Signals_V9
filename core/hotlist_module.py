#!/usr/bin/env python3
"""
Market Signals V7 - Hotlist Module
Complete hotlist functionality with ranking algorithm
"""
import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any
from enum import Enum

logger = logging.getLogger("HotlistModule")

class HotlistCategory(Enum):
    GAINERS = "gainers"
    LOSERS = "losers"
    MOST_ACTIVE = "most_active"
    PREMARKET = "premarket"
    AFTER_HOURS = "after_hours"
    TRENDING = "trending"
    VOLUME_LEADERS = "volume_leaders"

@dataclass
class HotlistItem:
    ticker: str
    name: str
    price: float
    change_pct: float
    volume: int
    market_cap: Optional[float] = None
    sector: Optional[str] = None
    category: HotlistCategory = HotlistCategory.GAINERS
    score: float = 0.0
    timestamp: float = field(default_factory=time.time)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "name": self.name,
            "price": self.price,
            "change_pct": self.change_pct,
            "volume": self.volume,
            "market_cap": self.market_cap,
            "sector": self.sector,
            "category": self.category.value,
            "score": self.score,
            "timestamp": self.timestamp,
            "updated": datetime.fromtimestamp(self.timestamp).isoformat(),
        }

class HotlistRanker:
    """
    Advanced ranking algorithm for hotlist items
    Considers multiple factors: price change, volume, market cap, momentum
    """
    
    def __init__(self):
        self.volume_history: Dict[str, List[float]] = {}
        self.price_history: Dict[str, List[float]] = {}
        self.max_history_size = 20
    
    def update_history(self, ticker: str, price: float, volume: int):
        """Track historical data for momentum calculation"""
        if ticker not in self.price_history:
            self.price_history[ticker] = []
            self.volume_history[ticker] = []
        
        self.price_history[ticker].append(price)
        self.volume_history[ticker].append(volume)
        
        # Keep only recent data
        if len(self.price_history[ticker]) > self.max_history_size:
            self.price_history[ticker].pop(0)
        if len(self.volume_history[ticker]) > self.max_history_size:
            self.volume_history[ticker].pop(0)
    
    def calculate_momentum(self, ticker: str) -> float:
        """Calculate price momentum based on trend"""
        prices = self.price_history.get(ticker, [])
        if len(prices) < 3:
            return 0.0
        
        # Simple momentum: rate of change over last 3 periods
        return (prices[-1] - prices[-3]) / prices[-3] if prices[-3] > 0 else 0.0
    
    def calculate_volume_surge(self, ticker: str, current_volume: int) -> float:
        """Calculate volume surge compared to average"""
        volumes = self.volume_history.get(ticker, [])
        if len(volumes) < 3:
            return 1.0
        
        avg_volume = sum(volumes[:-1]) / len(volumes[:-1])
        if avg_volume <= 0:
            return 1.0
        
        return current_volume / avg_volume
    
    def calculate_score(self, item: HotlistItem) -> float:
        """
        Calculate composite score (0-100)
        Weights:
        - Price change: 40%
        - Volume surge: 30%
        - Momentum: 20%
        - Market cap bonus: 10%
        """
        # Update history
        self.update_history(item.ticker, item.price, item.volume)
        
        # Normalize price change (cap at +/-20%)
        price_change_score = min(abs(item.change_pct), 20) / 20 * 40
        
        # Volume surge (cap at 5x)
        volume_surge = self.calculate_volume_surge(item.ticker, item.volume)
        volume_score = min(volume_surge, 5) / 5 * 30
        
        # Momentum (-10% to +10%)
        momentum = self.calculate_momentum(item.ticker)
        momentum_score = (momentum + 0.1) / 0.2 * 20
        
        # Market cap bonus (larger caps get slight bonus for stability)
        cap_bonus = 0
        if item.market_cap:
            if item.market_cap > 100_000_000_000:  # > $100B
                cap_bonus = 10
            elif item.market_cap > 10_000_000_000:  # > $10B
                cap_bonus = 7
            elif item.market_cap > 1_000_000_000:  # > $1B
                cap_bonus = 5
        
        total_score = price_change_score + volume_score + momentum_score + cap_bonus
        return min(max(total_score, 0), 100)  # Clamp to 0-100

class HotlistCache:
    """Thread-safe cache for hotlist data"""
    
    def __init__(self, ttl_seconds: int = 60):
        self.ttl_seconds = ttl_seconds
        self._cache: Dict[HotlistCategory, List[HotlistItem]] = {}
        self._last_update: Dict[HotlistCategory, float] = {}
        self._lock = threading.RLock()
    
    def get(self, category: HotlistCategory) -> Optional[List[HotlistItem]]:
        with self._lock:
            if category not in self._cache:
                return None
            
            last_update = self._last_update.get(category, 0)
            if time.time() - last_update > self.ttl_seconds:
                return None
            
            return self._cache[category]
    
    def set(self, category: HotlistCategory, items: List[HotlistItem]):
        with self._lock:
            self._cache[category] = items
            self._last_update[category] = time.time()
    
    def invalidate(self, category: Optional[HotlistCategory] = None):
        with self._lock:
            if category:
                self._cache.pop(category, None)
                self._last_update.pop(category, None)
            else:
                self._cache.clear()
                self._last_update.clear()
    
    def is_stale(self, category: HotlistCategory, max_age: int = 300) -> bool:
        with self._lock:
            last_update = self._last_update.get(category, 0)
            return time.time() - last_update > max_age

class HotlistModule:
    """
    Main hotlist module with full functionality
    """
    
    def __init__(self, cache_dir: Optional[Path] = None):
        if cache_dir is None:
            from core.config import data_path  # configured data folder (MS_DATA_DIR)
            cache_dir = data_path()
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(exist_ok=True)
        
        self.ranker = HotlistRanker()
        self.cache = HotlistCache(ttl_seconds=60)
        self._lock = threading.RLock()
        self._running = False
        self._update_task: Optional[asyncio.Task] = None
        
        # Load from disk cache on startup
        self._load_from_disk()
    
    def _get_cache_file(self, category: HotlistCategory) -> Path:
        return self.cache_dir / f"hotlist_{category.value}.json"
    
    def _save_to_disk(self, category: HotlistCategory, items: List[HotlistItem]):
        try:
            cache_file = self._get_cache_file(category)
            data = [item.to_dict() for item in items]
            with open(cache_file, 'w') as f:
                json.dump(data, f)
            logger.info(f"Saved {len(items)} hotlist items to disk for {category.value}")
        except Exception as e:
            logger.error(f"Failed to save hotlist cache: {e}")
    
    def _load_from_disk(self):
        """Load cached hotlist data from disk"""
        try:
            for category in HotlistCategory:
                cache_file = self._get_cache_file(category)
                if cache_file.exists():
                    with open(cache_file, 'r') as f:
                        data = json.load(f)
                    items = [
                        HotlistItem(
                            ticker=item["ticker"],
                            name=item.get("name", item["ticker"]),
                            price=item["price"],
                            change_pct=item["change_pct"],
                            volume=item["volume"],
                            market_cap=item.get("market_cap"),
                            sector=item.get("sector"),
                            category=HotlistCategory(item["category"]),
                            score=item.get("score", 0),
                            timestamp=item.get("timestamp", time.time()),
                        )
                        for item in data
                    ]
                    self.cache.set(category, items)
                    logger.info(f"Loaded {len(items)} hotlist items from disk for {category.value}")
        except Exception as e:
            logger.error(f"Failed to load hotlist cache: {e}")
    
    def get_hotlist(
        self,
        category: HotlistCategory = HotlistCategory.GAINERS,
        limit: int = 50,
        min_volume: int = 0,
        sector: Optional[str] = None,
    ) -> List[HotlistItem]:
        """
        Get hotlist for a specific category with optional filtering
        """
        # Try cache first
        cached = self.cache.get(category)
        
        if cached is None:
            logger.warning(f"Hotlist cache miss for {category.value}, returning empty list")
            return []
        
        # Apply filters
        filtered = cached
        if min_volume > 0:
            filtered = [item for item in filtered if item.volume >= min_volume]
        if sector:
            filtered = [item for item in filtered if item.sector == sector]
        
        # Sort by score descending
        filtered.sort(key=lambda x: x.score, reverse=True)
        
        return filtered[:limit]
    
    def update_hotlist(self, category: HotlistCategory, raw_data: List[Dict]):
        """
        Update hotlist data from external source (FMP, etc.)
        """
        try:
            items = []
            for data in raw_data:
                item = HotlistItem(
                    ticker=data.get("symbol", data.get("ticker", "")),
                    name=data.get("name", data.get("symbol", "")),
                    price=float(data.get("price", 0)),
                    change_pct=float(data.get("changesPercentage", data.get("change", 0))),
                    volume=int(data.get("volume", 0)),
                    market_cap=float(data.get("marketCap", 0)) if data.get("marketCap") else None,
                    sector=data.get("sector"),
                    category=category,
                )
                
                # Calculate ranking score
                item.score = self.ranker.calculate_score(item)
                items.append(item)
            
            # Sort by score
            items.sort(key=lambda x: x.score, reverse=True)
            
            # Update cache
            self.cache.set(category, items)
            
            # Persist to disk
            self._save_to_disk(category, items)
            
            logger.info(f"Updated hotlist for {category.value}: {len(items)} items")
            
        except Exception as e:
            logger.error(f"Failed to update hotlist for {category.value}: {e}")
    
    def get_all_categories(self) -> Dict[str, List[Dict]]:
        """Get all hotlist categories"""
        result = {}
        for category in HotlistCategory:
            items = self.get_hotlist(category, limit=20)
            result[category.value] = [item.to_dict() for item in items]
        return result
    
    def get_trending(self, limit: int = 20) -> List[HotlistItem]:
        """Get trending tickers across all categories"""
        all_items: Dict[str, HotlistItem] = {}
        
        for category in HotlistCategory:
            items = self.get_hotlist(category, limit=50)
            for item in items:
                if item.ticker not in all_items or item.score > all_items[item.ticker].score:
                    all_items[item.ticker] = item
        
        # Sort by score and return top
        trending = sorted(all_items.values(), key=lambda x: x.score, reverse=True)
        return trending[:limit]
    
    def search(self, query: str, limit: int = 10) -> List[HotlistItem]:
        """Search hotlist items by ticker or name"""
        query = query.upper()
        results = []
        
        for category in HotlistCategory:
            items = self.get_hotlist(category, limit=100)
            for item in items:
                if query in item.ticker.upper() or query in item.name.upper():
                    results.append(item)
        
        # Remove duplicates and sort by relevance (score)
        seen = set()
        unique = []
        for item in results:
            if item.ticker not in seen:
                seen.add(item.ticker)
                unique.append(item)
        
        unique.sort(key=lambda x: x.score, reverse=True)
        return unique[:limit]
    
    def start_background_updates(self):
        """Start background update task"""
        if self._running:
            return
        
        self._running = True
        logger.info("Hotlist background updates started")
    
    def stop_background_updates(self):
        """Stop background update task"""
        self._running = False
        if self._update_task:
            self._update_task.cancel()
        logger.info("Hotlist background updates stopped")
    
    def invalidate_cache(self):
        """Invalidate all caches"""
        self.cache.invalidate()
        logger.info("Hotlist cache invalidated")


# Singleton instance
_hotlist_module: Optional[HotlistModule] = None

def get_hotlist_module() -> HotlistModule:
    """Get or create singleton hotlist module"""
    global _hotlist_module
    if _hotlist_module is None:
        _hotlist_module = HotlistModule()
    return _hotlist_module


if __name__ == "__main__":
    # Test the module
    hl = get_hotlist_module()
    
    # Simulate some test data
    test_data = [
        {
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "price": 175.50,
            "changesPercentage": 5.2,
            "volume": 50000000,
            "marketCap": 2800000000000,
            "sector": "Technology",
        },
        {
            "symbol": "TSLA",
            "name": "Tesla Inc.",
            "price": 250.30,
            "changesPercentage": 8.5,
            "volume": 35000000,
            "marketCap": 800000000000,
            "sector": "Consumer Cyclical",
        },
    ]
    
    hl.update_hotlist(HotlistCategory.GAINERS, test_data)
    
    gainers = hl.get_hotlist(HotlistCategory.GAINERS, limit=10)
    for item in gainers:
        print(f"{item.ticker}: {item.change_pct:+.2f}% (Score: {item.score:.1f})")
