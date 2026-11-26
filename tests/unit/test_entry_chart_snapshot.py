from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from core.capital_manager import CapitalManager
from core.entry_chart_snapshot import capture_entry_chart_snapshot, resolve_entry_chart_path
from core.strategy_v2.context_builder import build_s8_multi_timeframe_context


def _frame(rows: int = 80, freq: str = "30min") -> pd.DataFrame:
    index = pd.date_range("2026-06-01", periods=rows, freq=freq, tz="UTC")
    close = pd.Series([100.0 + (i * 0.18) + (0.4 if i % 5 == 0 else 0.0) for i in range(rows)], index=index)
    return pd.DataFrame({
        "Open": close - 0.08,
        "High": close + 0.35,
        "Low": close - 0.35,
        "Close": close,
        "Volume": [1_000.0 + i * 12.0 for i in range(rows)],
    }, index=index)


def _evidence() -> dict:
    states = {
        "30m": {"available": True, "supertrend_direction": "BULLISH"},
        "1h": {"available": True, "supertrend_direction": "BULLISH"},
        "4h": {"available": True, "supertrend_direction": "BEARISH"},
    }
    return {
        "strategy_id": "S8_SUPERTREND_BOS",
        "market_structure": {"event": "BOS", "direction": "BULLISH", "event_level": 110.2},
        "multi_timeframe": {"primary": "30m", "confirmations": ["1h", "4h"], "aligned": ["1h"], "states": states},
        "sequence": [
            {"detail": "30m SuperTrend flipped bullish 2 bars ago."},
            {"detail": "30m Bullish BOS confirmed after the flip."},
            {"detail": "30m trigger checked against 1h and 4h; 1h aligned."},
        ],
    }


def test_entry_chart_snapshot_is_frozen_svg_with_levels_reasons_and_mtf(tmp_path: Path):
    position = {
        "ticker": "BTC-USD", "direction": "LONG", "entry_price": 112.5,
        "stop_loss_price": 109.0, "take_profit_price": 119.5,
    }
    metadata = capture_entry_chart_snapshot(
        position=position,
        decision_context={"strategy_evidence": _evidence()},
        frame=_frame(),
        source="test",
        output_dir=tmp_path,
    )

    assert metadata["available"] is True
    assert metadata["timeframe"] == metadata["minimum_timeframe"] == "30m"
    assert metadata["bar_count"] == 80
    assert metadata["lookahead_safe"] is True
    assert metadata["url"].startswith("/api/entry-chart/entry_")
    path = resolve_entry_chart_path(metadata["snapshot_id"], output_dir=tmp_path)
    assert path is not None
    svg = path.read_text(encoding="utf-8")
    assert "ENTRY SNAPSHOT" in svg
    assert "WHY THE POSITION OPENED" in svg
    assert "MULTI-TIMEFRAME CHECK" in svg
    assert "30m · TRIGGER" in svg
    assert "1h · CONFIRM" in svg
    assert "only candles available at entry are shown" in svg


def test_entry_chart_snapshot_reports_fetch_failure_without_fake_image(tmp_path: Path):
    with patch("core.entry_chart_snapshot._fetch_entry_frame", return_value=(None, "unavailable")):
        metadata = capture_entry_chart_snapshot(
            position={"ticker": "AAPL", "direction": "SHORT", "entry_price": 200.0},
            decision_context={"strategy_evidence": _evidence()},
            output_dir=tmp_path,
        )
    assert metadata["available"] is False
    assert metadata["capture_status"] == "ohlcv_unavailable"
    assert list(tmp_path.glob("*.svg")) == []


def test_resolver_rejects_path_traversal(tmp_path: Path):
    assert resolve_entry_chart_path("../../capital", output_dir=tmp_path) is None


def test_capital_manager_attaches_snapshot_and_refreshes_explainability():
    manager = object.__new__(CapitalManager)
    manager._lock = threading.RLock()
    manager.positions = [{
        "position_id": "pos_test", "ticker": "BTC-USD", "direction": "LONG",
        "entry_price": 100.0, "stop_loss_price": 98.0, "take_profit_price": 104.0,
        "usd_size": 100.0, "leverage": 1, "status": "OPEN",
        "strategy_type": "S8_SUPERTREND_BOS", "entry_reason": "S8 trigger",
        "decision_context": {"strategy_type": "S8_SUPERTREND_BOS", "strategy_evidence": _evidence()},
    }]
    manager._sync_from_storage = lambda initialize_if_missing=False: None
    manager._save_unlocked = lambda: None
    snapshot = {"available": True, "snapshot_id": "entry_" + "a" * 24, "url": "/api/entry-chart/entry_" + "a" * 24, "timeframe": "30m"}

    with patch("core.trade_audit.record_trade_event"):
        updated = manager.attach_entry_chart_snapshot("pos_test", snapshot)

    assert updated["entry_chart_snapshot"] == snapshot
    assert updated["decision_context"]["entry_chart_snapshot"] == snapshot
    from core.trade_decision import summarize_position_explainability

    assert summarize_position_explainability(updated)["entry_chart_snapshot"] == snapshot


def test_multi_timeframe_builder_analyzes_30m_1h_and_4h_and_promotes_30m_state():
    context = build_s8_multi_timeframe_context({
        "30m": _frame(freq="30min"),
        "1h": _frame(freq="1h"),
        "4h": _frame(freq="4h"),
    })
    assert context["s8_minimum_timeframe"] == "30m"
    assert context["s8_primary_timeframe"] == "30m"
    assert context["s8_confirmation_timeframes"] == ["1h", "4h"]
    assert all(context["s8_timeframes"][timeframe]["available"] for timeframe in ("30m", "1h", "4h"))
    assert context["supertrend_direction"] == context["s8_timeframes"]["30m"]["supertrend_direction"]
    assert context["atr"] > 0
