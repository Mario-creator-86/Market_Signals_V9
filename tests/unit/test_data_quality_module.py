import json
import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from core.data_quality import (
    QualityLevel,
    QualifiedValue,
    DataHealthDashboard,
    get_health_dashboard,
)

pytestmark = pytest.mark.unit


class TestQualifiedValue:
    def test_verified_factory(self):
        qv = QualifiedValue.verified(42, "test_source")
        assert qv.value == 42
        assert qv.level == QualityLevel.VERIFIED
        assert qv.source == "test_source"
        assert qv.is_valid() is True

    def test_estimated_factory(self):
        qv = QualifiedValue.estimated(100, "fallback", message="Interpolated")
        assert qv.value == 100
        assert qv.level == QualityLevel.ESTIMATED
        assert qv.message == "Interpolated"
        assert qv.is_valid() is True

    def test_stale_factory(self):
        qv = QualifiedValue.stale(55, "cache", age_seconds=300)
        assert qv.value == 55
        assert qv.level == QualityLevel.STALE
        assert "300s old" in qv.message
        assert qv.is_valid() is False

    def test_unavailable_factory(self):
        qv = QualifiedValue.unavailable("api", "Timeout")
        assert qv.value is None
        assert qv.level == QualityLevel.UNAVAILABLE
        assert qv.message == "Timeout"
        assert qv.is_valid() is False

    def test_unavailable_default_message(self):
        qv = QualifiedValue.unavailable("source")
        assert qv.message == "Missing data"

    def test_to_dict_roundtrip(self):
        qv = QualifiedValue.verified("test_data", "src")
        d = qv.to_dict()
        assert d["value"] == "test_data"
        assert d["level"] == "VERIFIED"
        assert d["source"] == "src"
        assert d["is_valid"] is True
        assert "timestamp" in d

    def test_stale_is_not_valid(self):
        qv = QualifiedValue.stale(10, "old_cache", age_seconds=600)
        assert qv.is_valid() is False

    def test_unavailable_is_not_valid(self):
        qv = QualifiedValue.unavailable("dead_source", "Connection refused")
        assert qv.is_valid() is False

    def test_verified_and_estimated_are_valid(self):
        assert QualifiedValue.verified(1, "s").is_valid() is True
        assert QualifiedValue.estimated(2, "s").is_valid() is True


class TestDataHealthDashboard:
    def test_register_and_get_summary(self):
        dh = DataHealthDashboard(log_dir="/tmp/test_health")
        dh.register("stream_a", QualifiedValue.verified(42, "src_a"))
        dh.register("stream_b", QualifiedValue.unavailable("src_b", "Down"))

        summary = dh.get_live_dashboard()
        assert summary["total_streams"] == 2
        assert summary["verified"] == 1
        assert summary["unavailable"] == 1
        assert "stream_a" in summary["streams"]
        assert "stream_b" in summary["streams"]

    def test_register_overwrites_previous(self):
        dh = DataHealthDashboard(log_dir="/tmp/test_health")
        dh.register("key", QualifiedValue.verified(1, "s"))
        dh.register("key", QualifiedValue.unavailable("s", "Gone"))

        summary = dh.get_live_dashboard()
        assert summary["total_streams"] == 1
        assert summary["verified"] == 0
        assert summary["unavailable"] == 1

    def test_daily_audit_creates_file(self, tmp_path):
        dh = DataHealthDashboard(log_dir=str(tmp_path))
        dh.register("test_stream", QualifiedValue.verified(99, "unit_test"))
        dh.perform_daily_audit()

        files = os.listdir(str(tmp_path))
        assert len(files) == 1
        assert files[0].startswith("health_audit_")
        assert files[0].endswith(".json")

        filepath = os.path.join(str(tmp_path), files[0])
        with open(filepath) as f:
            data = json.load(f)
        assert data["total_streams"] == 1
        assert data["verified"] == 1

    def test_register_unavailable_logs_warning(self):
        dh = DataHealthDashboard(log_dir="/tmp/test_health")
        dh.register("broken_stream", QualifiedValue.unavailable("src", "Error"))

        summary = dh.get_live_dashboard()
        assert summary["streams"]["broken_stream"]["level"] == "UNAVAILABLE"

    def test_get_live_dashboard_counts(self):
        dh = DataHealthDashboard(log_dir="/tmp/test_health")
        dh.register("s1", QualifiedValue.verified(1, "a"))
        dh.register("s2", QualifiedValue.estimated(2, "b"))
        dh.register("s3", QualifiedValue.stale(3, "c", age_seconds=100))
        dh.register("s4", QualifiedValue.unavailable("d", "e"))

        summary = dh.get_live_dashboard()
        assert summary["verified"] == 1
        assert summary["estimated"] == 1
        assert summary["stale"] == 1
        assert summary["unavailable"] == 1


class TestGetHealthDashboard:
    def test_returns_dashboard_instance(self):
        dh = get_health_dashboard()
        assert isinstance(dh, DataHealthDashboard)

    def test_global_instance_is_singleton(self):
        dh1 = get_health_dashboard()
        dh2 = get_health_dashboard()
        assert dh1 is dh2


class TestQualifiedValueAdditiveFields:
    """Phase 1A (§33): QualifiedValue gained optional freshness/coverage/conflicts/
    quality_flags. Backward-compatible: existing factories and call sites unchanged."""

    def test_new_fields_default_none(self):
        qv = QualifiedValue.verified(100.0, "yahoo")
        assert qv.freshness is None
        assert qv.coverage is None
        assert qv.conflicts is None
        assert qv.quality_flags == frozenset()

    def test_optional_fields_set_and_serialized(self):
        from datetime import timedelta
        from core.data_model.quality import Conflict
        qv = QualifiedValue(100.0, QualityLevel.VERIFIED, "yahoo",
                             datetime.now(timezone.utc),
                             freshness=timedelta(seconds=15),
                             coverage=0.9,
                             conflicts=(Conflict("price", (100.0, 101.5), ("yahoo", "alpaca"), 1.5),),
                             quality_flags=frozenset({"CROSS_SOURCE"}))
        d = qv.to_dict()
        assert d["freshness"] == 15.0
        assert d["coverage"] == 0.9
        assert "CROSS_SOURCE" in d["quality_flags"]
        assert d["conflicts"][0]["field"] == "price"

    def test_existing_factory_unchanged(self):
        # Backward-compat: factories still take the same args; new fields default.
        qv = QualifiedValue.stale(100.0, "yahoo", 120)
        assert qv.level is QualityLevel.STALE
        assert qv.freshness is None
        assert qv.is_valid() is False

    def test_to_dict_omits_unset_new_fields(self):
        # No new-field keys appear when they are unset (backward-compat shape).
        qv = QualifiedValue.verified("x", "src")
        d = qv.to_dict()
        assert "freshness" not in d
        assert "coverage" not in d
        assert "conflicts" not in d
        assert "quality_flags" not in d