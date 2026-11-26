"""tests/unit/test_trade_radar_signal_audit.py
=============================================
Regression guards for the Trade Radar signal-quality audit (2026-08).

Live evidence that drove each fix (24-candidate state, 2026-08-29):

  SIG-01 verdict staleness — BICOUSDT/ENAUSDT/QNRX carried LONG verdicts from
        their pump day while the CURRENT move was -15/-16/-6.9%. Root cause:
        `_build_local_analysis_stub` gave the previous verdict unconditional
        priority. Fix: a >=5% counter-move overrides the carried verdict.

  SIG-02 ATR floor — BICOUSDT entry 0.0227 rendered target 0.0427 (+88%) /
        stop 0.0077 (-66%): the synthetic-ATR floor `max(0.01, price*2%)`
        is a 44% band on a $0.02 token. Fix: relative-only floor.

  SIG-03 untradeable indices — a "^TNX SHORT -49.7%" radar row nobody could
        act on. Fix: '^'-prefixed symbols skipped at intake.

  SIG-04 backtest sample size — FTFT earned the full +10 backtest bonus from
        a 4-trade PF 4.39 and displayed conf 80 with every indicator missing.
        Fix: under 5 closed trades the bonus is halved.

  SIG-05 evidence cap — rows with ALL key indicators null still displayed
        conf 62-80. Fix: capped at 59 (below the 60 entry gate) with the cap
        recorded in the breakdown and explained in the reasons.

  SIG-06 processing fairness — due-ranking by |change| only let the same big
        stale movers monopolize the 4-per-cycle auto budget; QNRX went
        unprocessed for 24h while its card mixed a fresh -6.9% with a day-old
        "+27.84%" analysis. Fix: longest-waiting-first ranking.

  SIG-07 serve-time staleness flag — `last_seen` newer than `last_processed`
        now surfaces as the `analysis_older_than_move` red flag.
"""
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
__import__("os").environ.setdefault("TEST_MODE", "1")
__import__("os").environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

from agents.trade_radar_agent import TradeRadarAgent, _now_utc  # noqa: E402


# ── SIG-01: verdict flips on significant counter-move ──────────────────────

def _stub(verdict_prev, change_pct, trend=""):
    return TradeRadarAgent._build_local_analysis_stub(
        symbol="TEST",
        candidate={"analysis": {"verdict": verdict_prev, "strategy": {}}} if verdict_prev else {},
        watchlist_item={"trend_1h": trend},
        price=10.0,
        change_pct=change_pct,
        atr=None,
    )


def test_sig01_countermove_overrides_stale_long_verdict():
    """BICOUSDT case: prev verdict LONG, current move -16.3% -> SHORT."""
    stub = _stub("LONG", -16.3)
    assert stub["best_signal"]["direction"] == "SELL", (
        "SIG-01: a -16% move must override yesterday's LONG verdict "
        "(live bug: LONG verdict on -16.3% same-day reversal)"
    )


def test_sig01_countermove_overrides_stale_short_verdict():
    stub = _stub("SHORT", 22.4)
    assert stub["best_signal"]["direction"] == "BUY"


def test_sig01_small_pullback_keeps_carried_verdict():
    """A <5% counter-move is pullback noise — the carried verdict survives."""
    stub = _stub("LONG", -3.0)
    assert stub["best_signal"]["direction"] == "BUY"


def test_sig01_no_prev_verdict_uses_move_direction():
    stub = _stub(None, -6.9)
    assert stub["best_signal"]["direction"] == "SELL"


# ── SIG-02: relative ATR floor for sub-cent assets ─────────────────────────

def test_sig02_subcent_levels_are_relative():
    """BICOUSDT case: entry 0.0227 must give ~±4% levels, not +88%/-66%."""
    entry, tp, sl = TradeRadarAgent._derive_trade_levels(
        analysis_raw={"last_signal": {}},
        verdict="LONG",
        price=0.0227,
        atr=None,
    )
    assert entry == pytest.approx(0.0227, abs=1e-9)
    assert tp == pytest.approx(0.0227 * 1.04, abs=1e-6), (
        f"SIG-02: target {tp} on a $0.0227 token — the absolute $0.01 ATR "
        "floor is still leaking (live bug: +88% target on a PULLBACK row)"
    )
    assert sl == pytest.approx(0.0227 * 0.97, abs=1e-6)


def test_sig02_normal_price_levels_unchanged():
    entry, tp, sl = TradeRadarAgent._derive_trade_levels(
        analysis_raw={"last_signal": {}},
        verdict="LONG",
        price=100.0,
        atr=None,
    )
    assert tp == pytest.approx(104.0)   # +2 * 2% synthetic ATR
    assert sl == pytest.approx(97.0)    # -1.5 * 2%


# ── SIG-04: backtest bonus needs a sample ──────────────────────────────────

def _conf(backtest):
    return TradeRadarAgent._compute_confidence(
        verdict="LONG",
        strength=70.0,
        news_pack={"bullish_count": 0, "bearish_count": 0},
        calendar_pack={},
        backtest_pack=backtest,
    )


def test_sig04_four_trade_bonus_is_halved():
    res = _conf({"available": True, "profit_factor": 4.39, "max_drawdown": 5.0,
                 "closed_trades": 4, "signals_traded": 4})
    assert res["backtest_bonus"] == 5.0, (
        "SIG-04: PF from 4 trades is noise (live: FTFT conf 80 off a 4-trade "
        "backtest with every indicator missing)"
    )


def test_sig04_healthy_sample_keeps_full_bonus():
    res = _conf({"available": True, "profit_factor": 1.5, "max_drawdown": 10.0,
                 "closed_trades": 20, "signals_traded": 20})
    assert res["backtest_bonus"] == 10.0


def test_sig04_tiny_sample_moderate_tier_halves_to_two_point_five():
    res = _conf({"available": True, "profit_factor": 1.2, "max_drawdown": 18.0,
                 "closed_trades": 3, "signals_traded": 3})
    assert res["backtest_bonus"] == 2.5


# ── SIG-05: evidence cap under missing technicals ──────────────────────────

def test_sig05_confidence_capped_when_indicators_missing(monkeypatch):
    """Full enrich with all indicators absent -> final conf <= 59 and the
    breakdown records the cap (live: conf 62-80 with rsi/adx/atr all null)."""
    agent = TradeRadarAgent.__new__(TradeRadarAgent)
    agent._get_ohlc_cached = lambda **kwargs: (None, None, ["ohlc_local_cache_miss"])
    agent._build_news_pack = lambda **kwargs: ({"available": False, "bullish_count": 0, "bearish_count": 0, "top": [], "last_update": None}, {"news_ts": None})
    agent._build_calendar_pack = lambda **kwargs: ({"available": False, "earnings": {}, "macro_next_72h": [], "last_update": None}, {"calendar_ts": None})
    agent._run_backtest_lite = lambda **kwargs: {"available": False, "days": 30, "win_rate": 0.0, "profit_factor": 0.0, "max_drawdown": 0.0, "closed_trades": 0, "signals_traded": 0, "open_trades": 0, "variant_used": "RADAR_QUICK", "fallback_used": False, "last_update": None}
    agent._quick_backtester = None
    agent._last_news_refresh_ts = 0.0
    agent._last_calendar_refresh_ts = 0.0

    candidate = TradeRadarAgent._new_candidate("TESTUSDT")
    candidate.update({"change_pct": 16.0, "price": 0.02, "last_seen": "2026-08-29T00:00:00+00:00"})
    out = agent._enrich_candidate(
        symbol="TESTUSDT",
        candidate=candidate,
        watchlist_item={"signal_score": 80},
        cfg=dict(TradeRadarAgent.DEFAULT_CONFIG),
        now=_now_utc(),
        force_refresh=False,
    )
    analysis = out["analysis"]
    assert analysis["confidence"] <= 59, (
        f"SIG-05: conf {analysis['confidence']} with every indicator null — "
        "the evidence cap is not applied"
    )
    assert analysis["confidence_breakdown"]["evidence_cap"] == 59
    assert "capped" in analysis["confidence_breakdown"]["formula"]
    assert any("Evidence cap" in r for r in out["reasons"])
    assert out["readiness"]["status"] == "BLOCKED"


def test_sig05_confidence_uncapped_with_real_indicators(monkeypatch):
    """With indicators present the cap must not fire (backward-compat)."""
    import pandas as pd

    agent = TradeRadarAgent.__new__(TradeRadarAgent)
    frame = pd.DataFrame([
        {"timestamp": pd.Timestamp.utcnow(), "open": 10, "high": 10.2, "low": 9.9,
         "close": 10.1, "volume": 1000, "rsi": 55.0, "adx": 25.0, "macd_hist": 0.1, "atr": 0.2},
    ])
    agent._get_ohlc_cached = lambda **kwargs: (frame, "2026-08-29T00:00:00+00:00", [])
    agent._build_news_pack = lambda **kwargs: ({"available": False, "bullish_count": 0, "bearish_count": 0, "top": [], "last_update": None}, {"news_ts": None})
    agent._build_calendar_pack = lambda **kwargs: ({"available": False, "earnings": {}, "macro_next_72h": [], "last_update": None}, {"calendar_ts": None})
    agent._run_backtest_lite = lambda **kwargs: {"available": False, "days": 30, "win_rate": 0.0, "profit_factor": 0.0, "max_drawdown": 0.0, "closed_trades": 0, "signals_traded": 0, "open_trades": 0, "variant_used": "RADAR_QUICK", "fallback_used": False, "last_update": None}
    agent._quick_backtester = None
    agent._last_news_refresh_ts = 0.0
    agent._last_calendar_refresh_ts = 0.0

    candidate = TradeRadarAgent._new_candidate("TEST")
    candidate.update({"change_pct": 6.0, "price": 10.0, "last_seen": "2026-08-29T00:00:00+00:00"})
    out = agent._enrich_candidate(
        symbol="TEST",
        candidate=candidate,
        watchlist_item={"signal_score": 80},
        cfg=dict(TradeRadarAgent.DEFAULT_CONFIG),
        now=_now_utc(),
        force_refresh=False,
    )
    assert out["analysis"]["confidence"] == 80
    assert out["analysis"]["confidence_breakdown"]["evidence_cap"] is None


# ── SIG-06: due-ranking rotates by wait time ────────────────────────────────

def test_sig06_longest_waiting_processed_first():
    now = _now_utc()
    cmap = {
        "BIG": {"change_pct": 90.0, "last_processed": (now - timedelta(minutes=10)).isoformat()},
        "SMALL": {"change_pct": 6.0, "last_processed": (now - timedelta(hours=20)).isoformat()},
        "NEVER": {"change_pct": 5.5, "last_processed": None},
    }
    ranked = sorted(["BIG", "SMALL", "NEVER"], key=lambda s: TradeRadarAgent._due_rank(s, cmap, set(), now), reverse=True)
    assert ranked[0] in {"NEVER", "SMALL"}, (
        "SIG-06: never/longest-processed must outrank the fresher big mover — "
        "the old |change|-only ranking starved small names for a full day"
    )
    assert ranked[-1] == "BIG"


def test_sig06_forced_symbol_outranks_everything():
    now = _now_utc()
    cmap = {
        "OLD": {"change_pct": 5.0, "last_processed": (now - timedelta(hours=30)).isoformat()},
        "MAN": {"change_pct": 1.0, "last_processed": (now - timedelta(minutes=1)).isoformat()},
    }
    ranked = sorted(["OLD", "MAN"], key=lambda s: TradeRadarAgent._due_rank(s, cmap, {"MAN"}, now), reverse=True)
    assert ranked[0] == "MAN"


# ── SIG-07: serve-time staleness flag ───────────────────────────────────────

def test_sig07_analysis_older_than_move_flagged(monkeypatch):
    agent = TradeRadarAgent.__new__(TradeRadarAgent)
    agent._lock = __import__("threading").RLock()
    fresh_move = (_now_utc() - timedelta(minutes=5)).isoformat()
    old_analysis = (_now_utc() - timedelta(hours=20)).isoformat()
    agent._state = {
        "as_of": fresh_move,
        "config": {},
        "suppressed": {},
        "candidates": [{
            "symbol": "QNRX",
            "change_pct": -6.86,
            "last_seen": fresh_move,
            "last_processed": old_analysis,
            "red_flags": [],
            "analysis": {"verdict": "LONG", "confidence": 76},
        }],
    }
    state = agent.get_state(limit=10)
    assert "analysis_older_than_move" in state["candidates"][0]["red_flags"], (
        "SIG-07: scanner saw a newer move than the last analysis — the card "
        "mixes a fresh % with an old verdict and must say so"
    )
    # idempotent on re-serve
    state2 = agent.get_state(limit=10)
    assert state2["candidates"][0]["red_flags"].count("analysis_older_than_move") == 1


def test_sig07_fresh_analysis_not_flagged():
    agent = TradeRadarAgent.__new__(TradeRadarAgent)
    agent._lock = __import__("threading").RLock()
    ts = _now_utc().isoformat()
    agent._state = {
        "as_of": ts,
        "config": {},
        "suppressed": {},
        "candidates": [{
            "symbol": "FRESH",
            "change_pct": 6.0,
            "last_seen": ts,
            "last_processed": ts,
            "red_flags": [],
            "analysis": {"verdict": "LONG", "confidence": 61},
        }],
    }
    state = agent.get_state(limit=10)
    assert "analysis_older_than_move" not in state["candidates"][0]["red_flags"]
