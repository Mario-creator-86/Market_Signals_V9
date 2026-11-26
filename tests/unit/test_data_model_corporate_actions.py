from datetime import datetime, timezone

import pandas as pd

from core.data_model.entities import CorporateAction
from core.data_model.ingest import YFCorporateActionAdapter, actions_df_to_records
from core.data_model.store import HistoricalStore


def _actions_df():
    # yfinance .actions shape: DatetimeIndex + Dividends + "Stock Splits"
    idx = pd.to_datetime(["2026-01-15", "2026-03-15", "2026-05-15"])
    return pd.DataFrame(
        {"Dividends": [0.50, 0.0, 0.0], "Stock Splits": [0.0, 0.0, 2.0]},
        index=idx,
    )  # 2026-01-15 dividend; 2026-05-15 2-for-1 split; 2026-03-15 no action


def test_actions_df_to_records_dividend_and_split():
    recs = actions_df_to_records(_actions_df())
    assert len(recs) == 2  # the no-action row is skipped
    div = [r for r in recs if r["type"] == "DIVIDEND"][0]
    split = [r for r in recs if r["type"] == "SPLIT"][0]
    assert div["amount"] == 0.50 and div["ratio"] is None
    assert split["ratio"] == 2.0 and split["amount"] is None
    assert all(r["ex_date"].endswith("+00:00") for r in recs)


def test_actions_df_to_records_empty_and_none():
    assert actions_df_to_records(None) == []
    assert actions_df_to_records(pd.DataFrame()) == []


def test_yf_corporate_action_adapter_to_entity():
    adapter = YFCorporateActionAdapter("AAPL")
    recs = actions_df_to_records(_actions_df())
    ca = adapter.to_entity(recs[0])
    assert isinstance(ca, CorporateAction)
    assert ca.symbol.ticker == "AAPL"
    assert ca.type in ("DIVIDEND", "SPLIT")
    assert ca.pit is not None
    assert ca.pit.available_from is not None
    assert ca.pit.version == 1


def test_yf_corporate_action_adapter_skips_invalid():
    adapter = YFCorporateActionAdapter("AAPL")
    assert adapter.to_entity({"no_ex_date": 1}) is None
    assert adapter.to_entity("not a dict") is None


def test_yf_corporate_action_adapter_is_provider_record_adapter():
    adapter = YFCorporateActionAdapter("AAPL")
    assert adapter.entity_type == "CorporateAction"
    assert adapter.provider == "yfinance"
    assert callable(adapter.to_entity)


from core.data_model.ingest import ingest_via


def test_actions_as_of_returns_only_past_actions(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    adapter = YFCorporateActionAdapter("AAPL")
    ingest_via(adapter, store, actions_df_to_records(_actions_df()))
    # actions: 2026-01-15 dividend, 2026-05-15 split
    T_before_split = datetime.fromisoformat("2026-03-01T00:00:00+00:00")
    res = store.actions_as_of("AAPL", T_before_split)
    assert len(res) == 1  # only the 2026-01-15 dividend; split is future
    assert res[0]["type"] == "DIVIDEND"
    T_after_split = datetime.fromisoformat("2026-06-01T00:00:00+00:00")
    res2 = store.actions_as_of("AAPL", T_after_split)
    assert len(res2) == 2
    assert [r["type"] for r in res2] == ["DIVIDEND", "SPLIT"]  # ascending by ex_date


def test_actions_as_of_empty_when_none(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    assert store.actions_as_of("AAPL", datetime.fromisoformat("2026-06-01T00:00:00+00:00")) == []


from core.data_model.ingest import YFOHLCVAdapter, df_to_bar_rows


def _sample_bars():
    idx = pd.to_datetime(["2026-01-05T14:30:00"])
    return pd.DataFrame(
        {"Open": [99], "High": [101], "Low": [98], "Close": [100], "Volume": [1000]},
        index=idx,
    )


def test_yf_ohlcv_adapter_adjusted_default_true():
    adapter = YFOHLCVAdapter("AAPL")
    bar = adapter.to_entity(df_to_bar_rows(_sample_bars())[0])
    assert bar.adjusted is True  # 1C convention: adjusted is the default


def test_yf_ohlcv_adapter_adjusted_false_when_requested():
    adapter = YFOHLCVAdapter("AAPL", adjusted=False)
    bar = adapter.to_entity(df_to_bar_rows(_sample_bars())[0])
    assert bar.adjusted is False