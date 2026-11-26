"""Unit tests for the Capital live-quote pool deadline + zero-price fallback.

Root cause being locked in: `_build_capital_payload` (live_prices path) fanned
~28 per-position remote quote fetches across a pool but used `ex.map`, which
blocks until the SLOWEST job finishes. Obscure tickers that retry with backoff
(BONK/BEL-style) stalled the whole batch for 15s uncontended / 34-50s under
poll contention, blowing past the frontend's 30s timeout -> cache fallback ->
the Capital tab appeared frozen ("nu se actualizeaza").

Fix under test:
  1. The pool is bounded by a hard deadline (`as_completed` + timeout +
     `shutdown(wait=False, cancel_futures=True)`). A few stuck tickers can no
     longer stall the response.
  2. Any OPEN position still without a usable current_price (live quote timed
     out AND no cached price) falls back to its entry price so the card renders
     a real number + CACHE and PnL=0, instead of "$0.0000".

All network / capital_manager calls are mocked — no live calls, no real cm.
"""
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from api import state_service
from api.state_service import _build_capital_payload


def _verified(price: float, source: str = "live") -> dict:
    return {
        "execution_price": price,
        "source": source,
        "price_timestamp": "2026-07-08T12:00:00+03:00",
        "execution_price_quality": {"level": "VERIFIED", "is_valid": True},
    }


def _position(ticker, direction, entry, qty):
    return {
        "ticker": ticker,
        "direction": direction,
        "status": "OPEN",
        "entry_price": entry,
        "qty": qty,
        "leverage": 1,
        "usd_size": entry * qty,
        "commission_usd": 0.0,
    }


def _fake_cm(positions):
    return SimpleNamespace(
        get_positions=lambda: list(positions),
        get_balance=lambda: 10000.0,
        get_ledger=lambda limit=20: [],
        get_equity_curve=lambda limit=90: [],
        record_equity_point=lambda eq_prices: None,
        _get_cached_price=lambda ticker: None,
    )


@pytest.fixture
def _patched_cm(monkeypatch, tmp_path):
    """Patch the heavy collaborators so only the quote-pool path runs."""
    positions = [
        _position("AAPL", "LONG", 190.0, 10),
        _position("MSFT", "LONG", 380.0, 5),
        _position("BONK-USD", "LONG", 0.00002, 1_000_000),  # obscure crypto, stuck
    ]
    monkeypatch.setattr(state_service, "get_capital_manager", lambda: _fake_cm(positions))
    monkeypatch.setattr(state_service, "BASE_DIR", tmp_path)
    monkeypatch.setattr(state_service, "DATA_DIR", tmp_path)
    # The production code deliberately accepts fresh prices from the shared
    # SQLite cache even in fast-only mode. This unit test isolates the quote
    # pool, so a cache populated by an earlier test must not leak into it.
    monkeypatch.setattr("core.live_price_cache.get_live_prices", lambda _tickers: {})
    # explainability + trade audit are irrelevant to the pool path.
    monkeypatch.setattr(state_service, "summarize_position_explainability", lambda pos: {})
    monkeypatch.setattr(state_service, "get_trade_audit", lambda *a, **k: [])
    return positions


def test_live_quote_pool_respects_deadline_with_stuck_ticker(_patched_cm, monkeypatch):
    """A stuck quote job must not stall the whole Capital response past the deadline."""
    # Tight deadline so the test is fast; the stuck ticker sleeps well past it.
    monkeypatch.setenv("MS_CAPITAL_QUOTE_DEADLINE_SECONDS", "0.5")
    SLOW = {"BONK-USD"}

    def fake_quote(cm, ticker, side, *, allow_remote=True):
        if ticker in SLOW:
            time.sleep(4.0)  # simulate a retry-backoff stall
            return None
        return _verified(100.0 if ticker == "AAPL" else 200.0)

    with patch.object(state_service, "_get_capital_exec_quote_cached", side_effect=fake_quote):
        t0 = time.monotonic()
        payload = _build_capital_payload(
            [], {}, fast_only=False, include_opportunities=False, include_decisions=False
        )
        elapsed = time.monotonic() - t0

    # Deadline honored: must return well under the 4s the stuck job would take.
    assert elapsed < 2.0, f"Capital payload blocked {elapsed:.2f}s waiting for a stuck quote"

    by_tk = {p["ticker"]: p for p in payload["positions"]}
    # Fast tickers got a live quote.
    assert by_tk["AAPL"]["current_price_live"] is True
    assert by_tk["AAPL"]["current_price"] == 100.0
    assert by_tk["MSFT"]["current_price_live"] is True
    assert by_tk["MSFT"]["current_price"] == 200.0
    # Stuck ticker did NOT block the response and is marked not-live.
    assert by_tk["BONK-USD"]["current_price_live"] is False


def test_zero_price_position_falls_back_to_entry(_patched_cm, monkeypatch):
    """A position with no live quote AND no cached price must show entry, not $0."""
    monkeypatch.setenv("MS_CAPITAL_QUOTE_DEADLINE_SECONDS", "0.5")

    def fake_quote(cm, ticker, side, *, allow_remote=True):
        if ticker == "BONK-USD":
            time.sleep(4.0)  # times out -> no quote
            return None
        return _verified(100.0 if ticker == "AAPL" else 200.0)

    with patch.object(state_service, "_get_capital_exec_quote_cached", side_effect=fake_quote):
        payload = _build_capital_payload(
            [], {}, fast_only=False, include_opportunities=False, include_decisions=False
        )

    bonk = {p["ticker"]: p for p in payload["positions"]}["BONK-USD"]
    entry = bonk["entry_price"]
    # The cardinal sin was rendering $0.0000 for a held position. Entry-price
    # fallback gives a real number + gross PnL 0 (current == entry). unrealized_pnl
    # may still carry the estimated exit commission (a few cents) — that's honest,
    # not the garbage huge-negative you get from current_price=0.
    assert bonk["current_price"] == entry
    assert bonk["current_price"] > 0
    assert bonk["current_price_live"] is False
    assert bonk.get("current_pnl", 0.0) == 0.0  # gross PnL: current == entry
    assert abs(bonk.get("unrealized_pnl", 0.0)) < 1.0  # not a garbage blow-up


def test_fast_only_skips_pool_entirely(_patched_cm, monkeypatch):
    """fast_only (lean poll) must not touch the quote pool at all."""
    called = {"n": 0}

    def fake_quote(cm, ticker, side, *, allow_remote=True):
        called["n"] += 1
        return _verified(100.0)

    monkeypatch.setenv("MS_CAPITAL_QUOTE_DEADLINE_SECONDS", "0.5")
    with patch.object(state_service, "_get_capital_exec_quote_cached", side_effect=fake_quote):
        t0 = time.monotonic()
        payload = _build_capital_payload(
            [], {}, fast_only=True, include_opportunities=False, include_decisions=False
        )
        elapsed = time.monotonic() - t0

    assert called["n"] == 0, "fast_only must skip the live-quote pool"
    assert elapsed < 1.0
    # No live quotes applied; positions keep whatever (none here) they had.
    for p in payload["positions"]:
        assert p.get("current_price_live") in (None, False, 0)
