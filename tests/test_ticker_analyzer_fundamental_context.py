from core.ticker_analyzer import _build_fundamental_llm_snapshot


def test_build_fundamental_llm_snapshot_extracts_extended_metrics():
    data = {
        "owner_earnings": [{"ownerEarnings": 12300000000, "ownerEarningsPerShare": 6.42}],
        "enterprise_values": [{"enterpriseValue": 2500000000000, "evToEbitda": 18.5}],
        "income_statement_growth": [{"growthRevenue": 0.11, "growthNetIncome": 0.08, "growthEPS": 0.09}],
        "cash_flow_statement_growth": [{"growthFreeCashFlow": 0.07}],
        "reporting_dates": [{"date": "2026-01-31"}],
    }
    out = _build_fundamental_llm_snapshot(data)

    assert out["owner_earnings"] == 12300000000.0
    assert out["owner_earnings_per_share"] == 6.42
    assert out["enterprise_value"] == 2500000000000.0
    assert out["ev_to_ebitda"] == 18.5
    assert out["revenue_growth_yoy"] == 0.11
    assert out["net_income_growth_yoy"] == 0.08
    assert out["eps_growth_yoy"] == 0.09
    assert out["free_cash_flow_growth_yoy"] == 0.07
    assert out["reporting_date"] == "2026-01-31"


def test_build_fundamental_llm_snapshot_defaults():
    out = _build_fundamental_llm_snapshot({})
    assert out["owner_earnings"] == 0.0
    assert out["owner_earnings_per_share"] == 0.0
    assert out["enterprise_value"] == 0.0
    assert out["ev_to_ebitda"] == 0.0
    assert out["revenue_growth_yoy"] == 0.0
    assert out["free_cash_flow_growth_yoy"] == 0.0
    assert out["reporting_date"] == "unknown"

