"""tests/unit/test_trade_radar_audit_fixes.py
=============================================
Regression guards for the Trade Radar tab audit (2026-07).

  TR-COR-01 -- agents/trade_radar_agent.py _compute_confidence: backtest
        drawdown guard used the wrong sign. quick_backtester.max_drawdown_pct
        is a POSITIVE peak-to-trough magnitude (quick_backtester.py:145
        ``max_dd_r = max(max_dd_r, peak - cumulative)``), but the guard was
        ``dd >= -15.0`` -- always true for a positive magnitude -- so a
        strategy with a 60% drawdown still earned the +10 backtest bonus as
        long as pf>1.3. Inflated confidence_breakdown.backtest_bonus and pushed
        high-DD setups toward READY. Post-fix: ``dd <= 15.0`` / ``dd <= 20.0``
        (magnitude no worse than 15/20%). Win-rate-neutral in the current
        display-only Trade Radar (no signal opens from the agent).

  Q3 (display-only) -- Trade Radar computes readiness.can_open but never
        calls SignalManager/CapitalManager; the agent has zero references to
        open_position/add_signal/autonomous_candidates. This is by design (a
        radar, not an auto-trader). Wiring it to open trades would change
        which trades win/lose (CLAUDE.md §4/§8 -- needs A/B + signoff), so it
        is a deferred rule-improvement, NOT auto-applied. The test below
        guards the display-only contract so a future silent wiring is caught.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
__import__("os").environ.setdefault("TEST_MODE", "1")
__import__("os").environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")


# ─────────────────────────────────────────────────────────────────────────────
# TR-COR-01 -- _compute_confidence drawdown guard sign
# ─────────────────────────────────────────────────────────────────────────────

def _conf(backtest_pack):
    from agents.trade_radar_agent import TradeRadarAgent
    return TradeRadarAgent._compute_confidence(
        verdict="LONG",
        strength=50.0,
        news_pack={"bullish_count": 0, "bearish_count": 0},
        calendar_pack={},
        backtest_pack=backtest_pack,
    )


def test_tr_cor_01_high_drawdown_no_longer_earns_bonus():
    """TR-COR-01: a 60% drawdown (positive magnitude) with pf=1.5 must NOT earn
    the +10 backtest bonus. Pre-fix ``dd >= -15.0`` was always true -> bonus=10."""
    res = _conf({
        "available": True, "profit_factor": 1.5, "max_drawdown": 60.0,
        "closed_trades": 20, "signals_traded": 20,
    })
    assert res["backtest_bonus"] == 0.0, (
        f"TR-COR-01: 60% drawdown must earn 0 bonus; got {res['backtest_bonus']} "
        "(pre-fix the dead ``dd >= -15.0`` guard awarded +10 for any pf>1.3)"
    )


def test_tr_cor_01_moderate_drawdown_earns_lower_bonus():
    """TR-COR-01: pf=1.2, dd=18% -> +5 (the elif, dd<=20 but >15)."""
    res = _conf({
        "available": True, "profit_factor": 1.2, "max_drawdown": 18.0,
        "closed_trades": 20, "signals_traded": 20,
    })
    assert res["backtest_bonus"] == 5.0, (
        f"TR-COR-01: pf=1.2 dd=18 -> +5 (elif dd<=20); got {res['backtest_bonus']}"
    )


def test_tr_cor_01_low_drawdown_earns_full_bonus():
    """TR-COR-01: pf=1.5, dd=10% -> +10 (the if, dd<=15). Backward-compat for
    genuinely healthy backtests -- the fix must not penalize good setups."""
    res = _conf({
        "available": True, "profit_factor": 1.5, "max_drawdown": 10.0,
        "closed_trades": 20, "signals_traded": 20,
    })
    assert res["backtest_bonus"] == 10.0, (
        f"TR-COR-01: pf=1.5 dd=10 -> +10 (healthy backtest); got {res['backtest_bonus']}"
    )


def test_tr_cor_01_boundary_dd_exactly_15():
    """TR-COR-01: dd=15.0 exactly -> +10 (boundary, dd<=15 inclusive)."""
    res = _conf({
        "available": True, "profit_factor": 1.5, "max_drawdown": 15.0,
        "closed_trades": 20, "signals_traded": 20,
    })
    assert res["backtest_bonus"] == 10.0, f"dd=15 boundary -> +10; got {res['backtest_bonus']}"


def test_tr_cor_01_dd_just_over_15_drops_to_elif():
    """TR-COR-01: dd=16, pf=1.5 -> +5 (dropped from if to elif)."""
    res = _conf({
        "available": True, "profit_factor": 1.5, "max_drawdown": 16.0,
        "closed_trades": 20, "signals_traded": 20,
    })
    assert res["backtest_bonus"] == 5.0, f"dd=16 -> +5 (elif); got {res['backtest_bonus']}"


def test_tr_cor_01_pf_below_threshold_no_bonus():
    """TR-COR-01: pf=1.05, dd=5 -> 0 (pf not > 1.1). Sanity: bonus needs pf edge."""
    res = _conf({
        "available": True, "profit_factor": 1.05, "max_drawdown": 5.0,
        "closed_trades": 20, "signals_traded": 20,
    })
    assert res["backtest_bonus"] == 0.0, f"pf=1.05 -> 0 bonus; got {res['backtest_bonus']}"


def test_automatic_cycle_limit_respects_provider_safe_cap():
    from agents.trade_radar_agent import TradeRadarAgent

    cfg = {"max_per_cycle": 12, "auto_max_per_cycle": 4}

    assert TradeRadarAgent._resolve_cycle_limit(cfg, manual=False) == 4
    assert TradeRadarAgent._resolve_cycle_limit(cfg, manual=True) == 12


def test_automatic_loop_uses_cache_only_request_context():
    import threading

    from agents.trade_radar_agent import TradeRadarAgent
    from core.request_context import get_request_context

    agent = TradeRadarAgent.__new__(TradeRadarAgent)
    agent._startup_delay_seconds = 0
    agent._auto_cache_only = True
    agent._stop_event = threading.Event()
    agent._wake_event = threading.Event()
    agent._lock = threading.RLock()
    agent._pending_force = False
    agent._pending_symbols = set()
    agent._state = {"config": {"radar_interval_sec": 30}}
    observed = {}

    def capture_cycle(**_kwargs):
        observed["cache_only"] = get_request_context().cache_only
        agent._stop_event.set()
        agent._wake_event.set()
        return {}

    agent.run_cycle = capture_cycle
    agent._loop()

    assert observed["cache_only"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Q3 -- Trade Radar is display-only (no signal-opening path). Guard the
# contract so a future silent wiring to SignalManager/CapitalManager is caught.
# ─────────────────────────────────────────────────────────────────────────────

def test_tr_q3_agent_does_not_open_signals_or_positions():
    """Q3: Trade Radar must remain display-only unless explicitly wired with
    A/B + signoff. The agent module must NOT reference signal-opening APIs
    (SignalManager.add / CapitalManager.open_position / autonomous_candidates
    registration). If a future change wires the agent to open trades, this
    test fails loudly so the win-rate-affecting change is reviewed, not slipped
    in as a 'fix'."""
    import agents.trade_radar_agent as mod
    src = Path(mod.__file__).read_text(encoding="utf-8")
    forbidden = [
        "SignalManager",
        "open_position",
        "capital_manager",
        "CapitalManager",
        "autonomous_candidates",
        "register_candidate",
    ]
    leaks = [token for token in forbidden if token in src]
    assert leaks == [], (
        f"Q3 contract broken: Trade Radar agent now references {leaks} -- this "
        "wires a display-only radar into the live signal/position path, which "
        "changes which trades win/lose (CLAUDE.md §4/§8). Require A/B + signoff."
    )


def test_tr_q3_frontend_has_no_open_trade_action():
    """Q3: the Trade Radar render must not expose an 'open trade' / 'enter'
    action -- only Details + Suppress (display-only contract on the UI side)."""
    src = Path("frontend/app.js").read_text(encoding="utf-8")
    # The render is in renderTradeRadar; extract that block.
    start = src.find("UI.renderTradeRadar = function")
    end = src.find("UI.startTradeRadarAutoRefresh")
    assert start != -1 and end != -1, "could not locate renderTradeRadar block"
    block = src[start:end]
    forbidden = ["openTrade", "enterTrade", "Actions.tradeRadarOpen", "openSignal"]
    leaks = [t for t in forbidden if t in block]
    assert leaks == [], (
        f"Q3 contract broken: renderTradeRadar now exposes {leaks} -- a display-"
        "only radar must not offer a trade-opening action without signoff."
    )
    # The two permitted actions must still be present (display-only controls).
    assert "toggleTradeRadarDetails" in block, "Details toggle must remain"
    assert "suppressTradeRadarSymbol" in block, "Suppress action must remain"
