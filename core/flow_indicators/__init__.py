"""Free order-flow evidence indicators (owner spec, 2026-08-31).

- whale_flow: crypto whale pressure from Binance/Bybit/OKX public books+tape
- institutional_flow: INSTITUTIONAL FLOW PROXY from Yahoo/Alpaca-IEX/FINRA
- evidence: CONFIRM/NEUTRAL/CONTRADICT mapping for existing setups

These are evidence-only: no independent trades, no new vetoes, no changes to
existing scoring/risk. Configure via the `flow_indicators` config section.
"""
from typing import Any

from core.flow_indicators import evidence, institutional_flow, whale_flow
from core.flow_indicators.evidence import (
    flow_evidence_for_signal,
    flow_evidence_status,
)
from core.flow_indicators.institutional_flow import get_institutional_flow
from core.flow_indicators.whale_flow import get_whale_flow

__all__ = [
    "whale_flow", "institutional_flow", "evidence",
    "get_whale_flow", "get_institutional_flow",
    "flow_evidence_for_signal", "flow_evidence_status",
]


def configure_from_shared(config: Any) -> None:
    """Apply the `flow_indicators` section of the shared config, if present."""
    try:
        root = getattr(config, "config", config)
        section = ((root or {}).get("flow_indicators") or {}) if isinstance(root, dict) else {}
        whale_flow.configure({**section, **(section.get("whale") or {})})
        institutional_flow.configure({**section, **(section.get("institutional") or {})})
    except Exception:  # configuration must never break the indicators
        pass
