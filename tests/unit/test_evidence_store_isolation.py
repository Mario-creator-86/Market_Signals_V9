"""Evidence stores must follow MS_DATA_DIR so tests never write live data.

Regression (2026-11-12): the trade journal and strategy performance caches
used CWD/BASE_DIR-relative ``data/`` paths. Every suite run appended a fake
"AAPL MANUAL +97.90" close to the live journal (284 of 665 rows) and a 100%
win-rate entry to the live performance cache.
"""
from pathlib import Path

import pytest

from core import feedback_loop, news_evidence_ledger
from core.config import BASE_DIR, DATA_DIR
from core.ai import base_rates, reflection
from core.research_factory import weakness
from core.strategy_v2 import performance_cache

EVIDENCE_PATHS = {
    "trade journal": feedback_loop.FEEDBACK_JOURNAL_PATH,
    "research cohort journal": feedback_loop.RESEARCH_COHORT_JOURNAL_PATH,
    "strategy performance cache": performance_cache.STRATEGY_PERF_CACHE_PATH,
    "strategy research cache": performance_cache.STRATEGY_RESEARCH_CACHE_PATH,
    "ai reflections": reflection.REFLECTIONS_PATH,
    "news evidence ledger": news_evidence_ledger.NEWS_EVIDENCE_LEDGER_PATH,
    "base-rate capital store": base_rates.DB_PATH,
    **{f"weakness journal {i}": path for i, path in enumerate(weakness.DEFAULT_JOURNAL_PATHS)},
}


@pytest.mark.parametrize("name", sorted(EVIDENCE_PATHS))
def test_evidence_store_lives_under_the_isolated_test_data_dir(name):
    # DATA_DIR is resolved from MS_DATA_DIR at import; tests/conftest.py points
    # it at a temp directory before anything is imported.
    data_dir = Path(DATA_DIR).resolve()
    path = Path(EVIDENCE_PATHS[name]).resolve()

    assert BASE_DIR.resolve() not in data_dir.parents, "test session is using the live data directory"
    assert data_dir in path.parents, f"{name} at {path} escapes MS_DATA_DIR ({data_dir})"
