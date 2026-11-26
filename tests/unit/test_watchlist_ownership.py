import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

import api.actions_service as actions_service
import core.config as config_module
from agents.scanner_agent import ScannerAgent
from core.config import DEFAULT_CONFIG, normalize_market_ticker


class _ConfigStub:
    def __init__(self, scanner_config: Dict[str, Any]):
        self.agents = {"scanner": dict(scanner_config)}
        self.reload_count = 0

    def reload_watchlist_if_changed(self) -> None:
        self.reload_count += 1


class _WorkspaceStub:
    def __init__(self):
        self.invalidated: List[List[str]] = []
        self.upserts: List[tuple[str, Dict[str, Any]]] = []
        self.version_bumps = 0

    def invalidate_tickers(self, tickers: List[str]) -> None:
        self.invalidated.append(list(tickers))

    def upsert_market_data(self, ticker: str, payload: Dict[str, Any]) -> None:
        self.upserts.append((ticker, dict(payload)))

    def bump_version(self) -> None:
        self.version_bumps += 1


def _scanner(config: Dict[str, Any]) -> ScannerAgent:
    agent = object.__new__(ScannerAgent)
    agent.config_manager = _ConfigStub(config)
    return agent


def _write_watchlist(path: Path, **overrides: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "stocks": [],
        "crypto": [],
        "hot_list": [],
        "order": [],
        "settings": {},
        "scanner_exclusions": [],
        "schema_version": 2,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def _patch_action_dependencies(monkeypatch: pytest.MonkeyPatch, watchlist_path: Path):
    shared_config = _ConfigStub({})
    workspace = _WorkspaceStub()
    monkeypatch.setattr(actions_service, "WATCHLIST_PATH", watchlist_path)
    monkeypatch.setattr(actions_service, "get_shared_config", lambda: shared_config)
    monkeypatch.setattr(actions_service, "get_ticker_workspace_store", lambda: workspace)
    return shared_config, workspace




@pytest.mark.parametrize("symbol", ["-USD", "-USDT", "-USDC"])
def test_quote_only_crypto_symbols_are_invalid(symbol: str):
    assert normalize_market_ticker(symbol) == ""
    assert normalize_market_ticker("BTCUSDT") == "BTC-USD"


def test_remove_cleans_all_watchlist_ownership_state_and_adds_exclusion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    watchlist_path = tmp_path / "watchlist.json"
    _write_watchlist(
        watchlist_path,
        stocks=["AAPL", "MSFT"],
        crypto=["BTC-USD"],
        hot_list=["AAPL", "BTC-USD"],
        order=["AAPL", "MSFT", "BTC-USD"],
        settings={"AAPL": {"signals_enabled": False}, "MSFT": {"min_win_rate": 55}},
        scanner_exclusions=["OLD"],
    )
    shared_config, workspace = _patch_action_dependencies(
        monkeypatch,
        watchlist_path,
    )

    result = actions_service.remove_tickers(["aapl"], return_state=False)
    persisted = json.loads(watchlist_path.read_text(encoding="utf-8"))

    assert result["removed"] == ["AAPL"]
    assert persisted["stocks"] == ["MSFT"]
    assert persisted["crypto"] == ["BTC-USD"]
    assert persisted["hot_list"] == ["BTC-USD"]
    assert persisted["order"] == ["MSFT", "BTC-USD"]
    assert persisted["settings"] == {"MSFT": {"min_win_rate": 55}}
    assert persisted["scanner_exclusions"] == ["OLD", "AAPL"]
    assert workspace.invalidated == [["AAPL"]]
    assert shared_config.reload_count == 1


def test_manual_add_clears_prior_scanner_exclusion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    watchlist_path = tmp_path / "watchlist.json"
    _write_watchlist(
        watchlist_path,
        stocks=["MSFT"],
        order=["MSFT"],
        scanner_exclusions=["AAPL", "OLD"],
    )
    shared_config, workspace = _patch_action_dependencies(
        monkeypatch,
        watchlist_path,
    )

    result = actions_service.add_ticker("aapl", return_state=False)
    persisted = json.loads(watchlist_path.read_text(encoding="utf-8"))

    assert result["added"] is True
    assert persisted["stocks"] == ["MSFT", "AAPL"]
    assert persisted["order"] == ["MSFT", "AAPL"]
    assert persisted["scanner_exclusions"] == ["OLD"]
    assert workspace.upserts[0][0] == "AAPL"
    assert workspace.version_bumps == 1
    assert shared_config.reload_count == 1


