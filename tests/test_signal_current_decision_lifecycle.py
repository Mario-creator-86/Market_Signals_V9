from pathlib import Path

from core.signals import SignalManager


def _manager(tmp_path: Path) -> SignalManager:
    return SignalManager(path=tmp_path / "signals.json")


def _autonomous(signal_id: str, action: str, created_at: str) -> dict:
    return {
        "id": signal_id,
        "ticker": "UTK-USD",
        "action": action,
        "status": "Open",
        "source": "autonomous_monitor",
        "created_at": created_at,
    }


def test_dedupe_keeps_newest_autonomous_direction(tmp_path):
    manager = _manager(tmp_path)
    manager.open_signals = {
        "old-long": _autonomous("old-long", "BUY", "2026-07-18T08:00:00+00:00"),
        "new-short": _autonomous("new-short", "SHORT", "2026-07-18T09:00:00+00:00"),
    }

    removed = manager.dedupe_open_signals()

    assert removed == 1
    assert list(manager.open_signals) == ["new-short"]


def test_supersede_archives_unexecuted_but_preserves_paper_link(tmp_path):
    manager = _manager(tmp_path)
    manager.open_signals = {
        "old": _autonomous("old", "BUY", "2026-07-18T08:00:00+00:00"),
        "paper": {
            **_autonomous("paper", "SHORT", "2026-07-18T08:30:00+00:00"),
            "paper_position_id": "pos-1",
            "paper_status": "OPEN",
        },
    }

    removed = manager.supersede_unexecuted_autonomous_signals("UTK-USD", "fresh")

    assert removed == 1
    assert "old" not in manager.open_signals
    assert "paper" in manager.open_signals
    assert manager.closed_signals[0]["status"] == "SUPERSEDED"
    assert manager.closed_signals[0]["superseded_by_signal_id"] == "fresh"
