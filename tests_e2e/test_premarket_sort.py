"""E2E: Pre-Market candidate sort must match the displayed directional score.

REGRESSION (audit #9): the sort key for the 'score' order must be the SAME
directional score that is rendered (SHORT -> premarket_score_short, LONG ->
premarket_score_long, WATCH -> max). The old sort used max(long, short) for
every row, which diverged from the displayed number in the legacy-fallback
edge case (a LONG-classified ticker whose _short > _long): a lower displayed
score could rank above a higher one.
"""
from playwright.sync_api import Page

import pytest

pytestmark = pytest.mark.e2e


def test_premarket_sort_matches_displayed_directional_score(page: Page, base_url: str):
    page.goto(base_url)
    page.wait_for_function(
        "typeof UI !== 'undefined' && typeof UI._filterPremarketCandidates === 'function'"
    )
    order = page.evaluate(
        """() => {
            UI.premarketFilters = { direction: 'all', sort: 'score', minScore: 0, search: '' };
            // AAA is a LONG-classified ticker whose _short (90) > _long (50) — the
            // legacy-fallback edge case. It DISPLAYS 50. BBB is a normal LONG that
            // displays 70. Sort-by-max would put AAA (max 90) above BBB (max 70);
            // sort-by-displayed-directional must put BBB (70) above AAA (50).
            const candidates = [
                { symbol: 'AAA', direction: 'LONG', premarket_score_long: 50, premarket_score_short: 90 },
                { symbol: 'BBB', direction: 'LONG', premarket_score_long: 70, premarket_score_short: 10 },
            ];
            return UI._filterPremarketCandidates(candidates).map(c => c.symbol);
        }"""
    )
    assert order == ["BBB", "AAA"]


def test_premarket_sort_short_uses_short_score(page: Page, base_url: str):
    """A SHORT ticker sorts by premarket_score_short (what it displays), not by
    max(long, short)."""
    page.goto(base_url)
    page.wait_for_function(
        "typeof UI !== 'undefined' && typeof UI._filterPremarketCandidates === 'function'"
    )
    order = page.evaluate(
        """() => {
            UI.premarketFilters = { direction: 'all', sort: 'score', minScore: 0, search: '' };
            // Both SHORT. X displays 80 (short), Y displays 60 (short) even though
            // Y has the higher _long. Sort must follow the displayed short score.
            const candidates = [
                { symbol: 'Y', direction: 'SHORT', premarket_score_long: 95, premarket_score_short: 60 },
                { symbol: 'X', direction: 'SHORT', premarket_score_long: 5, premarket_score_short: 80 },
            ];
            return UI._filterPremarketCandidates(candidates).map(c => c.symbol);
        }"""
    )
    assert order == ["X", "Y"]