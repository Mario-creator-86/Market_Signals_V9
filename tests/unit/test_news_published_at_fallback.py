"""Audit 2026-09-02: unparseable article timestamps must not forge maximum
freshness — they are kept for bucket hydration but aged out of the recent-news
quality gates (top_prediction_max_news_age_hours default 96h)."""
from datetime import datetime, timedelta, timezone

import pytest

from agents.news_tab_agent import _parse_published_at

pytestmark = pytest.mark.unit


def test_valid_iso_and_z_suffix_parse():
    dt = _parse_published_at("2026-09-01T12:00:00Z")
    assert dt.tzinfo is not None
    assert (dt.year, dt.month, dt.day) == (2026, 9, 1)

    naive = _parse_published_at("2026-09-01T12:00:00")
    assert naive.tzinfo is not None


def test_invalid_timestamp_is_bounded_stale_not_fresh():
    stale = _parse_published_at("not-a-date")
    age = datetime.now(timezone.utc) - stale
    assert age > timedelta(hours=96)
    assert age < timedelta(days=8)


def test_missing_timestamp_is_bounded_stale_not_fresh():
    stale = _parse_published_at(None)
    age = datetime.now(timezone.utc) - stale
    assert age > timedelta(hours=96)
