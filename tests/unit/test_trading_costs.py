import pytest

from core.trading_costs import resolve_trading_cost_model


def _costs():
    return {
        "costs_by_asset": {
            "CRYPTO_FUTURES": {
                "commission_bps": 4.0,
                "entry_slippage_bps": 2.5,
                "slippage_sl_bps": 5.0,
                "slippage_tp_bps": 2.5,
            },
        },
    }


def test_cost_model_is_explicitly_configured_and_preserves_exit_asymmetry():
    model = resolve_trading_cost_model("BTC-USD", strategy_v2_config=_costs())

    assert model.asset_class == "CRYPTO_FUTURES"
    assert model.commission_fraction == pytest.approx(0.0004)
    assert model.entry_slippage_fraction == pytest.approx(0.00025)
    assert model.exit_slippage_fraction("stop_loss") == pytest.approx(0.0005)
    assert model.exit_slippage_fraction("take_profit") == pytest.approx(0.00025)
    assert model.to_dict()["is_realized_exchange_fee"] is False


def test_cost_model_fails_closed_when_a_required_assumption_is_missing():
    with pytest.raises(ValueError, match="incomplete"):
        resolve_trading_cost_model(
            "BTC-USD",
            strategy_v2_config={"costs_by_asset": {"CRYPTO_FUTURES": {"commission_bps": 4.0}}},
        )
