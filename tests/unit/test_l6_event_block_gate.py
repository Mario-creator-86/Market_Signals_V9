"""tests/unit/test_l6_event_block_gate.py
==========================================
Regression guard for L6 (Runda-5): the V2 ``event_block`` quality gate
(``core/strategy_v2/quality_gates.py:57``) was dead code pre-fix because the
context never carried a populated ``hours_to_event``/``event_risk`` -- the
engine omitted to pass ``hours_to_event`` into ``build_context`` (engine.py
now derives it from the earnings snapshot at :2549-2578 and forwards it at
:2578, and ``context_builder`` sets ``event_risk = hours_to_event <= 6.0``).

This test exercises the gate DIRECTLY through ``QualityGates.evaluate`` to
prove the gate now fires (returns False + an ``event_block:<h>h`` reason) when
the context reports event risk within the block window, and is inert otherwise.
Win-rate-neutral guard: it only locks the gate's wiring, not any scoring
membership change.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
__import__("os").environ.setdefault("TEST_MODE", "1")
__import__("os").environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

from core.strategy_v2.quality_gates import QualityGates  # noqa: E402
from core.strategy_v2.types import CandidateSignal  # noqa: E402


def _cand(rr: float = 3.0, asset_class: str = "US_STOCKS") -> CandidateSignal:
    return CandidateSignal(
        strategy_id="S1", strategy_name="trend_pullback", symbol="AAPL",
        asset_class=asset_class, direction="LONG", regime="TREND",
        entry=100.0, sl=98.0, tp1=106.0, rr=rr,
        trailing_rule="act2.0/f0.8", invalidation_condition="close<sl",
    )


def _ctx(**kw):
    base = {"volume": 5_000_000.0, "spread_bps": 1.0, "atr_pct": 0.01,
            "hours_to_event": 999.0, "event_risk": False}
    base.update(kw)
    return base


def test_l6_event_block_fires_when_event_risk_within_window():
    """L6: event_risk=True + hours_to_event(4) <= event_block_hours(6) -> gate
    FAILS with an ``event_block:<h>h`` reason. Pre-fix this never fired because
    the context always carried event_risk=False / hours_to_event=999."""
    gates = QualityGates({"event_block_hours": 6.0})
    ok, reasons = gates.evaluate(_ctx(event_risk=True, hours_to_event=4.0), _cand())
    assert ok is False, "event_block gate must reject when event risk is within the block window"
    assert any(r.startswith("event_block:") for r in reasons), (
        f"must emit an event_block: reason; got {reasons}"
    )


def test_l6_event_block_inert_when_no_event_risk():
    """L6 negative: a context with no event risk (the historical default) must
    NOT trip the event_block gate -- a clean candidate passes. Guards against
    the gate spuriously blocking every ticker."""
    gates = QualityGates({"event_block_hours": 6.0})
    ok, reasons = gates.evaluate(_ctx(event_risk=False, hours_to_event=999.0), _cand())
    assert ok is True, (
        f"a clean candidate with no event risk must pass; reasons={reasons}"
    )
    assert not any(r.startswith("event_block") for r in reasons), (
        f"event_block must not appear when event_risk=False; got {reasons}"
    )


def test_l6_event_block_inert_when_event_beyond_window():
    """L6 boundary: event_risk=True but hours_to_event(48) > event_block_hours(6)
    -> the event is too far out to block -> gate passes (event_risk flag alone
    is not enough; proximity within the window is required)."""
    gates = QualityGates({"event_block_hours": 6.0})
    ok, reasons = gates.evaluate(_ctx(event_risk=True, hours_to_event=48.0), _cand())
    assert ok is True, (
        f"event beyond the block window must not block; reasons={reasons}"
    )
    assert not any(r.startswith("event_block") for r in reasons)


def test_quality_gates_honor_relaxed_rr_above_safety_floor():
    gates = QualityGates({"min_rr": 1.6})

    accepted, reasons = gates.evaluate(_ctx(), _cand(rr=1.6))
    assert accepted is True
    assert not any(reason.startswith("rr_below_min") for reason in reasons)

    rejected, reasons = gates.evaluate(_ctx(), _cand(rr=1.19))
    assert rejected is False
    assert any(reason.startswith("rr_below_min") for reason in reasons)
