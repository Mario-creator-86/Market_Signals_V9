import pytest

import core.capital_manager as capital_mod
from core.capital_manager import CapitalManager


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", tmp_path / "capital.json")
    monkeypatch.setattr(capital_mod, "get_sqlite_store", lambda: None)
    CapitalManager._instance = None
    value = CapitalManager()
    yield value
    CapitalManager._instance = None


def test_partial_reduce_releases_margin_and_allocates_fees_exactly(manager):
    opened = manager.open_manual_position(
        ticker="AAPL",
        direction="long",
        leverage=5,
        usd_size=1000,
        entry_price=100,
        max_size_pct=0.5,
        fees_bps=10,
    )
    reduced = manager.reduce_position(
        position_id=opened["position_id"],
        fraction=0.5,
        exit_price=110,
        reason="owner configured risk reduction",
    )
    assert reduced["status"] == "OPEN"
    assert reduced["qty"] == pytest.approx(5.0)
    assert reduced["usd_size"] == pytest.approx(500.0)
    assert reduced["margin_used"] == pytest.approx(100.0)
    assert reduced["commission_usd"] == pytest.approx(0.5)
    assert reduced["realized_pnl"] == pytest.approx(48.95)
    assert manager.get_balance() == pytest.approx(948.45)
    assert reduced["realized_reductions"][0]["net_pnl"] == pytest.approx(48.95)
    assert {entry["type"] for entry in manager.get_history()} >= {
        "RELEASE_MARGIN", "REDUCE_POSITION"
    }
    closed = manager.close_position(position_id=opened["position_id"], exit_price=110)
    assert closed["net_pnl"] == pytest.approx(97.9)
    assert closed["close_leg_net_pnl"] == pytest.approx(48.95)
    assert manager.get_balance() == pytest.approx(1097.9)


@pytest.mark.parametrize("fraction", [0, 1, -0.1, 1.1])
def test_partial_reduce_rejects_non_partial_fraction(manager, fraction):
    with pytest.raises(ValueError, match="between 0 and 1"):
        manager.reduce_position(position_id="missing", fraction=fraction, exit_price=100)
