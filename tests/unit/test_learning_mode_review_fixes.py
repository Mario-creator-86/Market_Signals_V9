"""Learning mode: fixes from the independent review of 2026-11-24 (one test per finding)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import core.agent_manager as am
from core.learning_mode.book import LearningBook
from core.learning_mode.settings import learning_config
from core.learning_mode.worker import LearningWorker

NOW = datetime(2026, 11, 24, 17, 0, tzinfo=timezone.utc)
IDEA = {"side": "SHORT", "confidence": 0.6, "stop_pct": 4.0, "target_pct": 7.0, "hold_hours": 24, "why": "x"}
SCANNER = {"data_age_seconds": 30, "hot_tickers": [
    {"symbol": "SMTC", "price": 86.38, "change_pct": -17.05, "volume": 4_000_000, "source": "yahoo_losers"}]}


def _learning(n, status="OPEN"):
    return [{"ticker": f"T{i}", "strategy_type": "AI_MOVER_SHORT", "status": status, "opened_at": NOW.isoformat()}
            for i in range(n)]


def _worker(tmp_path, *, positions=(), scanner=SCANNER, news=(), should_run=lambda: True, ticker_news=None,
            ask_news=None, config=None):
    asked = []

    def ask_mover(row, context, cfg, **_):
        asked.append(row["ticker"])
        return {"idea": dict(IDEA), "model": "m", "error": "", "answer": {}}

    worker = LearningWorker(
        book=LearningBook(tmp_path), config=lambda: config or {"mode": "paper"}, scanner_state=lambda: scanner,
        news_rows=lambda: list(news), positions=lambda: list(positions), quote=lambda t, s: 240.0,
        ask_mover=ask_mover,
        ask_news=ask_news or (lambda *a, **k: {"ideas": [], "model": "m", "error": ""}),
        usage=lambda: {}, session=lambda now: {"us_regular": True, "label": "US regular session"},
        should_run=should_run, ticker_news=ticker_news or (lambda t: []), clock=lambda: NOW)
    return worker, asked


# C1 - learning trades must not crowd out the other experiments.
def test_learning_mode_holds_at_most_its_share_of_open_positions(tmp_path):
    assert learning_config({})["max_open_positions"] == 15
    worker, asked = _worker(tmp_path, positions=_learning(15))
    result = worker.run_once()
    assert asked == [] and result["status"] == "paused" and "15 of 15" in result["reason"]
    book = LearningBook(tmp_path / "b")
    book.add_idea(dict(IDEA), ticker="SMTC", source="mover", price=86.0, asset_class="US_STOCKS", headlines=[],
                  model="m", now=NOW)
    assert book.candidates({"mode": "paper"}, NOW, open_learning=15) == []
    assert len(book.candidates({"mode": "paper"}, NOW, open_learning=14)) == 1


def test_learning_goes_first_only_every_other_review():
    learning = {"autonomous_source": "learning", "strategy_type": "AI_MOVER_SHORT"}
    manager = object.__new__(am.AgentManager)
    manager._last_review_was_learning = False
    assert manager._learning_turn(learning) == 1
    manager._last_review_was_learning = True
    assert manager._learning_turn(learning) == 0                         # the other sources get the next slot
    assert manager._learning_turn({"autonomous_source": "scanner", "strategy_type": "SCANNER_AI_LONG"}) == 0


# I1 - the worker stops asking when the bot or the autonomous cycle is off, and expired ideas do not count.
def test_the_worker_does_not_ask_while_the_bot_or_the_cycle_is_off(tmp_path):
    worker, asked = _worker(tmp_path, should_run=lambda: False)
    result = worker.run_once()
    assert asked == [] and result["status"] == "off"


def test_expired_ideas_do_not_count_against_the_daily_cap(tmp_path):
    book = LearningBook(tmp_path)
    for i in range(3):
        book.add_idea(dict(IDEA), ticker=f"T{i}", source="mover", price=10.0, asset_class="US_STOCKS",
                      headlines=[], model="m", now=NOW - timedelta(minutes=30))
    assert book.pending_count(NOW, ttl_minutes=4) == 0


# I2 - an idea waits at most two cycles and keeps the time its price was observed.
def test_an_idea_carries_the_time_its_price_was_observed(tmp_path):
    assert learning_config({})["idea_ttl_minutes"] == 4
    book = LearningBook(tmp_path)
    book.add_idea(dict(IDEA), ticker="GLD", source="news", price=240.0, asset_class="US_STOCKS", headlines=[],
                  model="m", now=NOW)
    later = NOW + timedelta(minutes=3)
    unpriced = book.candidates({"mode": "paper"}, later)[0]
    assert unpriced["created_at"] == NOW.astimezone(unpriced_tz(unpriced)).isoformat()
    observed = NOW + timedelta(minutes=2, seconds=50)
    repriced = book.candidates({"mode": "paper"}, later, reprice=lambda t: (241.0, observed))[0]
    assert repriced["entry"] == 241.0
    assert repriced["created_at"] == observed.astimezone(unpriced_tz(repriced)).isoformat()
    assert book.candidates({"mode": "paper"}, NOW + timedelta(minutes=5)) == []


def unpriced_tz(signal):
    return datetime.fromisoformat(signal["created_at"]).tzinfo


# I3 - a stale scanner snapshot is not used.
def test_a_stale_scanner_snapshot_is_not_used(tmp_path):
    stale = dict(SCANNER, data_age_seconds=900)
    worker, asked = _worker(tmp_path, scanner=stale)
    worker.run_once()
    assert asked == []
    assert worker.latest_scanner_price("SMTC") is None
    fresh, _ = _worker(tmp_path / "f")
    price, observed = fresh.latest_scanner_price("SMTC")
    assert price == 86.38 and observed == NOW - timedelta(seconds=30)


# I4 / I5 - a misdated headline and a duplicate story are never shown as fresh.
def test_a_headline_from_the_future_is_set_aside_for_good(tmp_path):
    book = LearningBook(tmp_path)
    ahead = {"id": "a4", "title": "Clock ahead", "published_at": (NOW + timedelta(hours=2)).isoformat(), "impact_score": 90}
    assert book.unseen_headlines([ahead], NOW, max_age_minutes=30, min_impact=35) == []
    later = NOW + timedelta(hours=2, minutes=1)
    assert LearningBook(tmp_path).unseen_headlines([ahead], later, max_age_minutes=30, min_impact=35) == []


def test_the_same_story_from_another_outlet_is_not_asked_again(tmp_path):
    book = LearningBook(tmp_path)
    first = {"id": "a1", "title": "Troops cross the border", "story_fingerprint": "war-1",
             "published_at": (NOW - timedelta(minutes=5)).isoformat(), "impact_score": 80}
    copy_ = dict(first, id="b7", title="Troops have crossed the border")
    marked = dict(first, id="c9", is_duplicate=True, story_fingerprint="other")
    fresh = book.unseen_headlines([first, marked], NOW, max_age_minutes=30, min_impact=35)
    assert [row["id"] for row in fresh] == ["a1"]
    book.note_headlines_seen(fresh, NOW)
    assert book.unseen_headlines([copy_], NOW, max_age_minutes=30, min_impact=35) == []


# I6 - the test suite never starts the real worker.
def test_the_suite_never_starts_the_real_worker():
    assert am.get_learning_worker(lambda: (_ for _ in ()).throw(AssertionError("built a real worker"))) is None


# Minor 3 and 4 by effect: a failing headline lookup or an unreadable answer must not stall the worker.
def test_a_failing_headline_lookup_does_not_block_the_mover(tmp_path):
    def broken(ticker):
        raise RuntimeError("news down")

    worker, asked = _worker(tmp_path, ticker_news=broken)
    worker.run_once()
    assert asked == ["SMTC"]


def test_headlines_stay_unseen_when_opus_did_not_answer(tmp_path):
    fresh = {"id": "a1", "title": "Troops cross the border", "published_at": (NOW - timedelta(minutes=4)).isoformat(),
             "impact_score": 80}
    worker, _ = _worker(tmp_path, news=[fresh], ask_news=lambda *a, **k: {"ideas": [], "model": "m", "error": "timed out"})
    worker.run_once()
    assert [r["id"] for r in worker.book.unseen_headlines([fresh], NOW, max_age_minutes=30, min_impact=35)] == ["a1"]
