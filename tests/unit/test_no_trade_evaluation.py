"""Phase 13 unit tests — §90 no-trade rejection-quality measurement."""
from datetime import datetime, timedelta, timezone

from core.decision_ledger.no_trade_eval import evaluate_no_trade_decisions, is_no_trade_entry
from core.decision_ledger.types import SOURCE_REPLAY, SOURCE_SIGNAL_PATH, LedgerEntry


def _entry(decision_id, ticker, *, action="NO_TRADE", status="rejected",
           created_at="2026-08-04T21:00:00+00:00", source=SOURCE_SIGNAL_PATH):
    return LedgerEntry(
        decision_id=decision_id, created_at=created_at, ticker=ticker, action=action,
        horizon="SWING", source=source, status=status, strategy="MOMENTUM",
        ai={}, ai_proposal={}, validation={}, critic={}, risk={}, capital_allowed={},
        executed={},
    )


def _bars(closes):
    start = datetime(2026, 8, 4, 20, 0, tzinfo=timezone.utc)
    return [
        {
            "available_from": (start + timedelta(days=index)).isoformat(),
            "close": close,
        }
        for index, close in enumerate(closes)
    ]


def test_partition_of_rejections():
    bars = _bars([100.0, 100.5, 110.0, 111.0, 112.0, 113.0, 114.0])  # +14% after rejection
    entries = [_entry("dec_win", "AAPL"), _entry("dec_dup", "AAPL")]
    report = evaluate_no_trade_decisions(entries, {"AAPL": bars}, threshold_pct=1.0)
    assert report["total_recorded"] == 2
    assert report["incorrectly_rejected_profitable"] == 2
    assert report["correctly_rejected"] == 0
    assert report["rejection_precision"] == 0.0


def test_correctly_rejected_when_move_adverse():
    bars = _bars([100.0, 100.5, 90.0, 89.0, 88.0, 87.0, 86.0])  # -14% dodged
    report = evaluate_no_trade_decisions([_entry("dec_dodge", "AAPL")], {"AAPL": bars})
    assert report["correctly_rejected"] == 1
    assert report["incorrectly_rejected_profitable"] == 0
    assert report["rejection_precision"] == 1.0


def test_inconclusive_without_future_bars_is_not_guessed():
    bars = _bars([100.0, 100.5])  # baseline exists, no horizon future bars
    report = evaluate_no_trade_decisions([_entry("dec_thin", "AAPL")], {"AAPL": bars})
    assert report["inconclusive"] == 1
    assert report["evaluated"] == 0
    assert report["rejection_precision"] is None


def test_executed_decisions_are_excluded_and_replays_never_count():
    bars = _bars([100.0, 100.5, 110.0, 111.0, 112.0, 113.0, 114.0])
    executed = LedgerEntry(
        decision_id="dec_exec", created_at="2026-08-04T21:00:00+00:00", ticker="AAPL",
        action="OPEN_LONG", horizon="SWING", source=SOURCE_SIGNAL_PATH, status="accepted",
        strategy="MOMENTUM", ai={}, ai_proposal={}, validation={}, critic={},
        risk={}, capital_allowed={}, executed={},
    )
    replay = _entry("dec_replay_x", "AAPL", source=SOURCE_REPLAY)
    assert not is_no_trade_entry(executed)
    assert not is_no_trade_entry(replay)
    report = evaluate_no_trade_decisions([executed, replay], {"AAPL": bars})
    assert report["total_recorded"] == 0


def test_flat_move_within_threshold_is_inconclusive_class():
    bars = _bars([100.0, 100.2, 100.4, 100.3, 100.5, 100.4, 100.6])  # +0.6% < 1%
    report = evaluate_no_trade_decisions([_entry("dec_flat", "AAPL")], {"AAPL": bars})
    assert report["inconclusive"] == 1
