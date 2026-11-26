"""Point-in-time EPS estimate revision snapshots (free Yahoo data)."""
from datetime import datetime, timezone

import pandas as pd
import pytest

from core import estimate_revisions as er

NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


def _frames(current=1.10, d7=1.08, d30=1.00, d90=0.95, up=5, down=1, analysts=20):
    trend = pd.DataFrame(
        {"current": [current, 2.0], "7daysAgo": [d7, 2.0], "30daysAgo": [d30, 2.0], "60daysAgo": [d30, 2.0], "90daysAgo": [d90, 2.0]},
        index=["0q", "+1q"],
    )
    revisions = pd.DataFrame(
        {"upLast7days": [1, 0], "upLast30days": [up, 0], "downLast30days": [down, 0], "downLast7Days": [0, 0]},
        index=["0q", "+1q"],
    )
    estimate = pd.DataFrame({"avg": [current, 2.0], "numberOfAnalysts": [analysts, 3]}, index=["0q", "+1q"])
    return trend, revisions, estimate


@pytest.mark.unit
def test_snapshot_uses_current_quarter_and_percent_changes():
    snap = er.snapshot_from_frames(*_frames(), captured_at=NOW)
    assert snap["predicted_eps_trend_change_30d_pct"] == pytest.approx(10.0)
    assert snap["predicted_eps_trend_change_7d_pct"] == pytest.approx(1.8519, abs=1e-3)
    assert snap["predicted_eps_revisions_up_30d"] == 5 and snap["predicted_eps_revisions_down_30d"] == 1
    assert snap["predicted_eps_net_revision_ratio_30d"] == pytest.approx(0.2)
    assert snap["predicted_eps_analysts"] == 20
    assert snap["predicted_eps_revision_captured_utc"] == NOW.isoformat()
    assert set(snap) == set(er.FIELDS)


@pytest.mark.unit
def test_negative_eps_improvement_counts_as_upward_revision():
    snap = er.snapshot_from_frames(*_frames(current=-0.40, d30=-0.50), captured_at=NOW)
    assert snap["predicted_eps_trend_change_30d_pct"] == pytest.approx(20.0)
    assert er.revision_direction(snap) == 1


@pytest.mark.unit
def test_missing_or_broken_provider_data_stays_missing():
    assert er.snapshot_from_frames(None, None, None) is None
    assert er.snapshot_from_frames(pd.DataFrame(), pd.DataFrame(), pd.DataFrame()) is None
    snap = er.snapshot_from_frames(*_frames(d30=0.0), captured_at=NOW)
    assert snap["predicted_eps_trend_change_30d_pct"] is None

    def boom(_symbol):
        raise RuntimeError("provider down")

    assert er.fetch_snapshot("ORCL", ticker_factory=boom) is None
    assert er.fetch_snapshot("", ticker_factory=boom) is None


@pytest.mark.unit
def test_fetch_snapshot_reads_ticker_frames():
    trend, revisions, estimate = _frames()

    class FakeTicker:
        eps_trend = trend
        eps_revisions = revisions
        earnings_estimate = estimate

    snap = er.fetch_snapshot("orcl", ticker_factory=lambda s: FakeTicker(), now=NOW)
    assert snap["predicted_eps_trend_change_30d_pct"] == pytest.approx(10.0)


@pytest.mark.unit
def test_refresh_respects_budget_priority_and_freshness():
    clock = [1000.0]
    cache = er.RevisionSnapshotCache(clock=lambda: clock[0])
    calls = []

    def fetch(symbol):
        calls.append(symbol)
        return None if symbol == "MISS" else {"predicted_eps_trend_change_30d_pct": 1.0}

    assert er.refresh_snapshots(["aaa", "MISS", "BBB", "CCC"], cache, max_fetch=3, max_age_seconds=60, fetch=fetch) == 3
    assert calls == ["AAA", "MISS", "BBB"]
    assert cache.get("AAA", 60) == {"predicted_eps_trend_change_30d_pct": 1.0}
    assert cache.get("MISS", 60) is None and cache.is_fresh("MISS", 60)
    # Fresh entries (including remembered misses) are not refetched.
    assert er.refresh_snapshots(["AAA", "MISS", "CCC"], cache, max_fetch=5, max_age_seconds=60, fetch=fetch) == 1
    clock[0] += 61
    assert cache.get("AAA", 60) is None


@pytest.mark.unit
def test_revision_direction_threshold():
    assert er.revision_direction({"predicted_eps_trend_change_30d_pct": 0.2}) is None
    assert er.revision_direction({"predicted_eps_trend_change_30d_pct": -3.0}) == 0
    assert er.revision_direction({}) is None
