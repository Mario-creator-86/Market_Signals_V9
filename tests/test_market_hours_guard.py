import pytest

import core.capital_manager as capital_mod
from core.capital_manager import CapitalManager
from core.market_hours import MarketHoursError, is_equity_market_hours_enforced


@pytest.fixture
def isolated_manager(tmp_path, monkeypatch):
    capital_path = tmp_path / "capital.json"
    monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", capital_path)
    monkeypatch.setattr(capital_mod, "get_sqlite_store", lambda: None)
    monkeypatch.setenv("MS_MARKET_HOURS_OVERRIDE", "open")
    CapitalManager._instance = None
    manager = CapitalManager()
    yield manager
    CapitalManager._instance = None


def test_stock_paper_open_is_blocked_when_us_market_closed(isolated_manager, monkeypatch):
    monkeypatch.setenv("MS_MARKET_HOURS_OVERRIDE", "closed")

    with pytest.raises(MarketHoursError):
        isolated_manager.open_position(
            ticker="AAPL",
            direction="LONG",
            size_usd=10,
            stop_loss_pct=0.02,
            take_profit_pct=0.04,
            signal_id="sig-aapl",
        )


def test_manual_stock_open_allowed_when_us_market_closed(isolated_manager, monkeypatch):
    """Manual equity opens bypass the regular-hours guard (user opens 24/7)."""
    monkeypatch.setenv("MS_MARKET_HOURS_OVERRIDE", "closed")

    opened = isolated_manager.open_manual_position(
        ticker="AAPL",
        direction="long",
        leverage=1,
        usd_size=10,
        entry_price=100,
    )
    assert opened["status"] == "OPEN"
    assert opened["position_type"] == "manual"


def test_manual_stock_close_allowed_when_us_market_closed(isolated_manager, monkeypatch):
    """Manual equity closes bypass the regular-hours guard (user closes 24/7)."""
    import core.feedback_loop as feedback_loop

    monkeypatch.setattr(feedback_loop, "process_closed_trade", lambda closed_position: None)

    opened = isolated_manager.open_manual_position(
        ticker="AAPL",
        direction="long",
        leverage=1,
        usd_size=10,
        entry_price=100,
    )
    monkeypatch.setenv("MS_MARKET_HOURS_OVERRIDE", "closed")

    closed = isolated_manager.close_position(
        position_id=opened["position_id"],
        exit_price=101.0,
        reason="manual outside-hours close",
        sync_signal=False,
    )
    assert closed["status"] == "CLOSED"


def test_auto_stock_close_still_blocked_when_us_market_closed(isolated_manager, monkeypatch):
    """Automated (non-manual) equity closes still respect regular market hours."""
    opened = isolated_manager.open_manual_position(
        ticker="AAPL",
        direction="long",
        leverage=1,
        usd_size=10,
        entry_price=100,
    )
    # Re-label the position as an automated/scanner position so the manual
    # bypass must NOT apply.
    with isolated_manager._lock:
        for p in isolated_manager.positions:
            if p.get("position_id") == opened["position_id"]:
                p["position_type"] = "paper_signal"
                p["strategy_type"] = "SCANNER_TEST"
                break
        isolated_manager._save_unlocked()

    monkeypatch.setenv("MS_MARKET_HOURS_OVERRIDE", "closed")

    with pytest.raises(MarketHoursError):
        isolated_manager.close_position(
            position_id=opened["position_id"],
            exit_price=101.0,
            reason="auto close attempt",
        )


def test_crypto_can_open_and_close_when_us_stock_market_closed(isolated_manager, monkeypatch):
    monkeypatch.setenv("MS_MARKET_HOURS_OVERRIDE", "closed")
    import core.feedback_loop as feedback_loop

    monkeypatch.setattr(feedback_loop, "process_closed_trade", lambda closed_position: None)

    opened = isolated_manager.open_manual_position(
        ticker="CGPT-USD",
        direction="long",
        leverage=1,
        usd_size=10,
        entry_price=0.04,
    )
    closed = isolated_manager.close_position(
        position_id=opened["position_id"],
        exit_price=0.041,
        reason="test crypto close",
        sync_signal=False,
    )

    assert closed["status"] == "CLOSED"
    assert not is_equity_market_hours_enforced("CGPT-USD")
