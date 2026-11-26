"""tests/unit/test_autonomous_ticker_lockout.py
================================================
Regression guards for the per-ticker post-close re-entry lockout
(Capital Profitability A/B, 2026-07 -- see core/agent_manager.py
``_has_recent_close_lockout`` docstring and
scripts/ab_capital_profitability.py).

Why this gate exists (CLAUDE.md §4 before/after math): on 276 autonomous
closed trades, the baseline was WR 29.7% / PF 0.641 / net PnL -1381. Repeat
re-entries on the same ticker (VANRY 30 closes, EPIC 26, HMSTR 25, NFP 21,
SYN 19 ...) were the single biggest leak: a 6h post-close per-ticker lockout
lifts the kept population to WR 34.4% / PF 0.701 / net -376 -- +1005 PnL /
73% loss reduction at a still-respectable n=96. The existing 45-min
``autonomous_cooldown_minutes`` gate keys on candidate REVIEW, not on the
actual close event, so it cannot prevent this pattern. The new gate derives
from the CLOSED position history in capital state (source of truth), so it
survives process restarts with no new in-memory registry to lose.
"""
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
__import__("os").environ.setdefault("TEST_MODE", "1")
__import__("os").environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

import core.agent_manager as am_mod  # noqa: E402
from core.agent_manager import AgentManager  # noqa: E402


def _signal(ticker: str, action: str = "BUY") -> dict:
    return {"ticker": ticker, "action": action, "autonomous_source": "scanner"}


class _FakeCM:
    """Minimal capital-manager stand-in returning a fixed position list."""

    def __init__(self, positions: list[dict]):
        self._positions = positions

    def get_positions(self) -> list[dict]:
        return list(self._positions)


def _closed_pos(ticker: str, closed_at: datetime) -> dict:
    return {
        "ticker": ticker,
        "status": "CLOSED",
        "closed_at": closed_at.isoformat(),
        "direction": "LONG",
        "pnl": -1.0,
    }


@pytest.fixture
def patched_cm(monkeypatch):
    """Patch core.capital_manager.get_capital_manager (imported lazily inside
    _has_recent_close_lockout) and yield a setter so each test controls the
    closed-position set."""
    holder = {"cm": _FakeCM([])}

    def _factory():
        return holder["cm"]

    monkeypatch.setattr(
        "core.capital_manager.get_capital_manager",
        _factory,
        raising=True,
    )
    yield holder
    holder["cm"] = _FakeCM([])  # reset between tests


# ─────────────────────────────────────────────────────────────────────────────
# _has_recent_close_lockout unit behaviour
# ─────────────────────────────────────────────────────────────────────────────

def test_lockout_blocks_recent_close(patched_cm):
    """A CLOSED position on the same ticker 1h ago with a 6h lockout -> blocked."""
    patched_cm["cm"] = _FakeCM([_closed_pos("ETH-USD", datetime.now(timezone.utc) - timedelta(hours=1))])
    mgr = AgentManager.__new__(AgentManager)  # bypass __init__ (no engine deps)
    blocked, reason = mgr._has_recent_close_lockout(_signal("ETH-USD"), 6.0)
    assert blocked is True
    assert "ticker_lockout" in reason
    assert "ETH-USD" in reason


def test_lockout_allows_old_close(patched_cm):
    """A CLOSED position 10h ago is outside a 6h lockout -> allowed."""
    patched_cm["cm"] = _FakeCM([_closed_pos("ETH-USD", datetime.now(timezone.utc) - timedelta(hours=10))])
    mgr = AgentManager.__new__(AgentManager)
    blocked, reason = mgr._has_recent_close_lockout(_signal("ETH-USD"), 6.0)
    assert blocked is False
    assert reason == ""


def test_lockout_no_closed_position_on_ticker(patched_cm):
    """A close on a DIFFERENT ticker must not block this ticker."""
    patched_cm["cm"] = _FakeCM([_closed_pos("BTC-USD", datetime.now(timezone.utc) - timedelta(hours=1))])
    mgr = AgentManager.__new__(AgentManager)
    blocked, _ = mgr._has_recent_close_lockout(_signal("ETH-USD"), 6.0)
    assert blocked is False


def test_lockout_disabled_when_zero(patched_cm):
    """lockout_hours=0 disables the gate entirely (config kill-switch)."""
    patched_cm["cm"] = _FakeCM([_closed_pos("ETH-USD", datetime.now(timezone.utc) - timedelta(minutes=1))])
    mgr = AgentManager.__new__(AgentManager)
    blocked, _ = mgr._has_recent_close_lockout(_signal("ETH-USD"), 0.0)
    assert blocked is False


def test_lockout_ignores_open_positions(patched_cm):
    """An OPEN position (not CLOSED) must not trigger the lockout -- the gate
    is about re-entry AFTER a close, and the open-conflict gate already
    handles the open-position case."""
    patched_cm["cm"] = _FakeCM([{
        "ticker": "ETH-USD", "status": "OPEN",
        "closed_at": datetime.now(timezone.utc).isoformat(),
    }])
    mgr = AgentManager.__new__(AgentManager)
    blocked, _ = mgr._has_recent_close_lockout(_signal("ETH-USD"), 6.0)
    assert blocked is False


def test_lockout_boundary_just_inside(patched_cm):
    """Closed 5h59m ago with 6h lockout -> still blocked (boundary inclusive
    on the inside, ``< horizon_s``)."""
    patched_cm["cm"] = _FakeCM([
        _closed_pos("ETH-USD", datetime.now(timezone.utc) - timedelta(hours=5, minutes=59))
    ])
    mgr = AgentManager.__new__(AgentManager)
    blocked, _ = mgr._has_recent_close_lockout(_signal("ETH-USD"), 6.0)
    assert blocked is True


def test_lockout_boundary_just_outside(patched_cm):
    """Closed 6h01m ago with 6h lockout -> allowed."""
    patched_cm["cm"] = _FakeCM([
        _closed_pos("ETH-USD", datetime.now(timezone.utc) - timedelta(hours=6, minutes=1))
    ])
    mgr = AgentManager.__new__(AgentManager)
    blocked, _ = mgr._has_recent_close_lockout(_signal("ETH-USD"), 6.0)
    assert blocked is False


def test_lockout_fail_open_on_bad_timestamp(patched_cm):
    """A malformed closed_at must be skipped (not crash) -- and a position
    with NO closed_at must be skipped too. Fail-open on unparseable data."""
    patched_cm["cm"] = _FakeCM([{
        "ticker": "ETH-USD", "status": "CLOSED", "closed_at": "not-a-date",
    }])
    mgr = AgentManager.__new__(AgentManager)
    blocked, _ = mgr._has_recent_close_lockout(_signal("ETH-USD"), 6.0)
    assert blocked is False  # unparseable -> skipped, not crash


def test_lockout_multiple_tickers_uses_most_recent(patched_cm):
    """If a ticker has multiple closed positions, the MOST RECENT close
    governs the lockout. A 1h-ago close must block even if there is an older
    20h-ago close on the same ticker."""
    now = datetime.now(timezone.utc)
    patched_cm["cm"] = _FakeCM([
        _closed_pos("ETH-USD", now - timedelta(hours=20)),
        _closed_pos("ETH-USD", now - timedelta(hours=1)),
    ])
    mgr = AgentManager.__new__(AgentManager)
    blocked, _ = mgr._has_recent_close_lockout(_signal("ETH-USD"), 6.0)
    assert blocked is True


# ─────────────────────────────────────────────────────────────────────────────
# Integration: _autonomous_monitor_once respects the lockout and does NOT
# send a locked candidate to execution.
# ─────────────────────────────────────────────────────────────────────────────

def test_monitor_once_skips_locked_ticker(monkeypatch, patched_cm):
    """A candidate whose ticker recently closed must be skipped by
    _autonomous_monitor_once and never reach on_new_signal. This is the
    end-to-end guard that the gate is wired into the live autonomous path."""
    patched_cm["cm"] = _FakeCM([_closed_pos("ETH-USD", datetime.now(timezone.utc) - timedelta(hours=1))])

    mgr = AgentManager.__new__(AgentManager)
    mgr.agents = {"execution": type("E", (), {"is_running": True})()}
    mgr._autonomous_last_decisions = {}
    monkeypatch.setattr(mgr, "_bot_mode_active", lambda: True)

    # Force the config + candidate set the monitor loop reads.
    monkeypatch.setattr(mgr, "_execution_config", lambda: {
        "autonomous_candidates_enabled": True,
        "autonomous_sources": {"scanner": True, "pre_earnings": False},
        "max_autonomous_candidates_per_cycle": 4,
        "max_autonomous_trades_per_cycle": 1,
        "autonomous_cooldown_minutes": 45.0,
        "autonomous_ticker_lockout_hours": 6.0,
        "autonomous_allow_scale_in": False,
    })
    # One scanner candidate on the locked ticker.
    monkeypatch.setattr(mgr, "_scanner_candidates", lambda cfg: [_signal("ETH-USD", "BUY")])

    sent: list[dict] = []
    monkeypatch.setattr(mgr, "on_new_signal", lambda s: sent.append(s))
    # Bypass the market-hours / allowed-now check.
    monkeypatch.setattr(AgentManager, "_autonomous_trade_allowed_now",
                        staticmethod(lambda s: (True, "")))
    monkeypatch.setattr(mgr, "_has_open_ticker_conflict", lambda s, *, allow_scale_in: False)

    mgr._autonomous_monitor_once()

    assert sent == [], (
        "A ticker within its 6h post-close lockout must NOT be sent to "
        f"execution; got {sent}"
    )
    # The review-cooldown key must be stamped so the 45-min gate also applies
    # (consistent with the open-conflict skip path).
    assert "scanner:ETH-USD:BUY" in mgr._autonomous_last_decisions


def test_monitor_once_allows_unlocked_ticker(monkeypatch, patched_cm):
    """Negative: a ticker that closed 10h ago (outside the 6h lockout) must
    flow through to on_new_signal. Guards against an over-broad lockout."""
    patched_cm["cm"] = _FakeCM([_closed_pos("ETH-USD", datetime.now(timezone.utc) - timedelta(hours=10))])

    mgr = AgentManager.__new__(AgentManager)
    mgr.agents = {"execution": type("E", (), {"is_running": True})()}
    mgr._autonomous_last_decisions = {}
    monkeypatch.setattr(mgr, "_bot_mode_active", lambda: True)

    monkeypatch.setattr(mgr, "_execution_config", lambda: {
        "autonomous_candidates_enabled": True,
        "autonomous_sources": {"scanner": True, "pre_earnings": False},
        "max_autonomous_candidates_per_cycle": 4,
        "max_autonomous_trades_per_cycle": 1,
        "autonomous_cooldown_minutes": 45.0,
        "autonomous_ticker_lockout_hours": 6.0,
        "autonomous_allow_scale_in": False,
    })
    monkeypatch.setattr(mgr, "_scanner_candidates", lambda cfg: [_signal("ETH-USD", "BUY")])
    sent: list[dict] = []
    monkeypatch.setattr(mgr, "on_new_signal", lambda s: sent.append(s))
    monkeypatch.setattr(AgentManager, "_autonomous_trade_allowed_now",
                        staticmethod(lambda s: (True, "")))
    monkeypatch.setattr(mgr, "_has_open_ticker_conflict", lambda s, *, allow_scale_in: False)

    mgr._autonomous_monitor_once()

    assert len(sent) == 1, (
        "A ticker outside its lockout window must flow through to execution"
    )
