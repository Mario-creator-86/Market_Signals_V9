from datetime import timedelta

from core.config import get_bucharest_time
from core.signals import SignalManager


class _Config:
    signal = {"max_live_age_seconds": 900}


def _signal(signal_id: str, age_seconds: int, **extra):
    return {
        "id": signal_id,
        "ticker": "BTC-USD",
        "status": "Open",
        "created_at": (get_bucharest_time() - timedelta(seconds=age_seconds)).isoformat(),
        "signal_score": 90,
        **extra,
    }


def test_cleanup_archives_only_expired_unexecuted_signals(tmp_path):
    manager = SignalManager(tmp_path / "signals.json")
    manager.open_signals = {
        "expired": _signal("expired", 901),
        "fresh": _signal("fresh", 899),
        "paper": _signal("paper", 3600, paper_position_id="paper-1"),
    }

    removed = manager.cleanup_expired_unexecuted_signals(_Config())

    assert removed == 1
    assert set(manager.open_signals) == {"fresh", "paper"}
    archived = next(row for row in manager.closed_signals if row["id"] == "expired")
    assert archived["status"] == "EXPIRED"
    assert archived["outcome_data_valid"] is False
    assert "live window 900s" in archived["close_reason"]
