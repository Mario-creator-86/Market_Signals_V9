import os
import json
import logging
from pathlib import Path
from enum import Enum
from dataclasses import dataclass
from typing import Any, Optional, Dict
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("DataQuality")

class QualityLevel(Enum):
    VERIFIED = "VERIFIED"       # Real data from API/Broker
    ESTIMATED = "ESTIMATED"     # Pre-calculated, interpolated, or fallback data
    STALE = "STALE"             # Valid data but older than allowed threshold
    UNAVAILABLE = "UNAVAILABLE" # Missing completely or error fetching

@dataclass
class QualifiedValue:
    """A wrapper for values that guarantees traceability of data quality and origin."""
    value: Any
    level: QualityLevel
    source: str
    timestamp: datetime
    message: Optional[str] = None
    freshness: Optional[timedelta] = None
    coverage: Optional[float] = None
    conflicts: Optional[list] = None
    quality_flags: frozenset = frozenset()

    @classmethod
    def verified(cls, value: Any, source: str) -> "QualifiedValue":
        return cls(value, QualityLevel.VERIFIED, source, datetime.now(timezone.utc))

    @classmethod
    def estimated(cls, value: Any, source: str, message: str = "") -> "QualifiedValue":
        return cls(value, QualityLevel.ESTIMATED, source, datetime.now(timezone.utc), message)

    @classmethod
    def stale(cls, value: Any, source: str, age_seconds: int) -> "QualifiedValue":
        return cls(value, QualityLevel.STALE, source, datetime.now(timezone.utc), f"Data is {age_seconds}s old")

    @classmethod
    def unavailable(cls, source: str, message: str = "Missing data") -> "QualifiedValue":
        return cls(None, QualityLevel.UNAVAILABLE, source, datetime.now(timezone.utc), message)

    def is_valid(self) -> bool:
        """Returns True if the data is at least estimated and not completely missing or stale."""
        return self.level in (QualityLevel.VERIFIED, QualityLevel.ESTIMATED)

    def to_dict(self) -> dict:
        d = {
            "value": self.value,
            "level": self.level.value,
            "source": self.source,
            "timestamp": self.timestamp.isoformat(),
            "message": self.message,
            "is_valid": self.is_valid()
        }
        if self.freshness is not None:
            d["freshness"] = self.freshness.total_seconds()
        if self.coverage is not None:
            d["coverage"] = self.coverage
        if self.conflicts:
            d["conflicts"] = [
                {"field": c.field, "values": list(c.values), "sources": list(c.sources),
                 "deviation_pct": c.deviation_pct}
                for c in self.conflicts
            ]
        if self.quality_flags:
            d["quality_flags"] = sorted(self.quality_flags)
        return d


class DataHealthDashboard:
    """Live registry to track overall system data quality."""
    
    def __init__(self, log_dir: Optional[str] = None):
        self.log_dir = str(
            Path(
                log_dir
                or os.getenv("MS_AUDIT_LOG_DIR")
                or (Path(__file__).resolve().parent.parent / "audit_logs")
            )
        )
        self.registry: Dict[str, QualifiedValue] = {}
        os.makedirs(self.log_dir, exist_ok=True)
        
    def register(self, key: str, qval: QualifiedValue):
        """Update the live dashboard registry with the latest QualifiedValue for a key."""
        self.registry[key] = qval
        
        # Log unavailable immediately for tracking
        if qval.level == QualityLevel.UNAVAILABLE:
            logger.warning(f"DataQuality Issue [{key}]: {qval.message} (Source: {qval.source})")
            
    def get_live_dashboard(self) -> Dict[str, Any]:
        """Provides a summary of all registered data streams for UI Health checks."""
        summary = {
            "total_streams": len(self.registry),
            "verified": sum(1 for v in self.registry.values() if v.level == QualityLevel.VERIFIED),
            "estimated": sum(1 for v in self.registry.values() if v.level == QualityLevel.ESTIMATED),
            "stale": sum(1 for v in self.registry.values() if v.level == QualityLevel.STALE),
            "unavailable": sum(1 for v in self.registry.values() if v.level == QualityLevel.UNAVAILABLE),
            "streams": {k: v.to_dict() for k, v in self.registry.items()}
        }
        return summary
        
    def perform_daily_audit(self):
        """Run daily at 06:00 UTC (or generated periodically) to snapshot system data quality."""
        now = datetime.now(timezone.utc)
        dashboard_data = self.get_live_dashboard()
        
        filename = f"health_audit_{now.strftime('%Y%m%d_%H%M%S')}.json"
        filepath = os.path.join(self.log_dir, filename)
        
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(dashboard_data, f, indent=4)
            logger.info(f"Daily health audit saved to {filepath}")
        except Exception as e:
            logger.error(f"Failed to write daily health audit: {e}")

# Global instance for easy access
_global_dashboard = DataHealthDashboard()

def get_health_dashboard() -> DataHealthDashboard:
    return _global_dashboard
