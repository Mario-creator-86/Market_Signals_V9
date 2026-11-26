from core.ai_analysis_schema import (
    parse_structured_ai_output,
    parse_structured_ai_output_with_deterministic_fallback,
)


def test_parse_structured_ai_output_invalid_text_fallback():
    out = parse_structured_ai_output("this is not json")
    assert out["sentiment"] == "neutral"
    assert out["earnings_play"] == "WAIT"
    assert out["news_summary"]["last_30d_theme"] == "unknown"
    assert out["trade_plan"]["direction"] == "NONE"


def test_parse_structured_ai_output_valid_json_and_normalization():
    raw = """
    ```json
    {
      "sentiment":"bullish",
      "earnings_play":"PLAY_PRE",
      "confidence": 1.7,
      "key_drivers":["strong momentum"],
      "news_summary":{
        "last_7d_positive":["a","b","c","d","e","f","g"],
        "last_7d_negative":["n1"],
        "last_30d_theme":"upgrades",
        "major_risk_events":["r1","r2","r3","r4"]
      },
      "scenario":{"bull_case":"x","base_case":"y","bear_case":"z"},
      "trade_plan":{"direction":"LONG","entry_condition":"breakout","invalidations":"below support","tp_logic":"ATR","notes":"tight risk"}
    }
    ```
    """
    out = parse_structured_ai_output(raw)
    assert out["sentiment"] == "bullish"
    assert out["earnings_play"] == "PLAY_PRE"
    assert out["confidence"] == 1.0
    assert len(out["news_summary"]["last_7d_positive"]) == 6
    assert len(out["news_summary"]["major_risk_events"]) == 3
    assert out["trade_plan"]["direction"] == "LONG"


def test_parse_structured_ai_output_with_deterministic_fallback_builds_summary():
    out = parse_structured_ai_output_with_deterministic_fallback(
        "not-json",
        context_payload={
            "price": 100.0,
            "change_pct": 2.1,
            "technical": {"trend_1d": "BULL", "trend_4h": "BULL", "rsi": 58, "macd_hist": 0.8},
        },
    )
    assert out["confidence"] > 0.0
    assert out["trade_plan"]["direction"] in {"LONG", "SHORT", "NONE"}
    assert out["trade_plan"]["notes"].lower().startswith("deterministic fallback")
