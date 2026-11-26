from __future__ import annotations

from typing import Any, Dict, Optional


_ALIASES = {
    "news": "news_tab",
    "news_tab": "news_tab",
    "calendar": "economic_calendar",
    "economic_calendar": "economic_calendar",
    "trade_radar": "trade_radar",
    "traderadar": "trade_radar",
    "strategy_research": "strategy_research",
    "research": "strategy_research",
    "premarket": "premarket_scanner",
    "premarket_scanner": "premarket_scanner",
    "pre_earnings": "pre_earnings",
    "preearnings": "pre_earnings",
    "execution": "execution",
    "risk": "risk",
    "scanner": "scanner",
    "sentiment": "sentiment",
}


def normalize_agent_key(agent_key: str) -> str:
    key = str(agent_key or "").strip().lower()
    return _ALIASES.get(key, key)


def is_agent_enabled(agent_key: str, config: Optional[Dict[str, Any]] = None, default: bool = True) -> bool:
    key = normalize_agent_key(agent_key)

    cfg = config
    if cfg is None:
        try:
            from core.config import ConfigManager

            cfg = ConfigManager().config
        except Exception:
            return bool(default)

    agents = (cfg or {}).get("agents", {}) or {}
    value = agents.get(key)
    if isinstance(value, dict):
        return bool(value.get("enabled", default))
    if isinstance(value, bool):
        return bool(value)
    return bool(default)
