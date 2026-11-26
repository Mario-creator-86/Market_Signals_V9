import threading
import time

from agents.premarket_scanner_agent import PremarketScannerAgent
from agents.pre_earnings_agent import PreEarningsAgent


def _assert_non_blocking_get_state(agent) -> None:
    ready = threading.Event()

    def _holder():
        with agent._lock:
            ready.set()
            time.sleep(0.4)

    t = threading.Thread(target=_holder, daemon=True)
    t.start()
    assert ready.wait(timeout=1.0)

    start = time.time()
    state = agent.get_state()
    elapsed = time.time() - start

    assert elapsed < 0.35
    assert isinstance(state, dict)
    t.join(timeout=1.0)


def test_premarket_get_state_non_blocking_under_lock_contention():
    agent = PremarketScannerAgent()
    _assert_non_blocking_get_state(agent)


def test_pre_earnings_get_state_non_blocking_under_lock_contention():
    agent = PreEarningsAgent()
    _assert_non_blocking_get_state(agent)
