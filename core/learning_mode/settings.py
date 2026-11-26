"""Learning mode: who a learning trade is, and its settings.

Owner, 2026-11-24: "Mod de învățare, dar cel puțin 10.000 de tranzacții, așa
putem învăța!" On paper, Opus chooses the side of a scanner mover and turns
fresh high-impact news into trades; every trade is small, labelled and
measured. Design: docs/superpowers/specs/2026-11-24-learning-mode-design.md.
"""
from __future__ import annotations

from typing import Any, Dict

LEARNING_SOURCE = "learning"
LEARNING_SIGNAL_MODE = "AUTONOMOUS_LEARNING"
MOVER_PREFIX = "AI_MOVER_"
NEWS_PREFIX = "AI_NEWS_"

# US-listed ETFs a headline can be traded through during the regular session:
# gold, silver, oil, natural gas, broad indices, bonds, the dollar, sectors.
MACRO_INSTRUMENTS = (
    "GLD", "SLV", "USO", "UNG", "SPY", "QQQ", "IWM", "DIA", "TLT", "UUP",
    "XLE", "XLF", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "SMH", "ITA", "KRE",
)
CRYPTO_MAJORS = ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "BNB-USD", "DOGE-USD")

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "min_confidence": 0.55,
    "position_pct_of_equity": 1.0,
    "max_new_per_day": 100,
    "target_closed_trades": 10_000,
    "checkpoint_every": 1_000,
    "min_rr": 1.25,
    "max_rr": 4.0,
    "min_stop_pct": 1.0,
    "max_stop_pct": 10.0,
    "min_hold_hours": 1,
    "max_hold_hours": 72,
    # Two autonomous cycles: the Capital data gate refuses a price older than 300 s.
    "idea_ttl_minutes": 4,
    # Learning mode's share of the 40 open positions the Risk Constitution allows, so
    # that the weekly basket, the Strategy Lab and the dip rule keep room to trade.
    "max_open_positions": 15,
    # An idea whose price moved more than this since Opus judged it is dropped (the premise changed).
    "max_price_drift_pct": 3.0,
    "worker_interval_seconds": 60,
    "mover_min_abs_change_pct": 5.0,
    "mover_ask_cooldown_minutes": 60,
    "news_interval_seconds": 300,
    "news_max_age_minutes": 30,
    "news_min_impact_score": 35,
    "news_max_headlines": 12,
    "news_max_ideas": 3,
    "opus_reserve_calls": 600,
    "call_timeout_seconds": 90,
}


def learning_config(exec_cfg: Any) -> Dict[str, Any]:
    """The learning-mode settings: ``agents.execution.learning_mode`` over the defaults."""
    raw = (exec_cfg or {}).get("learning_mode") if isinstance(exec_cfg, dict) else None
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        out.update({key: value for key, value in raw.items() if value is not None})
    return out


def is_learning_signal(signal: Any) -> bool:
    """A learning trade: source ``learning`` and an ``AI_MOVER_`` / ``AI_NEWS_`` strategy type."""
    if not isinstance(signal, dict):
        return False
    strategy = str(signal.get("strategy_type") or signal.get("strategy_id") or "").upper()
    return (
        str(signal.get("autonomous_source") or "").lower() == LEARNING_SOURCE
        and strategy.startswith((MOVER_PREFIX, NEWS_PREFIX))
    )


def learning_ai_is_advisory(signal: Any, exec_cfg: Any) -> bool:
    """True when the pipeline's AI review of a learning trade is recorded but not obeyed.

    Opus already chose the trade; a second opinion on the same proposal is kept
    on record. Paper mode only, and only while learning mode is enabled.
    """
    cfg = exec_cfg if isinstance(exec_cfg, dict) else {}
    if str(cfg.get("mode", "paper")).lower() != "paper":
        return False
    return is_learning_signal(signal) and bool(learning_config(cfg).get("enabled", True))


__all__ = [
    "CRYPTO_MAJORS", "DEFAULTS", "LEARNING_SIGNAL_MODE", "LEARNING_SOURCE", "MACRO_INSTRUMENTS", "MOVER_PREFIX",
    "NEWS_PREFIX", "is_learning_signal", "learning_ai_is_advisory", "learning_config",
]
