from core.watchlist_quick_ai import default_quick_ai_payload, parse_quick_ai_output


def test_parse_quick_ai_output_valid_json():
    raw = """
    Random prefix
    {
      "bias": "bullish",
      "confidence": 126,
      "time_horizon": "1-7d",
      "top_reasons": ["r1", "r2", "r3", "r4"],
      "top_risks": ["k1", "k2", "k3", "k4"],
      "action": "BUY",
      "notes": "setup valid"
    }
    """
    parsed = parse_quick_ai_output(raw)
    assert parsed["bias"] == "bullish"
    assert parsed["confidence"] == 100
    assert parsed["time_horizon"] == "1-7d"
    assert parsed["action"] == "BUY"
    assert parsed["top_reasons"] == ["r1", "r2", "r3"]
    assert parsed["top_risks"] == ["k1", "k2", "k3"]


def test_parse_quick_ai_output_invalid_fallback():
    parsed = parse_quick_ai_output("not a json payload")
    assert parsed == default_quick_ai_payload()

