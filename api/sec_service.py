"""API-facing SEC service seam."""
from __future__ import annotations

import re
from typing import Any, Dict

from core.config import ConfigManager
from core.sec.service import get_sec_filing_service


_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,14}$")


def get_sec_context(ticker: str, *, refresh: bool = False) -> Dict[str, Any]:
    normalized = str(ticker or "").strip().upper()
    if not _TICKER_RE.fullmatch(normalized):
        raise ValueError("invalid US equity ticker")
    config = ConfigManager()
    config_data = dict(getattr(config, "_config", {}) or {})
    return get_sec_filing_service(config_data).get_context(normalized, refresh=bool(refresh))
