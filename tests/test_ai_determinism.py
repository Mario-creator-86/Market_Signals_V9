"""
tests/test_ai_determinism.py — Stage 4 AI Determinism & Fallback Tests

Covers:
- Schema validation: all required fields present with correct types
- Invalid JSON → deterministic fallback (not empty result)
- Corrupted/partial JSON → fallback
- Valid JSON roundtrips through validate_structured_analysis
- Confidence clamped to [0.0, 1.0]
- Sentiment/play/direction allowed values enforced
- build_deterministic_structured_analysis produces stable output for same input
- parse_structured_ai_output_with_deterministic_fallback returns deterministic when AI fails
- Metadata injected by ollama generate() (mocked)
"""
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.ai_analysis_schema import (
    default_structured_analysis,
    validate_structured_analysis,
    parse_structured_ai_output,
    parse_structured_ai_output_with_deterministic_fallback,
    build_deterministic_structured_analysis,
    is_structured_analysis_default,
    ALLOWED_SENTIMENT,
    ALLOWED_PLAY,
    ALLOWED_DIRECTION,
)


# ---------------------------------------------------------------------------
# Golden fixture: a valid AI response string
# ---------------------------------------------------------------------------
GOLDEN_AI_RESPONSE = """
Some preamble text from the LLM...
```json
{
  "sentiment": "bullish",
  "earnings_play": "PLAY_PRE",
  "confidence": 0.78,
  "key_drivers": ["Strong beat expected", "Macro tailwind"],
  "risks": ["Guidance cut risk"],
  "news_summary": {
    "last_7d_positive": ["Strong revenue"],
    "last_7d_negative": [],
    "last_30d_theme": "earnings momentum",
    "major_risk_events": []
  },
  "scenario": {
    "bull_case": "Gap up on earnings beat",
    "base_case": "In-line, flat reaction",
    "bear_case": "Miss on guidance"
  },
  "trade_plan": {
    "direction": "LONG",
    "entry_condition": "Breakout above 200 pre-market",
    "invalidations": "Close below 190",
    "tp_logic": "ATR 2x",
    "notes": "High conviction"
  }
}
```
"""

CORRUPTED_JSON = '{"sentiment": "bullish", "confidence": 0.7, "key_drivers": ["a"'  # truncated

BAD_TYPES = {
    "sentiment": "SUPER_BULLISH",   # Invalid
    "earnings_play": "YOLO",        # Invalid
    "confidence": 99.0,             # Out of range
    "key_drivers": "not a list",    # Wrong type
    "risks": [],
}


class TestSchemaValidation:

    def test_default_returns_complete_schema(self):
        """default_structured_analysis() must have all required keys."""
        d = default_structured_analysis()
        assert "sentiment" in d
        assert "earnings_play" in d
        assert "confidence" in d
        assert "key_drivers" in d
        assert "risks" in d
        assert "news_summary" in d
        assert "scenario" in d
        assert "trade_plan" in d

    def test_valid_golden_parses_correctly(self):
        """Golden AI response JSON → correct values after validation."""
        result = parse_structured_ai_output(GOLDEN_AI_RESPONSE)
        assert result["sentiment"] == "bullish"
        assert result["earnings_play"] == "PLAY_PRE"
        assert result["confidence"] == pytest.approx(0.78, abs=1e-4)
        assert "Strong beat expected" in result["key_drivers"]
        assert result["trade_plan"]["direction"] == "LONG"

    def test_sentiment_invalid_falls_back_to_neutral(self):
        """Invalid sentiment value → falls back to 'neutral'."""
        result = validate_structured_analysis(BAD_TYPES)
        assert result["sentiment"] == "neutral"

    def test_earnings_play_invalid_falls_back_to_wait(self):
        """Invalid earnings_play → falls back to 'WAIT'."""
        result = validate_structured_analysis(BAD_TYPES)
        assert result["earnings_play"] == "WAIT"

    def test_confidence_clamped(self):
        """confidence > 1.0 → clamped to 1.0."""
        result = validate_structured_analysis({"confidence": 99.0})
        assert result["confidence"] == 1.0

    def test_confidence_min_clamped(self):
        """confidence < 0.0 → clamped to 0.0."""
        result = validate_structured_analysis({"confidence": -5.0})
        assert result["confidence"] == 0.0

    def test_key_drivers_wrong_type_becomes_empty(self):
        """key_drivers that is a string (not list) → empty list."""
        result = validate_structured_analysis({"key_drivers": "just a string"})
        assert result["key_drivers"] == []

    def test_direction_invalid_defaults_to_none(self):
        """Invalid direction → 'NONE'."""
        result = validate_structured_analysis({
            "trade_plan": {"direction": "SIDEWAYS"}
        })
        assert result["trade_plan"]["direction"] == "NONE"

    def test_allowed_values_accepted(self):
        """All allowed enum values are preserved after validation."""
        for s in ALLOWED_SENTIMENT:
            r = validate_structured_analysis({"sentiment": s})
            assert r["sentiment"] == s
        for p in ALLOWED_PLAY:
            r = validate_structured_analysis({"earnings_play": p})
            assert r["earnings_play"] == p

    def test_corrupted_json_falls_back_to_default(self):
        """Truncated/invalid JSON → default_structured_analysis() values."""
        result = parse_structured_ai_output(CORRUPTED_JSON)
        default = default_structured_analysis()
        assert result["sentiment"] == default["sentiment"]
        assert result["confidence"] == default["confidence"]

    def test_empty_string_falls_back_to_default(self):
        """Empty input → default."""
        result = parse_structured_ai_output("")
        default = default_structured_analysis()
        assert result == default


class TestDeterministicFallback:

    def test_deterministic_fallback_has_all_keys(self):
        """Deterministic fallback output has all required schema keys."""
        ctx = {"price": 150.0, "change_pct": 2.5, "technical": {"rsi": 45.0, "macd_hist": 0.5, "trend_1d": "BULLISH"}}
        result = build_deterministic_structured_analysis(ctx)
        assert "sentiment" in result
        assert result["sentiment"] in ALLOWED_SENTIMENT
        assert "confidence" in result
        assert 0.0 <= result["confidence"] <= 1.0
        assert result["trade_plan"]["notes"] == "Deterministic fallback used because AI JSON was invalid."

    def test_deterministic_fallback_is_stable(self):
        """Same input → same output (deterministic)."""
        ctx = {"price": 200.0, "change_pct": 1.0, "technical": {"rsi": 55.0, "macd_hist": 0.3, "trend_1d": "BULLISH", "trend_4h": "BULLISH"}}
        r1 = build_deterministic_structured_analysis(ctx)
        r2 = build_deterministic_structured_analysis(ctx)
        assert r1["sentiment"] == r2["sentiment"]
        assert r1["confidence"] == r2["confidence"]
        assert r1["trade_plan"]["direction"] == r2["trade_plan"]["direction"]

    def test_parse_with_fallback_returns_deterministic_when_ai_fails(self):
        """When AI output is empty/invalid, deterministic fallback is returned (not default)."""
        ctx = {"price": 100.0, "change_pct": -2.0, "technical": {"rsi": 30.0, "macd_hist": -0.2, "trend_1d": "BEARISH"}}
        result = parse_structured_ai_output_with_deterministic_fallback("", context_payload=ctx)
        # Should NOT be the neutral plain default
        assert is_structured_analysis_default(result) is False, (
            "Expected deterministic result but got plain default"
        )

    def test_parse_with_fallback_uses_ai_when_valid(self):
        """When AI output is valid, it should use the AI result, not fallback."""
        result = parse_structured_ai_output_with_deterministic_fallback(GOLDEN_AI_RESPONSE)
        assert result["sentiment"] == "bullish"
        assert result["earnings_play"] == "PLAY_PRE"
        assert is_structured_analysis_default(result) is False
