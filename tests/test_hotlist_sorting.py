from core.hotlist_scoring import hotlist_sort_key, sort_hotlist_by_priority


def test_hotlist_sort_key_prefers_priority_then_overall():
    items = [
        {"ticker": "AAA", "priority_score": 88.0, "overall_score": 71.5, "earnings_edge_score": 72},
        {"ticker": "BBB", "priority_score": 95.0, "overall_score": 63.1, "earnings_edge_score": 65},
        {"ticker": "CCC", "priority_score": 95.0, "overall_score": 75.0, "earnings_edge_score": 68},
    ]
    ordered = sorted(items, key=hotlist_sort_key)
    assert [x["ticker"] for x in ordered] == ["CCC", "BBB", "AAA"]


def test_hotlist_sort_key_fallback_when_priority_missing():
    items = [
        {"ticker": "AAA", "earnings_days": 2, "earnings_edge_score": 70, "earnings_risk_flags": ["trend_conflict"]},
        {"ticker": "BBB", "earnings_days": 1, "earnings_edge_score": 60, "earnings_risk_flags": []},
        {"ticker": "CCC", "earnings_days": 2, "earnings_edge_score": 80, "earnings_risk_flags": []},
        {"ticker": "DDD", "earnings_days": -1, "earnings_edge_score": 95, "earnings_risk_flags": []},
    ]
    ordered = sorted(items, key=hotlist_sort_key)
    assert [x["ticker"] for x in ordered] == ["BBB", "CCC", "AAA", "DDD"]


def test_sort_hotlist_by_priority_applies_defaults():
    ordered = sort_hotlist_by_priority([{"ticker": "XYZ"}])
    assert ordered and ordered[0]["ticker"] == "XYZ"
    assert "overall_score" in ordered[0]
    assert "priority_score" in ordered[0]
