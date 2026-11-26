from __future__ import annotations

import pandas as pd

from core.volume_structure import VolumeStructureEngine, analyze_volume_structure, precompute_volume_structure


def _acceptance_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [100, 100, 100, 100, 101, 102],
            "high": [101, 101, 101, 101, 103, 104],
            "low": [99, 99, 99, 99, 100, 101],
            "close": [100, 100, 100, 101, 102, 103],
            "volume": [100, 100, 100, 100, 160, 170],
        },
        index=pd.date_range("2026-01-01", periods=6, freq="min", tz="UTC"),
    )


def test_price_acceptance_requires_the_second_closed_candle_beyond_the_level():
    states = precompute_volume_structure(_acceptance_frame())
    assert bool(states.iloc[-2]["price_acceptance"]) is False
    assert bool(states.iloc[-1]["price_acceptance"]) is True
    assert states.iloc[-1]["price_acceptance_direction"] == "BULLISH"
    assert states.iloc[-1]["volume_event"] == "PRICE_ACCEPTANCE"


def test_single_outside_close_is_not_accepted_price():
    engine = VolumeStructureEngine()
    frame = _acceptance_frame().iloc[:-1]
    states = [
        engine.update(row.open, row.high, row.low, row.close, row.volume, timestamp=index)
        for index, row in frame.iterrows()
    ]
    assert all(not state["price_acceptance"] for state in states)


def test_analysis_exposes_causal_volume_annotations():
    analysis = analyze_volume_structure(_acceptance_frame())
    assert analysis["price_acceptance_direction"] == "BULLISH"
    assert any(item["kind"] == "VOLUME_EXPANSION" for item in analysis["volume_events"])
    assert any(item["kind"] == "PRICE_ACCEPTANCE" for item in analysis["volume_events"])
