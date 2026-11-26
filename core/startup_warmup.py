import logging
import threading
import time
from typing import Iterable, List

from core.config import ConfigManager
from core.stock_data_client import StockDataClient

logger = logging.getLogger("WarmupManager")


class WarmupManager:
    """Background warmup to prefetch stock data with a strict RPM cap."""

    def __init__(self, limit_per_min: int = 300):
        self.limit_per_min = limit_per_min
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="WarmupManager")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _rate_wait(self) -> None:
        while not self._stop_event.is_set():
            if StockDataClient.get_requests_per_minute() < self.limit_per_min:
                return
            time.sleep(1)

    def _call(self, fn, *args, **kwargs):
        self._rate_wait()
        if self._stop_event.is_set():
            return None
        try:
            return fn(*args, **kwargs)
        except Exception:
            return None

    def _build_queue(self) -> List[str]:
        cfg = ConfigManager()
        hot = [t.upper() for t in (cfg.hot_list or [])]
        wl = cfg.watchlist or {}
        stocks = wl.get("yahoo", []) or wl.get("stocks", []) or []
        stocks = [t.upper() for t in stocks]

        # Preserve order: hot list first, then rest of watchlist
        seen = set()
        queue = []
        for t in hot + stocks:
            if t and t not in seen:
                seen.add(t)
                queue.append(t)
        return queue

    def _run(self) -> None:
        queue = self._build_queue()
        if not queue:
            return

        logger.info(f"Warmup: {len(queue)} tickers queued (limit {self.limit_per_min}/min)")

        for ticker in queue:
            if self._stop_event.is_set():
                break

            # Stocks only for warmup
            is_crypto = ticker.endswith("-USD") or ticker.endswith("-USDC") or ticker.endswith("USDT")
            if is_crypto:
                continue

            # Hot List Market Data (4 calls)
            self._call(StockDataClient.get_insider_trading, ticker)
            self._call(StockDataClient.get_adx, ticker)
            self._call(StockDataClient.get_ratings, ticker)
            self._call(StockDataClient.get_sector_for_ticker, ticker)

        logger.info("Warmup: completed")


_warmup_instance: WarmupManager | None = None
_warmup_lock = threading.Lock()


def get_warmup_manager() -> WarmupManager:
    global _warmup_instance
    with _warmup_lock:
        if _warmup_instance is None:
            _warmup_instance = WarmupManager()
        return _warmup_instance
