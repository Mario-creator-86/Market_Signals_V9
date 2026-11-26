"""Flow evidence → existing trade setups (CONFIRM / NEUTRAL / CONTRADICT).

The ONLY integration surface of the flow indicators with the decision
pipeline. It never generates trades, never vetoes, never alters scores or
risk — it turns an indicator verdict into a labelled evidence note that the
existing AI decision prompt, AI Critic and decision ledger can read.

Alignment rules (vs the setup's own direction):
- indicator UNAVAILABLE/stale/disabled -> UNAVAILABLE (existing logic must
  continue unchanged — absence of evidence is not evidence)
- bias matching the direction AND |score| >= 25 AND confidence >= 0.35 -> CONFIRM
- bias opposite the direction AND |score| >= 25 AND confidence >= 0.35 -> CONTRADICT
- anything else (weak, neutral, low confidence) -> NEUTRAL
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from core.flow_indicators import whale_flow, institutional_flow

logger = logging.getLogger(__name__)

ALIGNMENT_MIN_ABS_SCORE = 25.0
ALIGNMENT_MIN_CONFIDENCE = 0.35

_BULLISH = ("BULLISH", "LONG", "BUY")
_BEARISH = ("BEARISH", "SHORT", "SELL")


def asset_class_for(ticker: str) -> Optional[str]:
    """Classify a ticker for indicator routing (crypto whale vs stock proxy)."""
    if whale_flow._is_crypto_symbol(ticker):
        return "crypto"
    if institutional_flow._is_stock_symbol(ticker):
        return "stock"
    return None


def _sign(direction: str) -> Optional[int]:
    d = str(direction or "").upper()
    if d in _BULLISH:
        return 1
    if d in _BEARISH:
        return -1
    return None


def _alignment(direction: str, bias: str, score: Optional[float],
               confidence: Optional[float]) -> str:
    dir_sign = _sign(direction)
    if dir_sign is None:
        return "NEUTRAL"  # no directional setup to compare against
    if bias == "UNAVAILABLE" or score is None:
        return "UNAVAILABLE"
    if (abs(float(score)) < ALIGNMENT_MIN_ABS_SCORE
            or (confidence or 0.0) < ALIGNMENT_MIN_CONFIDENCE):
        return "NEUTRAL"
    bias_sign = 1 if bias in _BULLISH else (-1 if bias in _BEARISH else 0)
    if bias_sign == 0:
        return "NEUTRAL"
    return "CONFIRM" if bias_sign == dir_sign else "CONTRADICT"


def _summary(indicator: Dict[str, Any]) -> Dict[str, Any]:
    """Compact view for prompts/ledger — full detail stays in the indicator."""
    keys = ("indicator", "proxy", "bias", "score", "confidence",
            "buy_pressure", "sell_pressure", "persistence",
            "cancellation_activity", "spoofing_risk", "relative_volume")
    return {k: indicator.get(k) for k in keys if k in indicator}


def flow_evidence_for_signal(signal: Dict[str, Any]) -> Dict[str, Any]:
    """Build the evidence block for an EXISTING signal dict.

    Fail-safe by contract: any internal failure returns an UNAVAILABLE block,
    never raises, and never mutates the input signal.
    """
    evidence: Dict[str, Any] = {
        "alignment": "UNAVAILABLE",
        "asset_class": None,
        "note": "flow evidence unavailable",
        "summary": None,
    }
    try:
        ticker = str(signal.get("ticker") or "").upper()
        direction = str(signal.get("direction") or signal.get("action") or "").upper()
        if direction in ("HOLD", ""):
            direction = ""
        asset_class = asset_class_for(ticker)
        if asset_class is None:
            evidence["note"] = f"no flow indicator for symbol {ticker or '(none)'}"
            return evidence
        evidence["asset_class"] = asset_class
        if asset_class == "crypto":
            indicator = whale_flow.get_whale_flow(ticker)
        else:
            indicator = institutional_flow.get_institutional_flow(ticker)
        evidence["alignment"] = _alignment(direction, indicator.get("bias") or "UNAVAILABLE",
                                           indicator.get("score"), indicator.get("confidence"))
        evidence["summary"] = _summary(indicator)
        evidence["alignment_rule"] = (
            f"|score|>={ALIGNMENT_MIN_ABS_SCORE:.0f} and confidence>={ALIGNMENT_MIN_CONFIDENCE} "
            "vs setup direction; weaker or unavailable data reads NEUTRAL/UNAVAILABLE"
        )
        if indicator.get("proxy"):
            evidence["note"] = (
                "INSTITUTIONAL FLOW PROXY — free-data estimate, not full "
                "US Level 2 institutional flow"
            )
        else:
            evidence["note"] = (
                f"whale flow bias {indicator.get('bias')} "
                f"(score {indicator.get('score')}, confidence {indicator.get('confidence')})"
            )
        evidence["data_freshness"] = indicator.get("data_freshness")
    except Exception as exc:  # absolute fail-safe: evidence must never break a decision
        logger.warning("flow evidence failed: %s", exc)
        evidence["alignment"] = "UNAVAILABLE"
        evidence["note"] = f"flow evidence error: {str(exc)[:120]}"
    return evidence


def flow_evidence_status() -> Dict[str, Any]:
    return {
        "whale_flow": whale_flow.whale_flow_status(),
        "institutional_flow": institutional_flow.institutional_flow_status(),
    }
