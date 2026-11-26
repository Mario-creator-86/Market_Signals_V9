"""Sector population for Pre-Earnings (Task PE-premium C2).

`sector` is read from state_row / fundamentalSnapshot, but those are rarely
populated, so sector×cap calibration collapses to UNKNOWN. The yfinance .info
payload already fetched for analyst estimates carries `sector`/`industry` —
surface them (no extra request) and fall back through the 6h estimates cache.
"""

import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents.pre_earnings_agent import PreEarningsAgent
from core.stock_data_client import StockDataClient


def _agent():
    with patch("agents.pre_earnings_agent.ConfigManager"):
        agent = PreEarningsAgent.__new__(PreEarningsAgent)
        agent._lock = threading.RLock()
        return agent


@pytest.mark.unit
def test_get_estimates_includes_sector():
    fake_info = {
        "forwardEps": 5.0,
        "targetMeanPrice": 200.0,
        "sector": "Technology",
        "industry": "Semiconductors",
    }
    fake_ticker = MagicMock()
    fake_ticker.info = fake_info
    with patch("core.stock_data_client.yf") as fake_yf, \
         patch("core.stock_data_client.time.sleep"), \
         patch("core.stock_data_client.get_health_dashboard"):
        fake_yf.Ticker.return_value = fake_ticker
        result = StockDataClient.get_estimates("TSM")
    assert isinstance(result, dict)
    assert result["sector"] == "Technology"
    assert result["industry"] == "Semiconductors"


@pytest.mark.unit
def test_resolve_sector_state_row_wins():
    agent = _agent()
    out = agent._resolve_sector("AAPL", {"sector": "Technology"})
    assert out == "Technology"


@pytest.mark.unit
def test_resolve_sector_fundamental_snapshot():
    agent = _agent()
    out = agent._resolve_sector("AAPL", {"fundamentalSnapshot": {"sector": "Technology"}})
    assert out == "Technology"


@pytest.mark.unit
def test_resolve_sector_estimates_cache_fallback():
    agent = _agent()
    agent._analyst_ttl_cache = {
        "TSM": (time.time(), {"targetMeanPrice": 200.0, "sector": "Technology"}),
    }
    out = agent._resolve_sector("TSM", {})
    assert out == "Technology"


@pytest.mark.unit
def test_resolve_sector_missing_returns_none():
    agent = _agent()
    agent._analyst_ttl_cache = {}
    with patch.object(agent, "_get_analyst_estimates_cached", return_value=None):
        out = agent._resolve_sector("ZZZZ", {})
    assert out is None


@pytest.mark.unit
def test_resolve_sector_never_raises_on_cache_error():
    agent = _agent()
    with patch.object(agent, "_get_analyst_estimates_cached", side_effect=RuntimeError("boom")):
        out = agent._resolve_sector("AAPL", {})
    assert out is None
