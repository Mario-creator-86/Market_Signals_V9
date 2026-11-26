
import pytest
import pandas as pd
import numpy as np
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch
import sys
import os
import tempfile
from pathlib import Path

# Ensure root directory is in python path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Forceably set env vars BEFORE any imports
os.environ["TEST_MODE"] = "1"
os.environ["MS_DISABLE_ENGINE_AUTOSTART"] = "1"
os.environ["MS_DISABLE_WARMUP"] = "1"
os.environ["MS_DISABLE_STATE_PREFETCH"] = "1"
os.environ["MS_DISABLE_BG_WORKER"] = "1"
os.environ.setdefault("MS_MARKET_HOURS_OVERRIDE", "open")
_TEST_STORAGE_DIR = Path(tempfile.mkdtemp(prefix=f"market-signals-tests-{os.getpid()}-"))
_TEST_RUNTIME_DIR = _TEST_STORAGE_DIR / "runtime"
_TEST_DATA_DIR = _TEST_STORAGE_DIR / "data"
_TEST_LOG_DIR = _TEST_STORAGE_DIR / "logs"
for _directory in (_TEST_RUNTIME_DIR, _TEST_DATA_DIR, _TEST_LOG_DIR):
    _directory.mkdir(parents=True, exist_ok=True)

# These assignments are deliberately unconditional. A developer may have live
# MS_* paths in the parent shell; inheriting even one of them lets TestClient
# mutate the running bot's config/state while the rest of pytest is isolated.
os.environ.update({
    "MS_ENV": "test",
    "MS_RUNTIME_DIR": str(_TEST_RUNTIME_DIR),
    "MS_DATA_DIR": str(_TEST_DATA_DIR),
    "MS_LOG_DIR": str(_TEST_LOG_DIR),
    "MS_AUDIT_LOG_DIR": str(_TEST_STORAGE_DIR / "audit_logs"),
    "MS_BACKUP_DIR": str(_TEST_STORAGE_DIR / "backups"),
    "MS_CONFIG_PATH": str(_TEST_RUNTIME_DIR / "config.json"),
    "MS_CAPITAL_STATE_PATH": str(_TEST_RUNTIME_DIR / "capital.json"),
    "MS_STATE_PATH": str(_TEST_RUNTIME_DIR / "state.json"),
    "MS_SIGNALS_PATH": str(_TEST_RUNTIME_DIR / "signals_state.json"),
    "MS_SYMBOL_CACHE_PATH": str(_TEST_RUNTIME_DIR / "symbol_status.json"),
    "MS_WATCHLIST_PATH": str(_TEST_RUNTIME_DIR / "watchlist.json"),
    "MS_PRESETS_PATH": str(_TEST_RUNTIME_DIR / "presets.json"),
    "MS_HOTLIST_CACHE_PATH": str(_TEST_RUNTIME_DIR / "hotlist_cache.json"),
    "MS_SUPPRESSED_TICKERS_PATH": str(_TEST_RUNTIME_DIR / "suppressed_tickers.json"),
    "MS_SQLITE_DB_PATH": str(_TEST_DATA_DIR / "market_signals.db"),
    "MS_BACKTEST_RESULTS_PATH": str(_TEST_DATA_DIR / "backtest_results.json"),
    "MS_TRADE_RADAR_STATE_PATH": str(_TEST_DATA_DIR / "trade_radar_state.json"),
    "MS_TRADE_AUDIT_PATH": str(_TEST_DATA_DIR / "trade_decisions.json"),
    "MS_EXECUTION_DB_PATH": str(_TEST_DATA_DIR / "execution.db"),
    "MS_DECISION_LEDGER_PATH": str(_TEST_DATA_DIR / "decision_ledger.db"),
    "MS_RESEARCH_HYPOTHESES_PATH": str(_TEST_DATA_DIR / "research_hypotheses.json"),
    "MS_RESEARCH_EXPERIMENTS_PATH": str(_TEST_DATA_DIR / "research_experiments.json"),
    "MS_DEVELOPMENT_SUBMISSIONS_PATH": str(_TEST_DATA_DIR / "development_submissions.json"),
    "MS_AUTO_REPAIR_CASES_PATH": str(_TEST_DATA_DIR / "auto_repair_cases.json"),
    "MS_AUTO_REPAIR_ACTIVITY_PATH": str(_TEST_DATA_DIR / "auto_repair_activity.json"),
    "MS_PROMOTION_RECORDS_PATH": str(_TEST_DATA_DIR / "promotion_records.json"),
    "MS_NEWS_BACKUP_PATH": str(_TEST_DATA_DIR / "news_backup.json"),
    "MS_NEWS_QUARANTINE_PATH": str(_TEST_DATA_DIR / "news_quarantine.json"),
    "MS_NEWS_SUPPRESSION_PATH": str(_TEST_DATA_DIR / "news_suppression.json"),
    "MS_OPERATOR_AUDIT_PATH": str(_TEST_DATA_DIR / "operator_audit.jsonl"),
    "MS_ENTRY_CHART_DIR": str(_TEST_DATA_DIR / "entry_charts"),
    "MS_OOS_REPORTS_DIR": str(_TEST_DATA_DIR / "oos_reports"),
})

# Prevent dotenv from overriding these if it runs later
try:
    import dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: None
except ImportError:
    pass

from api.main import (
    app,
    _reset_quality_gate_endpoint_caches,
    _traffic_controller,
)
from core.cache_manager import cache_manager
from core.hotlist_news import reset_hotlist_news_caches
from core.http_utils import reset_http_governor_state
from core.news_pulse import reset_news_pulse_cache
from core.observability import reset_observability_state

@pytest.fixture(scope="session")
def client():
    # Mock engine start/stop to avoid background threads
    with patch("api.main.start_engine"), patch("api.main.stop_engine"):
        with TestClient(app) as c:
            yield c


@pytest.fixture(autouse=True)
def suppress_live_news_refresh_for_api_tests(request, monkeypatch):
    """Keep API tests offline without polluting standalone News agent tests."""
    if "client" not in request.fixturenames:
        yield
        return
    monkeypatch.setattr(
        "agents.news_tab_agent.NewsTabAgent.start_auto_refresh",
        lambda self, *a, **kw: None,
    )
    monkeypatch.setattr(
        "agents.news_tab_agent.NewsTabAgent.refresh_all_news",
        lambda self, *a, **kw: {
            "ticker_news": {},
            "market_news": [],
            "breaking_news": [],
        },
    )
    yield


@pytest.fixture
def mock_ohlcv_data():
    """Generates synthetic OHLCV data for testing."""
    dates = pd.date_range(start="2023-01-01", periods=100, freq="1h")
    df = pd.DataFrame({
        "Open": np.random.uniform(100, 200, 100),
        "High": np.random.uniform(200, 210, 100),
        "Low": np.random.uniform(90, 100, 100),
        "Close": np.random.uniform(100, 200, 100),
        "Volume": np.random.uniform(1000, 5000, 100)
    }, index=dates)
    return df

@pytest.fixture
def mock_ohlcv_edge_cases():
    """Generates edge case OHLCV data (NaN, zero volume, etc)."""
    dates = pd.date_range(start="2023-01-01", periods=20, freq="1h")
    df = pd.DataFrame({
        "Open": np.random.uniform(100, 200, 20),
        "High": np.random.uniform(200, 210, 20),
        "Low": np.random.uniform(90, 100, 20),
        "Close": np.random.uniform(100, 200, 20),
        "Volume": np.random.uniform(1000, 5000, 20)
    }, index=dates)
    
    # Add NaN
    df.loc[dates[5], "Close"] = np.nan
    
    # Add Zero Volume
    df.loc[dates[10], "Volume"] = 0
    
    return df

@pytest.fixture(autouse=True)
def mock_external_deps():
    """Mock external calls to avoid network usage during tests."""
    with patch("yfinance.download") as mock_yf, \
         patch("core.ollama_client.OllamaClient.check_availability", return_value=False), \
         patch("core.content_fetcher.fetch_article_content", return_value=None):
        mock_yf.return_value = pd.DataFrame()  # Default empty
        yield mock_yf


@pytest.fixture(autouse=True)
def reset_observability_and_cache():
    reset_observability_state()
    reset_http_governor_state()
    reset_news_pulse_cache()
    reset_hotlist_news_caches()
    cache_manager.clear()
    _traffic_controller.reset()
    _reset_quality_gate_endpoint_caches()
    yield


@pytest.fixture
def block_external_http(monkeypatch):
    import requests

    def _blocked(*args, **kwargs):
        raise requests.RequestException("blocked-by-test")

    monkeypatch.setattr("requests.request", _blocked)


@pytest.fixture(autouse=True)
def _reset_persisted_circuit_breaker():
    """The bot circuit breaker survives restarts by design; tests must not inherit it."""
    yield
    module = sys.modules.get("core.sqlite_store")
    store = getattr(getattr(module, "SQLiteKVStore", None), "_instance", None) if module else None
    if store is None:
        return
    try:
        if store.get_json("bot_circuit_breaker_state", None):
            store.set_json("bot_circuit_breaker_state", {})
    except Exception:
        pass


@pytest.fixture(autouse=True)
def no_real_learning_worker():
    """The learning-mode worker asks Opus every minute: no test may start the real one.

    Tests that need a worker patch ``core.agent_manager.get_learning_worker`` themselves.
    """
    with patch("core.agent_manager.get_learning_worker", lambda build=None: None):
        yield
