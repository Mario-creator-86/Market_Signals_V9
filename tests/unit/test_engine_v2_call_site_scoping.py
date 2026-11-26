"""
tests/unit/test_engine_v2_call_site_scoping.py
==============================================
Regression guard for the Critical #1 call-site scoping bug in
``core/engine.py::MarketEngine._analyze_ticker``.

THE BUG (pre-fix)
-----------------
``_apply_v2_override`` is called UNCONDITIONALLY at the tail of
``_analyze_ticker`` (~line 2779)::

    result = self._apply_v2_override(result, v2_candidate, v2_tp_pct, v2_sl_pct, v2_rr)
    return result

The V2 exit-level locals (``v2_tp``, ``v2_sl``, ``v2_rr``, ``v2_tp_pct``,
``v2_sl_pct``) were assigned ONLY inside ``if v2_candidate is not None:``.
On every bar where V2 produces no qualifying candidate — i.e. V1 mode, OR
``BOTH_AUTO_BEST`` with no candidate clearing ``min_edge_score`` (the COMMON
case, since V2 validation_score is 0 on a cold performance cache) — those
locals were unbound, so the call raised ``UnboundLocalError`` while Python
evaluated the arguments.

That exception was swallowed by the caller's broad
``except Exception -> return None`` (``_process_stock_ticker`` /
``_process_crypto_ticker``), so EVERY V1-valid signal was silently dropped:
the engine emitted zero / near-zero signals — directly against the standing
directive ("axează-te foarte mult pe semnale … profitabili").

THE FIX
-------
Initialise the locals to neutral defaults right after ``v2_candidate = None``
(~line 2490). ``_apply_v2_override`` early-returns ``result`` unchanged when
``v2_candidate is None``, so the defaults are never used to mutate the signal.

WHY THIS TEST EXISTS
--------------------
``tests/unit/test_v2_override_reactivated.py`` exercises
``_apply_v2_override`` in isolation with explicit ``0.0`` args — it does NOT
catch the call-site scoping bug, because the bug is that the args were never
bound at the call site. No other test calls ``_analyze_ticker`` end-to-end
with ``v2_candidate is None`` — which is exactly why the bug survived.

This test runs the REAL ``_analyze_ticker`` in V1 mode (V2 block skipped →
``v2_candidate`` stays ``None``), with a stubbed-but-valid V1 scoring path
that yields a qualifying ``best_signal``, and asserts the function RETURNS A
DICT (not ``None``, not an exception). Pre-fix this raised ``UnboundLocalError``
→ caller swallowed it → ``None``.
"""

import sys
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

from core.engine import MarketEngine  # noqa: E402


def _stub_score_result(score: float = 82.0, direction: str = "LONG") -> SimpleNamespace:
    """A valid ScoreResult-like object sufficient for the best_signal build."""
    return SimpleNamespace(
        is_valid=True,
        total_score=score,
        direction=direction,
        probability_score=0.72,
        raw_logit_value=0.9,
        mtf_alignment_count=3,
        mtf_alignment_total=5,
        details={"mtf": "3/5", "rsi": "neutral", "vwap": "above"},
        mtf_score=70.0,
        rsi_score=60.0,
        vwap_score=65.0,
        ema_score=68.0,
        macd_score=72.0,
        price_action_score=75.0,
        signal_thesis="stubbed V1 thesis",
        timeframe="intraday",
        expected_holding_period="1-4h",
    )


def _minimal_ohlc_df(n: int = 60) -> pd.DataFrame:
    """An OHLCV df that already carries every column _analyze_ticker reads off
    last_row, so a passthrough _calculate_indicators stub suffices."""
    idx = pd.date_range("2026-07-01 09:30", periods=n, freq="1min")
    base = 100.0
    closes = base + np.arange(n, dtype=float) * 0.01
    df = pd.DataFrame(
        {
            "open": closes - 0.05,
            "high": closes + 0.10,
            "low": closes - 0.10,
            "close": closes,
            "volume": np.full(n, 1000.0, dtype=float),
            "vwap": closes + 0.02,
            "ema_20": closes - 0.01,
            "atr": np.full(n, 0.5, dtype=float),
            "rsi": np.full(n, 55.0, dtype=float),
            "macd": np.full(n, 0.05, dtype=float),
            "macd_signal": np.full(n, 0.02, dtype=float),
            "macd_hist": np.full(n, 0.03, dtype=float),  # >0 -> trend_1m BULL
            "adx": np.full(n, 25.0, dtype=float),
            "vol_sma": np.full(n, 1000.0, dtype=float),
        },
        index=idx,
    )
    return df


def _build_engine() -> MarketEngine:
    """Construct a MarketEngine via __new__ with just enough state for the
    V1 tail-path; V2 block is skipped (signal_mode='V1') so no V2 selector /
    state_store is needed."""
    eng = MarketEngine.__new__(MarketEngine)
    eng.last_signal_indices = {}
    eng.signals = None  # unused in V1 mode (only touched inside V2 block)
    eng._v2_selector = None  # only touched inside V2 block

    config = SimpleNamespace()
    config.bot = {}  # min_edge_score default 75; irrelevant in V1 mode
    config.strategy_v2 = {"signal_mode": "V1"}  # -> V2 block skipped entirely
    config.get_ticker_settings = lambda ticker: {"signals_enabled": True}
    eng.config = config
    return eng


def _full_signal_cfg() -> dict:
    """A cfg dict carrying every key _analyze_ticker reads off `cfg`."""
    return {
        "signals_enabled": True,
        "variant_mode": "STRICT",
        "atr_period": 14,
        "atr_timeframe": "5m",
        "exit_mode": "ATR",
        "risk_mode": "MEDIUM",
        "min_candles_between_signals": 20,
        "enable_regime_gating": True,
        "regime": {"lookback_days": 20},
        "scoring": {},
        "profit_target_pct": 5.0,
        "stop_loss_pct": 2.0,
    }


@pytest.fixture
def _patched_v1_path():
    """Patch every helper the V1 scoring tail-path touches, so a real
    qualifying best_signal is produced and execution reaches the
    UNCONDITIONAL `_apply_v2_override` call at the tail with v2_candidate=None."""
    eng = _build_engine()
    df = _minimal_ohlc_df()
    cfg = _full_signal_cfg()
    score_result = _stub_score_result(82.0, "LONG")

    with patch.object(eng, "_calculate_indicators", side_effect=lambda d: d), \
         patch.object(eng, "_get_mtf_trends", return_value={}), \
         patch.object(eng, "_get_regime_metrics", return_value={}), \
         patch.object(eng, "_compute_regime_strength", return_value=0.6), \
         patch.object(eng, "_get_atr_for_timeframe", return_value=(0.5, "5m")), \
         patch.object(eng, "_resolve_atr_multipliers", return_value=(2.0, 1.0)), \
         patch.object(eng, "_calculate_exit_levels", return_value=(105.0, 98.0)), \
         patch.object(eng, "_compute_rr_ratio", return_value=2.5), \
         patch.object(eng, "_calculate_quality_score", return_value=80.0), \
         patch("core.engine.calculate_atr", return_value=np.full(len(df), 0.5)), \
         patch("core.engine.calculate_distance_in_atr", return_value=0.1), \
         patch("core.engine.detect_market_regime", return_value=("TREND_UP", {})), \
         patch("core.engine.allowed_modes_for_regime", return_value=["PULLBACK", "BREAKOUT", "CONTRA"]), \
         patch("core.engine.compute_signal_score", return_value=score_result), \
         patch("core.data_guard.get_data_guard") as _mock_dg:
        _mock_dg.return_value.is_global_refresh_blocked.return_value = False
        yield eng, df, cfg


class TestAnalyzeTickerV2CallSiteScoping:
    """Critical #1: the unconditional _apply_v2_override call must not raise
    UnboundLocalError when v2_candidate is None (the common V1 / no-candidate
    case). Pre-fix the exception was swallowed by the caller -> every V1-valid
    signal silently dropped."""

    def test_v1_mode_returns_dict_not_none(self, _patched_v1_path):
        eng, df, cfg = _patched_v1_path
        # V1 mode -> V2 block skipped -> v2_candidate stays None at the tail.
        result = eng._analyze_ticker(
            "AAPL", df, is_crypto=False, variant_mode="STRICT", signal_cfg=cfg,
            data_source="test", data_age=0.0,
        )
        # The single most important assertion: a dict is returned, not None.
        # Pre-fix this raised UnboundLocalError -> caller swallowed -> None.
        assert isinstance(result, dict), (
            "V1-mode _analyze_ticker must return a signal dict; pre-fix the "
            "unconditional _apply_v2_override call raised UnboundLocalError "
            "(v2_tp_pct unbound when v2_candidate is None) which the caller "
            "swallowed -> None -> every V1 signal dropped."
        )
        # V1 signal kept its own exit levels: override is a no-op for None
        # v2_candidate, so it must NOT have stamped any V2 key onto the signal.
        assert "strategy_source" not in result
        assert "strategy_type" not in result
        assert result["type"] == "BUY"  # direction LONG -> BUY

    def test_v1_mode_signal_score_carried_through(self, _patched_v1_path):
        eng, df, cfg = _patched_v1_path
        result = eng._analyze_ticker(
            "AAPL", df, is_crypto=False, variant_mode="STRICT", signal_cfg=cfg,
        )
        assert isinstance(result, dict)
        # The stubbed V1 score (82) must reach the returned signal unchanged
        # (override did not mutate it).
        assert result["signal_score"] == 82.0
        # Variant A/B/C banding: 82 -> "B" (>=70, <85).
        assert result["variant"] == "B"

    def test_no_unbound_local_when_v2_block_skipped(self, _patched_v1_path):
        """Explicit crash-freedom: calling _analyze_ticker in V1 mode must not
        raise ANY exception (pre-fix: UnboundLocalError on v2_tp_pct)."""
        eng, df, cfg = _patched_v1_path
        try:
            out = eng._analyze_ticker(
                "AAPL", df, is_crypto=False, variant_mode="STRICT", signal_cfg=cfg,
            )
        except UnboundLocalError as exc:
            pytest.fail(f"UnboundLocalError re-introduced at V2 override call site: {exc}")
        except Exception as exc:  # any other raise is also a regression here
            pytest.fail(f"V1-mode _analyze_ticker raised unexpectedly: {type(exc).__name__}: {exc}")
        assert isinstance(out, dict)

    def test_v2_mode_does_not_fall_back_to_unvalidated_v1_signal(self, _patched_v1_path):
        eng, df, cfg = _patched_v1_path
        eng.config.strategy_v2 = {"signal_mode": "V2"}
        with patch("core.strategy_v2.selector.StrategySelector.select", return_value={"best_candidate": None}):
            result = eng._analyze_ticker(
                "AAPL", df, is_crypto=False, variant_mode="STRICT", signal_cfg=cfg,
                data_source="test", data_age=0.0,
            )

        assert result is None

    def test_v1_signal_carries_news_for_ai_decision_layer(self, _patched_v1_path):
        """High-finding (e): the emitted signal must carry news_* so the AI
        decision layer (ollama_decision._compact_signal -> review prompt) can
        'use news_headlines/news_sentiment as part of the decision'. Pre-fix
        the news pulse was fetched ONLY inside the V2 block and never attached
        to `result`, so V1 signals (and the AI) were news-blind."""
        eng, df, cfg = _patched_v1_path
        fake_pulse = {
            "score": 72,
            "sentiment_trend": "bullish",
            "top_headlines": [
                {"title": "AAPL beats on revenue", "impact": "high", "impact_score": 0.9},
                {"title": "Guidance raised", "impact": "medium", "impact_score": 0.6},
            ],
        }
        with patch("core.news_pulse.build_news_pulse", return_value=fake_pulse):
            result = eng._analyze_ticker(
                "AAPL", df, is_crypto=False, variant_mode="STRICT", signal_cfg=cfg,
            )
        assert isinstance(result, dict)
        # News fields attached from the pulse for the AI consumer.
        assert result["news_sentiment"] == "bullish"
        assert result["news_score"] == 72.0
        assert "AAPL beats on revenue" in result["news_headlines"]
        # _compact_signal whitelist key (rich headline objects with impact).
        assert result["top_headlines"][0]["title"] == "AAPL beats on revenue"

    def test_v1_signal_carries_neutral_news_when_pulse_unavailable(self, _patched_v1_path):
        """When the news pulse is unavailable (agent off / error), the signal
        must still carry neutral/zero news fields (not crash, not be news-blind
        with missing keys that break downstream consumers)."""
        eng, df, cfg = _patched_v1_path
        with patch("core.news_pulse.build_news_pulse", side_effect=RuntimeError("no news agent")):
            result = eng._analyze_ticker(
                "AAPL", df, is_crypto=False, variant_mode="STRICT", signal_cfg=cfg,
            )
        assert isinstance(result, dict)
        assert result["news_score"] == 0.0
        assert result["news_sentiment"] in ("neutral", "unknown")
        assert result["news_headlines"] == []
