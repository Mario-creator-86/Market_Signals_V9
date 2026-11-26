"""Regression tests for the module audit of 2026-11-12."""
from datetime import datetime, timedelta

import pytest

from core.ai.fallback import generate_with_fallback, served_by
from core.ai.provider import AIResponse
from core.position_conflicts import normalize_ticker
from core.singleflight import SingleflightGroup


class _Provider:
    def __init__(self, name, response):
        self.name = name
        self._response = response

    def generate(self, prompt, *, model=None, system="", **opts):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


@pytest.mark.unit
def test_fallback_skips_an_empty_reply_and_names_the_provider_that_answered():
    empty = _Provider("primary", AIResponse(text="   "))
    backup = _Provider("backup", AIResponse(text='{"action": "NO_TRADE"}'))
    resp = generate_with_fallback(empty, "p", fallback_chain=[backup])
    assert resp.text.startswith("{")
    assert served_by(resp) == "backup"


@pytest.mark.unit
def test_fallback_returns_the_empty_reply_when_nobody_else_answers():
    empty = _Provider("primary", AIResponse(text=""))
    broken = _Provider("backup", RuntimeError("down"))
    resp = generate_with_fallback(empty, "p", fallback_chain=[broken])
    assert resp.text == "" and not resp.is_error
    assert served_by(resp) == "primary"


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["BTCUSDT", "BTC-USDT", "BTC/USDT", "btc-usd"])
def test_usdt_spellings_are_one_instrument(raw):
    assert normalize_ticker(raw) == "BTC-USD"


@pytest.mark.unit
def test_binance_cache_does_not_serve_an_old_price():
    from core.engine import BinanceClient

    now = datetime.now()
    recent = {"val": 100.0, "ts": now - timedelta(seconds=30)}
    old = {"val": 100.0, "ts": now - timedelta(seconds=BinanceClient.MAX_STALE_PRICE_SECONDS + 1)}
    assert BinanceClient._recent_cached_price(recent, now) == 100.0
    assert BinanceClient._recent_cached_price(old, now) is None
    assert BinanceClient._recent_cached_price(None, now) is None


@pytest.mark.unit
def test_critic_reply_without_a_verdict_is_not_a_pass():
    from core.ai.critic import run_critic
    from core.ai.decision import Action, Decision

    decision = Decision(action=Action.OPEN_LONG, ticker="AAA", confidence=0.4)
    verdict = run_critic(
        decision,
        {"ticker": "AAA", "signal_score": 50},
        config={"bot": {"mode": "live"}},
        services={"critic_provider": _Provider("critic", AIResponse(text="{}")), "critic_model": "m"},
    )
    assert verdict.veto is True


@pytest.mark.unit
def test_cross_site_post_is_rejected_but_same_origin_is_not():
    from fastapi.testclient import TestClient

    import api.main as main

    client = TestClient(main.app)
    blocked = client.post("/api/__no_such_route__", headers={"sec-fetch-site": "cross-site"})
    assert blocked.status_code == 403
    foreign = client.post("/api/__no_such_route__", headers={"origin": "https://evil.example"})
    assert foreign.status_code == 403
    same = client.post("/api/__no_such_route__", headers={"origin": "http://testserver"})
    assert same.status_code != 403
    plain = client.post("/api/__no_such_route__")
    assert plain.status_code != 403


@pytest.mark.unit
def test_stop_out_locks_the_ticker_longer_than_the_normal_cooldown():
    from core.config import get_bucharest_time
    from core.signals import SignalManager

    manager = object.__new__(SignalManager)
    manager.last_signal_time = {"AAA": get_bucharest_time() - timedelta(minutes=30)}
    manager.last_stop_time = {"AAA": get_bucharest_time() - timedelta(minutes=30)}
    assert manager.can_open_new_signal_for_ticker("AAA", 15) is True
    assert manager.can_open_new_signal_for_ticker("AAA", 15, 240) is False
    assert manager.can_open_new_signal_for_ticker("BBB", 15, 240) is True


@pytest.mark.unit
def test_breakout_level_excludes_the_bar_being_tested():
    import pandas as pd

    from core.strategy_backtester import _precompute_v2_ohlc_features

    n = 60
    idx = pd.date_range("2026-01-05 14:30", periods=n, freq="1min", tz="UTC")
    close = [100.0] * (n - 1) + [103.0]
    df = pd.DataFrame({
        "open": [100.0] * n,
        "high": [100.5] * (n - 1) + [103.5],
        "low": [99.5] * n,
        "close": close,
        "volume": [1000.0] * n,
    }, index=idx)
    features = _precompute_v2_ohlc_features(df, "US_STOCKS")
    assert float(features["resistance"].iloc[-1]) == pytest.approx(100.5)
    assert float(df["close"].iloc[-1]) > float(features["resistance"].iloc[-1])
