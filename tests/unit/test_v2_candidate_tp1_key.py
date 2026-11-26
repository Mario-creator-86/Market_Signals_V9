"""Regression: MarketEngine._build_v2_candidate_dict must read the V2
take-profit from the ``tp1`` key, not ``tp``.

Bug: ``StrategySelector.select(...)["best_candidate"]`` is a dict from
``CandidateSignal.to_dict()``, which emits the take-profit under ``tp1``
(there is no ``tp`` key). The live engine previously read ``bc.get("tp", 0)``
→ always 0 → ``_apply_v2_override`` skipped the TP override (live V2 signal
kept the V1 ATR TP while taking V2's structure SL → mixed risk models, and
V2 reversal signals were silently dropped by add_signal's TP-ordering
validation when V2 flipped direction vs V1). The backtester already read
``tp1`` correctly, so live and backtest diverged.

These tests feed a real ``CandidateSignal.to_dict()`` to the extracted
helper and pin that the V2 tp1/sl/direction flow through unchanged.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.engine import MarketEngine
from core.strategy_v2.types import CandidateSignal, DIRECTION_LONG, DIRECTION_SHORT


def _bc(direction=DIRECTION_LONG, tp1=108.0, sl=97.0, total_score=88.0):
    cand = CandidateSignal(
        strategy_id="long_breakout",
        strategy_name="Long Breakout",
        symbol="TEST",
        asset_class="US_STOCKS",
        direction=direction,
        regime="TREND",
        entry=100.0,
        sl=sl,
        tp1=tp1,
        rr=3.0,
        trailing_rule="none",
        invalidation_condition="none",
        setup_score=80.0,
        catalyst_score=50.0,
        risk_score=60.0,
        validation_score=70.0,
        total_score=total_score,
        confidence=0.7,
    )
    # The selector returns best_candidate = cand.to_dict() — a dict with tp1, NO tp key.
    d = cand.to_dict()
    assert "tp" not in d and "tp1" in d, "fixture must mirror to_dict() shape"
    return d


def test_v2_candidate_reads_tp1_not_tp():
    bc = _bc(direction=DIRECTION_LONG, tp1=108.0, sl=97.0)
    out = MarketEngine._build_v2_candidate_dict(bc, close=100.0, v2_score=88.0)
    # The V2 take-profit (tp1=108) must flow through — NOT 0.
    assert out["tp"] == 108.0
    assert out["sl"] == 97.0
    assert out["direction"] == "LONG"
    assert out["type"] == "BUY"
    assert out["strategy_source"] == "V2"
    assert out["strategy_type"] == "long_breakout"
    assert out["signal_score"] == 88.0
    assert out["validation_score"] == 70.0
    assert out["catalyst_score"] == 50.0


def test_v2_candidate_short_direction_tp_on_correct_side():
    # V2 SHORT reversal: tp1 must be below entry (short TP), and must NOT be 0
    # — otherwise _apply_v2_override skips the TP override, the live signal
    # keeps the V1 LONG tp (above entry), and add_signal drops the SHORT.
    bc = _bc(direction=DIRECTION_SHORT, tp1=92.0, sl=103.0)
    out = MarketEngine._build_v2_candidate_dict(bc, close=100.0, v2_score=85.0)
    assert out["direction"] == "SHORT"
    assert out["type"] == "SHORT"
    assert out["tp"] == 92.0  # below entry, correct side for a short
    assert out["sl"] == 103.0
    assert out["tp"] > 0  # guard: tp must be populated so the override applies


def test_v2_candidate_tp_zero_when_v2_did_not_compute_exit():
    # When V2 genuinely has no tp1 (0), the helper must pass 0 through so the
    # engine's ATR-fallback (if v2_tp <= 0) computes exits. This preserves the
    # intended fallback for missing V2 exits.
    bc = _bc(direction=DIRECTION_LONG, tp1=0.0, sl=0.0)
    out = MarketEngine._build_v2_candidate_dict(bc, close=100.0, v2_score=80.0)
    assert out["tp"] == 0.0
    assert out["sl"] == 0.0


def test_research_candidate_preserves_asset_class_and_factual_evidence():
    bc = _bc(direction=DIRECTION_LONG, tp1=108.0, sl=97.0, total_score=61.0)
    bc["asset_class"] = "COMMODITIES"
    bc["confluence_score"] = 82.0
    bc["meta"] = {
        "research_only": True,
        "capital_execution_eligible": False,
        "capital_execution_block_reason": "Faithful OOS validation is pending",
        "validation_rejection": "sample_too_small:0<30",
        "strategy_evidence": {"market_structure": {"event": "CHOCH"}},
        "confluence": {"score": 82.0, "grade": "A", "coverage": 1.0, "reasons": ["fresh_sweep"]},
    }

    out = MarketEngine._build_v2_candidate_dict(
        bc, close=100.0, v2_score=61.0, research_signal_threshold=50.0
    )

    assert out["asset_class"] == "COMMODITIES"
    assert out["research_only"] is True
    assert out["capital_execution_eligible"] is False
    assert out["autonomous_source"] == "v2_engine_research"
    assert out["signal_threshold"] == 50.0
    assert out["strategy_evidence"]["market_structure"]["event"] == "CHOCH"
    assert out["confluence_score"] == 82.0
    assert out["strategy_evidence"]["confluence"]["grade"] == "A"

    overridden = MarketEngine.__new__(MarketEngine)._apply_v2_override(
        {"reasons": []}, out, v2_tp_pct=2.4, v2_sl_pct=2.0, v2_rr=1.2
    )
    assert overridden["autonomous_source"] == "v2_engine_research"


def test_engine_handoff_preserves_s7_research_source_for_agent_manager():
    """The persisted S7 payload must retain the cohort-routing source.

    ``_apply_v2_override`` adds ``v2_engine_research`` to the in-memory
    candidate. This test covers the later ``_process_signal`` handoff, where a
    dropped field would make the research cohort reject the signal as
    non-autonomous.
    """
    engine = MarketEngine.__new__(MarketEngine)
    engine.config = SimpleNamespace(
        signal={"variant_mode_default": "STRICT", "dynamic_sizing": False, "signal_cooldown_minutes": 0},
        backtest={"enabled": False},
        strategy_v2={"allow_low_sample_fallback": False},
        agents={"risk": {"position_size_pct": 0.5}},
        get_ticker_settings=lambda _ticker: {},
    )
    engine.signals = MagicMock()
    engine.signals.has_open_signal_for_ticker.return_value = None
    engine.signals.can_open_new_signal_for_ticker.return_value = True
    engine.agent_manager = MagicMock()
    engine.quick_backtester = MagicMock()
    engine.last_signal_time = {}
    engine.new_signals = []
    engine._is_ticker_suppressed = MagicMock(return_value=False)

    signal = {
        "type": "BUY",
        "entry": 100.0,
        "tp": 102.4,
        "sl": 98.0,
        "signal_score": 67.2,
        "quality_score": 80.0,
        "variant": "C",
        "mtf_score": 70.0,
        "rsi": 52.0,
        "macd": 0.2,
        "macd_hist": 0.1,
        "risk_mode": "RELAX",
        "signal_mode": "S7_LIQUIDITY_SWEEP_REVERSAL",
        "signal_threshold": 40.0,
        "mtf_trends": {"1h": "BULL", "30m": "BULL", "5m": "BULL"},
        "details": {"mtf": "aligned"},
        "reasons": ["Bullish liquidity sweep reclaimed", "Bullish CHOCH confirmed"],
        "asset_class": "COMMODITIES",
        "strategy_source": "V2",
        "strategy_type": "S7_LIQUIDITY_SWEEP_REVERSAL",
        "research_only": True,
        "autonomous_source": "v2_engine_research",
        "capital_execution_eligible": False,
        "capital_execution_block_reason": "Faithful OOS validation is pending",
        "strategy_evidence": {"market_structure": {"event": "CHOCH"}},
        "atr": 1.0,
    }
    capital = MagicMock()
    capital.get_balance.return_value = 10_000.0

    with patch("core.engine.get_capital_manager", return_value=capital):
        engine._process_signal("GLD", signal, "stock")

    persisted = engine.signals.add_signal.call_args.args[0]
    assert persisted["research_only"] is True
    assert persisted["autonomous_source"] == "v2_engine_research"
    assert persisted["strategy_source"] == "V2"
    assert persisted["strategy_type"] == "S7_LIQUIDITY_SWEEP_REVERSAL"
    engine.agent_manager.on_new_signal.assert_called_once_with(persisted)
