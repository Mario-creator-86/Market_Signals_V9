from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


ASSET_US_STOCKS = "US_STOCKS"
ASSET_CRYPTO_FUTURES = "CRYPTO_FUTURES"
ASSET_FOREX_MAJORS = "FOREX_MAJORS"
ASSET_COMMODITIES = "COMMODITIES"

REGIME_TREND = "TREND"
REGIME_RANGE = "RANGE"
REGIME_HIGH_VOL = "HIGH_VOL"
REGIME_EVENT_RISK = "EVENT_RISK"
REGIME_SESSION_BREAKOUT = "SESSION_BREAKOUT"

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"


@dataclass
class CandidateSignal:
    strategy_id: str
    strategy_name: str
    symbol: str
    asset_class: str
    direction: str
    regime: str
    entry: float
    sl: float
    tp1: float
    rr: float
    trailing_rule: str
    invalidation_condition: str
    setup_score: float = 0.0
    catalyst_score: float = 0.0
    risk_score: float = 0.0
    validation_score: float = 0.0
    # Neutral until a causal OHLCV context is evaluated; zero would falsely
    # penalize programmatic candidates that pre-date the confluence layer.
    confluence_score: float = 50.0
    total_score: float = 0.0
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)
    perf_snapshot: Dict[str, Any] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["setup_score"] = round(float(self.setup_score), 2)
        payload["catalyst_score"] = round(float(self.catalyst_score), 2)
        payload["risk_score"] = round(float(self.risk_score), 2)
        payload["validation_score"] = round(float(self.validation_score), 2)
        payload["confluence_score"] = round(float(self.confluence_score), 2)
        payload["total_score"] = round(float(self.total_score), 2)
        payload["confidence"] = round(float(self.confidence), 3)
        payload["entry"] = float(self.entry)
        payload["sl"] = float(self.sl)
        payload["tp1"] = float(self.tp1)
        payload["rr"] = round(float(self.rr), 4)
        return payload
