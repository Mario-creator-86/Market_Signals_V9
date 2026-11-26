"""Learning mode: the queue of Opus's trades, the signals they become, and the record of their results."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.learning_mode.book import LearningBook, build_learning_signal
from core.learning_mode.settings import learning_config

NOW = datetime(2026, 11, 24, 17, 0, tzinfo=timezone.utc)
CFG = {"mode": "paper"}
SHORT_IDEA = {"side": "SHORT", "confidence": 0.66, "stop_pct": 4.0, "target_pct": 7.0, "hold_hours": 24,
              "why": "Sell-the-news after the guidance cut."}


def _book(tmp_path):
    return LearningBook(tmp_path / "learning_mode")


def _add(book, ticker="SMTC", idea=SHORT_IDEA, source="mover", price=86.38, now=NOW):
    return book.add_idea(dict(idea), ticker=ticker, source=source, price=price, asset_class="US_STOCKS",
                         headlines=[{"title": "Semtech guides lower", "source": "Reuters", "age": "2h"}],
                         model="claude-opus-5-5", now=now)


def test_an_idea_becomes_a_paper_signal_with_opus_levels(tmp_path):
    entry = _add(_book(tmp_path))
    signal = build_learning_signal(entry, learning_config({}), now=NOW + timedelta(minutes=2))
    assert signal["ticker"] == "SMTC" and signal["action"] == "SHORT" and signal["type"] == "SHORT"
    assert signal["strategy_type"] == signal["strategy_id"] == "AI_MOVER_SHORT"
    assert signal["autonomous_source"] == "learning" and signal["signal_mode"] == "AUTONOMOUS_LEARNING"
    assert signal["entry_strategy"] == "learning_mover" and signal["strategy_status"] == "RESEARCH"
    assert signal["entry"] == pytest.approx(86.38)
    assert signal["sl"] == pytest.approx(86.38 * 1.04) and signal["tp"] == pytest.approx(86.38 * 0.93)
    assert signal["risk_reward_ratio"] == pytest.approx(1.75)
    assert signal["time_stop_minutes"] == 24 * 60 and signal["score"] == signal["signal_score"] == 95
    assert signal["learning_size_pct"] == 1.0
    assert signal["learning"]["confidence"] == 0.66 and signal["learning"]["model"] == "claude-opus-5-5"
    assert signal["top_headlines"][0]["title"] == "Semtech guides lower"
    news = build_learning_signal(_add(_book(tmp_path), "GLD", dict(SHORT_IDEA, side="LONG"), "news", 240.0),
                                 learning_config({}), now=NOW)
    assert news["strategy_type"] == "AI_NEWS_LONG" and news["entry_strategy"] == "learning_news"
    assert news["sl"] < 240.0 < news["tp"]


def test_ideas_wait_for_the_cycle_at_most_two_cycles_and_leave_once_reviewed(tmp_path):
    book = _book(tmp_path)
    entry = _add(book)
    assert [s["id"] for s in book.candidates(CFG, NOW + timedelta(minutes=3))] == [entry["id"]]
    assert [s["id"] for s in book.candidates(CFG, NOW + timedelta(minutes=3))] == [entry["id"]]   # not yet reviewed
    book.note_reviewed(entry["id"])
    assert book.candidates(CFG, NOW + timedelta(minutes=3)) == []
    _add(book, "IREN")
    assert book.candidates(CFG, NOW + timedelta(minutes=5)) == []              # stale: never traded late
    assert book.candidates({"mode": "live"}, NOW) == []


def test_a_mover_is_asked_at_most_once_an_hour(tmp_path):
    book = _book(tmp_path)
    assert book.may_ask_mover("SMTC", NOW, 60)
    book.note_mover_asked("SMTC", NOW)
    assert not book.may_ask_mover("smtc", NOW + timedelta(minutes=59), 60)
    assert book.may_ask_mover("SMTC", NOW + timedelta(minutes=61), 60)


def test_a_headline_is_offered_once_even_after_a_restart(tmp_path):
    rows = [{"id": "a1", "title": "Troops cross the border", "published_at": (NOW - timedelta(minutes=6)).isoformat(),
             "impact_score": 80},
            {"id": "a2", "title": "Old story", "published_at": (NOW - timedelta(minutes=45)).isoformat(), "impact_score": 80},
            {"id": "a3", "title": "Minor note", "published_at": (NOW - timedelta(minutes=3)).isoformat(), "impact_score": 10},
            {"id": "a4", "title": "Clock ahead", "published_at": (NOW + timedelta(minutes=30)).isoformat(), "impact_score": 90}]
    book = _book(tmp_path)
    fresh = book.unseen_headlines(rows, NOW, max_age_minutes=30, min_impact=35)
    assert [row["id"] for row in fresh] == ["a1"] and fresh[0]["age_minutes"] == 6
    book.note_headlines_seen(fresh, NOW)
    again = _book(tmp_path)
    assert again.unseen_headlines(rows, NOW, max_age_minutes=30, min_impact=35) == []


def _position(pid, strategy="AI_MOVER_SHORT", status="CLOSED", pnl=3.0, opened=NOW, confidence=0.66):
    return {"position_id": pid, "ticker": "SMTC", "strategy_type": strategy, "direction": strategy.rsplit("_", 1)[1],
            "status": status, "net_pnl": pnl, "margin_used": 100.0, "opened_at": opened.isoformat(),
            "closed_at": (opened + timedelta(hours=3)).isoformat(), "close_reason": "take_profit",
            "decision_context": {"learning": {"confidence": confidence, "source": "mover", "model": "claude-opus-5-5"}}}


def test_closed_trades_are_recorded_once_and_survive_the_300_position_limit(tmp_path):
    book = _book(tmp_path)
    positions = [_position("p1"), _position("p2", pnl=-2.0, confidence=0.8),
                 _position("p3", status="OPEN"), _position("x", strategy="SCANNER_AI_LONG")]
    assert book.sync_closed(positions) == 2
    assert book.sync_closed(positions) == 0
    assert _book(tmp_path).sync_closed(positions[:1]) == 0                # after a restart, still once
    progress = _book(tmp_path).progress(learning_config({}), positions)
    assert progress["closed"] == 2 and progress["target"] == 10_000 and progress["open"] == 1
    assert progress["net_pnl"] == pytest.approx(1.0) and progress["win_rate_pct"] == 50.0
    assert progress["by_source"]["mover"]["closed"] == 2
    assert progress["by_side"]["SHORT"]["closed"] == 2
    assert progress["by_confidence"]["0.55-0.65"]["closed"] == 0
    assert progress["by_confidence"]["0.65-0.75"]["closed"] == 1 and progress["by_confidence"]["0.75+"]["closed"] == 1
    assert progress["checkpoint"] == 0 and progress["recent"][0]["position_id"] in {"p1", "p2"}


def test_trades_opened_today_count_open_and_closed_learning_positions(tmp_path):
    book = _book(tmp_path)
    yesterday = NOW - timedelta(days=1)
    positions = [_position("p1"), _position("p2", status="OPEN"), _position("p3", opened=yesterday),
                 _position("x", strategy="XS_MOMENTUM_LONG")]
    assert book.opened_today(positions, NOW) == 2


def test_an_idea_is_reanchored_to_the_latest_price_or_dropped_when_the_price_ran_away(tmp_path):
    """Seen live on 2026-11-24: SOXL moved 3.2% between Opus's answer and the cycle, and the price check refused it."""
    book = _book(tmp_path)
    near = _add(book, "SOXL", price=32.345)
    far = _add(book, "IREN", price=12.27)
    latest = {"SOXL": 32.90, "IREN": 12.80}                                    # +1.7% and +4.3%
    signals = book.candidates(CFG, NOW + timedelta(minutes=2), reprice=latest.get)
    assert [s["ticker"] for s in signals] == ["SOXL"]
    soxl = signals[0]
    assert soxl["entry"] == pytest.approx(32.90) and soxl["sl"] == pytest.approx(32.90 * 1.04)
    assert soxl["learning"]["opus_price"] == pytest.approx(32.345)
    assert book.candidates(CFG, NOW + timedelta(minutes=2), reprice=latest.get) == signals   # IREN is gone for good
    assert book.pending_count() == 1 and near["id"] != far["id"]
    no_quote = _add(book, "ANF", price=50.0)
    assert [s["ticker"] for s in book.candidates(CFG, NOW + timedelta(minutes=2), reprice=lambda t: None)] == ["SOXL", "ANF"]
    assert no_quote["price"] == 50.0


def test_a_trade_without_a_recorded_confidence_is_not_put_in_a_confidence_band(tmp_path):
    book = _book(tmp_path)
    legacy = _position("old")
    legacy["decision_context"] = {}                      # opened before the position kept Opus's decision
    book.sync_closed([legacy])
    progress = book.progress(learning_config({}))
    assert progress["closed"] == 1
    assert sum(band["closed"] for band in progress["by_confidence"].values()) == 0
