from datetime import datetime, timezone

import pytest

from core.portfolio.adapter import (
    PortfolioAdapterError,
    adapt_v7_position,
    build_v7_constraint_inputs,
)
from core.portfolio.constraints import RiskConstitution


NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


def _constitution(**changes):
    values = {
        "owner_confirmed": True,
        "max_risk_per_trade_pct": 2.0,
        "max_position_size_pct": 25.0,
        "max_total_exposure_pct": 80.0,
        "max_leverage": 2.0,
        "minimum_cash_pct": 10.0,
        "max_sector_exposure_pct": 50.0,
        "correlation_threshold": 0.7,
        "max_correlated_exposure_pct": 50.0,
        "max_positions": 5,
        "max_liquidity_participation_pct": 5.0,
        "max_daily_loss_pct": 5.0,
        "max_daily_loss_trades": 2,
        "max_drawdown_pct": 10.0,
        "max_data_age_seconds": 60,
        "block_data_conflicts": True,
        "require_approved_strategy": True,
    }
    values.update(changes)
    return RiskConstitution(**values)


def _position(**changes):
    values = {
        "ticker": "AAPL",
        "sector": "Technology",
        "usd_size": 1_000.0,
        "leverage": 1,
        "entry_price": 100.0,
        "stop_loss_price": 98.0,
    }
    values.update(changes)
    return values


def _signal(**changes):
    values = {
        "ticker": "NVDA",
        "sector": "Technology",
        "usd_size": 1_000.0,
        "invested_amount": 500.0,
        "leverage": 2,
        "entry": 100.0,
        "sl": 98.0,
        "estimated_fees_usd": 1.0,
        "average_daily_volume_usd": 1_000_000.0,
        "timestamp": "2026-08-13T11:59:50+00:00",
        "has_data_conflict": False,
        "strategy_status": "APPROVED",
        "portfolio_correlations": {"AAPL": 0.2},
    }
    values.update(changes)
    return values


def _inputs(**changes):
    values = {
        "equity_usd": 10_000.0,
        "cash_usd": 8_000.0,
        "open_positions": [_position()],
        "daily_realized_pnl_usd": -25.0,
        "equity_curve": [{"total_equity": 10_100.0}],
        "signal": _signal(),
        "application_config": {
            "autonomy": {
                "mode": "PAPER_AUTO",
                "kill_switch_active": False,
                "live_auto_owner_enabled": False,
            }
        },
        "constitution": _constitution(),
        "now": NOW,
    }
    values.update(changes)
    return values


def test_v7_adapter_builds_app_authoritative_snapshot_and_proposal():
    snapshot, proposal = build_v7_constraint_inputs(**_inputs())

    assert snapshot.equity_usd == 10_000.0
    assert snapshot.cash_usd == 8_000.0
    assert snapshot.peak_equity_usd == 10_100.0
    assert snapshot.positions[0].loss_at_stop_usd == pytest.approx(20.0)
    assert proposal.loss_at_stop_usd == pytest.approx(20.0)
    assert proposal.margin_required_usd == 500.0
    assert proposal.data_age_seconds == 10.0
    assert proposal.is_live_autonomous is False


def test_v7_adapter_takes_kill_switch_and_live_permission_only_from_app_config():
    config = {
        "autonomy": {
            "mode": "LIVE_AUTO",
            "kill_switch_active": True,
            "live_auto_owner_enabled": False,
        }
    }
    _, proposal = build_v7_constraint_inputs(
        **_inputs(
            application_config=config,
            signal=_signal(kill_switch_active=False, live_auto_owner_enabled=True),
        )
    )

    assert proposal.kill_switch_active is True
    assert proposal.is_live_autonomous is True
    assert proposal.live_auto_owner_enabled is False


def test_open_position_adapter_rejects_unknown_sector_or_missing_stop_evidence():
    with pytest.raises(PortfolioAdapterError, match="sector"):
        adapt_v7_position(_position(sector="UNKNOWN"))
    with pytest.raises(PortfolioAdapterError, match="stop_loss_price"):
        adapt_v7_position(_position(stop_loss_price=None))


@pytest.mark.parametrize(
    ("signal", "error"),
    [
        (_signal(timestamp=None), "timestamp"),
        (_signal(has_data_conflict=None), "has_data_conflict"),
        (_signal(strategy_status=""), "strategy lifecycle"),
    ],
)
def test_confirmed_constitution_rejects_missing_proposal_evidence(signal, error):
    with pytest.raises(PortfolioAdapterError, match=error):
        build_v7_constraint_inputs(**_inputs(signal=signal))


def test_optional_conflict_and_strategy_policies_keep_honest_unspecified_state():
    signal = _signal(has_data_conflict=None, strategy_status="")
    signal.pop("has_data_conflict")
    snapshot, proposal = build_v7_constraint_inputs(
        **_inputs(
            signal=signal,
            constitution=_constitution(
                block_data_conflicts=False,
                require_approved_strategy=False,
            ),
        )
    )

    assert snapshot.positions
    assert proposal.has_data_conflict is False
    assert proposal.strategy_status == "UNSPECIFIED"


def test_missing_signal_sector_derives_explicit_value_not_rejection():
    """Scanner/crypto signals carry no sector taxonomy; the adapter derives an
    explicit value (asset_class fact first, else UNKNOWN) so the constitution
    stays evaluable instead of rejecting 100% of signals."""
    crypto_inputs = _inputs(signal=_signal(sector=None, asset_sector=None,
                                           ticker="SNDKB-USD", asset_class="CRYPTO"),
                            open_positions=[])
    _, proposal = build_v7_constraint_inputs(**crypto_inputs)
    assert proposal.sector == "CRYPTO"

    unknown_inputs = _inputs(signal=_signal(sector="", asset_sector=None, ticker="FIGR"),
                             open_positions=[])
    _, proposal = build_v7_constraint_inputs(**unknown_inputs)
    assert proposal.sector == "UNKNOWN"


def test_scanner_shaped_signal_passes_adapter_with_derived_evidence():
    """The exact shape that blocked 100% of live signals (no sector, no
    leverage, no USD fees/ADV, volume in base units) now adapts with honest
    derived values instead of raising."""
    scanner_signal = {
        "ticker": "SNDKB-USD",
        "direction": "short",
        "invested_amount": 500.0,
        "entry": 1792.14,
        "sl_pct": 4.0,       # the app's own pct convention (no price fields)
        "volume": 12_500.0,  # base-asset units
        "timestamp": "2026-08-13T11:59:50+00:00",
        "has_data_conflict": False,
        "portfolio_correlations": {},
        "strategy_status": "RESEARCH",  # honest lifecycle fact, not a fake APPROVED
    }
    inputs = _inputs(signal=scanner_signal, open_positions=[])
    snapshot, proposal = build_v7_constraint_inputs(**inputs)
    assert proposal.sector == "CRYPTO"
    assert proposal.leverage == 1
    assert proposal.fees_usd > 0  # real cost model estimate, never hidden
    assert proposal.average_daily_volume_usd == pytest.approx(1792.14 * 12_500.0)


def test_proposal_notional_auto_clamped_to_max_adv_liquidity():
    """When a requested trade would exceed max_liquidity_participation_pct of ADV (e.g. ALAR 3.6% vs 2.0% max),
    the adapter clamps the size down to the exact maximum allowable participation instead of vetoing."""
    signal = _signal(
        ticker="ALAR",
        usd_size=1440.78,
        invested_amount=1440.78,
        average_daily_volume_usd=39704.0,  # 2% max is $794.08
    )
    constitution = _constitution(max_liquidity_participation_pct=2.0)
    inputs = _inputs(signal=signal, constitution=constitution, open_positions=[])
    _, proposal = build_v7_constraint_inputs(**inputs)

    expected_max = 39704.0 * 0.02
    assert proposal.notional_usd == pytest.approx(expected_max, rel=1e-4)
    assert signal.get("adv_liquidity_capped") is True
    assert signal["usd_size"] == pytest.approx(expected_max, rel=1e-4)

