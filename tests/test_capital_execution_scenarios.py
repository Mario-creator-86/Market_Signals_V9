import json
import threading
from dataclasses import dataclass
from typing import Dict, List, Tuple

import pytest

import core.capital_manager as capital_mod
from core.capital_manager import CapitalManager
from core.signals import SignalManager
from core.trade_decision import summarize_position_explainability


def _quote(price: float, source: str = "unit_test", commission_rate: float = 0.0025) -> dict:
    return {
        "execution_price": float(price),
        "execution_price_quality": {"quality": "verified", "source": source},
        "spread_pct": 0.0,
        "slippage_pct": 0.0,
        "commission_rate": float(commission_rate),
        "total_cost_pct": float(commission_rate) * 100.0,
        "price_timestamp": "2026-04-26T10:00:00+00:00",
        "source": source,
    }


@pytest.fixture
def capital_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MS_DISABLE_CAPITAL_THREADS", "1")
    monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", tmp_path / "capital.json")
    monkeypatch.setattr(capital_mod, "get_sqlite_store", lambda: None)
    try:
        import core.trade_audit as trade_audit

        monkeypatch.setattr(trade_audit, "record_trade_event", lambda *args, **kwargs: None)
    except Exception:
        pass
    try:
        import core.feedback_loop as feedback_loop

        monkeypatch.setattr(feedback_loop, "process_closed_trade", lambda *args, **kwargs: None)
    except Exception:
        pass

    CapitalManager._instance = None
    CapitalManager._price_cache = {}
    CapitalManager._atr_cache = {}
    manager = CapitalManager()
    manager.set_balance(10_000.0, reason="test", clear_positions=True)
    yield manager, capital_mod.CAPITAL_STATE_PATH
    CapitalManager._instance = None
    CapitalManager._price_cache = {}
    CapitalManager._atr_cache = {}


@dataclass(frozen=True)
class TradeCase:
    name: str
    direction: str
    open_side: str
    close_side: str
    close_price: float
    expected_gross: float
    expected_pct: float
    expected_reason: str


@pytest.mark.parametrize(
    "case",
    [
        TradeCase("long_take_profit", "LONG", "BUY", "SELL", 110.0, 100.0, 10.0, "take_profit"),
        TradeCase("long_stop_loss", "LONG", "BUY", "SELL", 95.0, -50.0, -5.0, "stop_loss"),
        TradeCase("short_take_profit", "SHORT", "SELL", "BUY", 90.0, 100.0, 10.0, "take_profit"),
        TradeCase("short_stop_loss", "SHORT", "SELL", "BUY", 105.0, -50.0, -5.0, "stop_loss"),
    ],
    ids=lambda case: case.name,
)
def test_auto_position_opens_and_closes_at_expected_bid_ask_side(capital_env, monkeypatch, case):
    manager, _ = capital_env
    calls: List[Tuple[str, str]] = []

    def fake_execution_price(ticker: str, side: str, **kwargs) -> dict:
        calls.append((ticker, side))
        assert kwargs.get("use_cache") is False
        if side == case.open_side:
            return _quote(100.0)
        if side == case.close_side:
            return _quote(case.close_price)
        raise AssertionError(f"unexpected side {side}")

    monkeypatch.setattr(manager, "_get_execution_price", fake_execution_price)

    opened = manager.open_position(
        ticker="AAPL",
        direction=case.direction,
        size_usd=1000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id=f"sig_{case.name}",
        note="scenario",
    )
    closed = manager.close_position(
        position_id=opened["position_id"],
        exit_price=0.0,
        reason=case.expected_reason,
        sync_signal=False,
    )

    assert calls == [("AAPL", case.open_side), ("AAPL", case.close_side)]
    assert opened["entry_price"] == 100.0
    assert opened["qty"] == pytest.approx(10.0)
    assert opened["commission_usd"] == pytest.approx(2.5)
    assert opened["source_signal_id"] == f"sig_{case.name}"
    assert closed["exit_price"] == case.close_price
    assert closed["gross_pnl"] == pytest.approx(case.expected_gross)

    # Entry + exit commission both deducted
    entry_commission = 2.5
    exit_notional = case.close_price * 10.0  # close_price * qty
    exit_commission = exit_notional * 0.0025
    assert closed["exit_commission"] == pytest.approx(exit_commission)
    assert closed["net_pnl"] == pytest.approx(case.expected_gross - entry_commission - exit_commission)
    assert closed["cash_pnl"] == pytest.approx(case.expected_gross - exit_commission)
    assert closed["pnl_pct_notional"] == pytest.approx(case.expected_pct)
    assert closed["pnl_pct_margin"] == pytest.approx(case.expected_pct)
    assert closed["close_reason"] == case.expected_reason
    assert manager.get_balance() == pytest.approx(10_000.0 + case.expected_gross - entry_commission - exit_commission)


def test_open_crypto_position_persists_inferred_asset_class(capital_env, monkeypatch):
    manager, _ = capital_env
    monkeypatch.setattr(manager, "_get_execution_price", lambda *args, **kwargs: _quote(0.01))

    opened = manager.open_position(
        ticker="VET-USD",
        direction="SHORT",
        size_usd=100.0,
        stop_loss_pct=0.03,
        take_profit_pct=0.06,
        signal_id="sig_crypto_asset_class",
    )

    assert opened["asset_class"] == "CRYPTO_FUTURES"


def test_capital_blocks_opposite_direction_for_same_ticker(capital_env, monkeypatch):
    manager, _ = capital_env
    monkeypatch.setattr(manager, "_get_execution_price", lambda *args, **kwargs: _quote(100.0))

    opened = manager.open_position(
        ticker="BB",
        direction="SHORT",
        size_usd=100.0,
        stop_loss_pct=0.03,
        take_profit_pct=0.05,
        signal_id="scanner_short_bb",
    )

    with pytest.raises(ValueError, match="Opposite open position for BB"):
        manager.open_position(
            ticker="BB",
            direction="LONG",
            size_usd=100.0,
            stop_loss_pct=0.03,
            take_profit_pct=0.05,
            signal_id="pre_earnings_long_bb",
        )

    active = [position for position in manager.get_positions() if position.get("status") == "OPEN"]
    assert [(position["position_id"], position["direction"]) for position in active] == [
        (opened["position_id"], "SHORT")
    ]


def test_research_cohort_position_is_labelled_and_rejects_stale_reference_price(capital_env, monkeypatch):
    manager, _ = capital_env
    monkeypatch.setattr(manager, "_get_execution_price", lambda ticker, side, **kwargs: _quote(100.0))

    opened = manager.open_position(
        ticker="BTC-USD",
        direction="LONG",
        size_usd=100.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="cohort_signal_1",
        position_type="research_cohort",
        research_only=True,
        expected_entry_price=101.0,
        max_entry_deviation_pct=2.0,
    )

    assert opened["position_type"] == "research_cohort"
    assert opened["research_only"] is True
    assert opened["entry_price_deviation_pct"] == pytest.approx(100.0 / 101.0)

    with pytest.raises(ValueError, match="deviates"):
        manager.open_position(
            ticker="ETH-USD",
            direction="LONG",
            size_usd=100.0,
            stop_loss_pct=0.02,
            take_profit_pct=0.04,
            signal_id="cohort_signal_stale",
            position_type="research_cohort",
            research_only=True,
            expected_entry_price=90.0,
            max_entry_deviation_pct=2.0,
        )


def test_research_cohort_never_changes_deployable_capital_or_position_sizing(capital_env, monkeypatch):
    manager, _ = capital_env

    def execution_price(ticker, side, **kwargs):
        if ticker == "BTC-USD" and side == "SELL":
            return _quote(110.0)
        return _quote(100.0)

    monkeypatch.setattr(manager, "_get_execution_price", execution_price)
    research = manager.open_position(
        ticker="BTC-USD",
        direction="LONG",
        size_usd=100.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="research_isolated",
        position_type="research_cohort",
        research_only=True,
    )

    assert manager.get_balance() == pytest.approx(9899.75)
    assert manager.get_deployable_balance() == pytest.approx(10_000.0)
    opened_accounting = manager.get_capital_accounting()
    assert opened_accounting["research_open_margin"] == pytest.approx(100.0)
    assert opened_accounting["research_open_entry_fees"] == pytest.approx(0.25)

    closed = manager.close_position(
        position_id=research["position_id"],
        exit_price=0.0,
        reason="research_exit",
        sync_signal=False,
    )
    assert closed["net_pnl"] == pytest.approx(9.475)
    assert manager.get_balance() == pytest.approx(10_009.475)
    assert manager.get_deployable_balance() == pytest.approx(10_000.0)
    assert manager.get_capital_accounting()["research_realized_pnl"] == pytest.approx(9.475)

    capital = manager.open_position(
        ticker="ETH-USD",
        direction="LONG",
        size_usd=5_000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="capital_after_research",
    )
    assert capital["usd_size"] == pytest.approx(2_000.0)
    assert manager.get_deployable_balance() == pytest.approx(7_995.0)


def test_paper_s7_capital_position_is_standard_and_keeps_its_experiment_label(capital_env, monkeypatch):
    manager, _ = capital_env
    monkeypatch.setattr(manager, "_get_execution_price", lambda ticker, side, **kwargs: _quote(100.0))

    opened = manager.open_position(
        ticker="BTC-USD",
        direction="LONG",
        size_usd=100.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="paper_s7_1",
        decision_context={
            "paper_capital_experiment": True,
            "execution": {"mode": "paper", "paper_capital_experiment": True},
        },
        strategy_type="S7_LIQUIDITY_SWEEP_REVERSAL",
    )

    assert opened["position_type"] == "paper_signal"
    assert opened["research_only"] is False
    assert opened["paper_capital_experiment"] is True


def test_auto_position_caps_size_to_twenty_percent_of_available_capital(capital_env, monkeypatch):
    manager, _ = capital_env
    manager.set_balance(1_000.0, reason="small account", clear_positions=True)
    monkeypatch.setattr(manager, "_get_execution_price", lambda ticker, side, **kwargs: _quote(50.0))

    opened = manager.open_position(
        ticker="MSFT",
        direction="LONG",
        size_usd=900.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="sig_size_cap",
    )

    assert opened["usd_size"] == pytest.approx(200.0)
    assert opened["qty"] == pytest.approx(4.0)
    assert opened["margin_used"] == pytest.approx(200.0)
    assert opened["commission_usd"] == pytest.approx(0.5)
    assert manager.get_balance() == pytest.approx(799.5)


def test_auto_position_uses_leverage_as_margin_multiplier(capital_env, monkeypatch):
    manager, _ = capital_env
    monkeypatch.setattr(manager, "_get_execution_price", lambda ticker, side, **kwargs: _quote(100.0))

    opened = manager.open_position(
        ticker="MSFT",
        direction="LONG",
        size_usd=2000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="sig_ai_leverage",
        leverage=2,
    )

    assert opened["usd_size"] == pytest.approx(2000.0)
    assert opened["margin_used"] == pytest.approx(1000.0)
    assert opened["leverage"] == 2
    assert opened["commission_usd"] == pytest.approx(5.0)
    assert manager.get_balance() == pytest.approx(8995.0)


def test_auto_position_is_idempotent_for_same_signal_id(capital_env, monkeypatch):
    manager, _ = capital_env
    monkeypatch.setattr(manager, "_get_execution_price", lambda ticker, side, **kwargs: _quote(100.0))

    first = manager.open_position(
        ticker="HUMA-USD",
        direction="SHORT",
        size_usd=1000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="sig_huma",
    )
    second = manager.open_position(
        ticker="HUMA-USD",
        direction="SHORT",
        size_usd=1000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="sig_huma",
    )

    open_positions = [p for p in manager.get_positions() if p["status"] == "OPEN"]
    assert second["position_id"] == first["position_id"]
    assert len(open_positions) == 1
    assert open_positions[0]["usd_size"] == pytest.approx(1000.0)
    assert manager.get_balance() == pytest.approx(8997.5)
    assert [h["type"] for h in manager.get_history()].count("OPEN_POSITION") == 1


def test_auto_position_scales_existing_position_for_new_signal_id(capital_env, monkeypatch):
    manager, _ = capital_env
    prices = iter([100.0, 110.0])

    def fake_execution_price(ticker: str, side: str, **kwargs) -> dict:
        return _quote(next(prices))

    monkeypatch.setattr(manager, "_get_execution_price", fake_execution_price)

    first = manager.open_position(
        ticker="HUMA-USD",
        direction="SHORT",
        size_usd=1000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="sig_huma_1",
    )
    scaled = manager.open_position(
        ticker="HUMA-USD",
        direction="SHORT",
        size_usd=1000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="sig_huma_2",
    )

    open_positions = [p for p in manager.get_positions() if p["status"] == "OPEN"]
    assert scaled["position_id"] == first["position_id"]
    assert len(open_positions) == 1
    assert scaled["usd_size"] == pytest.approx(2000.0)
    assert scaled["qty"] == pytest.approx(10.0 + (1000.0 / 110.0))
    assert scaled["entry_price"] == pytest.approx(104.76190476)
    assert scaled["commission_usd"] == pytest.approx(5.0)
    assert scaled["add_count"] == 2
    assert scaled["source_signal_ids"] == ["sig_huma_1", "sig_huma_2"]
    assert manager.get_balance() == pytest.approx(7995.0)
    assert manager.get_history()[-1]["type"] == "SCALE_POSITION"


def test_position_explainability_uses_live_pnl_and_invested_amount_sizing():
    summary = summarize_position_explainability(
        {
            "ticker": "HUMA-USD",
            "direction": "SHORT",
            "entry_price": 0.0209,
            "current_price": 0.0211,
            "qty": 81617.1128,
            "status": "OPEN",
            "current_pnl": -16.3234,
            "decision_context": {
                "decision_source": "autonomous",
                "reason": "HUMA-USD SHORT",
                "reasons": ["HUMA-USD SHORT | entry 0.0247 | target 0.0235 | stop 0.0252"],
                "scoring": {"signal_score": 92},
                "sizing": {"usd_size": 0.0, "invested_amount": 1707.43, "leverage": 1},
            },
        }
    )

    assert "Live PnL: $-16.32" in summary["summary"]
    assert "Sizing unavailable" not in summary["summary"]
    assert summary["sizing"]["usd_size"] == pytest.approx(1707.43)


@pytest.mark.parametrize(
    "kwargs, expected_message",
    [
        ({"direction": "SIDEWAYS"}, "direction must be LONG/SHORT/BUY/SELL"),
        ({"size_usd": 0.0}, "size_usd must be > 0"),
        ({"stop_loss_pct": 0.0}, "stop_loss_pct and take_profit_pct must be > 0"),
        ({"take_profit_pct": -0.01}, "stop_loss_pct and take_profit_pct must be > 0"),
    ],
)
def test_auto_position_rejects_invalid_open_requests(capital_env, monkeypatch, kwargs, expected_message):
    manager, _ = capital_env
    monkeypatch.setattr(manager, "_get_execution_price", lambda ticker, side, **kwargs: _quote(100.0))
    payload = {
        "ticker": "AAPL",
        "direction": "LONG",
        "size_usd": 1000.0,
        "stop_loss_pct": 0.02,
        "take_profit_pct": 0.04,
        "signal_id": "sig_invalid",
    }
    payload.update(kwargs)

    with pytest.raises(ValueError, match=expected_message):
        manager.open_position(**payload)


def test_auto_position_rejects_zero_available_capital(capital_env, monkeypatch):
    manager, _ = capital_env
    manager.set_balance(0.0, reason="empty", clear_positions=True)
    monkeypatch.setattr(manager, "_get_execution_price", lambda ticker, side, **kwargs: _quote(100.0))

    with pytest.raises(ValueError, match="Insufficient capital"):
        manager.open_position(
            ticker="AAPL",
            direction="LONG",
            size_usd=100.0,
            stop_loss_pct=0.02,
            take_profit_pct=0.04,
        )


def test_auto_position_rejects_zero_execution_price(capital_env, monkeypatch):
    manager, _ = capital_env
    monkeypatch.setattr(manager, "_get_execution_price", lambda ticker, side, **kwargs: _quote(0.0))

    with pytest.raises(ValueError, match="execution_price must be > 0"):
        manager.open_position(
            ticker="AAPL",
            direction="LONG",
            size_usd=1000.0,
            stop_loss_pct=0.02,
            take_profit_pct=0.04,
        )


def test_manual_leveraged_position_uses_manual_entry_and_fee_once(capital_env):
    manager, _ = capital_env

    opened = manager.open_manual_position(
        ticker="TSLA",
        direction="short",
        leverage=5,
        usd_size=1000.0,
        entry_price=100.0,
        max_size_pct=0.5,
        fees_bps=10,
    )

    assert opened["entry_price"] == 100.0
    assert opened["direction"] == "short"
    assert opened["margin_used"] == pytest.approx(200.0)
    assert opened["fees_estimate"] == pytest.approx(1.0)
    assert opened["commission_usd"] == pytest.approx(1.0)
    # Fees now debited at entry alongside margin
    assert manager.get_balance() == pytest.approx(10_000.0 - 200.0 - 1.0)

    closed = manager.close_position(
        position_id=opened["position_id"],
        exit_price=90.0,
        reason="manual_take_profit",
        sync_signal=False,
    )

    entry_commission = 1.0
    qty = 10.0
    exit_notional = 90.0 * qty
    exit_commission = exit_notional * (10.0 / 10000.0)  # fees_bps=10 → 0.001
    assert closed["gross_pnl"] == pytest.approx(100.0)
    assert closed["exit_commission"] == pytest.approx(exit_commission)
    assert closed["net_pnl"] == pytest.approx(100.0 - entry_commission - exit_commission)
    assert closed["cash_pnl"] == pytest.approx(100.0 - exit_commission)
    assert closed["pnl_pct_margin"] == pytest.approx(closed["pnl_pct_notional"] * 5)
    # balance = entry_balance + margin + cash_pnl = 9799 + 200 + (100 - 0.9) = 10098.1
    assert manager.get_balance() == pytest.approx(10_000.0 + 100.0 - entry_commission - exit_commission)


def test_close_position_rejects_missing_or_already_closed_positions(capital_env, monkeypatch):
    manager, _ = capital_env
    monkeypatch.setattr(manager, "_get_execution_price", lambda ticker, side, **kwargs: _quote(100.0))
    opened = manager.open_position(
        ticker="AAPL",
        direction="LONG",
        size_usd=1000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
    )
    manager.close_position(position_id=opened["position_id"], exit_price=101.0, sync_signal=False)

    with pytest.raises(ValueError, match="position already closed"):
        manager.close_position(position_id=opened["position_id"], exit_price=101.0, sync_signal=False)
    with pytest.raises(KeyError, match="position not found"):
        manager.close_position(position_id="pos_missing", exit_price=101.0, sync_signal=False)
    with pytest.raises(ValueError, match="position_id is required"):
        manager.close_position(position_id="", exit_price=101.0, sync_signal=False)


def test_close_position_relabels_invalid_negative_take_profit(capital_env, monkeypatch):
    manager, _ = capital_env
    monkeypatch.setattr(manager, "_get_execution_price", lambda ticker, side, **kwargs: _quote(100.0))

    opened = manager.open_position(
        ticker="MSFT",
        direction="SHORT",
        size_usd=1000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="sig_bad_tp",
    )
    closed = manager.close_position(
        position_id=opened["position_id"],
        exit_price=101.0,
        reason="take_profit",
        sync_signal=False,
    )

    assert closed["gross_pnl"] < 0
    assert closed["close_reason"] == "market_exit_after_invalid_take_profit"
    assert closed["requested_close_reason"] == "take_profit"
    assert "take_profit no longer matched" in closed["close_reason_warning"]


def test_close_position_rejects_unverified_live_exit_without_manual_price(capital_env, monkeypatch):
    manager, _ = capital_env
    monkeypatch.setattr(manager, "_get_execution_price", lambda ticker, side, **kwargs: _quote(100.0))
    opened = manager.open_position(
        ticker="AAPL",
        direction="LONG",
        size_usd=1000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
    )
    monkeypatch.setattr(
        manager,
        "_get_execution_price",
        lambda ticker, side, **kwargs: _quote(1.0, source="Fallback"),
    )

    with pytest.raises(ValueError, match="verified live exit price unavailable"):
        manager.close_position(position_id=opened["position_id"], exit_price=0.0, sync_signal=False)

    assert manager.get_position(opened["position_id"])["status"] == "OPEN"


def test_exit_recheck_skips_stale_take_profit_that_no_longer_matches_live_price(capital_env, monkeypatch):
    manager, _ = capital_env
    prices = iter([100.0, 101.0])

    def fake_execution_price(ticker: str, side: str, **kwargs) -> dict:
        assert kwargs.get("use_cache") is False
        return _quote(next(prices))

    monkeypatch.setattr(manager, "_get_execution_price", fake_execution_price)
    opened = manager.open_position(
        ticker="MSFT",
        direction="SHORT",
        size_usd=1000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="sig_stale_tp",
    )

    closed = manager._close_position_from_exit_decision(
        opened,
        reason="take_profit",
        trigger_price=95.0,
    )

    position = manager.get_position(opened["position_id"])
    assert closed is False
    assert position["status"] == "OPEN"
    assert "Exit recheck skipped MSFT take_profit" in position["last_exit_recheck_warning"]
    assert [h["type"] for h in manager.get_history()].count("CLOSE_POSITION") == 0


def test_exit_recheck_uses_actual_hard_reason_if_live_price_hits_stop(capital_env, monkeypatch):
    manager, _ = capital_env
    prices = iter([100.0, 103.0])

    def fake_execution_price(ticker: str, side: str, **kwargs) -> dict:
        assert kwargs.get("use_cache") is False
        return _quote(next(prices))

    monkeypatch.setattr(manager, "_get_execution_price", fake_execution_price)
    opened = manager.open_position(
        ticker="MSFT",
        direction="SHORT",
        size_usd=1000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="sig_rechecked_stop",
    )

    closed = manager._close_position_from_exit_decision(
        opened,
        reason="take_profit",
        trigger_price=95.0,
    )

    position = manager.get_position(opened["position_id"])
    assert closed is True
    assert position["status"] == "CLOSED"
    assert position["close_reason"] == "stop_loss"
    assert position["requested_close_reason"] == "stop_loss"
    assert position["trigger_price"] == pytest.approx(95.0)


def test_get_execution_price_uses_cache_with_side_adjustment(capital_env, monkeypatch):
    manager, _ = capital_env
    monkeypatch.setattr(manager, "_get_cached_price", lambda ticker: (100.0, "state_store"))

    buy = manager._get_execution_price("AAPL", "BUY")
    sell = manager._get_execution_price("AAPL", "SELL")
    long_alias = manager._get_execution_price("AAPL", "LONG")
    short_alias = manager._get_execution_price("AAPL", "SHORT")

    assert buy["execution_price"] == pytest.approx(100.01)
    assert sell["execution_price"] == pytest.approx(99.99)
    assert long_alias["execution_price"] == pytest.approx(buy["execution_price"])
    assert short_alias["execution_price"] == pytest.approx(sell["execution_price"])
    assert buy["source"] == "state_store"


def test_get_execution_price_uses_crypto_orderbook_bid_ask(capital_env, monkeypatch):
    manager, _ = capital_env
    monkeypatch.setattr(manager, "_get_cached_price", lambda ticker: None)

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"bids": [["99.50", "2"]], "asks": [["100.50", "2"]]}

    calls = []

    def fake_http_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr("core.http_utils.http_get", fake_http_get)

    buy = manager._get_execution_price("BTC-USD", "BUY")
    sell = manager._get_execution_price("BTC-USD", "SELL")

    assert buy["execution_price"] == pytest.approx(100.50)
    assert sell["execution_price"] == pytest.approx(99.50)
    assert buy["source"] == "Binance_orderbook"
    assert sell["source"] == "Binance_orderbook"
    assert calls[0][1]["params"]["symbol"] == "BTCUSDT"
    assert calls[0][1]["priority"] is True
    assert calls[0][1]["max_retries"] == 0


def test_get_execution_price_can_bypass_cache_for_crypto_orderbook(capital_env, monkeypatch):
    manager, _ = capital_env
    manager._update_price_cache("BTC-USD", 80.0)

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"bids": [["99.50", "2"]], "asks": [["100.50", "2"]]}

    monkeypatch.setattr("core.http_utils.http_get", lambda url, **kwargs: FakeResponse())

    buy = manager._get_execution_price("BTC-USD", "BUY", use_cache=False)
    sell = manager._get_execution_price("BTC-USD", "SELL", use_cache=False)

    assert buy["execution_price"] == pytest.approx(100.50)
    assert sell["execution_price"] == pytest.approx(99.50)
    assert buy["source"] == "Binance_orderbook"
    assert sell["source"] == "Binance_orderbook"


@pytest.mark.parametrize(
    "action,direction,current_price,expected_status,expected_reason,expected_gross",
    [
        ("BUY", "LONG", 104.0, "WIN", "Take Profit Hit (BUY)", 40.0),
        ("BUY", "LONG", 98.0, "LOST", "Stop Loss Hit (BUY)", -20.0),
        ("SHORT", "SHORT", 96.0, "WIN", "Take Profit Hit (SHORT)", 40.0),
        ("SHORT", "SHORT", 102.0, "LOST", "Stop Loss Hit (SHORT)", -20.0),
    ],
)
def test_signal_outcome_closes_attached_paper_position_at_exact_engine_price(
    capital_env,
    monkeypatch,
    tmp_path,
    action,
    direction,
    current_price,
    expected_status,
    expected_reason,
    expected_gross,
):
    manager, _ = capital_env
    monkeypatch.setattr(manager, "_get_execution_price", lambda ticker, side, **kwargs: _quote(100.0))
    monkeypatch.setattr("core.signals.get_capital_manager", lambda: manager)

    opened = manager.open_position(
        ticker="AAPL",
        direction=direction,
        size_usd=1000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="sig_engine_close",
    )
    sm = SignalManager(path=tmp_path / "signals.json")
    sm.open_signals = {
        "sig_engine_close": {
            "id": "sig_engine_close",
            "ticker": "AAPL",
            "action": action,
            "type": action,
            "entry": 100.0,
            "tp": 104.0 if action == "BUY" else 96.0,
            "sl": 98.0 if action == "BUY" else 102.0,
            "status": "Open",
            "paper_position_id": opened["position_id"],
            "executed_entry_price": opened["entry_price"],
        }
    }

    outcomes = sm.update_outcomes("AAPL", current_price)
    position = manager.get_position(opened["position_id"])

    assert outcomes == expected_status
    assert "sig_engine_close" not in sm.open_signals
    assert sm.closed_signals[0]["status"] == expected_status
    assert sm.closed_signals[0]["exit_price"] == pytest.approx(current_price)
    assert sm.closed_signals[0]["close_reason"] == expected_reason
    assert position["status"] == "CLOSED"
    assert position["exit_price"] == pytest.approx(current_price)
    assert position["close_reason"] == expected_reason
    assert position["gross_pnl"] == pytest.approx(expected_gross)
    # Entry + exit commission both deducted from net PnL
    entry_comm = 2.5
    exit_comm = current_price * 10.0 * 0.0025
    assert position["net_pnl"] == pytest.approx(expected_gross - entry_comm - exit_comm)


def test_state_file_persists_exact_closed_position_prices(capital_env, monkeypatch):
    manager, capital_path = capital_env
    monkeypatch.setattr(manager, "_get_execution_price", lambda ticker, side, **kwargs: _quote(123.456789))

    opened = manager.open_position(
        ticker="BTC-USD",
        direction="LONG",
        size_usd=1000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="sig_persist",
    )
    closed = manager.close_position(
        position_id=opened["position_id"],
        exit_price=130.123456,
        reason="persist_check",
        sync_signal=False,
    )
    saved = json.loads(capital_path.read_text(encoding="utf-8"))
    saved_position: Dict = next(p for p in saved["positions"] if p["position_id"] == opened["position_id"])

    assert saved_position["entry_price"] == opened["entry_price"]
    assert saved_position["exit_price"] == closed["exit_price"]
    assert saved_position["source_signal_id"] == "sig_persist"
    assert saved_position["status"] == "CLOSED"


def test_load_merges_duplicate_open_positions_and_refunds_same_signal(tmp_path, monkeypatch):
    monkeypatch.setenv("MS_DISABLE_CAPITAL_THREADS", "1")
    capital_path = tmp_path / "capital.json"
    monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", capital_path)
    monkeypatch.setattr(capital_mod, "get_sqlite_store", lambda: None)
    CapitalManager._instance = None

    capital_path.write_text(
        json.dumps(
            {
                "balance": 7995.0,
                "history": [],
                "positions": [
                    {
                        "position_id": "pos_a",
                        "ticker": "HUMA-USD",
                        "direction": "SHORT",
                        "entry_price": 100.0,
                        "qty": 10.0,
                        "usd_size": 1000.0,
                        "margin_used": 1000.0,
                        "commission_usd": 2.5,
                        "stop_loss_price": 102.0,
                        "take_profit_price": 96.0,
                        "opened_at": "2026-04-29T20:05:00",
                        "status": "OPEN",
                        "signal_id": "sig_huma",
                        "source_signal_id": "sig_huma",
                    },
                    {
                        "position_id": "pos_b",
                        "ticker": "HUMA-USD",
                        "direction": "SHORT",
                        "entry_price": 101.0,
                        "qty": 9.9009901,
                        "usd_size": 1000.0,
                        "margin_used": 1000.0,
                        "commission_usd": 2.5,
                        "stop_loss_price": 103.02,
                        "take_profit_price": 96.96,
                        "opened_at": "2026-04-29T20:06:00",
                        "status": "OPEN",
                        "signal_id": "sig_huma",
                        "source_signal_id": "sig_huma",
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    manager = CapitalManager()
    open_positions = [p for p in manager.get_positions() if p["status"] == "OPEN"]

    assert len(open_positions) == 1
    assert open_positions[0]["position_id"] == "pos_a"
    assert open_positions[0]["usd_size"] == pytest.approx(1000.0)
    assert open_positions[0]["ignored_duplicate_position_ids"] == ["pos_b"]
    assert manager.get_balance() == pytest.approx(8997.5)
    assert manager.get_history()[-1]["type"] == "DUPLICATE_POSITION_REFUND"
    saved = json.loads(capital_path.read_text(encoding="utf-8"))
    assert len([p for p in saved["positions"] if p["status"] == "OPEN"]) == 1

    CapitalManager._instance = None


def test_auto_position_deducts_exit_commission_on_close(capital_env, monkeypatch):
    manager, _ = capital_env

    def fake_execution_price(ticker: str, side: str, **kwargs) -> dict:
        if side == "BUY":
            return _quote(100.0)
        return _quote(105.0)

    monkeypatch.setattr(manager, "_get_execution_price", fake_execution_price)

    opened = manager.open_position(
        ticker="AAPL",
        direction="LONG",
        size_usd=1000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="sig_exit_comm",
    )
    closed = manager.close_position(
        position_id=opened["position_id"],
        exit_price=0.0,
        reason="Manual close",
        sync_signal=False,
    )

    exit_notional = 105.0 * 10.0
    expected_exit_comm = exit_notional * 0.0025
    assert closed["exit_commission"] == pytest.approx(expected_exit_comm)
    assert closed["exit_commission_rate"] == pytest.approx(0.0025)
    assert closed["exit_commission"] > 0
    assert closed["cash_pnl"] == pytest.approx(closed["gross_pnl"] - expected_exit_comm)
    assert closed["net_pnl"] == pytest.approx(closed["gross_pnl"] - 2.5 - expected_exit_comm)


def test_manual_position_debits_fees_at_entry(capital_env):
    manager, _ = capital_env

    opened = manager.open_manual_position(
        ticker="TSLA",
        direction="long",
        leverage=1,
        usd_size=1000.0,
        entry_price=100.0,
        fees_bps=10,
    )

    assert opened["commission_usd"] == pytest.approx(1.0)
    assert opened["fees_estimate"] == pytest.approx(1.0)
    assert opened["commission_rate"] == pytest.approx(0.001)
    assert opened["fees_bps"] == 10.0
    assert manager.get_balance() == pytest.approx(10_000.0 - 1000.0 - 1.0)


def test_merge_position_uses_conservative_sl_tp_for_long(capital_env, monkeypatch):
    manager, _ = capital_env

    def fake_execution_price(ticker: str, side: str, **kwargs) -> dict:
        return _quote(100.0)

    monkeypatch.setattr(manager, "_get_execution_price", fake_execution_price)

    first = manager.open_position(
        ticker="AAPL",
        direction="LONG",
        size_usd=1000.0,
        stop_loss_pct=0.05,
        take_profit_pct=0.10,
        signal_id="sig_merge_1",
    )

    prices = iter([105.0])
    monkeypatch.setattr(
        manager,
        "_get_execution_price",
        lambda ticker, side, **kw: _quote(next(prices)),
    )
    second = manager.open_position(
        ticker="AAPL",
        direction="LONG",
        size_usd=500.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.05,
        signal_id="sig_merge_2",
    )

    assert second["stop_loss_price"] == pytest.approx(95.0)
    assert second["take_profit_price"] == pytest.approx(110.25)
    assert second["trailing_sl"] == 0.0
    assert second["max_favorable_excursion"] == 0.0


def test_merge_position_uses_conservative_sl_tp_for_short(capital_env, monkeypatch):
    manager, _ = capital_env

    def fake_execution_price(ticker: str, side: str, **kwargs) -> dict:
        return _quote(100.0)

    monkeypatch.setattr(manager, "_get_execution_price", fake_execution_price)

    first = manager.open_position(
        ticker="AAPL",
        direction="SHORT",
        size_usd=1000.0,
        stop_loss_pct=0.05,
        take_profit_pct=0.10,
        signal_id="sig_short_1",
    )

    prices = iter([95.0])
    monkeypatch.setattr(
        manager,
        "_get_execution_price",
        lambda ticker, side, **kw: _quote(next(prices)),
    )
    second = manager.open_position(
        ticker="AAPL",
        direction="SHORT",
        size_usd=1000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.05,
        signal_id="sig_short_2",
    )

    assert second["stop_loss_price"] == pytest.approx(105.0)
    assert second["take_profit_price"] == pytest.approx(90.0)
    assert second["trailing_sl"] == 0.0


def test_merge_position_resets_trailing_state(capital_env, monkeypatch):
    manager, _ = capital_env

    def fake_execution_price(ticker: str, side: str, **kwargs) -> dict:
        return _quote(100.0)

    monkeypatch.setattr(manager, "_get_execution_price", fake_execution_price)

    opened = manager.open_position(
        ticker="AAPL",
        direction="LONG",
        size_usd=1000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="sig_trail_reset_1",
    )

    with manager._lock:
        for i, p in enumerate(manager.positions):
            if p.get("position_id") == opened["position_id"]:
                manager.positions[i]["trailing_sl"] = 103.0
                manager.positions[i]["max_favorable_excursion"] = 5.0
                break

    second = manager.open_position(
        ticker="AAPL",
        direction="LONG",
        size_usd=500.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="sig_trail_reset_2",
    )

    assert second["trailing_sl"] == 0.0
    assert second["max_favorable_excursion"] == 0.0


def test_closed_position_includes_pnl_pct_margin(capital_env, monkeypatch):
    manager, _ = capital_env

    def fake_execution_price(ticker: str, side: str, **kwargs) -> dict:
        if side == "BUY":
            return _quote(100.0)
        return _quote(110.0)

    monkeypatch.setattr(manager, "_get_execution_price", fake_execution_price)

    opened = manager.open_position(
        ticker="AAPL",
        direction="LONG",
        size_usd=2000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        leverage=2,
        signal_id="sig_margin_pct",
    )
    closed = manager.close_position(
        position_id=opened["position_id"],
        exit_price=0.0,
        sync_signal=False,
    )

    assert closed["pnl_pct_notional"] == pytest.approx(10.0)
    assert closed["pnl_pct_margin"] == pytest.approx(20.0)


# ─────────────────────────────────────────────────────────────────────────────
# CAP1 (Runda-5): open_position must NOT hold self._lock across the
# _get_execution_price HTTP fetch. Pre-fix the lock was acquired at :1147 and
# held through the fetch at :1253 (up to the 5s request timeout), serializing
# every other capital op (close/equity/snapshot) behind a concurrent open. The
# fetch is now hoisted outside the lock (mirroring close_position at :1512).
# ─────────────────────────────────────────────────────────────────────────────


def test_open_position_does_not_hold_lock_during_execution_price_fetch(capital_env, monkeypatch):
    """CAP1: while _get_execution_price is in flight, self._lock must be
    acquirable by another thread (i.e. NOT held during the HTTP fetch)."""
    manager, _ = capital_env
    fetch_started = threading.Event()
    proceed = threading.Event()

    def slow_execution_price(ticker, side, **kwargs):
        fetch_started.set()
        proceed.wait(timeout=5.0)  # simulate multi-second external HTTP latency
        return _quote(100.0)

    monkeypatch.setattr(manager, "_get_execution_price", slow_execution_price)

    result: Dict = {}

    def run_open():
        try:
            result["pos"] = manager.open_position(
                ticker="AAPL", direction="LONG", size_usd=1000.0,
                stop_loss_pct=0.02, take_profit_pct=0.04, signal_id="sig_cap1",
            )
        except Exception as exc:  # noqa: BLE001
            result["err"] = exc

    t = threading.Thread(target=run_open, name="cap1-open")
    t.start()
    try:
        assert fetch_started.wait(timeout=3.0), "execution-price fetch never started"
        # While the fetch is blocked, the lock MUST be acquirable (not held).
        # RLock from a DIFFERENT thread is not reentrant, so acquire(timeout=...)
        # returns False if the open thread still holds it (pre-fix regression).
        acquired = manager._lock.acquire(timeout=1.0)
        assert acquired, (
            "CAP1 regression: self._lock held during _get_execution_price -- "
            "open_position would serialize all capital ops behind the HTTP fetch"
        )
        manager._lock.release()
    finally:
        proceed.set()
        t.join(timeout=5.0)

    assert not t.is_alive(), "open_position thread did not finish"
    assert "err" not in result, f"open_position raised unexpectedly: {result.get('err')}"
    assert result.get("pos") is not None, "open_position returned no position"
