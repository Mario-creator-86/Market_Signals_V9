"""Regression: ORB_FVG backtest fetch path must not ImportError.

Bug (C3): ``run_orb_fvg_backtest`` imported a non-existent ``fetch_stock_bars_yf``
from ``core.quick_backtester`` when called without ``df_1min`` — the only way both
production callers (``api/actions_service.py`` and ``core/engine.py``) invoke it.
The ImportError was swallowed into ``results['error']``, so ORB_FVG was dead in
production. The fetch path now uses ``QuickBacktester.fetch_backtest_data``.
"""
from unittest.mock import patch

import numpy as np
import pandas as pd

from core.orb_fvg_backtester import run_orb_fvg_backtest


def _synthetic_intraday_df(days: int = 5) -> pd.DataFrame:
    """Build `days` weekdays of 1-min OHLCV bars in ET market hours (9:30-15:45)."""
    import pytz

    et = pytz.timezone("America/New_York")
    rng = np.random.default_rng(42)
    rows = []
    price = 100.0
    # 2024-01-08 is a Monday -> 5 consecutive business days
    base = pd.Timestamp("2024-01-08 09:30:00", tz=et)
    for d in range(days):
        day = base + pd.Timedelta(days=d)
        minutes = pd.date_range(day, periods=375, freq="1min")
        for ts in minutes:
            ret = rng.normal(0, 0.0008)
            open_ = price
            close = max(open_ * (1 + ret), 1.0)
            high = max(open_, close) * (1 + abs(rng.normal(0, 0.0003)))
            low = min(open_, close) * (1 - abs(rng.normal(0, 0.0003)))
            vol = float(rng.integers(1000, 10000))
            rows.append((ts, open_, high, low, close, vol))
            price = close
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.set_index("timestamp")
    df.index.name = None
    return df


def test_orb_fvg_fetch_path_runs_full_pipeline_without_importerror():
    """The fetch branch must call QuickBacktester.fetch_backtest_data (not the old
    missing function) and the full pipeline must run without an import/fetch error."""
    synthetic = _synthetic_intraday_df()
    with patch("core.quick_backtester.QuickBacktester") as MockQB:
        instance = MockQB.return_value
        instance.fetch_backtest_data.return_value = (synthetic, [])
        result = run_orb_fvg_backtest(ticker="AAPL", days=5, entry_type="BOTH")

    # The new fetch path was invoked (proves wiring changed away from the missing import).
    assert instance.fetch_backtest_data.called
    args, kwargs = instance.fetch_backtest_data.call_args
    # ticker passed positionally as first arg
    assert args[0] == "AAPL"
    assert kwargs.get("is_crypto") is False

    # No import / fetch error — the function ran the full pipeline.
    assert "error" not in result, f"unexpected error: {result.get('error')}"
    assert result["ticker"] == "AAPL"
    assert result["trade_mode"] == "ORB_FVG"
    assert "trades" in result
    assert "win_rate" in result


def test_orb_fvg_insufficient_data_returns_clean_error_not_importerror():
    """When fetch returns too few rows, we get a clean 'Insufficient data' error,
    NOT the old ImportError referencing the missing function."""
    with patch("core.quick_backtester.QuickBacktester") as MockQB:
        instance = MockQB.return_value
        instance.fetch_backtest_data.return_value = (None, ["no data"])
        result = run_orb_fvg_backtest(ticker="AAPL", days=5, entry_type="BOTH")

    assert instance.fetch_backtest_data.called
    assert result.get("error") == "Insufficient data for AAPL (1min)."
    # specifically NOT an ImportError message from the old missing function
    assert "fetch_stock_bars_yf" not in str(result.get("error", ""))
    assert "cannot import" not in str(result.get("error", "")).lower()