import agents.pre_earnings_agent as pe


def test_compute_data_confidence_replaces_inline_band(monkeypatch):
    # The agent module must re-export the new canonical confidence function so
    # the inline band block can delegate to it without duplicating logic.
    from core.pre_earnings.score import compute_data_confidence
    assert pe.compute_data_confidence is compute_data_confidence


def test_compute_pre_earnings_score_replaces_opportunity_score(monkeypatch):
    from core.pre_earnings.score import compute_pre_earnings_score
    assert pe.compute_pre_earnings_score is compute_pre_earnings_score