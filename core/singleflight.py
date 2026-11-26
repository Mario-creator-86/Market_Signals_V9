"""
core/singleflight.py — In-process request deduplication (Stage 3: Performance).

Problem: When multiple concurrent requests hit the same expensive endpoint,
the backend computes the same payload N times simultaneously.

Solution: SingleflightGroup coalesces concurrent calls with the same key —
only one call executes, all others wait and share the result.

Usage:
    group = SingleflightGroup(ttl_seconds=5.0)
    result = group.do("my_key", lambda: expensive_computation())
"""
import threading
import time
from typing import Any, Callable, Dict, Optional

_SENTINEL = object()


class _Inflight:
    """State for a single in-flight or TTL-cached call."""
    __slots__ = ("lock", "result", "exc", "done", "ts")

    def __init__(self) -> None:
        self.lock = threading.Event()
        self.result: Any = _SENTINEL
        self.exc: Optional[BaseException] = None
        self.done: bool = False
        self.ts: float = 0.0


class SingleflightGroup:
    """
    Coalesces concurrent calls with the same key.

    Parameters
    ----------
    ttl_seconds : float
        If > 0, caches the result and serves from cache for subsequent calls
        within the TTL window. Set to 0 to disable TTL (dedup only).
    max_waiters : int
        If more than this many callers are already waiting, execute independently.
    """

    def __init__(self, ttl_seconds: float = 0.0, max_waiters: int = 50) -> None:
        self._mu = threading.Lock()
        self._flights: Dict[str, _Inflight] = {}
        self._leaders: set = set()          # keys currently being computed
        self._waiter_counts: Dict[str, int] = {}
        self._ttl = float(ttl_seconds)
        self._max_waiters = int(max_waiters)

    # ------------------------------------------------------------------

    def do(self, key: str, fn: Callable[[], Any]) -> Any:
        """Execute fn() for key, or wait for in-flight execution."""
        key = str(key)
        now = time.monotonic()

        with self._mu:
            flight = self._flights.get(key)

            # 1. TTL cache hit
            if flight is not None and flight.done and self._ttl > 0:
                if now - flight.ts < self._ttl:
                    if flight.exc:
                        raise flight.exc
                    return flight.result

            # 2. Stale / expired — remove and start fresh
            if flight is not None and flight.done:
                del self._flights[key]
                self._leaders.discard(key)
                flight = None

            # 3. In-flight — join as waiter
            if flight is not None:
                wc = self._waiter_counts.get(key, 0)
                if wc >= self._max_waiters:
                    # Circuit-break: too many waiters, execute independently
                    return fn()
                self._waiter_counts[key] = wc + 1
                is_leader = False
            else:
                # 4. First caller — become leader
                flight = _Inflight()
                self._flights[key] = flight
                self._leaders.add(key)
                self._waiter_counts[key] = 0
                is_leader = True

        # ------------------------------------------------------------------
        # Outside the global lock
        # ------------------------------------------------------------------
        if is_leader:
            try:
                result = fn()
            except BaseException as exc:
                with self._mu:
                    flight.exc = exc
                    flight.done = True
                    flight.ts = time.monotonic()
                    self._leaders.discard(key)
                    if self._ttl <= 0:
                        self._flights.pop(key, None)
                        self._waiter_counts.pop(key, None)
                flight.lock.set()
                raise
            else:
                with self._mu:
                    flight.result = result
                    flight.done = True
                    flight.ts = time.monotonic()
                    self._leaders.discard(key)
                    if self._ttl <= 0:
                        self._flights.pop(key, None)
                        self._waiter_counts.pop(key, None)
                flight.lock.set()
                return result
        else:
            # Waiter: block until leader finishes
            finished = flight.lock.wait(timeout=30.0)
            with self._mu:
                wc = self._waiter_counts.get(key, 1)
                self._waiter_counts[key] = max(0, wc - 1)
            if not finished:
                # The leader is still running: compute independently rather
                # than hand back the unset placeholder.
                return fn()
            if flight.exc:
                raise flight.exc
            return flight.result

    def invalidate(self, key: str) -> None:
        """Force next call to re-execute (bypass TTL cache)."""
        with self._mu:
            self._flights.pop(str(key), None)
            self._leaders.discard(str(key))
            self._waiter_counts.pop(str(key), None)

    def active_flights(self) -> int:
        """Number of calls currently in flight."""
        with self._mu:
            return len(self._leaders)


# ---------------------------------------------------------------------------
# Module-level shared groups for the most expensive endpoints
# ---------------------------------------------------------------------------
news_state_flight    = SingleflightGroup(ttl_seconds=5.0)
calendar_state_flight = SingleflightGroup(ttl_seconds=10.0)
premarket_flight     = SingleflightGroup(ttl_seconds=5.0)
pre_earnings_flight  = SingleflightGroup(ttl_seconds=10.0)
hotlist_flight       = SingleflightGroup(ttl_seconds=5.0)
scanner_flight       = SingleflightGroup(ttl_seconds=10.0)
