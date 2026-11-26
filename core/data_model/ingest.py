"""V9 ingestion seam — adapt existing V7 provider output to canonical §27
entities and append to the point-in-time store.

Adapters WRAP the working V7 clients (§7/§23): they never call providers and
never see credentials (§106). Callers fetch via the existing client, then feed
raw records through the adapter. The store owns persistence + point-in-time.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Protocol

from core.data_model.entities import CorporateAction, OHLCVBar, Symbol, get_exchange
from core.data_model.pit import PointInTime
from core.data_model.store import HistoricalStore


class ProviderRecordAdapter(Protocol):
    """A pure transform from one raw provider record to one canonical §27 entity.

    Adapters carry no network/credential state — they declare their entity_type
    and provider and implement to_entity(raw) -> entity | None (None = skip)."""
    entity_type: str
    provider: str

    def to_entity(self, raw: Any) -> Optional[Any]: ...


def df_to_bar_rows(df: Any) -> list:
    """Normalize a yfinance history DataFrame (DatetimeIndex + Open/High/Low/
    Close/Volume columns, possibly tz-stripped) into a list of plain bar dicts:
    {"ts": iso-UTC, "open","high","low","close","volume": float}. None/empty -> []."""
    if df is None:
        return []
    try:
        if df.empty:
            return []
    except Exception:
        return []
    import pandas as pd
    df = df.copy()  # avoid mutating the caller's DataFrame
    if isinstance(df.index, pd.DatetimeIndex):
        df.index = df.index.tz_localize("UTC") if df.index.tz is None else df.index.tz_convert("UTC")
    rows = []
    for ts, row in df.iterrows():
        rows.append({
            "ts": pd.Timestamp(ts).isoformat(),
            "open": float(row.get("Open", row.get("open", 0.0)) or 0.0),
            "high": float(row.get("High", row.get("high", 0.0)) or 0.0),
            "low": float(row.get("Low", row.get("low", 0.0)) or 0.0),
            "close": float(row.get("Close", row.get("close", 0.0)) or 0.0),
            "volume": float(row.get("Volume", row.get("volume", 0.0)) or 0.0),
        })
    return rows


class YFOHLCVAdapter:
    """Proof adapter: yfinance bar dict -> OHLCVBar with an explicit PointInTime
    (available_from = bar timestamp; the bar-close availability refinement is a
    Phase 7 backtest concern, documented in the Phase 1B design)."""
    entity_type = "OHLCVBar"
    provider = "yfinance"

    def __init__(
        self,
        symbol: str,
        *,
        interval: str = "5m",
        exchange_mic: str = "XNYS",
        fetched_at: Optional[datetime] = None,
        adjusted: bool = True,
    ):
        self.symbol = str(symbol or "").upper().strip()
        self.interval = interval
        self.exchange_mic = exchange_mic
        self.fetched_at = fetched_at or datetime.now(timezone.utc)
        self.adjusted = adjusted

    def to_entity(self, raw: Any) -> Optional[OHLCVBar]:
        if not isinstance(raw, dict) or "ts" not in raw:
            return None
        try:
            ts = datetime.fromisoformat(raw["ts"])
        except (ValueError, TypeError):
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ex = get_exchange(self.exchange_mic)
        if ex is None:
            return None
        sym = Symbol(ticker=self.symbol, exchange=ex, asset_class=ex.asset_class, currency=ex.currency)
        pit = PointInTime(event_time=ts, available_from=ts, fetched_at=self.fetched_at, version=1)
        return OHLCVBar(
            symbol=sym,
            timestamp=ts,
            open=float(raw.get("open", 0.0)),
            high=float(raw.get("high", 0.0)),
            low=float(raw.get("low", 0.0)),
            close=float(raw.get("close", 0.0)),
            volume=float(raw.get("volume", 0.0)),
            resolution=self.interval,
            adjusted=self.adjusted,
            pit=pit,
        )


def ingest_via(
    adapter: ProviderRecordAdapter,
    store: HistoricalStore,
    raw_batch: Iterable[Any],
    *,
    symbol: Optional[str] = None,
) -> int:
    """Run each raw record through the adapter and append the resulting entity to
    the store. Returns the number appended (skips raw records the adapter maps to None)."""
    count = 0
    for raw in raw_batch:
        entity = adapter.to_entity(raw)
        if entity is not None:
            store.append(entity, provider=adapter.provider, symbol=symbol)
            count += 1
    return count


def actions_df_to_records(df: Any) -> list:
    """Normalize a yfinance .actions DataFrame (DatetimeIndex + 'Dividends' +
    'Stock Splits' columns) into action dicts: {"ex_date": iso-UTC, "type":
    "DIVIDEND"|"SPLIT", "amount", "ratio"}. Rows with no action are skipped.
    None/empty -> []. (§31 corporate-action capture.)"""
    if df is None:
        return []
    try:
        if df.empty:
            return []
    except Exception:
        return []
    import pandas as pd
    df = df.copy()  # do not mutate the caller's DataFrame
    if isinstance(df.index, pd.DatetimeIndex):
        df.index = df.index.tz_localize("UTC") if df.index.tz is None else df.index.tz_convert("UTC")
    out = []
    for ts, row in df.iterrows():
        ts_iso = pd.Timestamp(ts).isoformat()
        div = float(row.get("Dividends", 0.0) or 0.0)
        split = float(row.get("Stock Splits", 0.0) or 0.0)
        if div != 0.0:
            out.append({"ex_date": ts_iso, "type": "DIVIDEND", "amount": div, "ratio": None})
        if split != 0.0:
            out.append({"ex_date": ts_iso, "type": "SPLIT", "ratio": split, "amount": None})
    return out


class YFCorporateActionAdapter:
    """Proof adapter (§31): a yfinance action dict -> CorporateAction with an
    explicit PointInTime (event_time=available_from=ex_date). Wrap-not-replace
    (§23); never sees credentials (§106)."""
    entity_type = "CorporateAction"
    provider = "yfinance"

    def __init__(self, symbol: str, *, exchange_mic: str = "XNYS", fetched_at: Optional[datetime] = None):
        self.symbol = str(symbol or "").upper().strip()
        self.exchange_mic = exchange_mic
        self.fetched_at = fetched_at or datetime.now(timezone.utc)

    def to_entity(self, raw: Any) -> Optional[CorporateAction]:
        if not isinstance(raw, dict) or "ex_date" not in raw or "type" not in raw:
            return None
        try:
            ex_date = datetime.fromisoformat(raw["ex_date"])
        except (ValueError, TypeError):
            return None
        if ex_date.tzinfo is None:
            ex_date = ex_date.replace(tzinfo=timezone.utc)
        ex = get_exchange(self.exchange_mic)
        if ex is None:
            return None
        sym = Symbol(ticker=self.symbol, exchange=ex, asset_class=ex.asset_class, currency=ex.currency)
        pit = PointInTime(event_time=ex_date, available_from=ex_date, fetched_at=self.fetched_at, version=1)
        return CorporateAction(
            symbol=sym,
            ex_date=ex_date,
            type=raw["type"],
            ratio=raw.get("ratio"),
            amount=raw.get("amount"),
            from_ticker=raw.get("from_ticker"),
            to_ticker=raw.get("to_ticker"),
            pit=pit,
        )