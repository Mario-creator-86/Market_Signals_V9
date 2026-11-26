from datetime import datetime, timezone

import pandas as pd

from core.data_model.ingest import (
    ProviderRecordAdapter,
    YFOHLCVAdapter,
    df_to_bar_rows,
    ingest_via,
)
from core.data_model.store import HistoricalStore


def _sample_df():
    idx = pd.to_datetime(["2026-01-05T14:30:00", "2026-01-05T14:35:00", "2026-01-05T14:40:00"])
    return pd.DataFrame(
        {"Open": [99, 100, 101], "High": [101, 102, 103], "Low": [98, 99, 100],
         "Close": [100, 101, 102], "Volume": [1000, 1100, 1200]},
        index=idx,
    )


def test_df_to_bar_rows_three_rows_utc():
    rows = df_to_bar_rows(_sample_df())
    assert len(rows) == 3
    assert rows[0]["close"] == 100.0
    assert rows[0]["ts"].endswith("+00:00")  # naive index localized to UTC
    assert rows[2]["volume"] == 1200.0


def test_df_to_bar_rows_empty_and_none():
    assert df_to_bar_rows(None) == []
    assert df_to_bar_rows(pd.DataFrame()) == []


def test_yf_adapter_to_entity_ohlcvbar():
    adapter = YFOHLCVAdapter("AAPL")
    rows = df_to_bar_rows(_sample_df())
    bar = adapter.to_entity(rows[0])
    assert bar is not None
    assert bar.symbol.ticker == "AAPL"
    assert bar.close == 100.0
    assert bar.resolution == "5m"
    assert bar.pit is not None
    assert bar.pit.available_from is not None
    assert bar.pit.version == 1


def test_yf_adapter_skips_invalid_raw():
    adapter = YFOHLCVAdapter("AAPL")
    assert adapter.to_entity({"no_ts": 1}) is None
    assert adapter.to_entity("not a dict") is None


def test_yf_adapter_is_provider_record_adapter():
    # structural typing: YFOHLCVAdapter satisfies the Protocol attributes
    adapter = YFOHLCVAdapter("AAPL")
    assert adapter.entity_type == "OHLCVBar"
    assert adapter.provider == "yfinance"
    assert callable(adapter.to_entity)


def test_ingest_via_counts_and_appends(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    adapter = YFOHLCVAdapter("AAPL")
    rows = df_to_bar_rows(_sample_df())
    n = ingest_via(adapter, store, rows)
    assert n == 3
    assert len(store.query("OHLCVBar", "AAPL")) == 3


def test_ingest_via_skips_none_entities(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    adapter = YFOHLCVAdapter("AAPL")
    mixed = [{"ts": "2026-01-05T14:30:00+00:00", "open": 1, "high": 2, "low": 0, "close": 1, "volume": 10},
             {"no_ts": 1}]
    n = ingest_via(adapter, store, mixed)
    assert n == 1


from core.data_model import (
    HistoricalStore as _HS,
    YFOHLCVAdapter as _YFA,
    df_to_bar_rows as _DTBR,
    ingest_via as _IV,
    UNAVAILABLE as _UN,
)


def test_public_exports_importable():
    import core.data_model as dm
    for name in [
        "HistoricalStore", "get_historical_store", "ProviderRecordAdapter",
        "ingest_via", "YFOHLCVAdapter", "df_to_bar_rows",
    ]:
        assert hasattr(dm, name), f"missing export: {name}"


def test_end_to_end_ingest_then_as_of(tmp_path):
    from core.data_model import HistoricalStore, YFOHLCVAdapter, df_to_bar_rows, ingest_via, UNAVAILABLE
    store = HistoricalStore(path=tmp_path / "t.db")
    adapter = YFOHLCVAdapter("AAPL")
    ingest_via(adapter, store, df_to_bar_rows(_sample_df()))
    t_before = datetime.fromisoformat("2026-01-05T14:00:00+00:00")
    t_mid = datetime.fromisoformat("2026-01-05T14:37:00+00:00")
    t_after = datetime.fromisoformat("2026-01-05T15:00:00+00:00")
    assert store.as_of("OHLCVBar", "AAPL", t_before) is UNAVAILABLE
    mid = store.as_of("OHLCVBar", "AAPL", t_mid)
    assert mid is not UNAVAILABLE
    assert mid["close"] == 101  # 14:35 bar is the newest <= 14:37
    after = store.as_of("OHLCVBar", "AAPL", t_after)
    assert after is not UNAVAILABLE
    assert after["close"] == 102  # 14:40 bar