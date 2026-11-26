from core.pre_earnings.score import (
    PreEarningsScoreResult, compute_data_confidence, compute_pre_earnings_score,
)


def test_score_matches_legacy_opportunity_formula_and_back_compat():
    # Legacy: (0.65*gap + 0.35*|bias|) * coverage/100
    r = compute_pre_earnings_score(
        gap_risk=80, direction_bias=60, coverage_score=70,
        coverage_contributors=[], historical_samples=4, news_count=5,
        implied_available=True,
    )
    expected = round((0.65 * 80 + 0.35 * 60) * 0.70)  # 67.55 -> 68
    assert r.score == expected
    assert r.is_not_probability is True
    assert r.data_confidence == "HIGH"  # 4 quarters + coverage 70


def test_insufficient_history_scores_zero_and_none_confidence():
    r = compute_pre_earnings_score(
        gap_risk=None, direction_bias=40, coverage_score=50,
        coverage_contributors=[], historical_samples=0, news_count=3,
        implied_available=False,
    )
    assert r.score == 0
    assert r.data_confidence == "NONE"
    assert r.is_not_probability is True


def test_data_confidence_bands_never_upgrade_only_downgrade():
    high_cov = compute_data_confidence(historical_samples=4, coverage_score=80,
                                       implied_available=True, news_count=6)
    assert high_cov[0] == "HIGH"
    low_cov = compute_data_confidence(historical_samples=4, coverage_score=20,
                                      implied_available=False, news_count=0)
    assert low_cov[0] == "MEDIUM"  # coverage downgrades HIGH -> MEDIUM
    two = compute_data_confidence(historical_samples=2, coverage_score=90,
                                  implied_available=True, news_count=8)
    assert two[0] == "MEDIUM"  # 2 quarters stays MEDIUM regardless of coverage
    one = compute_data_confidence(historical_samples=1, coverage_score=90,
                                  implied_available=True, news_count=8)
    assert one[0] == "LOW"  # 1 quarter stays LOW (never upgraded by coverage)
    zero = compute_data_confidence(historical_samples=0, coverage_score=90,
                                   implied_available=True, news_count=8)
    assert zero[0] == "NONE" and zero[2] is True  # is_insufficient


def test_score_components_visible_and_coverage_delegated():
    r = compute_pre_earnings_score(
        gap_risk=70, direction_bias=30, coverage_score=60,
        coverage_contributors=[{"name": "news_context", "value": 0.8, "weight": 0.3, "note": "x"}],
        historical_samples=3, news_count=6, implied_available=True,
    )
    names = [c["name"] for c in r.components]
    assert "gap_risk" in names and "direction_bias_magnitude" in names and "coverage_modulator" in names
    assert r.coverage_score == 60 and r.coverage_label == "MEDIUM"
    assert r.coverage_contributors[0]["name"] == "news_context"


def test_weight_overrides_change_split():
    r_default = compute_pre_earnings_score(
        gap_risk=100, direction_bias=0, coverage_score=100,
        coverage_contributors=[], historical_samples=4, news_count=6, implied_available=True)
    r_override = compute_pre_earnings_score(
        gap_risk=100, direction_bias=0, coverage_score=100,
        coverage_contributors=[], historical_samples=4, news_count=6, implied_available=True,
        weight_overrides={"gap_risk": 1.0, "direction_bias": 0.0})
    # default: (0.65*100+0.35*0)*1.0 = 65 ; override: (1.0*100+0.0*0)*1.0 = 100
    assert r_default.score == 65
    assert r_override.score == 100