"""Regression: V2 strategy override must be reachable in MarketEngine._analyze_ticker.

Bug (H1): the V2 override block was physically AFTER a ``return {...}`` statement in
``_analyze_ticker``, so V2 candidates were computed (selector run, exit levels
recomputed) but never applied to the returned signal — ``strategy_source`` never
became "V2" and the V2 strategy layer was silently inert. The override was extracted
into ``MarketEngine._apply_v2_override`` and is now called before the single return.

These tests exercise the extracted helper directly (no engine init / no data I/O).
"""
from core.engine import MarketEngine


def _engine():
    # _apply_v2_override is a pure function over its arguments; avoid the heavy
    # MarketEngine.__init__ (state load, background threads).
    return MarketEngine.__new__(MarketEngine)


def _base_result():
    return {
        "type": "BUY",
        "entry": 100.0,
        "tp": 105.0,
        "sl": 98.0,
        "tp_pct": 5.0,
        "sl_pct": 2.0,
        "signal_score": 70,
        "signal_mode": "PULLBACK",
        "strategy_source": "ENGINE",
        "strategy_type": "",
        "rr_ratio": 2.5,
        "reasons": ["base reason"],
    }


def _v2_candidate(direction="LONG"):
    return {
        "type": "BUY" if direction == "LONG" else "SHORT",
        "tp": 108.0,
        "sl": 97.0,
        "direction": direction,
        "signal_score": 88.0,
        "strategy_type": "long_breakout",
        "validation_score": 70.0,
        "setup_score": 80.0,
        "risk_score": 60.0,
        "catalyst_score": 50.0,
    }


def test_apply_v2_override_returns_unchanged_when_no_candidate():
    engine = _engine()
    base = _base_result()
    out = engine._apply_v2_override(base, None, v2_tp_pct=8.0, v2_sl_pct=3.0, v2_rr=3.6)
    assert out is base
    assert out["strategy_source"] == "ENGINE"
    assert out["type"] == "BUY"
    assert out["signal_score"] == 70
    assert out["reasons"] == ["base reason"]


def test_apply_v2_override_applies_candidate_fields():
    engine = _engine()
    base = _base_result()
    cand = _v2_candidate("LONG")
    out = engine._apply_v2_override(base, cand, v2_tp_pct=8.0, v2_sl_pct=3.0, v2_rr=3.6)
    assert out["strategy_source"] == "V2"
    assert out["strategy_type"] == "long_breakout"
    assert out["type"] == "BUY"
    assert out["direction"] == "LONG"
    assert out["signal_score"] == 88.0
    assert out["signal_mode"] == "long_breakout"
    assert out["tp"] == 108.0
    assert out["tp_pct"] == 8.0
    assert out["sl"] == 97.0
    assert out["sl_pct"] == 3.0
    assert out["rr_ratio"] == round(3.6, 3)
    assert out["validation_score"] == 70.0
    assert out["setup_score"] == 80.0
    assert out["risk_score"] == 60.0
    assert out["catalyst_score"] == 50.0
    assert "Strategy: long_breakout" in out["reasons"]
    assert "V2 Score: 88.0" in out["reasons"]
    # original reason preserved
    assert out["reasons"][0] == "base reason"


def test_apply_v2_override_short_direction():
    engine = _engine()
    base = _base_result()
    cand = _v2_candidate("SHORT")
    out = engine._apply_v2_override(base, cand, v2_tp_pct=8.0, v2_sl_pct=3.0, v2_rr=3.6)
    assert out["direction"] == "SHORT"
    assert out["type"] == "SHORT"


def test_apply_v2_override_skips_tp_sl_when_zero():
    engine = _engine()
    base = _base_result()
    cand = _v2_candidate()
    cand["tp"] = 0
    cand["sl"] = 0
    out = engine._apply_v2_override(base, cand, v2_tp_pct=8.0, v2_sl_pct=3.0, v2_rr=3.6)
    # tp/sl kept from base when V2 didn't compute them
    assert out["tp"] == 105.0
    assert out["sl"] == 98.0
    assert out["tp_pct"] == 5.0
    assert out["sl_pct"] == 2.0
    # but strategy_source still V2
    assert out["strategy_source"] == "V2"


# --------------------------------------------------------------------------- #
# M8 (Runda-5): V2 signal_threshold must carry the V2 min_edge_score gate
# --------------------------------------------------------------------------- #
def test_apply_v2_override_m8_overwrites_signal_threshold_with_v2_gate():
    """M8: a V2 signal must carry the V2 threshold (min_edge_score), not the
    stale V1 ``best_signal["threshold"]`` that _analyze_ticker set before the
    override. Live Open/Pending cutoff in signals.py is ``max(70,
    signal_threshold+10)``; if a V2 signal keeps the V1 threshold, V2 signals
    (which already passed the V2 gate) can be rejected as "below threshold" by
    the live cutoff -- a self-contradiction. The fix overwrites
    ``signal_threshold`` with the V2 gate only when min_edge_score>0."""
    engine = _engine()
    base = _base_result()
    base["signal_threshold"] = 60  # stale V1 threshold (would corrupt the V2 cutoff)
    cand = _v2_candidate("LONG")
    out = engine._apply_v2_override(
        base, cand, v2_tp_pct=8.0, v2_sl_pct=3.0, v2_rr=3.6, min_edge_score=75)
    assert out["signal_threshold"] == 75, (
        "M8: V2 signal_threshold must be the V2 min_edge_score (75), not the "
        f"stale V1 threshold; got {out['signal_threshold']!r}"
    )


def test_apply_v2_override_m8_no_min_edge_score_leaves_threshold_untouched():
    """M8 backward-compat: when min_edge_score is omitted (or 0), the helper
    must NOT touch signal_threshold -- preserves the 5-arg call sites used by
    the existing tests and any non-V2-gated callers."""
    engine = _engine()
    base = _base_result()
    base["signal_threshold"] = 60
    cand = _v2_candidate("LONG")
    out = engine._apply_v2_override(
        base, cand, v2_tp_pct=8.0, v2_sl_pct=3.0, v2_rr=3.6)  # no min_edge_score
    assert out["signal_threshold"] == 60, (
        "M8: omitting min_edge_score must leave signal_threshold unchanged "
        f"(backward compat); got {out['signal_threshold']!r}"
    )
    # min_edge_score=0 (falsy) is also a no-op
    base2 = _base_result()
    base2["signal_threshold"] = 60
    out2 = engine._apply_v2_override(
        base2, cand, v2_tp_pct=8.0, v2_sl_pct=3.0, v2_rr=3.6, min_edge_score=0.0)
    assert out2["signal_threshold"] == 60, (
        "M8: min_edge_score=0 must be a no-op on signal_threshold"
    )