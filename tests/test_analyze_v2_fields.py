"""Regression test: analyze_ticker_v2() must include all fields the frontend reads."""
import types
from unittest.mock import patch, MagicMock


def _fake_row():
    """Simulated watchlist row with trend data."""
    return {
        "ticker": "TEST",
        "price": 100.0,
        "change_pct": 2.5,
        "asset_type": "stock",
        "trend_1d": "BULL",
        "trend_4h": "BULL",
        "trend_1h": "BEAR",
        "trend_30m": "BULL",
        "trend_5m": "BEAR",
        "rsi": 55.0,
        "macd_hist": 0.02,
        "atr": 1.5,
        "ema20": 98.0,
    }


def _fake_context():
    return {
        "price": 100.0,
        "change_pct": 2.5,
        "rsi": 55.0,
        "macd_hist": 0.02,
        "atr": 1.5,
        "ema20": 98.0,
    }


def _fake_selected():
    return {
        "best_candidate": None,
        "regime": {"label": "MIXED"},
        "best_long": None,
        "best_short": None,
        "reverse_consideration": {},
        "rejected": [],
        "candidates_considered": 0,
    }


@patch("api.actions_service._set_cached_analysis")
@patch("api.actions_service._get_cached_analysis", return_value=None)
@patch("api.actions_service.StrategyPerformanceCache")
@patch("api.actions_service.StrategySelector")
@patch("api.actions_service.build_context", return_value=_fake_context())
@patch("api.actions_service.load_cached_ohlc", return_value=None)
@patch("api.actions_service.infer_asset_class", return_value="US_STOCK")
@patch("api.actions_service._get_watchlist_row", return_value=_fake_row())
@patch("api.actions_service._get_last_signal_for_ticker", return_value=None)
@patch("api.actions_service.get_shared_config")
def test_v2_output_has_all_frontend_fields(
    mock_cfg, mock_last_sig, mock_row, mock_asset, mock_ohlc,
    mock_build_ctx, mock_selector_cls, mock_perf_cache_cls,
    mock_get_cache, mock_set_cache,
):
    # Setup mock config
    cfg = MagicMock()
    cfg.strategy_v2 = {}
    cfg.get_ticker_settings.return_value = {"strategy_notes": "test note"}
    mock_cfg.return_value = cfg

    # Setup selector mock
    selector_inst = MagicMock()
    selector_inst.select.return_value = _fake_selected()
    mock_selector_cls.return_value = selector_inst

    from api.actions_service import analyze_ticker_v2

    result = analyze_ticker_v2("TEST", force_refresh=True, disable_ollama=True)

    # --- All fields the frontend modal reads must be present ---
    required_fields = [
        "ticker", "price", "change_pct", "asset_type",
        "rsi", "macd_hist", "atr",
        "trend_1d", "trend_4h", "trend_1h", "trend_30m", "trend_5m",
        "vwap", "ema20",
        "distance_from_vwap_pct", "distance_from_ema20_pct",
        "best_signal", "mode_scores", "fundamentals",
        "quote", "quote_batch", "ai_analysis",
        "v2", "text", "timestamp",
    ]
    for field in required_fields:
        assert field in result, f"Missing field: {field}"

    # Trend values should come from watchlist row
    assert result["trend_1d"] == "BULL"
    assert result["trend_1h"] == "BEAR"
    assert result["trend_5m"] == "BEAR"

    # Indicators should be populated
    assert result["vwap"] == 100.0
    assert result["ema20"] == 98.0
    assert result["rsi"] == 55.0
    assert result["atr"] == 1.5

    # Distance from EMA20 should be computed
    expected_dist = abs((100.0 - 98.0) / 98.0 * 100)
    assert abs(result["distance_from_ema20_pct"] - expected_dist) < 0.01


def test_v2_output_missing_row_trends_default_to_na():
    """When watchlist row has no trend data, fields default to N/A."""
    empty_row = {"ticker": "EMPTY", "price": 50.0}

    with patch("api.actions_service.get_shared_config") as mock_cfg, \
         patch("api.actions_service._get_last_signal_for_ticker", return_value=None), \
         patch("api.actions_service._get_watchlist_row", return_value=empty_row), \
         patch("api.actions_service.infer_asset_class", return_value="US_STOCK"), \
         patch("api.actions_service.load_cached_ohlc", return_value=None), \
         patch("api.actions_service.build_context", return_value={"price": 50.0, "rsi": 50, "macd_hist": 0, "atr": 0.5, "ema20": 50.0}), \
         patch("api.actions_service.StrategySelector") as mock_sel_cls, \
         patch("api.actions_service.StrategyPerformanceCache"), \
         patch("api.actions_service._get_cached_analysis", return_value=None), \
         patch("api.actions_service._set_cached_analysis"):

        cfg = MagicMock()
        cfg.strategy_v2 = {}
        cfg.get_ticker_settings.return_value = {}
        mock_cfg.return_value = cfg

        sel = MagicMock()
        sel.select.return_value = _fake_selected()
        mock_sel_cls.return_value = sel

        from api.actions_service import analyze_ticker_v2
        result = analyze_ticker_v2("EMPTY", force_refresh=True, disable_ollama=True)

        assert result["trend_1d"] == "N/A"
        assert result["trend_4h"] == "N/A"
        assert result["trend_1h"] == "N/A"
        assert result["trend_30m"] == "N/A"
        assert result["trend_5m"] == "N/A"


def test_v2_output_missing_rsi_stays_null():
    row = {"ticker": "NULLRSI", "price": 50.0}

    with patch("api.actions_service.get_shared_config") as mock_cfg, \
         patch("api.actions_service._get_last_signal_for_ticker", return_value=None), \
         patch("api.actions_service._get_watchlist_row", return_value=row), \
         patch("api.actions_service.infer_asset_class", return_value="US_STOCK"), \
         patch("api.actions_service.load_cached_ohlc", return_value=None), \
         patch("api.actions_service.build_context", return_value={"price": 50.0, "macd_hist": 0, "atr": 0.5, "ema20": 50.0}), \
         patch("api.actions_service.StrategySelector") as mock_sel_cls, \
         patch("api.actions_service.StrategyPerformanceCache"), \
         patch("api.actions_service._get_cached_analysis", return_value=None), \
         patch("api.actions_service._set_cached_analysis"):

        cfg = MagicMock()
        cfg.strategy_v2 = {}
        cfg.get_ticker_settings.return_value = {}
        mock_cfg.return_value = cfg

        sel = MagicMock()
        sel.select.return_value = _fake_selected()
        mock_sel_cls.return_value = sel

        from api.actions_service import analyze_ticker_v2
        result = analyze_ticker_v2("NULLRSI", force_refresh=True, disable_ollama=True)

        assert result["rsi"] is None
