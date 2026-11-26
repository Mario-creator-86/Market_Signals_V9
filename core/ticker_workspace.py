import json
import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from core.config import DATA_DIR
from core.sqlite_store import get_sqlite_store

logger = logging.getLogger("TickerWorkspace")

_DATA_PATH = DATA_DIR / "ticker_workspace.json"
_SQLITE_PREFIX = "ticker_workspace::"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_ticker(value: Any) -> str:
    return str(value or "").upper().strip()


class TickerWorkspaceStore:
    def __init__(self, *, max_items: int = 800, ttl_seconds: int = 1800):
        self.max_items = max(50, int(max_items or 800))
        self.ttl_seconds = max(60, int(ttl_seconds or 1800))
        self._lock = threading.RLock()
        self._cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._version = 1
        self._store = None
        try:
            self._store = get_sqlite_store()
        except Exception as e:
            logger.debug(f"SQLite unavailable for ticker workspace: {e}")
        self._load_disk_snapshot()

    def _empty_workspace(self, ticker: str) -> Dict[str, Any]:
        return {
            "ticker": ticker,
            "updated_at": _utc_iso(),
            "market_data": {},
            "news": {},
            "calendar": {},
            "premarket": {},
            "pre_earnings": {},
            "backtest_lite": {},
            "quality_flags": {},
            "workspace_version": self._version,
        }

    def _touch_locked(self, ticker: str) -> None:
        if ticker in self._cache:
            self._cache.move_to_end(ticker)

    def _prune_locked(self) -> None:
        now_ts = time.time()
        stale_keys = []
        for key, payload in self._cache.items():
            updated = payload.get("updated_at") or ""
            try:
                ts = datetime.fromisoformat(str(updated).replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = 0.0
            if ts and (now_ts - ts) > self.ttl_seconds:
                stale_keys.append(key)
        for key in stale_keys:
            self._cache.pop(key, None)
        while len(self._cache) > self.max_items:
            self._cache.popitem(last=False)

    def _load_disk_snapshot(self) -> None:
        if not _DATA_PATH.exists():
            return
        try:
            payload = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        entries = payload.get("items", {}) if isinstance(payload.get("items"), dict) else {}
        with self._lock:
            for raw_ticker, row in entries.items():
                ticker = _normalize_ticker(raw_ticker)
                if not ticker or not isinstance(row, dict):
                    continue
                self._cache[ticker] = row
            self._version = int(payload.get("version") or self._version)
            self._prune_locked()

    def _persist_locked(self) -> None:
        try:
            _DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": self._version,
                "saved_at": _utc_iso(),
                "items": dict(self._cache),
            }
            _DATA_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.debug(f"Ticker workspace disk persist failed: {e}")

    def get_workspace(self, ticker: str) -> Dict[str, Any]:
        symbol = _normalize_ticker(ticker)
        if not symbol:
            return self._empty_workspace("")
        with self._lock:
            row = self._cache.get(symbol)
            if isinstance(row, dict):
                self._touch_locked(symbol)
                return dict(row)
        # Try sqlite fallback for cold start
        if self._store is not None:
            try:
                row = self._store.get_json(f"{_SQLITE_PREFIX}{symbol}", None)
                if isinstance(row, dict):
                    with self._lock:
                        self._cache[symbol] = row
                        self._touch_locked(symbol)
                    return dict(row)
            except Exception:
                pass
        empty = self._empty_workspace(symbol)
        with self._lock:
            self._cache[symbol] = dict(empty)
            self._touch_locked(symbol)
            self._prune_locked()
        return empty

    def upsert_section(self, ticker: str, section: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        symbol = _normalize_ticker(ticker)
        sec = str(section or "").strip().lower()
        if not symbol or not sec:
            raise ValueError("ticker and section are required")
        with self._lock:
            row = self._cache.get(symbol) or self._empty_workspace(symbol)
            row = dict(row)
            row[sec] = dict(payload or {})
            row["updated_at"] = _utc_iso()
            row["workspace_version"] = self._version
            self._cache[symbol] = row
            self._touch_locked(symbol)
            self._prune_locked()
            if self._store is not None:
                try:
                    self._store.set_json(f"{_SQLITE_PREFIX}{symbol}", row)
                except Exception:
                    pass
            self._persist_locked()
            return dict(row)

    def upsert_market_data(self, ticker: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.upsert_section(ticker, "market_data", payload)

    def get_market_data(self, ticker: str) -> Dict[str, Any]:
        row = self.get_workspace(ticker)
        data = row.get("market_data", {})
        return dict(data) if isinstance(data, dict) else {}

    def invalidate_tickers(self, tickers: Iterable[str]) -> None:
        symbols = [_normalize_ticker(t) for t in (tickers or [])]
        with self._lock:
            for symbol in symbols:
                if symbol:
                    self._cache.pop(symbol, None)
            self._version += 1
            self._persist_locked()

    def bump_version(self) -> int:
        with self._lock:
            self._version += 1
            self._persist_locked()
            return self._version

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            self._prune_locked()
            return {
                "version": self._version,
                "size": len(self._cache),
                "saved_at": _utc_iso(),
            }


_WORKSPACE_SINGLETON: Optional[TickerWorkspaceStore] = None
_WORKSPACE_LOCK = threading.Lock()


def get_ticker_workspace_store() -> TickerWorkspaceStore:
    global _WORKSPACE_SINGLETON
    with _WORKSPACE_LOCK:
        if _WORKSPACE_SINGLETON is None:
            _WORKSPACE_SINGLETON = TickerWorkspaceStore()
        return _WORKSPACE_SINGLETON
