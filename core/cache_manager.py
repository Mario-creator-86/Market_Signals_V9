import threading
import time
from typing import Any, Dict, Optional, Tuple

from core.observability import record_cache_decision
from core.request_context import get_request_context
from core.data_quality import get_health_dashboard, QualifiedValue


class CacheManager:
    DEFAULT_MAX_ENTRIES = 500

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES):
        self._lock = threading.RLock()
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._hits_total = 0
        self._miss_total = 0
        self._max_entries = max(50, max_entries)

    def get(self, key: str, ttl_seconds: Optional[int] = None, *, source: str = "generic") -> Tuple[Any, str, Optional[float]]:
        now_ts = time.time()
        with self._lock:
            entry = self._entries.get(key)
            if not entry:
                self._miss_total += 1
                self._record(key, "MISS", "missing key", source, ttl_seconds=ttl_seconds, age_seconds=None)
                return None, "MISS", None

            ttl = int(ttl_seconds if ttl_seconds is not None else entry.get("ttl_seconds", 0) or 0)
            ts = float(entry.get("ts", 0.0))
            age = max(0.0, now_ts - ts)
            if ttl > 0 and age > ttl:
                self._miss_total += 1
                entry["misses"] = int(entry.get("misses", 0)) + 1
                # Auto-evict expired entry on access
                stale_value = entry.get("value")
                del self._entries[key]
                self._record(key, "STALE", "ttl expired", source, ttl_seconds=ttl, age_seconds=age)
                get_health_dashboard().register(
                    f"cache_stale_{key}",
                    QualifiedValue.stale(stale_value, source, int(age))
                )
                return None, "STALE", age

            self._hits_total += 1
            entry["hits"] = int(entry.get("hits", 0)) + 1
            self._record(key, "HIT", "cache hit", source, ttl_seconds=ttl, age_seconds=age)
            return entry.get("value"), "HIT", age

    def set(self, key: str, value: Any, ttl_seconds: int, *, source: str = "generic") -> None:
        now_ts = time.time()
        with self._lock:
            # Evict expired entries when approaching max size
            if len(self._entries) + 1 > self._max_entries:
                self._evict_expired()
            # If still over max after eviction, remove oldest entries
            if len(self._entries) + 1 > self._max_entries:
                excess = len(self._entries) + 1 - self._max_entries
                self._evict_oldest(count=max(1, excess))
            prev = self._entries.get(key, {})
            self._entries[key] = {
                "value": value,
                "ts": now_ts,
                "ttl_seconds": max(0, int(ttl_seconds or 0)),
                "hits": int(prev.get("hits", 0)),
                "misses": int(prev.get("misses", 0)),
                "source": source,
            }
            self._record(key, "SET", "write cache", source, ttl_seconds=int(ttl_seconds or 0), age_seconds=0.0)

    def delete(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._hits_total = 0
            self._miss_total = 0

    def _evict_expired(self) -> int:
        """Remove all expired entries. Must be called with lock held."""
        now_ts = time.time()
        expired_keys = []
        for key, entry in self._entries.items():
            ttl = int(entry.get("ttl_seconds", 0))
            ts = float(entry.get("ts", 0.0))
            if ttl > 0 and (now_ts - ts) > ttl:
                expired_keys.append(key)
        for key in expired_keys:
            del self._entries[key]
        return len(expired_keys)

    def _evict_oldest(self, count: int = 1) -> int:
        """Remove the oldest entries by timestamp. Must be called with lock held."""
        if not self._entries:
            return 0
        sorted_keys = sorted(
            self._entries.keys(),
            key=lambda k: float(self._entries[k].get("ts", 0.0))
        )
        to_remove = sorted_keys[:max(1, count)]
        for key in to_remove:
            del self._entries[key]
        return len(to_remove)

    def inventory(self) -> Dict[str, Any]:
        now_ts = time.time()
        rows = []
        with self._lock:
            for key, entry in self._entries.items():
                ts = float(entry.get("ts", 0.0))
                ttl = int(entry.get("ttl_seconds", 0))
                age = max(0.0, now_ts - ts)
                rows.append(
                    {
                        "key": key,
                        "source": entry.get("source", "generic"),
                        "age_seconds": round(age, 3),
                        "ttl_seconds": ttl,
                        "is_expired": bool(ttl > 0 and age > ttl),
                        "hits": int(entry.get("hits", 0)),
                        "misses": int(entry.get("misses", 0)),
                    }
                )
            return {
                "entries": rows,
                "size": len(rows),
                "hit_total": int(self._hits_total),
                "miss_total": int(self._miss_total),
            }

    def record_manual_decision(
        self,
        *,
        key: str,
        decision: str,
        reason: str,
        source: str = "generic",
        ttl_seconds: Optional[int] = None,
        age_seconds: Optional[float] = None,
    ) -> None:
        self._record(
            key,
            decision,
            reason,
            source,
            ttl_seconds=ttl_seconds,
            age_seconds=age_seconds,
        )

    def _record(
        self,
        key: str,
        decision: str,
        reason: str,
        source: str,
        *,
        ttl_seconds: Optional[int],
        age_seconds: Optional[float],
    ) -> None:
        ctx = get_request_context()
        if ctx is not None and isinstance(ctx.metadata, dict):
            ctx.metadata["cache_reason"] = str(decision or "").upper()
        record_cache_decision(
            key=key,
            decision=decision,
            reason=reason,
            source=source,
            ttl_seconds=ttl_seconds,
            age_seconds=age_seconds,
            context_reason=(ctx.reason if ctx else None),
        )


cache_manager = CacheManager()
