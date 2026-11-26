import time
import logging
from typing import Dict

logger = logging.getLogger("DataGuard")

class DataGuard:
    def __init__(self, tolerance_seconds: int = 300):
        self.tolerance_seconds = tolerance_seconds
        self.last_timestamps: Dict[str, float] = {}
        
    def _is_market_hours(self) -> bool:
        try:
            from core.engine import is_us_market_open
            return is_us_market_open()
        except Exception:
            return True
            
    def update_timestamp(self, source: str, ts: float = None):
        """Updates the last successful fetch timestamp for a source."""
        if ts is None:
            ts = time.time()
        self.last_timestamps[source.lower()] = ts
        
    def check_freshness(self) -> Dict[str, str]:
        """Returns the freshness state of each critical source."""
        now = time.time()
        state = {}
        in_hours = self._is_market_hours()
        
        for src in ["binance", "news", "yahoo"]:
            ts = self.last_timestamps.get(src, 0.0)
            if ts == 0.0:
                state[src] = "UNKNOWN"
            else:
                age = now - ts
                if age > self.tolerance_seconds:
                    if src in ("binance", "news") or in_hours:
                        state[src] = "STALE"
                    else:
                        state[src] = "FRESH_OFF_HOURS"
                else:
                    state[src] = "FRESH"
                    
        return state

    def is_global_refresh_blocked(self) -> bool:
        """Returns True if any strictly required source is STALE."""
        state = self.check_freshness()
        for src, status in state.items():
            if status == "STALE":
                # Stale critical source during required hours blocks global refresh
                if src in ("yahoo",) and not self._is_market_hours():
                    continue # Ignore stock freshness after hours
                logger.warning(f"DataGuard BLOCKING refresh due to trailing source: {src}={status}")
                return True
        return False

_global_guard = DataGuard()

def get_data_guard() -> DataGuard:
    return _global_guard
