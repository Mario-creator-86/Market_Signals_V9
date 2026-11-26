from core.price_integrity import validate_price_consistency


def test_price_integrity_accepts_concordant_independent_prices():
    result = validate_price_consistency(
        live_price=100.8,
        technical_price=100.0,
        asset_type="stock",
        live_source="yahoo_quote",
        technical_source="yahoo_history",
    )

    assert result["status"] == "verified"
    assert result["is_verified"] is True
    assert result["deviation_pct"] == 0.8


def test_price_integrity_rejects_conflicting_prices():
    result = validate_price_consistency(
        live_price=13.895,
        technical_price=327.010,
        asset_type="stock",
        live_source="quote",
        technical_source="candles",
    )

    assert result["status"] == "conflict"
    assert result["is_verified"] is False
    assert result["deviation_pct"] > result["max_deviation_pct"]


def test_price_integrity_does_not_treat_one_source_as_execution_evidence():
    result = validate_price_consistency(
        live_price=None,
        technical_price=100.0,
        asset_type="crypto",
        live_source="unavailable",
        technical_source="binance_klines",
    )

    assert result["status"] == "single_source"
    assert result["is_verified"] is False
