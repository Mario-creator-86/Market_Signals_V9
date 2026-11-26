import time
import threading
from types import SimpleNamespace

import agents.scanner_agent as scanner_module
from agents.scanner_agent import ScannerAgent


def _fake_cfg():
    return SimpleNamespace(scan={"scan_interval_seconds": 60})


def test_get_state_lazy_refreshes_when_empty(monkeypatch):
    agent = ScannerAgent(_fake_cfg())
    agent.startup_lazy_refresh_delay = 0
    agent.last_scan_time = 0
    calls = {"n": 0}

    def fake_run_scan():
        calls["n"] += 1
        agent.cached_hot_list = [{"symbol": "AAPL", "change_pct": 1.2, "price": 200.0, "source": "test"}]
        return agent.cached_hot_list

    monkeypatch.setattr(agent, "run_scan", fake_run_scan)

    state = agent.get_state()
    deadline = time.time() + 1.0
    while time.time() < deadline and calls["n"] == 0:
        time.sleep(0.01)
    state = agent.get_state()
    assert calls["n"] == 1
    assert state["hot_tickers"]
    assert state["hot_tickers"][0]["symbol"] == "AAPL"


def test_get_state_does_not_refresh_inside_interval(monkeypatch):
    agent = ScannerAgent(_fake_cfg())
    agent.cached_hot_list = [{"symbol": "MSFT"}]
    agent.last_scan_time = time.time()
    calls = {"n": 0}

    def fake_run_scan():
        calls["n"] += 1
        return agent.cached_hot_list

    monkeypatch.setattr(agent, "run_scan", fake_run_scan)

    state = agent.get_state()
    assert calls["n"] == 0
    assert state["hot_tickers"][0]["symbol"] == "MSFT"


def test_get_state_refreshes_when_stale(monkeypatch):
    agent = ScannerAgent(_fake_cfg())
    agent.cached_hot_list = [{"symbol": "OLD"}]
    agent.last_scan_time = time.time() - (agent.watchlist_update_interval + 2)
    calls = {"n": 0}

    def fake_run_scan():
        calls["n"] += 1
        agent.cached_hot_list = [{"symbol": "NEW"}]
        return agent.cached_hot_list

    monkeypatch.setattr(agent, "run_scan", fake_run_scan)

    state = agent.get_state()
    deadline = time.time() + 1.0
    while time.time() < deadline and calls["n"] == 0:
        time.sleep(0.01)
    state = agent.get_state()
    assert calls["n"] == 1
    assert state["hot_tickers"][0]["symbol"] == "NEW"


def test_get_state_is_non_blocking_when_scan_is_slow(monkeypatch):
    agent = ScannerAgent(_fake_cfg())
    agent.startup_lazy_refresh_delay = 0
    agent.last_scan_time = 0
    calls = {"n": 0}

    def fake_run_scan():
        calls["n"] += 1
        time.sleep(0.3)
        return [{"symbol": "FAST"}]

    monkeypatch.setattr(agent, "run_scan", fake_run_scan)

    t0 = time.time()
    state = agent.get_state()
    elapsed = time.time() - t0

    assert elapsed < 0.1
    assert isinstance(state.get("hot_tickers"), list)
    deadline = time.time() + 1.0
    while time.time() < deadline and calls["n"] == 0:
        time.sleep(0.01)
    assert calls["n"] == 1


def test_completed_scan_is_shared_with_a_separate_scanner_process(tmp_path, monkeypatch):
    """The API scanner cache must be consumable by the engine process."""
    snapshot_path = tmp_path / "scanner_state.json"
    monkeypatch.setattr(scanner_module, "SCANNER_SHARED_STATE_PATH", snapshot_path)

    writer = ScannerAgent(_fake_cfg())
    monkeypatch.setattr(
        writer,
        "_scan_stocks",
        lambda: [{"symbol": "AAPL", "change_pct": 6.5, "price": 200.0, "source": "test"}],
    )
    monkeypatch.setattr(writer, "_scan_crypto", lambda: [])
    writer.run_scan()

    reader = ScannerAgent(_fake_cfg())
    state = reader.get_state()

    assert snapshot_path.exists()
    assert state["hot_tickers"] == [{"symbol": "AAPL", "change_pct": 6.5, "price": 200.0, "source": "test"}]
    assert state["liquid_crypto_tickers"] == []
    assert state["data_age_seconds"] is not None


def test_liquid_crypto_universe_is_shared_with_a_separate_scanner_process(tmp_path, monkeypatch):
    snapshot_path = tmp_path / "scanner_state.json"
    monkeypatch.setattr(scanner_module, "SCANNER_SHARED_STATE_PATH", snapshot_path)
    writer = ScannerAgent(_fake_cfg())
    writer.cached_liquid_crypto_list = [
        {"symbol": "BTCUSDT", "quoteVolume": 1_000_000_000.0, "source": "binance"}
    ]
    writer._publish_shared_snapshot([])

    reader = ScannerAgent(_fake_cfg())
    state = reader.get_state()

    assert state["liquid_crypto_tickers"] == writer.cached_liquid_crypto_list


def test_overlapping_scans_collapse_into_active_run(monkeypatch):
    agent = ScannerAgent(_fake_cfg())
    agent.cached_hot_list = [{"symbol": "CACHED"}]
    entered = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def slow_scan():
        calls["n"] += 1
        entered.set()
        assert release.wait(timeout=2)
        return [{"symbol": "FRESH"}]

    monkeypatch.setattr(agent, "_run_scan_impl", slow_scan)
    first_result = []
    first = threading.Thread(target=lambda: first_result.extend(agent.run_scan()))
    first.start()
    assert entered.wait(timeout=1)

    collapsed = agent.run_scan()
    release.set()
    first.join(timeout=2)

    assert not first.is_alive()
    assert calls["n"] == 1
    assert collapsed == [{"symbol": "CACHED"}]
    assert first_result == [{"symbol": "FRESH"}]
