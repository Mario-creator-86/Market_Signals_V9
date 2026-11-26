"""Learning mode worker: what it asks Opus about, when it holds back, and what it queues."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.learning_mode.book import LearningBook
from core.learning_mode.worker import LearningWorker

NOW = datetime(2026, 11, 24, 17, 0, tzinfo=timezone.utc)
PAPER = {"mode": "paper", "learning_mode": {"enabled": True}}
IDEA = {"side": "SHORT", "confidence": 0.7, "stop_pct": 4.0, "target_pct": 7.0, "hold_hours": 24, "why": "x"}
SCANNER = {
    "hot_tickers": [
        {"symbol": "SMTC", "price": 86.38, "change_pct": -17.05, "volume": 4_000_000, "source": "yahoo_losers"},
        {"symbol": "IREN", "price": 12.27, "change_pct": -16.15, "volume": 9_000_000, "source": "yahoo_losers"},
        {"symbol": "TINY", "price": 2.0, "change_pct": -30.0, "volume": 10_000, "source": "yahoo_losers"},
        {"symbol": "CALM", "price": 50.0, "change_pct": 1.2, "volume": 9_000_000, "source": "yahoo_gainers"},
    ],
    "liquid_crypto_tickers": [
        {"symbol": "BTCUSDT", "price": 90_000.0, "change_pct": -4.0, "volume": 9e9},
        {"symbol": "ZROUSDT", "price": 1.03, "change_pct": -16.9, "volume": 40_000_000},
    ],
}


class _Harness:
    def __init__(self, tmp_path, *, config=PAPER, us_open=True, positions=(), usage=None, news=()):
        self.mover_calls, self.news_calls = [], []
        self.book = LearningBook(tmp_path / "learning_mode")
        self.clock = [NOW]
        self.worker = LearningWorker(
            book=self.book, config=lambda: config, scanner_state=lambda: SCANNER, news_rows=lambda: list(news),
            positions=lambda: list(positions), quote=lambda ticker, side: {"GLD": 240.0, "BTC-USD": 90_000.0}.get(ticker),
            ask_mover=self._ask_mover, ask_news=self._ask_news,
            usage=lambda: usage or {"claude_subscription": {"calls_today": 100, "daily_call_limit": 3000}},
            session=lambda now: {"us_regular": us_open, "label": "US regular session" if us_open else "US market closed"},
            excluded=lambda ticker: ticker == "SPCXB-USD", clock=lambda: self.clock[0],
        )

    def _ask_mover(self, row, context, cfg, **_):
        self.mover_calls.append((row["ticker"], context))
        return {"idea": dict(IDEA), "model": "claude-opus-5-5", "error": "", "answer": {}}

    def _ask_news(self, headlines, tradable, context, cfg, **_):
        self.news_calls.append(([h["id"] for h in headlines], list(tradable)))
        return {"ideas": [dict(IDEA, side="LONG", ticker="GLD")], "model": "claude-opus-5-5", "error": ""}


def test_the_biggest_tradable_mover_is_asked_and_its_trade_queued(tmp_path):
    h = _Harness(tmp_path)
    result = h.worker.run_once()
    assert [ticker for ticker, _ in h.mover_calls] == ["SMTC"]                 # TINY is too thin, CALM too small
    assert "US regular session" in h.mover_calls[0][1]["session"]
    assert result["mover"] == {"ticker": "SMTC", "side": "SHORT", "queued": True}
    assert [s["ticker"] for s in h.book.candidates(PAPER, NOW)] == ["SMTC"]
    h.clock[0] = NOW + timedelta(minutes=1)
    h.worker.run_once()
    assert [ticker for ticker, _ in h.mover_calls] == ["SMTC", "ZRO-USD"]     # SMTC waits an hour; ZRO fell more than IREN


def test_stocks_wait_for_the_us_session_and_open_positions_are_skipped(tmp_path):
    h = _Harness(tmp_path, us_open=False, positions=[{"ticker": "ZRO-USD", "status": "OPEN", "strategy_type": "XS_MOMENTUM_LONG"}])
    h.worker.run_once()
    assert h.mover_calls == []                                                  # only crypto at night, and ZRO is held


def test_nothing_is_asked_in_live_mode_when_off_over_the_daily_cap_or_low_on_opus(tmp_path):
    for kwargs in ({"config": {"mode": "live"}},
                   {"config": {"mode": "paper", "learning_mode": {"enabled": False}}},
                   {"usage": {"claude_subscription": {"calls_today": 2450, "daily_call_limit": 3000}}},
                   {"config": {"mode": "paper", "learning_mode": {"max_new_per_day": 2}},
                    "positions": [{"strategy_type": "AI_NEWS_LONG", "status": "OPEN", "opened_at": NOW.isoformat()},
                                  {"strategy_type": "AI_MOVER_SHORT", "status": "CLOSED", "opened_at": NOW.isoformat()}]}):
        h = _Harness(tmp_path, **kwargs)
        result = h.worker.run_once()
        assert h.mover_calls == [] and h.news_calls == [], kwargs
        assert result["status"] in {"off", "paused"} and result.get("reason"), kwargs


def test_news_is_asked_every_five_minutes_with_what_is_tradable_now(tmp_path):
    fresh = {"id": "a1", "title": "Troops cross the border", "published_at": (NOW - timedelta(minutes=4)).isoformat(),
             "impact_score": 80, "ticker": "LMT", "related_tickers": ["RTX"]}
    h = _Harness(tmp_path, news=[fresh])
    h.worker.run_once()
    assert h.news_calls[0][0] == ["a1"]
    tradable = h.news_calls[0][1]
    assert {"GLD", "USO", "SPY", "LMT", "RTX", "BTC-USD"} <= set(tradable)
    assert [s["ticker"] for s in h.book.candidates(PAPER, NOW) if s["strategy_type"].startswith("AI_NEWS_")] == ["GLD"]
    h.clock[0] = NOW + timedelta(minutes=2)
    h.worker.run_once()
    assert len(h.news_calls) == 1                                               # not before 5 minutes, and a1 was seen

    night = _Harness(tmp_path / "night", us_open=False, news=[dict(fresh, id="a2")])
    night.worker.run_once()
    assert set(night.news_calls[0][1]) <= {"BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "BNB-USD", "DOGE-USD", "ZRO-USD"}


def test_a_failing_input_is_reported_not_raised(tmp_path):
    h = _Harness(tmp_path)

    def broken():
        raise RuntimeError("scanner down")

    h.worker._scanner_state = broken
    result = h.worker.run_once()
    assert "scanner down" in " ".join(result["errors"])
