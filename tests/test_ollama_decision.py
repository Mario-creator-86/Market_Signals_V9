from core.ollama_decision import (
    parse_exit_review,
    parse_trade_review,
    should_veto_trade,
    _compact_signal,
    build_trade_review_prompt,
)


def test_parse_trade_review_validates_and_clamps_values():
    review = parse_trade_review(
        """
        {
          "recommendation": "reduce",
          "confidence": 1.7,
          "size_factor": -0.2,
          "leverage": 9,
          "sentiment": "bearish",
          "supports_direction": false,
          "reason": "Conflicting sentiment.",
          "risks": ["news conflict", "thin liquidity"]
        }
        """
    )

    assert review["available"] is True
    assert review["recommendation"] == "reduce"
    assert review["confidence"] == 1.0
    assert review["size_factor"] == 0.0
    assert review["leverage"] == 5
    assert review["sentiment"] == "bearish"
    assert review["supports_direction"] is False
    assert review["risks"] == ["news conflict", "thin liquidity"]


def test_parse_trade_review_invalid_json_falls_back_to_watch():
    review = parse_trade_review("not json")

    assert review["available"] is False
    assert review["recommendation"] == "watch"
    assert review["fallback_used"] == "invalid_json"


def test_should_veto_trade_requires_avoid_and_confidence_threshold():
    cfg = {"ollama": {"decision_veto_enabled": True, "decision_min_confidence": 0.65}}

    assert should_veto_trade({"available": True, "recommendation": "avoid", "confidence": 0.7}, cfg)
    assert not should_veto_trade({"available": True, "recommendation": "avoid", "confidence": 0.4}, cfg)
    assert not should_veto_trade({"available": True, "recommendation": "reduce", "confidence": 0.9}, cfg)
    assert not should_veto_trade({"available": False, "recommendation": "avoid", "confidence": 1.0}, cfg)


def test_parse_exit_review_validates_action_and_stop():
    review = parse_exit_review(
        """
        {
          "action": "tighten_stop",
          "confidence": 0.91,
          "new_stop": 101.25,
          "reason": "Protect profit.",
          "risks": ["headline risk"]
        }
        """
    )

    assert review["available"] is True
    assert review["action"] == "tighten_stop"
    assert review["confidence"] == 0.91
    assert review["new_stop"] == 101.25
    assert review["risks"] == ["headline risk"]


# ─────────────────────────────────────────────────────────────────────────────
# High-finding (e): news context must reach the AI decision layer
# ─────────────────────────────────────────────────────────────────────────────
# engine._analyze_ticker now attaches news_score/news_sentiment/news_headlines/
# top_headlines to the emitted signal. _compact_signal whitelists exactly those
# keys, and build_trade_review_prompt instructs the model to "use
# news_headlines/news_sentiment as part of the decision when present". These
# tests guard the consumer side: a news-bearing signal must propagate news into
# the compacted payload AND the rendered prompt (so the AI actually sees news).


def test_compact_signal_propagates_news_fields_when_present():
    """A signal carrying news_* must keep them after _compact_signal
    (the whitelist must not drop them)."""
    signal = {
        "ticker": "AAPL",
        "type": "BUY",
        "entry": 100.0,
        "tp": 105.0,
        "sl": 98.0,
        "signal_score": 82,
        "news_score": 72.0,
        "news_sentiment": "bullish",
        "news_headlines": ["AAPL beats on revenue", "Guidance raised"],
        "top_headlines": [
            {"title": "AAPL beats on revenue", "impact": "high", "impact_score": 0.9}
        ],
    }
    compact = _compact_signal(signal)
    assert compact["news_score"] == 72.0
    assert compact["news_sentiment"] == "bullish"
    assert compact["news_headlines"] == ["AAPL beats on revenue", "Guidance raised"]
    assert compact["top_headlines"][0]["title"] == "AAPL beats on revenue"


def test_compact_signal_drops_news_when_absent():
    """A signal with NO news must not synthesize fake news keys (absence
    passes through as absence — _compact_signal only keeps present keys)."""
    compact = _compact_signal({"ticker": "AAPL", "type": "BUY", "signal_score": 80})
    for k in ("news_score", "news_sentiment", "news_headlines", "top_headlines"):
        assert k not in compact


def test_trade_review_prompt_includes_news_when_present():
    """The rendered prompt must contain the news context so the AI model can
    'use news_headlines/news_sentiment as part of the decision' per the
    prompt's own instruction. Pre-fix the engine never emitted news -> the
    prompt never carried it -> the AI decided news-blind."""
    signal = {
        "ticker": "MSFT",
        "type": "BUY",
        "signal_score": 84,
        "news_score": 68.0,
        "news_sentiment": "bullish",
        "news_headlines": ["Microsoft raises cloud guidance"],
    }
    prompt = build_trade_review_prompt(signal, risk_evaluation={"max_size_pct": 5.0})
    assert "Microsoft raises cloud guidance" in prompt
    assert "bullish" in prompt


# A4/A5: decision-relevant context the engine computes but pre-fix never reached
# the AI reviewer (directive: "toate informațiile valabile pentru a lua cea mai
# bună decizie"). _compact_signal whitelists rr_ratio/risk_mode (R:R + risk band),
# regime/MTF context (trend alignment), 30-day realized perf (recent edge), and
# V2 sub-scores (setup/catalyst/risk/validation). If the whitelist regresses, the
# model reverts to deciding news-blind on momentum/regime/history -- directly
# contra the standing AI-integration directive.
_A4_A5_KEYS = [
    "rr_ratio",
    "risk_mode",
    "market_regime",
    "regime_detected",
    "regime_strength",
    "regime_details",
    "mtf_alignment",
    "mtf_score",
    "mtf_trends",
    "adx_5m",
    "bb_width_5m",
    "atr_percentile_5m",
    "win_rate_30d",
    "expectancy_R_30d",
    "profit_factor_30d",
    "trades_30d",
    "quick_backtest_30d",
    "catalyst_score",
    "setup_score",
    "risk_score",
    "validation_score",
]


def test_compact_signal_propagates_a4_a5_decision_context():
    """A4/A5: every decision-relevant key the engine attaches must survive
    _compact_signal so the AI reviewer sees R:R, regime/MTF alignment, 30d
    performance, and V2 sub-scores -- not just news+basics."""
    signal = {
        "ticker": "AAPL",
        "type": "BUY",
        "signal_score": 82,
        "rr_ratio": 2.1,
        "risk_mode": "STRONG",
        "market_regime": "trending",
        "regime_detected": True,
        "regime_strength": 0.72,
        "regime_details": {"trend": "up", "volatility": "low"},
        "mtf_alignment": "aligned",
        "mtf_score": 0.8,
        "mtf_trends": {"5m": "up", "15m": "up", "1h": "up"},
        "adx_5m": 28.5,
        "bb_width_5m": 0.04,
        "atr_percentile_5m": 35.0,
        "win_rate_30d": 58.0,
        "expectancy_R_30d": 0.42,
        "profit_factor_30d": 1.6,
        "trades_30d": 22,
        "quick_backtest_30d": {"net_pnl": 340.0, "win_rate": 58.0},
        "catalyst_score": 70.0,
        "setup_score": 80.0,
        "risk_score": 65.0,
        "validation_score": 75.0,
    }
    compact = _compact_signal(signal)
    for key in _A4_A5_KEYS:
        assert key in compact, f"_compact_signal dropped decision-relevant key: {key}"
    # Spot-check values pass through unmodified (not just key presence).
    assert compact["rr_ratio"] == 2.1
    assert compact["risk_mode"] == "STRONG"
    assert compact["market_regime"] == "trending"
    assert compact["win_rate_30d"] == 58.0
    assert compact["catalyst_score"] == 70.0


def test_compact_signal_drops_a4_a5_context_when_absent():
    """When the producer attaches no regime/MTF/perf context, _compact_signal
    must not synthesize it (the model decides only on what is actually present)."""
    compact = _compact_signal({"ticker": "AAPL", "type": "BUY", "signal_score": 80})
    for key in _A4_A5_KEYS:
        assert key not in compact


def test_trade_review_prompt_includes_a4_a5_context_when_present():
    """The rendered prompt must carry regime/MTF/30d-perf so the model can weigh
    trend alignment and recent realized edge, per the AI-integration directive."""
    signal = {
        "ticker": "AAPL",
        "type": "BUY",
        "signal_score": 82,
        "rr_ratio": 2.1,
        "risk_mode": "STRONG",
        "market_regime": "trending",
        "win_rate_30d": 58.0,
        "catalyst_score": 70.0,
    }
    prompt = build_trade_review_prompt(signal, risk_evaluation={})
    assert "trending" in prompt
    assert "STRONG" in prompt
    assert "58.0" in prompt


# Round-3 AI-3: additional decision-relevant engine fields must survive the
# _compact_signal whitelist so the AI reviewer sees volatility/momentum/
# extension/liquidity/thesis, not just news+regime+score.
_AI3_KEYS = [
    "atr",
    "atr_value",
    "avg_atr_20d",
    "rsi",
    "macd",
    "macd_hist",
    "dist_vwap_atr",
    "dist_ema_atr",
    "spread_estimate",
    "tp_pct",
    "sl_pct",
    "signal_thesis",
    "signal_threshold",
    "strict_disagreement",
]


def test_compact_signal_propagates_ai3_engine_fields():
    """AI-3: atr/rsi/macd_hist/dist_vwap_atr/spread_estimate/tp_pct/sl_pct/
    signal_thesis/signal_threshold/strict_disagreement must reach the AI reviewer
    (directive: toate informațiile valabile). Pre-fix the model was asked to
    judge liquidity/extension/MTF alignment but denied the fields quantifying them."""
    signal = {
        "ticker": "AAPL", "type": "BUY", "signal_score": 82,
        "atr": 1.45, "atr_value": 1.45, "avg_atr_20d": 1.30,
        "rsi": 68.0, "macd": 0.4, "macd_hist": 0.12,
        "dist_vwap_atr": 2.1, "dist_ema_atr": 1.6, "spread_estimate": 0.0008,
        "tp_pct": 4.0, "sl_pct": 2.0,
        "signal_thesis": "Breakout above prior swing high on volume.",
        "signal_threshold": 78.0, "strict_disagreement": False,
    }
    compact = _compact_signal(signal)
    for key in _AI3_KEYS:
        assert key in compact, f"_compact_signal dropped engine field: {key}"
    assert compact["signal_thesis"] == "Breakout above prior swing high on volume."
    assert compact["strict_disagreement"] is False


def test_compact_signal_drops_ai3_fields_when_absent():
    compact = _compact_signal({"ticker": "AAPL", "type": "BUY", "signal_score": 80})
    for key in _AI3_KEYS:
        assert key not in compact


# Round-3 AI-1: the EXIT review must carry fresh news context so the model can
# judge whether the entry catalyst has flipped. Pre-fix _compact_position had no
# news keys and evaluate_position_exit_with_ollama never fetched news.
def test_compact_position_propagates_news_fields_when_present():
    """AI-1: a position carrying news_* keeps them after _compact_position so the
    exit co-pilot sees the current catalyst, not a news-blind position."""
    from core.ollama_decision import _compact_position
    position = {
        "position_id": "p1", "ticker": "AAPL", "direction": "long",
        "entry_price": 100.0, "qty": 10.0, "usd_size": 1000.0, "margin_used": 1000.0,
        "leverage": 1, "stop_loss_price": 98.0, "take_profit_price": 105.0,
        "news_score": 72.0, "news_sentiment": "bearish", "news_sentiment_trend": "bearish",
        "news_headlines": ["AAPL faces regulatory probe"],
        "top_headlines": ["AAPL faces regulatory probe"],
    }
    compact = _compact_position(position, 102.0)
    assert compact["news_score"] == 72.0
    assert compact["news_sentiment"] == "bearish"
    assert compact["news_headlines"] == ["AAPL faces regulatory probe"]
    assert compact["top_headlines"] == ["AAPL faces regulatory probe"]


def test_compact_position_drops_news_when_absent():
    """AI-1: a position with no news must not synthesize fake news keys."""
    from core.ollama_decision import _compact_position
    compact = _compact_position(
        {"position_id": "p1", "ticker": "AAPL", "direction": "long"}, 100.0
    )
    for k in ("news_score", "news_sentiment", "news_sentiment_trend",
              "news_headlines", "top_headlines"):
        assert k not in compact


def test_compact_position_propagates_post_earnings_and_flow_evidence():
    from core.ollama_decision import _compact_position

    compact = _compact_position(
        {
            "ticker": "AAPL",
            "direction": "LONG",
            "post_earnings_review": {"reason": "pre_earnings_post_event_ai_review"},
            "flow_evidence": {
                "alignment": "CONFIRM",
                "summary": {"relative_volume": 2.1, "score": 42.0, "confidence": 0.7},
            },
        },
        103.0,
    )

    assert compact["post_earnings_review"]["reason"] == "pre_earnings_post_event_ai_review"
    assert compact["flow_evidence"]["alignment"] == "CONFIRM"
    assert compact["flow_evidence"]["summary"]["relative_volume"] == 2.1


def test_exit_review_prompt_includes_news_when_present():
    """AI-1: the exit prompt must carry news context + the new instruction telling
    the model to treat a flipped sentiment as a close/tighten signal."""
    from core.ollama_decision import build_exit_review_prompt
    position = {
        "position_id": "p1", "ticker": "AAPL", "direction": "long",
        "entry_price": 100.0,
        "news_score": 80.0, "news_sentiment": "bearish",
        "news_headlines": ["AAPL faces regulatory probe"],
    }
    prompt = build_exit_review_prompt(position, 102.0)
    assert "AAPL faces regulatory probe" in prompt
    assert "bearish" in prompt
    # The new instruction tying flipped sentiment to close/tighten.
    assert "flipped against the trade direction" in prompt


def test_evaluate_position_exit_fetches_fresh_news_pulse(monkeypatch):
    """AI-1 end-to-end: evaluate_position_exit_with_ollama must fetch a fresh
    build_news_pulse for the position's ticker and inject news_score/news_sentiment/
    top_headlines into the prompt (so the exit co-pilot sees CURRENT news, not the
    stale/empty context persisted at entry). Uses a fake ollama + a fake news pulse."""
    from core import ollama_decision as od

    captured_prompt = {}

    class _FakeOllama:
        @staticmethod
        def check_availability():
            return True

        @staticmethod
        def is_cloud_host():
            return False

        @staticmethod
        def get_preferred_model():
            return "fake-model"

        @staticmethod
        def generate(prompt, model=None, system="", ui_call=False, timeout_override=None):
            captured_prompt["prompt"] = prompt
            captured_prompt["timeout_override"] = timeout_override
            return {"response": '{"action":"hold","confidence":0.6,"new_stop":null,'
                               '"reason":"news still aligned","risks":[]}'}

    monkeypatch.setattr(od, "_check_availability_cached", lambda inst: True)

    def _fake_news_pulse(ticker, allow_blocking=False):
        return {
            "score": 77.0,
            "sentiment_trend": "bearish",
            "top_headlines": [
                {"title": "AAPL sued by regulators", "impact": "high", "impact_score": 0.9},
                {"title": "Analyst downgrades AAPL", "impact": "medium", "impact_score": 0.6},
            ],
        }

    position = {
        "position_id": "p1", "ticker": "AAPL", "direction": "long",
        "entry_price": 100.0, "qty": 10.0, "usd_size": 1000.0, "margin_used": 1000.0,
        "leverage": 1, "stop_loss_price": 98.0, "take_profit_price": 105.0,
        "post_earnings_review": {"reason": "pre_earnings_post_event_ai_review"},
    }
    config = {"ollama": {"exit_decision_enabled": True, "exit_decision_timeout_seconds": 30.0}}
    with monkeypatch.context() as m:
        m.setitem(__import__("sys").modules, "core.ollama_client", type("M", (), {"ollama": _FakeOllama()}))
        m.setattr("core.news_pulse.build_news_pulse", _fake_news_pulse, raising=False)
        review = od.evaluate_position_exit_with_ollama(position, 102.0, config=config)

    prompt = captured_prompt.get("prompt", "")
    assert "AAPL sued by regulators" in prompt, "fresh news headline must reach the exit prompt"
    assert "bearish" in prompt, "fresh sentiment must reach the exit prompt"
    assert "77" in prompt, "fresh news_score must reach the exit prompt"
    assert captured_prompt["timeout_override"] == 90
    assert review["available"] is True
    assert review["action"] == "hold"


def test_evaluate_position_exit_news_failure_is_non_fatal(monkeypatch):
    """AI-1: if build_news_pulse raises, the exit review must still proceed
    news-blind (degrade gracefully) rather than fail the whole review."""
    from core import ollama_decision as od

    class _FakeOllama:
        @staticmethod
        def check_availability():
            return True

        @staticmethod
        def is_cloud_host():
            return False

        @staticmethod
        def get_preferred_model():
            return "fake-model"

        @staticmethod
        def generate(prompt, model=None, system="", ui_call=False, timeout_override=None):
            return {"response": '{"action":"hold","confidence":0.5,"new_stop":null,'
                               '"reason":"ok","risks":[]}'}

    monkeypatch.setattr(od, "_check_availability_cached", lambda inst: True)

    def _raising_news_pulse(ticker, allow_blocking=False):
        raise RuntimeError("news provider down")

    position = {
        "position_id": "p1", "ticker": "AAPL", "direction": "long",
        "entry_price": 100.0, "qty": 10.0, "usd_size": 1000.0, "margin_used": 1000.0,
        "leverage": 1, "stop_loss_price": 98.0, "take_profit_price": 105.0,
    }
    config = {"ollama": {"exit_decision_enabled": True}}
    with monkeypatch.context() as m:
        m.setitem(__import__("sys").modules, "core.ollama_client", type("M", (), {"ollama": _FakeOllama()}))
        m.setattr("core.news_pulse.build_news_pulse", _raising_news_pulse, raising=False)
        review = od.evaluate_position_exit_with_ollama(position, 102.0, config=config)
    # News failed but the review still completed (graceful degradation).
    assert review["available"] is True
    assert review["action"] == "hold"


def test_exit_close_guard_rejects_mild_bearish_news_without_price_invalidation():
    from core.ollama_decision import evaluate_exit_close_guard

    guard = evaluate_exit_close_guard(
        {
            "action": "close",
            "thesis_invalidated": True,
            "news_exit_context": {"severity": "low", "evidence_quality": "verified"},
        },
        {"direction": "LONG", "entry_price": 100.0, "stop_loss_price": 95.0},
        99.5,
        config={"ollama": {"exit_close_technical_risk_fraction": 0.75}},
    )

    assert guard["allowed"] is False
    assert guard["news_severity"] == "low"
    assert guard["risk_fraction"] == 0.1


def test_exit_close_guard_allows_high_verified_thesis_invalidation_or_material_price_risk():
    from core.ollama_decision import evaluate_exit_close_guard

    high_news = evaluate_exit_close_guard(
        {
            "action": "close",
            "thesis_invalidated": True,
            "news_exit_context": {"severity": "high", "evidence_quality": "verified"},
        },
        {"direction": "LONG", "entry_price": 100.0, "stop_loss_price": 95.0},
        99.5,
        config={"ollama": {"exit_close_technical_risk_fraction": 0.75}},
    )
    near_stop = evaluate_exit_close_guard(
        {"action": "close", "thesis_invalidated": False, "news_exit_context": {}},
        {"direction": "LONG", "entry_price": 100.0, "stop_loss_price": 95.0},
        96.0,
        config={"ollama": {"exit_close_technical_risk_fraction": 0.75}},
    )

    assert high_news["allowed"] is True
    assert "High-severity" in high_news["reason"]
    assert near_stop["allowed"] is True
    assert near_stop["risk_fraction"] == 0.8


def test_post_earnings_close_guard_rejects_favorable_flow_without_invalidation():
    from core.ollama_decision import evaluate_exit_close_guard

    guard = evaluate_exit_close_guard(
        {
            "action": "close",
            "thesis_invalidated": True,
            "post_earnings_review": {"reason": "pre_earnings_post_event_ai_review"},
            "news_exit_context": {
                "severity": "low",
                "evidence_quality": "verified",
                "sentiment": "bullish",
            },
            "flow_evidence": {
                "alignment": "CONFIRM",
                "summary": {"score": 48.0, "confidence": 0.8, "relative_volume": 2.4},
            },
        },
        {"direction": "LONG", "entry_price": 100.0, "stop_loss_price": 95.0},
        103.0,
    )

    assert guard["allowed"] is False
    assert guard["flow_alignment"] == "CONFIRM"


def test_post_earnings_close_guard_allows_adverse_flow_with_material_volume():
    from core.ollama_decision import evaluate_exit_close_guard

    guard = evaluate_exit_close_guard(
        {
            "action": "close",
            "thesis_invalidated": True,
            "post_earnings_review": {"reason": "pre_earnings_post_event_ai_review"},
            "news_exit_context": {
                "severity": "moderate",
                "evidence_quality": "source_backed",
                "sentiment": "bearish",
            },
            "flow_evidence": {
                "alignment": "CONTRADICT",
                "summary": {"score": -52.0, "confidence": 0.72, "relative_volume": 2.3},
            },
        },
        {"direction": "LONG", "entry_price": 100.0, "stop_loss_price": 95.0},
        101.0,
    )

    assert guard["allowed"] is True
    assert guard["flow_alignment"] == "CONTRADICT"
    assert guard["relative_volume"] == 2.3
