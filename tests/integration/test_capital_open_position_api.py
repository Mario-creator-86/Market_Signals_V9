import pytest

import core.capital_manager as capital_mod
from core.capital_manager import CapitalManager


pytestmark = pytest.mark.integration


def _quote(price: float, source: str = "integration_test", level: str = "VERIFIED") -> dict:
    return {
        "execution_price": float(price),
        "execution_price_quality": {
            "value": float(price),
            "level": level,
            "source": source,
            "is_valid": level != "UNAVAILABLE",
        },
        "spread_pct": 0.0,
        "slippage_pct": 0.0,
        "commission_rate": 0.0025,
        "total_cost_pct": 0.25,
        "price_timestamp": "2026-04-26T12:00:00+00:00",
        "source": source,
    }


@pytest.fixture
def isolated_capital_api(tmp_path, monkeypatch):
    capital_path = tmp_path / "capital_api.json"
    monkeypatch.setenv("MS_DISABLE_CAPITAL_THREADS", "1")
    monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", capital_path)
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
    yield capital_path
    CapitalManager._instance = None
    CapitalManager._price_cache = {}
    CapitalManager._atr_cache = {}


def test_open_position_api_accepts_valid_payload(client, isolated_capital_api, monkeypatch):
    calls = []

    def fake_execution_price(self, ticker, side, **kwargs):
        calls.append((ticker, side, kwargs))
        return _quote(100.0)

    monkeypatch.setattr(CapitalManager, "_get_execution_price", fake_execution_price)

    response = client.post(
        "/api/capital/open-position",
        json={
            "ticker": "AAPL",
            "direction": "long",
            "leverage": 5,
            "usd_size": 1000,
            "stop_loss_pct": 0.02,
            "take_profit_pct": 0.04,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload.get("status") == "ok"
    position = payload.get("position", {})
    assert position.get("ticker") == "AAPL"
    assert position.get("direction") == "long"
    assert position.get("entry_price") == pytest.approx(100.0)
    assert position.get("stop_loss_price") == pytest.approx(98.0)
    assert position.get("take_profit_price") == pytest.approx(104.0)
    assert position.get("entry_reason") == "Manual terminal order"
    assert position.get("source") == "integration_test"
    assert position.get("exit_management_mode") == "manual_levels_only"
    assert position.get("automatic_trailing_enabled") is False
    assert position.get("ai_exit_management_enabled") is False
    assert position.get("trailing_sl") == 0.0
    assert position.get("cost_model", {}).get("commission_bps_per_side") == pytest.approx(25.0)
    assert position.get("cost_model", {}).get("provenance") == "execution_quote_estimate"
    assert isinstance(payload.get("history"), list)
    assert calls == [("AAPL", "BUY", {"use_cache": False})]


@pytest.mark.parametrize(
    "requested,canonical",
    [("BTCUSDT", "BTC-USD"), ("GOLD", "GC=F"), ("OIL", "CL=F"), ("AAPL", "AAPL")],
)
def test_manual_chart_order_normalizes_supported_asset_classes(
    client, isolated_capital_api, monkeypatch, requested, canonical
):
    import core.market_hours as market_hours

    calls = []
    monkeypatch.setattr(market_hours, "assert_trade_allowed", lambda ticker, action: None)

    def fake_execution_price(self, ticker, side, **kwargs):
        calls.append((ticker, side, kwargs))
        return _quote(100.0)

    monkeypatch.setattr(CapitalManager, "_get_execution_price", fake_execution_price)
    response = client.post(
        "/api/capital/open-position",
        json={
            "ticker": requested,
            "direction": "short",
            "leverage": 2,
            "usd_size": 100,
            "stop_loss_pct": 0.02,
            "take_profit_pct": 0.04,
            "reason": "Rejected resistance from the internal chart",
            "strategy_type": "MANUAL_CHART",
        },
    )

    assert response.status_code == 200
    position = response.json()["position"]
    assert position["ticker"] == canonical
    assert position["direction"] == "short"
    assert position["strategy_type"] == "MANUAL_CHART"
    assert position["entry_reason"] == "Rejected resistance from the internal chart"
    assert position["decision_context"]["strategy_type"] == "MANUAL_CHART"
    assert position["decision_context"]["reasons"] == ["Rejected resistance from the internal chart"]
    assert {
        "entry_thesis",
        "supporting_evidence",
        "opposing_evidence",
        "strategy",
        "signal",
        "ai_model",
        "invalidation_rules",
        "risk_state",
    }.issubset(position)
    assert position["entry_thesis"] == "Rejected resistance from the internal chart"
    assert position["strategy"] == "MANUAL_CHART"
    assert position["ai_model"] is None
    assert position["risk_state"]["status"] == "NORMAL"
    assert calls == [(canonical, "SELL", {"use_cache": False})]


def test_manual_open_api_allowed_outside_market_hours(client, isolated_capital_api, monkeypatch):
    """MANUAL strategy_type opens bypass the regular-hours guard (24/7 manual)."""
    monkeypatch.setenv("MS_MARKET_HOURS_OVERRIDE", "closed")
    monkeypatch.setattr(
        CapitalManager, "_get_execution_price", lambda self, ticker, side, **kw: _quote(100.0)
    )

    response = client.post(
        "/api/capital/open-position",
        json={
            "ticker": "AAPL",
            "direction": "long",
            "leverage": 1,
            "usd_size": 100,
            "strategy_type": "MANUAL",
        },
    )
    assert response.status_code == 200
    assert response.json()["position"]["ticker"] == "AAPL"


def test_non_manual_open_api_blocked_outside_market_hours(client, isolated_capital_api, monkeypatch):
    """Non-MANUAL strategy_type opens still enforce regular market hours."""
    monkeypatch.setenv("MS_MARKET_HOURS_OVERRIDE", "closed")
    monkeypatch.setattr(
        CapitalManager, "_get_execution_price", lambda self, ticker, side, **kw: _quote(100.0)
    )

    response = client.post(
        "/api/capital/open-position",
        json={
            "ticker": "AAPL",
            "direction": "long",
            "leverage": 1,
            "usd_size": 100,
            "strategy_type": "SCANNER_AUTO",
        },
    )
    assert response.status_code == 409


def test_open_position_api_rejects_leverage_over_5(client, isolated_capital_api, monkeypatch):
    monkeypatch.setattr(CapitalManager, "_get_execution_price", lambda self, ticker, side, **kwargs: _quote(100.0))

    response = client.post(
        "/api/capital/open-position",
        json={
            "ticker": "AAPL",
            "direction": "long",
            "leverage": 8,
            "usd_size": 1000,
        },
    )
    assert response.status_code == 400


def test_open_position_api_rejects_unverified_fallback_price(client, isolated_capital_api, monkeypatch):
    monkeypatch.setattr(
        CapitalManager,
        "_get_execution_price",
        lambda self, ticker, side, **kwargs: _quote(1.0, source="Fallback", level="ESTIMATED"),
    )

    response = client.post(
        "/api/capital/open-position",
        json={
            "ticker": "MSFT",
            "direction": "long",
            "leverage": 1,
            "usd_size": 1000,
        },
    )
    assert response.status_code == 409
    assert "verified live entry price unavailable" in response.json()["detail"]


@pytest.mark.parametrize(
    "direction,exit_price,expected_gross,expected_net,expected_cash,expected_pct",
    [
        # Manual position: fees_bps=10 → entry_comm=1.0, exit_comm=exit_price*10*0.001
        # net_pnl = gross - entry_comm - exit_comm
        # cash_pnl = gross - exit_comm (entry already debited at open)
        ("long", 110.0, 100.0, 94.75, 97.25, 10.0),
        ("long", 95.0, -50.0, -54.875, -52.375, -5.0),
        ("short", 90.0, 100.0, 95.25, 97.75, 10.0),
        ("short", 105.0, -50.0, -55.125, -52.625, -5.0),
    ],
)
def test_open_close_position_api_records_exact_exit_price_and_pnl(
    client,
    isolated_capital_api,
    monkeypatch,
    direction,
    exit_price,
    expected_gross,
    expected_net,
    expected_cash,
    expected_pct,
):
    monkeypatch.setattr(CapitalManager, "_get_execution_price", lambda self, ticker, side, **kwargs: _quote(100.0))
    reset = client.post("/api/capital/reset", json={"amount": 10000, "reason": "integration"})
    assert reset.status_code == 200

    opened = client.post(
        "/api/capital/open-position",
        json={
            "ticker": "AAPL",
            "direction": direction,
            "leverage": 1,
            "usd_size": 1000,
            "stop_loss_pct": 0.02,
            "take_profit_pct": 0.04,
        },
    )
    assert opened.status_code == 200
    position_id = opened.json()["position"]["position_id"]

    closed = client.post(
        "/api/capital/close-position",
        json={"position_id": position_id, "exit_price": exit_price, "reason": "integration close"},
    )
    assert closed.status_code == 200
    position = closed.json()["position"]
    assert position["status"] == "CLOSED"
    assert position["exit_price"] == pytest.approx(exit_price)
    assert position["gross_pnl"] == pytest.approx(expected_gross)
    assert position["net_pnl"] == pytest.approx(expected_net)
    assert position["cash_pnl"] == pytest.approx(expected_cash)
    assert position["pnl_pct_notional"] == pytest.approx(expected_pct)


def test_close_position_api_rejects_fat_finger_exit_price(client, isolated_capital_api, monkeypatch):
    monkeypatch.setattr(CapitalManager, "_get_execution_price", lambda self, ticker, side, **kwargs: _quote(100.0))
    opened = client.post(
        "/api/capital/open-position",
        json={"ticker": "AAPL", "direction": "long", "leverage": 1, "usd_size": 100},
    )
    assert opened.status_code == 200
    position_id = opened.json()["position"]["position_id"]

    closed = client.post(
        "/api/capital/close-position",
        json={"position_id": position_id, "exit_price": 130.0},
    )
    assert closed.status_code == 400
    assert "deviates" in closed.json()["detail"]


def test_close_position_api_live_exit_uses_uncached_closing_side(client, isolated_capital_api, monkeypatch):
    calls = []
    prices = iter([100.0, 101.0])

    def fake_execution_price(self, ticker, side, **kwargs):
        calls.append((ticker, side, kwargs))
        return _quote(next(prices))

    monkeypatch.setattr(CapitalManager, "_get_execution_price", fake_execution_price)

    opened = client.post(
        "/api/capital/open-position",
        json={"ticker": "MSFT", "direction": "long", "leverage": 1, "usd_size": 100},
    )
    assert opened.status_code == 200
    position_id = opened.json()["position"]["position_id"]

    closed = client.post(
        "/api/capital/close-position",
        json={"position_id": position_id, "reason": "integration live close"},
    )
    assert closed.status_code == 200
    assert closed.json()["position"]["exit_price"] == pytest.approx(101.0)
    assert calls == [
        ("MSFT", "BUY", {"use_cache": False}),
        ("MSFT", "SELL", {"use_cache": False}),
    ]


def test_close_position_api_rejects_unverified_live_exit_price(client, isolated_capital_api, monkeypatch):
    monkeypatch.setattr(CapitalManager, "_get_execution_price", lambda self, ticker, side, **kwargs: _quote(100.0))
    opened = client.post(
        "/api/capital/open-position",
        json={"ticker": "AAPL", "direction": "long", "leverage": 1, "usd_size": 100},
    )
    assert opened.status_code == 200
    position_id = opened.json()["position"]["position_id"]

    monkeypatch.setattr(
        CapitalManager,
        "_get_execution_price",
        lambda self, ticker, side, **kwargs: _quote(1.0, source="Fallback", level="ESTIMATED"),
    )
    closed = client.post(
        "/api/capital/close-position",
        json={"position_id": position_id, "reason": "integration close"},
    )
    assert closed.status_code == 409
    assert "verified live exit price unavailable" in closed.json()["detail"]


def test_update_position_levels_uses_live_price_and_disables_trailing(
    client, isolated_capital_api, monkeypatch
):
    calls = []

    def fake_execution_price(self, ticker, side, **kwargs):
        calls.append((ticker, side, kwargs))
        return _quote(100.0 if side == "BUY" else 105.0)

    monkeypatch.setattr(CapitalManager, "_get_execution_price", fake_execution_price)
    opened = client.post(
        "/api/capital/open-position",
        json={
            "ticker": "AAPL",
            "direction": "long",
            "leverage": 2,
            "usd_size": 200,
            "stop_loss_pct": 0.02,
            "take_profit_pct": 0.04,
        },
    )
    assert opened.status_code == 200
    position_id = opened.json()["position"]["position_id"]

    updated = client.post(
        "/api/capital/update-position-levels",
        json={
            "position_id": position_id,
            "stop_loss_price": 101.0,
            "take_profit_price": 112.0,
            "reason": "Protect open profit",
        },
    )

    assert updated.status_code == 200
    payload = updated.json()
    assert payload["live_price"] == pytest.approx(105.0)
    assert payload["position"]["stop_loss_price"] == pytest.approx(101.0)
    assert payload["position"]["take_profit_price"] == pytest.approx(112.0)
    assert payload["position"]["automatic_trailing_enabled"] is False
    assert payload["position"]["ai_exit_management_enabled"] is False
    assert payload["position"]["manual_risk_override"] is True
    assert calls[-1] == ("AAPL", "SELL", {"use_cache": False})


def test_update_position_levels_rejects_levels_that_would_trigger_immediately(
    client, isolated_capital_api, monkeypatch
):
    monkeypatch.setattr(
        CapitalManager,
        "_get_execution_price",
        lambda self, ticker, side, **kwargs: _quote(100.0 if side == "BUY" else 105.0),
    )
    opened = client.post(
        "/api/capital/open-position",
        json={"ticker": "AAPL", "direction": "long", "leverage": 1, "usd_size": 100},
    )
    assert opened.status_code == 200
    position_id = opened.json()["position"]["position_id"]

    rejected = client.post(
        "/api/capital/update-position-levels",
        json={
            "position_id": position_id,
            "stop_loss_price": 105.0,
            "take_profit_price": 112.0,
        },
    )

    assert rejected.status_code == 400
    assert "LONG stop loss" in rejected.json()["detail"]
    position = CapitalManager().get_position(position_id)
    assert position["stop_loss_price"] == pytest.approx(98.0)
    assert position["take_profit_price"] == pytest.approx(104.0)


def test_reset_capital_api_validates_amount_and_can_preserve_positions(client, isolated_capital_api, monkeypatch):
    monkeypatch.setattr(CapitalManager, "_get_execution_price", lambda self, ticker, side, **kwargs: _quote(100.0))

    bad_reset = client.post("/api/capital/reset", json={"amount": 0})
    assert bad_reset.status_code == 400

    opened = client.post(
        "/api/capital/open-position",
        json={"ticker": "AAPL", "direction": "long", "leverage": 1, "usd_size": 100},
    )
    assert opened.status_code == 200

    preserved = client.post(
        "/api/capital/reset",
        json={"amount": 15000, "reason": "preserve", "clear_positions": False},
    )
    assert preserved.status_code == 200
    # Capital-integrity (C1): a reset that KEEPS open positions must not leave
    # the reserved margin un-debited from the new balance -- otherwise the
    # margin is double-available (free cash now + refunded again on close).
    # set_balance subtracts sum(open margin_used) so balance reflects the live
    # reservation. Assert balance = amount - reserved_margin (derived from the
    # kept position, not hardcoded, so it stays correct if commission changes).
    preserved_body = preserved.json()
    assert len(preserved_body["positions"]) == 1
    reserved_margin = float(preserved_body["positions"][0].get("margin_used", 0.0) or 0.0)
    assert preserved_body["balance"] == pytest.approx(15000.0 - reserved_margin)

    cleared = client.post(
        "/api/capital/reset",
        json={"amount": 12000, "reason": "clear", "clear_positions": True},
    )
    assert cleared.status_code == 200
    assert cleared.json()["positions"] == []
