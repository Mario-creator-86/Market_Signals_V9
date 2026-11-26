"""tests/unit/test_trade_radar_ghost_expiry.py
=================================================
Regression guards for the Trade Radar ghost-setup fixes (2026-07):

* TR-COR-04 — candidates whose ``last_seen`` is older than
  ``candidate_max_age_hours`` are expired at the start of each cycle so a
  stalled refresh cycle cannot leave month-stale "READY" setups.
* TR-COR-03 — a candidate with all key indicators null (rsi/adx/atr) gets the
  ``missing_indicators`` red flag, which is now a hard blocker.
* The hard_blocker_set now includes ``backtest_no_trades`` and
  ``ohlc_local_cache_insufficient`` so a 0-trade / cache-too-short candidate
  cannot reach READY.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
__import__("os").environ.setdefault("TEST_MODE", "1")
__import__("os").environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

from agents.trade_radar_agent import TradeRadarAgent  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# hard_blocker_set membership (pure, no I/O)
# ─────────────────────────────────────────────────────────────────────────────

def test_missing_indicators_is_hard_blocker():
    res = TradeRadarAgent._build_readiness(
        verdict="LONG", confidence=70.0, entry=100.0, tp=106.0, sl=97.0,
        red_flags=["missing_indicators"],
    )
    assert res["status"] == "BLOCKED"
    assert "missing_indicators" in res["blockers"]
    assert res["can_open"] is False


def test_backtest_no_trades_is_hard_blocker():
    res = TradeRadarAgent._build_readiness(
        verdict="LONG", confidence=70.0, entry=100.0, tp=106.0, sl=97.0,
        red_flags=["backtest_no_trades"],
    )
    assert res["status"] == "BLOCKED"
    assert res["can_open"] is False


def test_ohlc_local_cache_insufficient_is_hard_blocker():
    res = TradeRadarAgent._build_readiness(
        verdict="SHORT", confidence=65.0, entry=100.0, tp=94.0, sl=103.0,
        red_flags=["ohlc_local_cache_insufficient"],
    )
    assert res["status"] == "BLOCKED"
    assert res["can_open"] is False


def test_clean_candidate_still_reaches_ready():
    """No red flags + verdict + levels + confidence>=60 -> READY (regression
    guard: the new blockers must not over-block a valid setup)."""
    res = TradeRadarAgent._build_readiness(
        verdict="LONG", confidence=70.0, entry=100.0, tp=106.0, sl=97.0,
        red_flags=[],
    )
    assert res["status"] == "READY"
    assert res["can_open"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Ghost-setup expiry — exercises run_cycle's expiry branch without network.
# We monkeypatch the heavy I/O collaborators and feed a stale state directly.
# ─────────────────────────────────────────────────────────────────────────────

def _stale_candidate(symbol="GHOST", hours_old=48):
    from datetime import datetime, timedelta, timezone
    ls = (datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat()
    return {
        "symbol": symbol,
        "source": "scanner",
        "change_pct": 25.0,
        "price": 10.0,
        "volume": 1000.0,
        "last_seen": ls,
        "analysis": {"verdict": "LONG", "confidence": 80.0,
                     "entry": 10.0, "tp": 10.6, "sl": 9.7},
        "readiness": {"status": "READY", "can_open": True},
    }


def _fresh_candidate(symbol="LIVE", hours_old=1):
    c = _stale_candidate(symbol, hours_old)
    c["change_pct"] = 18.0
    return c


def test_ghost_candidate_expired_by_max_age(monkeypatch):
    agent = TradeRadarAgent.__new__(TradeRadarAgent)
    agent.config_manager = type("CM", (), {"config": {}, "reload_if_changed": lambda self: None})()
    agent._lock = __import__("threading").Lock()
    agent._state = {
        "config": {"candidate_max_age_hours": 24, "threshold_pct": 10.0,
                    "threshold_pct_crypto": None, "threshold_pct_stock": None},
        "suppressed": {},
        "candidates": [_stale_candidate("GHOST", 48), _fresh_candidate("LIVE", 1)],
    }
    # Avoid network: scanner returns nothing, watchlist empty -> only expiry runs
    monkeypatch.setattr(agent, "_collect_scanner_candidates", lambda: [])
    monkeypatch.setattr(agent, "_load_watchlist_map", lambda: {})
    monkeypatch.setattr(TradeRadarAgent, "_candidate_map", lambda self, cur: {c["symbol"]: dict(c) for c in cur})
    monkeypatch.setattr(TradeRadarAgent, "_sanitize_config", lambda self, c, e: c)
    monkeypatch.setattr(TradeRadarAgent, "_cleanup_suppressed_map", lambda self, m: m)
    monkeypatch.setattr(agent, "_persist_locked", lambda: None)
    # is_agent_enabled
    import agents.trade_radar_agent as mod
    monkeypatch.setattr(mod, "is_agent_enabled", lambda *a, **k: True)

    out = agent.run_cycle()
    syms = [c["symbol"] for c in out["candidates"]]
    assert "GHOST" not in syms, "stale ghost must be expired"
    assert "LIVE" in syms, "fresh candidate must survive"


def test_expiry_disabled_when_max_age_zero(monkeypatch):
    agent = TradeRadarAgent.__new__(TradeRadarAgent)
    agent.config_manager = type("CM", (), {"config": {}, "reload_if_changed": lambda self: None})()
    agent._lock = __import__("threading").Lock()
    agent._state = {
        "config": {"candidate_max_age_hours": 0, "threshold_pct": 10.0,
                    "threshold_pct_crypto": None, "threshold_pct_stock": None},
        "suppressed": {},
        "candidates": [_stale_candidate("GHOST", 720)],  # 30 days old
    }
    monkeypatch.setattr(agent, "_collect_scanner_candidates", lambda: [])
    monkeypatch.setattr(agent, "_load_watchlist_map", lambda: {})
    monkeypatch.setattr(TradeRadarAgent, "_candidate_map", lambda self, cur: {c["symbol"]: dict(c) for c in cur})
    monkeypatch.setattr(TradeRadarAgent, "_sanitize_config", lambda self, c, e: c)
    monkeypatch.setattr(TradeRadarAgent, "_cleanup_suppressed_map", lambda self, m: m)
    monkeypatch.setattr(agent, "_persist_locked", lambda: None)
    import agents.trade_radar_agent as mod
    monkeypatch.setattr(mod, "is_agent_enabled", lambda *a, **k: True)

    out = agent.run_cycle()
    syms = [c["symbol"] for c in out["candidates"]]
    # 0 = disabled -> ghost survives (legacy behaviour)
    assert "GHOST" in syms


# ─────────────────────────────────────────────────────────────────────────────
# missing_indicators red flag emission (unit-level on the flag, not full enrich)
# ─────────────────────────────────────────────────────────────────────────────

def test_missing_indicators_blocks_ready_via_build_readiness():
    """End-to-end-ish: the flag the enrich step emits must hard-block."""
    flags = ["missing_indicators"]
    res = TradeRadarAgent._build_readiness(
        verdict="LONG", confidence=80.0, entry=100.0, tp=106.0, sl=97.0,
        red_flags=flags,
    )
    assert res["can_open"] is False
    assert res["status"] == "BLOCKED"