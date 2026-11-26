from core.cache_manager import cache_manager
import pytest

pytestmark = pytest.mark.unit


def test_cache_manager_hit_miss_stale():
    cache_manager.clear()

    value, decision, age = cache_manager.get("k1", ttl_seconds=5, source="test")
    assert value is None
    assert decision == "MISS"
    assert age is None

    cache_manager.set("k1", {"ok": True}, ttl_seconds=1, source="test")
    value, decision, age = cache_manager.get("k1", ttl_seconds=1, source="test")
    assert value == {"ok": True}
    assert decision == "HIT"
    assert age is not None


def test_cache_manager_inventory_contains_counters():
    cache_manager.clear()
    cache_manager.set("alpha", 1, ttl_seconds=30, source="unit")
    cache_manager.get("alpha", ttl_seconds=30, source="unit")
    cache_manager.get("missing", ttl_seconds=30, source="unit")

    snapshot = cache_manager.inventory()
    assert snapshot["size"] >= 1
    assert snapshot["hit_total"] >= 1
    assert snapshot["miss_total"] >= 1
