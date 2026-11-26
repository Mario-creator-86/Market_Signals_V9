import pytest

from core.pre_earnings.score import compute_data_confidence, compute_pre_earnings_score

pytestmark = pytest.mark.golden


# GOLD-050 — the §50 score formula is frozen (evolve-not-rewrite back-compat).
def test_gold050_score_formula_frozen():
    cases = [
        # (gap, bias, coverage, hist_samples, expected_score)
        (80, 60, 70, 4, round((0.65 * 80 + 0.35 * 60) * 0.70)),   # 68
        (100, 0, 100, 4, 65),                                       # 0.65*100*1.0
        (0, 100, 100, 4, 35),                                       # 0.35*100*1.0
        (50, 50, 50, 3, round((0.65 * 50 + 0.35 * 50) * 0.50)),    # 25
        (70, 30, 60, 3, round((0.65 * 70 + 0.35 * 30) * 0.60)),    # 34.2 -> 34
    ]
    for gap, bias, cov, hs, expected in cases:
        r = compute_pre_earnings_score(
            gap_risk=gap, direction_bias=bias, coverage_score=cov,
            coverage_contributors=[], historical_samples=hs, news_count=6,
            implied_available=True)
        assert r.score == expected, f"gap={gap} bias={bias} cov={cov}: {r.score} != {expected}"


def test_gold050_score_is_not_probability_invariant():
    for gap in (None, 0, 50, 100):
        for bias in (None, -80, 0, 80):
            r = compute_pre_earnings_score(
                gap_risk=gap, direction_bias=bias, coverage_score=55,
                coverage_contributors=[], historical_samples=3, news_count=4,
                implied_available=True)
            assert r.is_not_probability is True
            assert "NOT probability" in r.components[2]["note"]


def test_gold050_data_confidence_band_boundaries():
    # HIGH requires >=3 quarters AND coverage >= 45
    assert compute_data_confidence(historical_samples=3, coverage_score=45,
                                   implied_available=True, news_count=6)[0] == "HIGH"
    assert compute_data_confidence(historical_samples=3, coverage_score=44,
                                   implied_available=True, news_count=6)[0] == "MEDIUM"
    # 2 quarters -> MEDIUM; 1 -> LOW; 0 -> NONE (coverage cannot upgrade)
    assert compute_data_confidence(historical_samples=2, coverage_score=10,
                                   implied_available=False, news_count=0)[0] == "MEDIUM"
    assert compute_data_confidence(historical_samples=1, coverage_score=95,
                                   implied_available=True, news_count=12)[0] == "LOW"
    label, _reason, insufficient = compute_data_confidence(
        historical_samples=0, coverage_score=95, implied_available=True, news_count=12)
    assert label == "NONE" and insufficient is True