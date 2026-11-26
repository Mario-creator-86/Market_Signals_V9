"""
tests/unit/test_singleflight.py
================================
Unit tests for core/singleflight.py :: SingleflightGroup

Tests:
  - Basic deduplication (single caller)
  - TTL cache hit / expiry
  - Concurrent callers share result (only one fn() call)
  - Exception propagation to all waiters
  - invalidate() clears cache
  - active_flights() counter
  - max_waiters circuit-breaker (no dedup when overloaded)
  - Module-level singleton groups exist + have correct TTLs
"""

import sys
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

from core.singleflight import (
    SingleflightGroup,
    news_state_flight,
    calendar_state_flight,
    premarket_flight,
    pre_earnings_flight,
    hotlist_flight,
    scanner_flight,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Basic single-caller usage
# ═══════════════════════════════════════════════════════════════════════════════

class TestBasicUsage:
    def test_returns_fn_result(self):
        g = SingleflightGroup()
        result = g.do("k", lambda: 42)
        assert result == 42

    def test_fn_called_once_per_key(self):
        g = SingleflightGroup()
        counter = [0]
        def inc():
            counter[0] += 1
            return counter[0]
        r = g.do("k", inc)
        assert r == 1
        assert counter[0] == 1

    def test_different_keys_independent(self):
        g = SingleflightGroup()
        r1 = g.do("a", lambda: 1)
        r2 = g.do("b", lambda: 2)
        assert r1 == 1 and r2 == 2

    def test_key_coerced_to_str(self):
        g = SingleflightGroup()
        r = g.do(123, lambda: "ok")
        assert r == "ok"

    def test_returns_none_result(self):
        g = SingleflightGroup()
        assert g.do("k", lambda: None) is None

    def test_returns_complex_object(self):
        g = SingleflightGroup()
        data = {"x": [1, 2, 3]}
        assert g.do("k", lambda: data) == data


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Exception propagation
# ═══════════════════════════════════════════════════════════════════════════════

class TestExceptionPropagation:
    def test_exception_re_raised_to_leader(self):
        g = SingleflightGroup()
        def boom():
            raise ValueError("exploded")
        with pytest.raises(ValueError, match="exploded"):
            g.do("k", boom)

    def test_exception_re_raised_type_error(self):
        g = SingleflightGroup()
        with pytest.raises(TypeError):
            g.do("k", lambda: 1 / "bad")

    def test_after_exception_key_cleaned_up(self):
        g = SingleflightGroup()
        try:
            g.do("k", lambda: (_ for _ in ()).throw(RuntimeError("oops")))
        except RuntimeError:
            pass
        # Next call should work fine
        result = g.do("k", lambda: 99)
        assert result == 99

    def test_concurrent_exception_propagated_to_waiters(self):
        """All waiting threads must receive the exception."""
        g = SingleflightGroup()
        call_count = [0]
        errors = []
        results = []
        hold = threading.Event()

        def slow_fail():
            call_count[0] += 1
            hold.wait(timeout=3.0)
            raise RuntimeError("shared failure")

        def worker():
            try:
                r = g.do("k", slow_fail)
                results.append(r)
            except RuntimeError as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()

        # Give threads time to enter do(); on loaded CI 100 ms is safe
        time.sleep(0.1)

        hold.set()  # release leader
        for t in threads:
            t.join(timeout=5.0)

        # Only leader ran fn() — 1 call
        assert call_count[0] == 1
        # All 3 got the exception
        assert len(errors) == 3
        assert all("shared failure" in e for e in errors)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Deduplication under concurrency
# ═══════════════════════════════════════════════════════════════════════════════

class TestConcurrentDeduplication:
    def _run_concurrent(self, g, n=5, sleep=0.05):
        """Spin up n threads all calling g.do('k', slow_fn)."""
        call_count = [0]
        results = []
        lock = threading.Lock()
        go = threading.Event()

        def slow_fn():
            with lock:
                call_count[0] += 1
            time.sleep(sleep)
            return "shared"

        def worker():
            go.wait(timeout=5.0)
            results.append(g.do("k", slow_fn))

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        # Let all workers reach go.wait(), then release simultaneously
        time.sleep(0.05)
        go.set()
        for t in threads:
            t.join(timeout=10.0)

        return call_count[0], results

    def test_only_one_fn_call_among_concurrent(self):
        g = SingleflightGroup()
        call_count, results = self._run_concurrent(g, n=5)
        assert call_count == 1
        assert results == ["shared"] * 5

    def test_active_flights_increments_during_call(self):
        g = SingleflightGroup()
        started = threading.Event()
        finished = threading.Event()

        def slow():
            started.set()
            finished.wait(timeout=2.0)
            return "done"

        t = threading.Thread(target=lambda: g.do("k", slow))
        t.start()
        started.wait(timeout=2.0)
        assert g.active_flights() == 1
        finished.set()
        t.join(timeout=2.0)

    def test_active_flights_zero_after_completion(self):
        g = SingleflightGroup()
        g.do("k", lambda: 1)
        assert g.active_flights() == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 4. TTL cache
# ═══════════════════════════════════════════════════════════════════════════════

class TestTtlCache:
    def test_ttl_cache_serves_from_cache(self):
        g = SingleflightGroup(ttl_seconds=5.0)
        call_count = [0]
        def fn():
            call_count[0] += 1
            return 100
        r1 = g.do("k", fn)
        r2 = g.do("k", fn)
        assert r1 == r2 == 100
        assert call_count[0] == 1  # second call served from TTL cache

    def test_ttl_zero_no_cache(self):
        g = SingleflightGroup(ttl_seconds=0.0)
        call_count = [0]
        def fn():
            call_count[0] += 1
            return call_count[0]
        g.do("k", fn)
        g.do("k", fn)
        assert call_count[0] == 2  # no caching → both calls executed

    def test_ttl_expired_re_executes(self):
        g = SingleflightGroup(ttl_seconds=0.05)  # 50ms TTL
        call_count = [0]
        def fn():
            call_count[0] += 1
            return call_count[0]
        g.do("k", fn)
        time.sleep(0.1)  # wait for TTL to expire
        g.do("k", fn)
        assert call_count[0] == 2

    def test_ttl_different_keys_independent_cache(self):
        g = SingleflightGroup(ttl_seconds=5.0)
        g.do("a", lambda: 1)
        g.do("b", lambda: 2)
        call_count = [0]
        g.do("a", lambda: (call_count.__setitem__(0, call_count[0] + 1)) or 99)
        assert call_count[0] == 0  # "a" was cached

    def test_ttl_exception_not_cached(self):
        """Exceptions with TTL=0 should not be cached; next call re-executes."""
        g = SingleflightGroup(ttl_seconds=0.0)
        call_count = [0]
        def fn():
            call_count[0] += 1
            if call_count[0] == 1:
                raise ValueError("first call fails")
            return "ok"
        with pytest.raises(ValueError):
            g.do("k", fn)
        result = g.do("k", fn)
        assert result == "ok"
        assert call_count[0] == 2


# ═══════════════════════════════════════════════════════════════════════════════
# 5. invalidate()
# ═══════════════════════════════════════════════════════════════════════════════

class TestInvalidate:
    def test_invalidate_clears_ttl_cache(self):
        g = SingleflightGroup(ttl_seconds=5.0)
        call_count = [0]
        def fn():
            call_count[0] += 1
            return call_count[0]
        g.do("k", fn)
        g.invalidate("k")
        g.do("k", fn)
        assert call_count[0] == 2

    def test_invalidate_nonexistent_key_is_safe(self):
        g = SingleflightGroup(ttl_seconds=5.0)
        g.invalidate("does-not-exist")  # should not raise

    def test_invalidate_other_key_unaffected(self):
        g = SingleflightGroup(ttl_seconds=5.0)
        call_a = [0]
        call_b = [0]
        g.do("a", lambda: (call_a.__setitem__(0, call_a[0]+1)) or 1)
        g.do("b", lambda: (call_b.__setitem__(0, call_b[0]+1)) or 2)
        g.invalidate("a")
        g.do("a", lambda: (call_a.__setitem__(0, call_a[0]+1)) or 1)
        g.do("b", lambda: (call_b.__setitem__(0, call_b[0]+1)) or 2)
        assert call_a[0] == 2  # a was invalidated → re-executed
        assert call_b[0] == 1  # b still cached


# ═══════════════════════════════════════════════════════════════════════════════
# 6. max_waiters circuit-breaker
# ═══════════════════════════════════════════════════════════════════════════════

class TestMaxWaiters:
    def test_circuit_breaker_executes_independently_when_overloaded(self):
        """With max_waiters=1, a 3rd+ caller executes independently."""
        g = SingleflightGroup(max_waiters=1)
        hold = threading.Event()   # keeps leader fn() running
        ready = threading.Event()  # set when leader started
        call_count = [0]
        lock = threading.Lock()

        def slow():
            with lock:
                call_count[0] += 1
            ready.set()
            hold.wait(timeout=3.0)
            return "r"

        results = []

        def worker():
            results.append(g.do("k", slow))

        # Start leader
        t1 = threading.Thread(target=worker)
        t1.start()
        ready.wait(timeout=2.0)  # ensure leader is in-flight

        # Start 1st waiter (hits max_waiters=1 waiter slot)
        t2 = threading.Thread(target=worker)
        t2.start()
        time.sleep(0.02)  # give t2 time to register as waiter

        # Start overflow caller — exceeds max_waiters → runs independently
        t3 = threading.Thread(target=worker)
        t3.start()
        time.sleep(0.02)

        hold.set()  # release leader
        for t in [t1, t2, t3]:
            t.join(timeout=5.0)

        # Leader t1 + overflow t3 each ran fn() independently
        assert call_count[0] >= 2

    def test_max_waiters_default_is_generous(self):
        g = SingleflightGroup()
        assert g._max_waiters == 50


# ═══════════════════════════════════════════════════════════════════════════════
# 7. active_flights()
# ═══════════════════════════════════════════════════════════════════════════════

class TestActiveFlights:
    def test_zero_when_idle(self):
        g = SingleflightGroup()
        assert g.active_flights() == 0

    def test_zero_after_ttl_cache_hit(self):
        g = SingleflightGroup(ttl_seconds=5.0)
        g.do("k", lambda: 1)
        assert g.active_flights() == 0

    def test_multiple_keys_counted(self):
        g = SingleflightGroup()
        barriers = {k: threading.Barrier(2) for k in ["a", "b"]}
        started = {k: threading.Event() for k in ["a", "b"]}
        done = threading.Event()

        def make_fn(k):
            def fn():
                started[k].set()
                barriers[k].wait(timeout=3.0)
                return k
            return fn

        threads = [threading.Thread(target=lambda k=k: g.do(k, make_fn(k))) for k in ["a", "b"]]
        for t in threads:
            t.start()

        for k in ["a", "b"]:
            started[k].wait(timeout=2.0)

        assert g.active_flights() == 2

        for k in ["a", "b"]:
            barriers[k].wait(timeout=3.0)
        for t in threads:
            t.join(timeout=3.0)

        assert g.active_flights() == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Module-level singleton groups
# ═══════════════════════════════════════════════════════════════════════════════

class TestModuleSingletons:
    def test_news_state_flight_exists(self):
        assert isinstance(news_state_flight, SingleflightGroup)

    def test_calendar_state_flight_exists(self):
        assert isinstance(calendar_state_flight, SingleflightGroup)

    def test_premarket_flight_exists(self):
        assert isinstance(premarket_flight, SingleflightGroup)

    def test_pre_earnings_flight_exists(self):
        assert isinstance(pre_earnings_flight, SingleflightGroup)

    def test_hotlist_flight_exists(self):
        assert isinstance(hotlist_flight, SingleflightGroup)

    def test_scanner_flight_exists(self):
        assert isinstance(scanner_flight, SingleflightGroup)

    def test_news_state_ttl_is_5(self):
        assert news_state_flight._ttl == pytest.approx(5.0)

    def test_calendar_state_ttl_is_10(self):
        assert calendar_state_flight._ttl == pytest.approx(10.0)

    def test_premarket_ttl_is_5(self):
        assert premarket_flight._ttl == pytest.approx(5.0)

    def test_pre_earnings_ttl_is_10(self):
        assert pre_earnings_flight._ttl == pytest.approx(10.0)

    def test_hotlist_ttl_is_5(self):
        assert hotlist_flight._ttl == pytest.approx(5.0)

    def test_scanner_ttl_is_10(self):
        assert scanner_flight._ttl == pytest.approx(10.0)

    def test_all_singletons_idle(self):
        for g in [news_state_flight, calendar_state_flight, premarket_flight,
                  pre_earnings_flight, hotlist_flight, scanner_flight]:
            assert g.active_flights() == 0
