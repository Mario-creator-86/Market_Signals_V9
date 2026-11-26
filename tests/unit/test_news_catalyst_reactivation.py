"""Regression: AI/news -> V2 catalyst reactivation (Pasul 1).

Bug: ``MarketEngine`` built the V2 ``watch_row`` with only price/change_pct/volume
(engine.py ~2489). The snapshot path read ``watchlist_snapshot`` from state_store,
a key that is NEVER written, so the news fields never reached ``watch_row``.
``context_builder.build_context`` reads ``news_sentiment_trend`` /
``news_pulse_score`` from ``watch_row`` (defaulting to neutral/0), so
``evaluators._news_catalyst_score`` always scored 0 -> ``catalyst_score`` was
permanently 0, and 15% of the V2 ``total_score`` (selector.py:61) was inert. The
ollama_decision news whitelist (news_score/news_sentiment) likewise never fired.

Fix: engine now calls ``build_news_pulse(ticker, allow_blocking=False)`` and
populates ``watch_row["news_sentiment_trend"]`` + ``watch_row["news_pulse_score"]``
before ``v2_build_context``. This test exercises the two reused primitives
(``build_context`` propagation + ``_news_catalyst_score`` sensitivity) so the
wiring stays correct without a live engine loop.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def test_build_context_propagates_news_fields():
    """build_context must copy news_sentiment_trend/news_pulse_score from watch_row
    into the V2 context dict that evaluators read (context.get('news_sentiment'))."""
    from core.strategy_v2.context_builder import build_context

    watch_row = {
        "price": 100.0,
        "change_pct": 1.2,
        "volume": 1_000_000.0,
        "news_sentiment_trend": "bullish",
        "news_pulse_score": 72.0,
    }
    ctx = build_context(symbol="AAPL", asset_class="US_STOCKS",
                        watch_row=watch_row, ohlc_df=None, row_context=None)

    assert ctx["news_sentiment"] == "bullish"
    assert ctx["news_pulse_score"] == pytest.approx(72.0)
    # Neutral default when missing -> catalyst stays baseline (sanity).
    ctx_neutral = build_context(symbol="AAPL", asset_class="US_STOCKS",
                                watch_row={"price": 100.0}, ohlc_df=None, row_context=None)
    assert ctx_neutral["news_sentiment"] == "neutral"
    assert ctx_neutral["news_pulse_score"] == 0.0


def test_news_catalyst_score_responds_to_sentiment():
    """A aligned (bullish+LONG) catalyst with a real news score must exceed the
    neutral baseline. Pre-fix this delta was 0 because the news score was never
    wired; post-fix the 15% V2 weight actually moves total_score."""
    from core.strategy_v2.evaluators import _news_catalyst_score

    aligned = _news_catalyst_score("bullish", "LONG", news_score=70.0)
    neutral = _news_catalyst_score("neutral", "LONG", news_score=0.0)

    assert aligned > neutral
    # Aligned formula: 55 + 70*0.5 = 90 (clamped to 100).
    assert aligned == pytest.approx(90.0, abs=0.5)
    # Neutral formula: 35 + 0*0.3 = 35.
    assert neutral == pytest.approx(35.0, abs=0.5)
    # The delta (55 pts) * 15% selector weight = up to ~8.25 pts on total_score V2,
    # enough to cross the BOTH_AUTO_BEST >=80 gate where it previously blocked.
    assert (aligned - neutral) * 0.15 > 5.0


def test_engine_watch_row_population_via_stub(monkeypatch):
    """Simulate the engine injection: patch build_news_pulse and confirm the
    watch_row that would be handed to v2_build_context carries the news fields.
    This is the exact sequence engine.py now executes (no network, no engine loop)."""
    import core.news_pulse as np

    def fake_pulse(ticker, ttl_seconds=45, max_items=180, allow_blocking=False):
        return {"score": 64, "sentiment_trend": "bearish"}

    monkeypatch.setattr(np, "build_news_pulse", fake_pulse)

    pulse = np.build_news_pulse("SOL-USD", allow_blocking=False)
    watch_row = {"price": 50.0, "change_pct": 0.0, "volume": 123.0}
    # Mirror engine.py injection block:
    if isinstance(pulse, dict):
        watch_row["news_sentiment_trend"] = str(pulse.get("sentiment_trend") or "neutral").lower()
        watch_row["news_pulse_score"] = float(pulse.get("score") or 0.0)

    from core.strategy_v2.context_builder import build_context
    ctx = build_context(symbol="SOL-USD", asset_class="CRYPTO_FUTURES",
                        watch_row=watch_row, ohlc_df=None, row_context=None)
    assert ctx["news_sentiment"] == "bearish"
    assert ctx["news_pulse_score"] == pytest.approx(64.0)


def test_build_context_normalises_unknown_news_to_neutral():
    """build_news_pulse returns sentiment_trend="unknown" on every error/empty
    path. Without normalisation, "unknown" falls through _news_catalyst_score's
    three explicit branches into the counter-sentiment branch (scored 30 at
    news_score=0, BELOW the neutral score of 35) — penalising V2's catalyst
    when news is simply unavailable. build_context must map any non-
    {bullish,bearish,neutral} value (including "unknown", "", junk) to neutral
    so unavailable news is treated as no-information, not counter-sentiment.
    """
    from core.strategy_v2.context_builder import build_context
    from core.strategy_v2.evaluators import _news_catalyst_score

    for raw in ("unknown", "UNKNOWN", "", "  ", "n/a", "bogus"):
        ctx = build_context(symbol="AAPL", asset_class="US_STOCKS",
                            watch_row={"price": 100.0, "news_sentiment_trend": raw},
                            ohlc_df=None, row_context=None)
        assert ctx["news_sentiment"] == "neutral", f"raw={raw!r} leaked into context"

    # The normalised neutral feeds the neutral branch of _news_catalyst_score
    # (35 at news_score=0), NOT the counter-sentiment branch (30).
    assert _news_catalyst_score("neutral", "LONG", news_score=0.0) == pytest.approx(35.0, abs=0.5)
    assert _news_catalyst_score("neutral", "LONG", news_score=0.0) > _news_catalyst_score("bogus", "LONG", news_score=0.0)