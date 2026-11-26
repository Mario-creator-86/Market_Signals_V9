from core.trade_decision import (
    build_manual_decision_context,
    build_signal_decision_context,
    summarize_position_explainability,
)


def test_build_signal_decision_context_keeps_explainability_fields():
    ctx = build_signal_decision_context(
        {
            "id": "sig-1",
            "ticker": "AAPL",
            "action": "BUY",
            "entry": 100.0,
            "tp": 110.0,
            "sl": 95.0,
            "signal_score": 84.0,
            "probability_score": 0.79,
            "signal_mode": "PULLBACK",
            "decision_reasons": ["Trend alignment", "Clean pullback"],
        },
        strategy_type="PULLBACK",
    )

    assert ctx["ticker"] == "AAPL"
    assert ctx["reason"] == "Trend alignment"
    assert ctx["strategy_type"] == "PULLBACK"
    assert ctx["risk"]["risk_reward_ratio"] == 2.0
    assert ctx["scoring"]["signal_score"] == 84.0


def test_build_manual_decision_context_includes_risk_levels():
    ctx = build_manual_decision_context(
        ticker="BTC-USD",
        direction="long",
        entry_price=100.0,
        take_profit_price=108.0,
        stop_loss_price=96.0,
        usd_size=1000.0,
        leverage=2,
        reason="Operator setup",
    )

    assert ctx["decision_source"] == "manual"
    assert ctx["reason"] == "Operator setup"
    assert ctx["risk"]["stop"] == 96.0
    assert ctx["risk"]["target"] == 108.0


def test_summarize_position_explainability_recovers_manual_context():
    explain = summarize_position_explainability(
        {
            "ticker": "MSFT",
            "direction": "LONG",
            "entry_price": 100.0,
            "take_profit_price": 105.0,
            "stop_loss_price": 98.0,
            "usd_size": 500.0,
            "entry_reason": "Recovered",
            "strategy_type": "RECOVERED",
        }
    )

    assert explain["strategy_type"] == "RECOVERED"
    assert explain["reason"] == "Recovered"
    assert explain["risk"]["target"] == 105.0


def test_summarize_position_explainability_surfaces_ai_decision_logic():
    ctx = build_signal_decision_context(
        {
            "id": "sig-ai",
            "ticker": "SOL-USD",
            "action": "SHORT",
            "entry": 82.83,
            "tp": 78.68,
            "sl": 84.48,
            "signal_score": 82.0,
            "probability_score": 0.92,
            "signal_threshold": 80.0,
            "decision_reasons": ["Short setup cleared momentum gate"],
        },
        risk_evaluation={
            "accepted": True,
            "reason": "passed",
            "checks": [{"check": "min_risk_reward_ratio", "passed": True, "detail": "2.50 >= 1.20"}],
        },
    )
    ctx["ollama"] = {
        "available": True,
        "provider": "ollama_cloud",
        "model": "gpt-oss:120b-cloud",
        "recommendation": "approve",
        "confidence": 0.86,
        "sentiment": "neutral",
        "supports_direction": True,
        "reason": "AI approved because score, risk reward, and exposure were aligned.",
        "risks": ["Crypto volatility can invalidate the setup quickly."],
    }

    explain = summarize_position_explainability(
        {
            "ticker": "SOL-USD",
            "direction": "SHORT",
            "entry_price": 82.76,
            "take_profit_price": 78.68,
            "stop_loss_price": 84.48,
            "usd_size": 82.0,
            "status": "OPEN",
            "decision_context": ctx,
        }
    )

    assert "AI approved" in explain["ai_decision_story"]
    assert explain["ai_review"]["model"] == "gpt-oss:120b-cloud"
    assert any(item["label"] == "AI review" for item in explain["decision_factors"])
    assert explain["expectations"]
    assert explain["trade_plan"]
    assert any(item["source"] == "AI risk" for item in explain["ai_warnings"])


def test_summarize_position_explainability_surfaces_source_backed_news_evidence():
    ctx = build_signal_decision_context(
        {"id": "sig-news", "ticker": "BTC-USD", "action": "BUY", "entry": 100, "tp": 104, "sl": 98},
    )
    ctx["autonomous_news_gate"] = {
        "allowed": True,
        "status": "aligned",
        "reason": "Fresh source-backed news context has no directional conflict",
        "evaluated_at": "2026-07-14T10:00:00+00:00",
        "context": {
            "ai_sentiment": "bullish",
            "source_backed_count_24h": 1,
            "ai_analyzed_count_24h": 1,
            "top_headlines": [{
                "title": "Source-backed catalyst",
                "url": "https://example.test/btc",
                "source": "Example News",
                "time": "2026-07-14T09:00:00+00:00",
                "ai_micro_summary": {"overall_sentiment": "bullish", "tldr": "Positive catalyst"},
            }],
        },
    }

    explain = summarize_position_explainability({"decision_context": ctx})

    assert explain["news_evidence"]["headlines"][0]["url"] == "https://example.test/btc"
    assert explain["news_evidence"]["ai_sentiment"] == "bullish"
    assert any(item["label"] == "News evidence" for item in explain["decision_factors"])


def test_s7_strategy_evidence_is_preserved_in_capital_explanation():
    ctx = build_signal_decision_context(
        {
            "id": "sig-s7",
            "ticker": "BTCUSDT",
            "action": "BUY",
            "entry": 100.0,
            "tp": 110.0,
            "sl": 95.0,
            "signal_score": 58.0,
            "signal_threshold": 50.0,
            "research_only": True,
            "capital_execution_eligible": False,
            "capital_execution_block_reason": "Faithful OOS validation is pending",
            "strategy_type": "S7_LIQUIDITY_SWEEP_REVERSAL",
            "strategy_evidence": {
                "strategy_id": "S7_LIQUIDITY_SWEEP_REVERSAL",
                "strategy_name": "Liquidity Sweep + Structure Reversal",
                "sequence": [
                    {"detail": "Bullish liquidity sweep reclaimed at 96.00000000; confirmation age 1 bar(s)."},
                    {"detail": "Bullish CHOCH confirmed after the sweep."},
                    {"detail": "Volume ratio 2.00x versus the rolling average."},
                ],
                "liquidity": {"event": "SWEEP_RECLAIM", "direction": "BULLISH", "sweep_level": 96.0, "age_bars": 1},
                "market_structure": {"event": "CHOCH", "direction": "BULLISH", "trend": "BEARISH", "support": 95.0, "resistance": 105.0},
                "volume": {"current": 2000.0, "average": 1000.0, "ratio": 2.0},
                "confluence": {"score": 84.0, "grade": "A", "coverage": 1.0, "reasons": ["fresh_sweep", "price_accepted_beyond_level"]},
            },
        },
        risk_evaluation={"accepted": True, "reason": "risk checks passed", "checks": []},
    )

    explain = summarize_position_explainability({"status": "OPEN", "decision_context": ctx})

    labels = {item["label"] for item in explain["decision_factors"]}
    assert {"Strategy trigger chain", "Liquidity event", "Market structure", "Volume confirmation", "Causal confluence", "Execution status"} <= labels
    assert explain["research_only"] is True
    assert any(item["source"] == "Validation" for item in explain["ai_warnings"])


def test_s7_paper_capital_experiment_is_explained_without_research_label():
    ctx = build_signal_decision_context(
        {
            "id": "sig-s7-paper",
            "ticker": "BTC-USD",
            "action": "BUY",
            "entry": 100.0,
            "tp": 104.0,
            "sl": 98.0,
            "strategy_type": "S7_LIQUIDITY_SWEEP_REVERSAL",
            "paper_capital_experiment": True,
            "research_only": False,
        }
    )
    ctx["paper_capital_experiment"] = True
    ctx["execution"] = {"mode": "paper", "paper_capital_experiment": True}
    ctx["capital_entry_gate"] = {
        "allowed": True,
        "check": "paper_s7_experiment",
        "status": "paper_only",
        "reason": "S7 paper Capital experiment passed data integrity",
    }

    explain = summarize_position_explainability(
        {"status": "OPEN", "paper_capital_experiment": True, "decision_context": ctx}
    )

    labels = {item["label"] for item in explain["decision_factors"]}
    assert "Paper test mode" in labels
    assert "Execution status" not in labels
    assert explain["research_only"] is False


def test_s8_capital_explanation_names_supertrend_and_bos_trigger_chain():
    ctx = build_signal_decision_context(
        {
            "id": "sig-s8-paper",
            "ticker": "GC=F",
            "action": "SHORT",
            "entry": 2400.0,
            "tp": 2370.0,
            "sl": 2415.0,
            "strategy_type": "S8_SUPERTREND_BOS",
            "strategy_evidence": {
                "strategy_id": "S8_SUPERTREND_BOS",
                "strategy_name": "SuperTrend Flip + Break of Structure",
                "sequence": [
                    {"detail": "SuperTrend flipped bearish 3 bars ago."},
                    {"detail": "Bearish BOS confirmed at 2390 after the flip."},
                ],
                "supertrend": {
                    "direction": "BEARISH",
                    "flip_direction": "BEARISH",
                    "flip_age_bars": 3,
                    "line": 2410.0,
                    "period": 10,
                    "multiplier": 3.0,
                },
                "market_structure": {
                    "event": "BOS",
                    "direction": "BEARISH",
                    "event_level": 2390.0,
                    "event_age_bars": 0,
                    "trend": "BEARISH",
                    "support": 2380.0,
                    "resistance": 2410.0,
                },
                "multi_timeframe": {
                    "primary": "30m",
                    "confirmations": ["1h", "4h"],
                    "aligned": ["1h", "4h"],
                    "states": {
                        "30m": {"available": True, "supertrend_direction": "BEARISH"},
                        "1h": {"available": True, "supertrend_direction": "BEARISH"},
                        "4h": {"available": True, "supertrend_direction": "BEARISH"},
                    },
                },
            },
        }
    )
    ctx["entry_chart_snapshot"] = {
        "available": True,
        "snapshot_id": "entry_" + "c" * 24,
        "url": "/api/entry-chart/entry_" + "c" * 24,
        "timeframe": "30m",
    }
    explain = summarize_position_explainability({"status": "OPEN", "decision_context": ctx})
    labels = {item["label"] for item in explain["decision_factors"]}
    assert "Strategy trigger chain" in labels
    assert "SuperTrend confirmation" in labels
    assert "Market structure" in labels
    assert "Multi-timeframe confirmation" in labels
    assert explain["entry_chart_snapshot"]["timeframe"] == "30m"
    assert "Exact technical proof" in explain["ai_decision_story"]
