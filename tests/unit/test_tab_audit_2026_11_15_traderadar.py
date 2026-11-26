"""Trade Radar tab audit, 2026-11-15 — regression guards.

Live evidence (server 09:50, 7 candidates): every card read BLOCKED with
"key indicators missing"; crypto cards additionally "No OHLC data".

Root causes confirmed in code and pinned here:

  TR15-01  Indicators were never computed from price history: the remote OHLC
           frame is raw bars and the local-cache path called
           ``QuickBacktester._calculate_indicators`` (does not exist), so every
           off-watchlist candidate had rsi/adx/atr/macd null even with a month
           of bars in hand (live: VEON had a 6-trade backtest and no RSI).
  TR15-02  Crypto klines were fetched through the automatic loop's cache-only
           HTTP context and silently refused; Force inherited the same context.
           Crypto now goes to Binance 5m klines (never Yahoo), user-triggered
           cycles reach the network, and a cached miss does not block Force.
  TR15-03  Un-scored candidates were served as "NEUTRAL · PULLBACK · 0 ·
           BLOCKED". They are now flagged pending.
  TR15-04  "PULLBACK" was a placeholder shown as the setup of a short that
           simply follows a -58% move.
  TR15-05  Suppress stored the symbol but the card stayed; no undo existed.
  TR15-06  Unmeasured values printed as measurements (PF 0.00 with no losing
           trade, 30d label regardless of the bars, calendar "unavailable"
           for an empty window / "clean" for an empty feed).
  TR15-07  A below-threshold or vanished mover kept its frozen % and was
           re-analysed every cooldown; a failed enrichment left READY behind;
           the alphabetical 10-flag cut could drop a hard blocker.
"""
import sys
import threading
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
__import__("os").environ.setdefault("TEST_MODE", "1")
__import__("os").environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

import agents.trade_radar_agent as mod  # noqa: E402
from agents.trade_radar_agent import (  # noqa: E402
    TradeRadarAgent,
    _now_utc,
    _to_binance_pair,
    filter_suppressed_candidates,
)
from core.request_context import get_request_context, request_context  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────

def _bars(n=400, start=10.0, step=0.01, freq="5min", flat=False):
    """Deterministic raw OHLC frame (no indicator columns)."""
    idx = pd.date_range("2026-11-01", periods=n, freq=freq, tz="UTC")
    rows = []
    price = start
    for i in range(n):
        drift = 0.0 if flat else (step if (i // 7) % 2 == 0 else -step * 0.6)
        open_ = price
        close = price + drift
        rows.append({
            "open": open_,
            "high": max(open_, close) + step * 0.5,
            "low": min(open_, close) - step * 0.5,
            "close": close,
            "volume": 1000.0 + i,
        })
        price = close
    df = pd.DataFrame(rows, index=idx)
    df["timestamp"] = df.index
    return df


def _agent():
    agent = TradeRadarAgent.__new__(TradeRadarAgent)
    agent._ohlc_cache = {}
    agent._auto_ohlc_remote = True
    agent._quick_backtester = None
    agent._last_news_refresh_ts = 0.0
    agent._last_calendar_refresh_ts = 0.0
    agent._lock = threading.RLock()
    agent._build_news_pack = lambda **kw: (
        {"available": False, "checked": True, "bullish_count": 0, "bearish_count": 0, "top": [], "last_update": None},
        {"news_ts": None},
    )
    agent._build_calendar_pack = lambda **kw: (
        {"available": False, "checked": True, "macro_checked": True, "earnings_checked": True,
         "earnings": {}, "macro_next_72h": [], "last_update": None},
        {"calendar_ts": None},
    )
    return agent


def _enrich(agent, symbol, price, change_pct, watchlist_item=None, cfg_extra=None):
    cand = TradeRadarAgent._new_candidate(symbol)
    cand.update({"price": price, "change_pct": change_pct, "last_seen": _now_utc().isoformat()})
    cfg = {**TradeRadarAgent.DEFAULT_CONFIG, "enable_backtest": False, **(cfg_extra or {})}
    return agent._enrich_candidate(
        symbol=symbol, candidate=cand, watchlist_item=watchlist_item or {},
        cfg=cfg, now=_now_utc(), force_refresh=False,
    )


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def _klines(n, end_ms=1_800_000_000_000):
    out = []
    for i in range(n):
        t = end_ms - (n - 1 - i) * 300_000
        out.append([t, "1.0", "1.1", "0.9", "1.05", "10", t + 299_999])
    return out


# ── TR15-02: crypto goes to Binance klines with the right symbol ────────────

@pytest.mark.parametrize("raw,pair", [
    ("KAVAUSDT", "KAVAUSDT"),
    ("BTC-USD", "BTCUSDT"),
    ("ETH-USDT", "ETHUSDT"),
    ("shellusdt", "SHELLUSDT"),
    ("SOLUSD", "SOLUSDT"),
])
def test_tr15_02_binance_pair_mapping(raw, pair):
    assert _to_binance_pair(raw) == pair


def test_tr15_02_crypto_ohlc_uses_binance_5m_klines(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp(_klines(400))

    monkeypatch.setattr(mod, "http_get", fake_get)
    df, warnings = TradeRadarAgent._fetch_crypto_ohlc("KAVAUSDT", 30)
    assert df is not None and len(df) == 400
    assert {"open", "high", "low", "close", "volume", "timestamp"} <= set(df.columns)
    assert calls, "no kline request was made"
    url, kwargs = calls[0]
    assert "api.binance.com" in url and url.endswith("/klines")
    assert kwargs["provider"] == "binance"
    assert kwargs["params"]["symbol"] == "KAVAUSDT"
    assert kwargs["params"]["interval"] == "5m"
    assert warnings == []


def test_tr15_02_crypto_never_reaches_the_stock_yahoo_path(monkeypatch):
    """is_crypto must route to the exchange; the stock fetcher (Yahoo) must
    not be called for an XXXUSDT symbol."""
    agent = _agent()

    class _NoStockPath:
        def fetch_backtest_data(self, **kwargs):
            raise AssertionError(f"crypto symbol sent to the stock/Yahoo path: {kwargs}")

    agent._quick_backtester = _NoStockPath()
    monkeypatch.setattr(agent, "_load_ohlc_from_local_cache", lambda symbol, days: (None, ["ohlc_local_cache_miss"]))
    monkeypatch.setattr(mod, "http_get", lambda url, **kw: _Resp(_klines(400)))
    df, _ts, warnings = agent._get_ohlc_cached(
        symbol="KAVAUSDT", is_crypto=True, days=30, ttl_sec=300, allow_remote=True,
    )
    assert df is not None
    assert df.attrs.get("radar_ohlc_source") == "binance"
    # the bars arrived, so the local-cache miss is not a caveat any more
    assert not any("ohlc_local_cache_miss" in w for w in warnings)


def test_tr15_02_unknown_pair_is_reported_not_invented(monkeypatch):
    monkeypatch.setattr(mod, "http_get", lambda url, **kw: _Resp({"code": -1121}, status=400))
    import api.market_data_service as mds
    monkeypatch.setattr(mds.KuCoinClient, "fetch_klines", classmethod(lambda cls, *a, **k: None))
    df, warnings = TradeRadarAgent._fetch_crypto_ohlc("NOSUCHCOINUSDT", 30)
    assert df is None
    assert any("No historical data" in w for w in warnings)
    assert "ohlc_remote_no_data" in TradeRadarAgent._normalize_ohlc_warnings(warnings)


def test_tr15_02_forced_cycle_is_not_cache_only():
    """Force / a requested symbol must reach the network; the plain automatic
    cycle stays cache-only (pinned separately in test_trade_radar_audit_fixes)."""
    agent = TradeRadarAgent.__new__(TradeRadarAgent)
    agent._startup_delay_seconds = 0
    agent._auto_cache_only = True
    agent._stop_event = threading.Event()
    agent._wake_event = threading.Event()
    agent._lock = threading.RLock()
    agent._pending_force = True
    agent._pending_symbols = set()
    agent._state = {"config": {"radar_interval_sec": 30}}
    observed = {}

    def capture_cycle(**kwargs):
        observed["cache_only"] = get_request_context().cache_only
        observed["force"] = kwargs.get("force_refresh")
        agent._stop_event.set()
        agent._wake_event.set()
        return {}

    agent.run_cycle = capture_cycle
    agent._loop()
    assert observed == {"cache_only": False, "force": True}


def test_tr15_02_ohlc_fetch_is_let_through_cache_only_context():
    agent = _agent()
    with request_context(reason="AGENT_REFRESH", cache_only=True):
        with agent._ohlc_fetch_context():
            assert get_request_context().cache_only is False
        assert get_request_context().cache_only is True
        agent._auto_ohlc_remote = False  # kill-switch restores strict behaviour
        with agent._ohlc_fetch_context():
            assert get_request_context().cache_only is True


def test_tr15_02_cached_miss_does_not_block_a_forced_refresh(monkeypatch):
    agent = _agent()
    monkeypatch.setattr(agent, "_load_ohlc_from_local_cache", lambda symbol, days: (None, ["ohlc_local_cache_miss"]))
    df, _ts, _w = agent._get_ohlc_cached(symbol="KAVAUSDT", is_crypto=True, days=30, ttl_sec=300, allow_remote=False)
    assert df is None  # miss is cached
    monkeypatch.setattr(mod, "http_get", lambda url, **kw: _Resp(_klines(400)))
    df2, _ts2, _w2 = agent._get_ohlc_cached(
        symbol="KAVAUSDT", is_crypto=True, days=30, ttl_sec=300, allow_remote=True, force_refresh=True,
    )
    assert df2 is not None, "Force was served the cached miss instead of fetching"


# ── TR15-01: indicators come from the bars ──────────────────────────────────

def test_tr15_01_indicator_snapshot_from_raw_bars():
    snap = TradeRadarAgent._indicator_snapshot(_bars(400))
    assert snap["timeframe"] == "5m"
    for key in ("rsi", "macd_hist", "atr", "adx"):
        assert isinstance(snap[key], float), f"{key} not computed from the bars"
    assert 0.0 < snap["rsi"] < 100.0
    assert snap["atr"] > 0


def test_tr15_01_one_minute_bars_are_resampled_to_five():
    snap = TradeRadarAgent._indicator_snapshot(_bars(1500, freq="1min"))
    assert snap["timeframe"] == "5m"
    assert snap["bars"] == 300
    assert isinstance(snap["rsi"], float)


def test_tr15_01_too_few_bars_gives_none_not_a_default():
    snap = TradeRadarAgent._indicator_snapshot(_bars(20))
    assert snap["rsi"] is None and snap["atr"] is None and snap["adx"] is None and snap["macd_hist"] is None


def test_tr15_01_flat_series_never_emits_nan():
    snap = TradeRadarAgent._indicator_snapshot(_bars(200, flat=True, step=0.0))
    for key in ("rsi", "macd_hist", "atr", "adx"):
        assert snap[key] is None or snap[key] == snap[key], f"{key} is NaN"


def test_tr15_01_enrich_fills_indicators_and_atr_levels_from_bars():
    agent = _agent()
    frame = _bars(400)
    agent._get_ohlc_cached = lambda **kw: (frame, "2026-11-15T07:00:00+00:00", [])
    price = float(frame["close"].iloc[-1])
    out = _enrich(agent, "VEON", price, 5.35)
    ind = out["analysis"]["key_indicators"]
    assert all(ind[k] is not None for k in ("rsi", "adx", "macd_hist", "atr")), ind
    assert "missing_indicators" not in out["red_flags"]
    assert "missing_ohlc" not in out["red_flags"]
    assert out["analysis"]["indicator_timeframe"] == "5m"
    # levels are ATR multiples of the measured ATR, and say so
    assert out["analysis"]["levels_basis"] == "atr"
    atr = ind["atr"]
    assert out["analysis"]["tp"] == pytest.approx(price + 2.0 * atr, rel=1e-4)
    assert out["analysis"]["sl"] == pytest.approx(price - 1.5 * atr, rel=1e-4)


def test_tr15_01_no_bars_keeps_flat_levels_but_labels_them():
    agent = _agent()
    agent._get_ohlc_cached = lambda **kw: (None, None, ["ohlc_local_cache_miss"])
    out = _enrich(agent, "TESTUSDT", 0.004831, -17.5)
    assert out["analysis"]["levels_basis"] == "default_pct"
    assert out["analysis"]["key_indicators"]["atr"] is None
    assert "missing_indicators" in out["red_flags"]


def test_tr15_01_local_cache_path_no_longer_calls_a_missing_method(tmp_path, monkeypatch):
    """The cache reader used to call QuickBacktester._calculate_indicators
    (AttributeError, swallowed) on every hit."""
    agent = _agent()
    cache_dir = tmp_path / "data" / "cache_backtest"
    cache_dir.mkdir(parents=True)
    frame = _bars(400)
    recent = pd.date_range(end=pd.Timestamp.utcnow().tz_localize(None).floor("5min"), periods=400, freq="5min")
    out = frame.reset_index(drop=True).drop(columns=["timestamp"])
    out.insert(0, "timestamp", recent)
    out.to_csv(cache_dir / "VEON_5min.csv", index=False)
    # The OHLC cache lives in the configured data folder (MS_DATA_DIR), no
    # longer under a hard-coded <repo>/data (wave 3, data isolation).
    monkeypatch.setattr("core.config.DATA_DIR", tmp_path / "data")
    df, warnings = agent._load_ohlc_from_local_cache("VEON", 30)
    assert df is not None, warnings
    assert warnings == []
    assert isinstance(TradeRadarAgent._indicator_snapshot(df)["rsi"], float)


# ── TR15-04: truthful setup label and reasons ───────────────────────────────

def test_tr15_04_move_following_short_is_not_called_pullback():
    agent = _agent()
    agent._get_ohlc_cached = lambda **kw: (None, None, [])
    out = _enrich(agent, "KAVAUSDT", 0.0284, -58.36)
    strategy = out["analysis"]["strategy"]
    assert out["analysis"]["verdict"] == "SHORT"
    assert strategy["trade_mode_source"] == "default"
    assert strategy["setup_label"] == "MOMENTUM"
    assert out["analysis"]["strength_estimated"] is True
    joined = " ".join(out["reasons"])
    assert "via PULLBACK" not in joined
    assert "no strategy signal" in joined and "estimated" in joined
    assert "Evidence cap 59" in joined
    assert "capped" in out["analysis"]["confidence_breakdown"]["formula"]


def test_tr15_04_cap_names_estimated_strength_when_indicators_exist():
    agent = _agent()
    frame = _bars(400)
    agent._get_ohlc_cached = lambda **kw: (frame, "2026-11-15T07:00:00+00:00", [])
    out = _enrich(agent, "KAVAUSDT", float(frame["close"].iloc[-1]), -58.36)
    assert out["analysis"]["confidence"] == 59
    cap_reason = [r for r in out["reasons"] if r.startswith("Evidence cap")]
    assert cap_reason and "strength estimated" in cap_reason[0]
    assert "key indicators missing" not in cap_reason[0]
    assert out["analysis"]["confidence_breakdown"]["formula"].endswith("(capped: strength estimated)")
    assert out["readiness"]["status"] == "BLOCKED"
    assert out["readiness"]["blockers"] == ["analysis_strength_estimated"]


def test_tr15_04_strategy_mode_from_watchlist_is_shown_as_is():
    agent = _agent()
    frame = _bars(400)
    agent._get_ohlc_cached = lambda **kw: (frame, "2026-11-15T07:00:00+00:00", [])
    out = _enrich(
        agent, "VEON", float(frame["close"].iloc[-1]), 6.0,
        watchlist_item={"signal_score": 80, "best_strategy": {"trade_mode": "BREAKOUT"}},
    )
    strategy = out["analysis"]["strategy"]
    assert strategy == {
        "risk_mode": "MEDIUM", "trade_mode": "BREAKOUT",
        "trade_mode_source": "watchlist_strategy", "setup_label": "BREAKOUT",
    }
    assert out["analysis"]["strength_estimated"] is False
    assert any(r.startswith("Technical verdict LONG via BREAKOUT") for r in out["reasons"])


def test_tr15_04_setup_label_against_the_move_is_pullback():
    assert TradeRadarAgent._setup_label("LONG", "PULLBACK", "default", -3.0) == "PULLBACK"
    assert TradeRadarAgent._setup_label("NEUTRAL", "PULLBACK", "default", 12.0) == "UNCLASSIFIED"


# ── TR15-03: pending candidates ─────────────────────────────────────────────

def test_tr15_03_new_candidate_is_pending_not_blocked():
    readiness = TradeRadarAgent._new_candidate("BERAUSDT")["readiness"]
    assert readiness["status"] == "PENDING"
    assert readiness["can_open"] is False


def test_tr15_03_get_state_flags_pending_and_analysed():
    agent = _agent()
    now = _now_utc().isoformat()
    scored = {"symbol": "KAVAUSDT", "change_pct": -58.4, "last_seen": now, "last_processed": now,
              "red_flags": [], "analysis": {"verdict": "SHORT", "confidence": 59}}
    queued = TradeRadarAgent._new_candidate("BERAUSDT")
    queued.update({"change_pct": -16.5, "last_seen": now})
    agent._state = {"as_of": now, "config": {}, "suppressed": {}, "candidates": [scored, queued]}
    by_symbol = {c["symbol"]: c for c in agent.get_state(limit=10)["candidates"]}
    assert by_symbol["BERAUSDT"]["analysis_pending"] is True
    assert by_symbol["KAVAUSDT"]["analysis_pending"] is False


def test_tr15_03_analysis_within_cooldown_is_not_flagged_as_stale():
    """Re-sighted 5 minutes after analysis (cooldown 10 min + 5 min cycle) is
    the normal steady state; only real lag is flagged."""
    agent = _agent()
    now = _now_utc()

    def cand(symbol, analysed_minutes_ago):
        return {"symbol": symbol, "change_pct": -20.0, "last_seen": now.isoformat(),
                "last_processed": (now - timedelta(minutes=analysed_minutes_ago)).isoformat(),
                "red_flags": [], "analysis": {"verdict": "SHORT", "confidence": 55}}

    agent._state = {"as_of": now.isoformat(), "config": dict(TradeRadarAgent.DEFAULT_CONFIG), "suppressed": {},
                    "candidates": [cand("FRESHUSDT", 5), cand("LAGUSDT", 40)]}
    flags = {c["symbol"]: c["red_flags"] for c in agent.get_state(limit=10)["candidates"]}
    assert "analysis_older_than_move" not in flags["FRESHUSDT"]
    assert "analysis_older_than_move" in flags["LAGUSDT"]


# ── TR15-05: suppress / unsuppress ──────────────────────────────────────────

def _state_agent(symbols):
    agent = _agent()
    now = _now_utc().isoformat()
    agent._state = {
        "as_of": now,
        "config": dict(TradeRadarAgent.DEFAULT_CONFIG),
        "suppressed": {},
        "candidates": [
            {"symbol": s, "change_pct": 20.0, "last_seen": now, "last_processed": now,
             "red_flags": [], "analysis": {"verdict": "LONG", "confidence": 50}}
            for s in symbols
        ],
    }
    agent._persist_locked = lambda: None
    return agent


def test_tr15_05_suppressed_symbol_leaves_the_served_list_and_undo_restores_it():
    agent = _state_agent(["KAVAUSDT", "VEON"])
    result = agent.suppress_symbols(["kavausdt"], duration_sec=3600)
    assert result["count"] == 1

    served = agent.get_state(limit=10, include_suppressed=False)
    assert [c["symbol"] for c in served["candidates"]] == ["VEON"]
    assert served["total_candidates"] == 1
    assert list(served["suppressed"]) == ["KAVAUSDT"]

    undone = agent.unsuppress_symbols(["KAVAUSDT"])
    assert undone["removed"] == ["KAVAUSDT"] and undone["suppressed"] == {}
    served = agent.get_state(limit=10, include_suppressed=False)
    assert sorted(c["symbol"] for c in served["candidates"]) == ["KAVAUSDT", "VEON"]


def test_tr15_05_default_get_state_still_includes_suppressed():
    """core.agent_manager reads get_state(limit=50) for trade_radar_opportunity;
    its view must not change because a card was hidden in the UI."""
    agent = _state_agent(["KAVAUSDT", "VEON"])
    agent.suppress_symbols(["KAVAUSDT"])
    assert len(agent.get_state(limit=50)["candidates"]) == 2


def test_tr15_05_expired_suppression_is_pruned_at_serve_time():
    past = (_now_utc() - timedelta(minutes=5)).isoformat()
    payload = {"suppressed": {"KAVAUSDT": past}, "candidates": [{"symbol": "KAVAUSDT"}], "total_candidates": 1}
    filter_suppressed_candidates(payload)
    assert payload["suppressed"] == {}
    assert len(payload["candidates"]) == 1


def test_tr15_05_api_routes_wired():
    src = Path("api/main.py").read_text(encoding="utf-8")
    assert '@app.post("/api/traderadar/unsuppress")' in src
    assert "agent.get_state(limit=limit, include_suppressed=False)" in src


def test_tr15_05_state_and_unsuppress_routes_end_to_end(monkeypatch):
    """The served payload hides a suppressed symbol; the undo route restores it."""
    import json

    import api.main as main

    agent = _state_agent(["KAVAUSDT", "VEON"])
    agent.suppress_symbols(["KAVAUSDT"], duration_sec=3600)
    monkeypatch.setattr(main, "_agent_enabled", lambda name: True)
    monkeypatch.setattr(mod, "get_trade_radar_agent", lambda *a, **k: agent)
    monkeypatch.setattr(mod, "get_trade_radar_agent_if_ready", lambda: agent)

    main._endpoint_payload_clear_prefix("traderadar_state:")
    body = json.loads(main.api_traderadar_state(limit=10).body)
    assert [c["symbol"] for c in body["candidates"]] == ["VEON"]
    assert list(body["suppressed"]) == ["KAVAUSDT"]

    res = main.api_traderadar_unsuppress(main.TradeRadarSuppressRequest(symbols=["KAVAUSDT"]))
    assert res["status"] == "ok" and res["removed"] == ["KAVAUSDT"]
    body = json.loads(main.api_traderadar_state(limit=10).body)  # route cleared its payload cache
    assert sorted(c["symbol"] for c in body["candidates"]) == ["KAVAUSDT", "VEON"]
    assert body["suppressed"] == {}
    main._endpoint_payload_clear_prefix("traderadar_state:")


# ── TR15-06: unmeasured values are not printed as measurements ──────────────

class _FakeBacktester:
    def __init__(self, result):
        self._result = result

    def quick_backtest(self, **kwargs):
        return dict(self._result)


def test_tr15_06_profit_factor_is_null_without_a_losing_trade():
    agent = _agent()
    agent._quick_backtester = _FakeBacktester({
        "wins": 3, "losses": 0, "total_signals": 3, "win_rate": 100.0, "win_rate_trades": 100.0,
        "profit_factor": None, "max_drawdown_pct": 0.0,
    })
    pack = agent._run_backtest_lite(symbol="VEON", df=_bars(400), trade_mode="PULLBACK", days=30)
    assert pack["available"] is True
    assert pack["closed_trades"] == 3
    assert pack["profit_factor"] is None
    # 400 five-minute bars cover ~1.4 days: the label must not say 30d
    assert pack["days"] == 1 and pack["days_requested"] == 30
    assert pack["scope"] == "generic_rsi_macd_both_directions"

    info = TradeRadarAgent._compute_confidence("LONG", 60.0, {}, {}, pack)
    assert info["backtest_bonus"] == 0.0  # an unmeasurable ratio earns nothing
    reasons = TradeRadarAgent._build_reasons(
        symbol="VEON", change_pct=6.0, threshold=5.0, verdict="LONG", trade_mode="PULLBACK",
        strength=60.0, rsi=None, adx=None, news_pack={}, backtest_pack=pack, confidence_info=info,
    )
    bt = [r for r in reasons if r.startswith("Backtest-lite")]
    assert bt and "PF n/a" in bt[0] and "PF 0.00" not in bt[0] and "Backtest-lite 1d" in bt[0]


def test_tr15_06_too_few_bars_is_unavailable_not_a_zero_trade_backtest():
    agent = _agent()
    agent._quick_backtester = _FakeBacktester({
        "wins": 0, "losses": 0, "total_signals": 0, "win_rate": 0.0, "profit_factor": None,
        "max_drawdown_pct": 0.0, "insufficient_data": True,
    })
    pack = agent._run_backtest_lite(symbol="NEWUSDT", df=_bars(60), trade_mode="PULLBACK", days=30)
    assert pack["available"] is False
    assert pack["unavailable_reason"] == "insufficient_price_history"
    flags = TradeRadarAgent._build_red_flags("LONG", {}, {}, pack, 1.0, 6.0)
    assert "backtest_unavailable" in flags and "backtest_no_trades" not in flags


def test_tr15_06_calendar_and_news_flags_distinguish_empty_from_unavailable():
    flags = TradeRadarAgent._build_red_flags
    read_empty_cal = {"available": False, "checked": True, "macro_checked": True}
    read_empty_news = {"available": False, "checked": True}
    out = flags("LONG", read_empty_news, read_empty_cal, {}, 10.0, 6.0)
    assert "calendar_unavailable" not in out and "macro_calendar_unavailable" not in out
    assert "no_symbol_news" in out and "news_unavailable" not in out

    # macro feed empty (live 2026-11-15: economic == []) -> risk NOT assessed
    out = flags("LONG", {"available": False}, {"available": False, "checked": True, "macro_checked": False}, {}, 10.0, 6.0)
    assert "macro_calendar_unavailable" in out
    assert "news_unavailable" in out

    out = flags("LONG", {"available": False}, {"available": False}, {}, 10.0, 6.0)
    assert "calendar_unavailable" in out


def test_tr15_06_breakdown_marks_unassessed_components():
    agent = _agent()
    agent._get_ohlc_cached = lambda **kw: (None, None, [])
    agent._build_calendar_pack = lambda **kw: ({"available": False, "earnings": {}, "macro_next_72h": []}, {"calendar_ts": None})
    agent._build_news_pack = lambda **kw: ({"available": False, "top": []}, {"news_ts": None})
    breakdown = _enrich(agent, "KAVAUSDT", 0.0284, -58.36)["analysis"]["confidence_breakdown"]
    assert breakdown["base_estimated"] is True
    assert breakdown["news_assessed"] is False
    assert breakdown["calendar_assessed"] is False
    assert breakdown["backtest_assessed"] is False


# ── TR15-07: cycle hygiene ──────────────────────────────────────────────────

def _cycle_agent(candidates, scanner, monkeypatch):
    agent = _agent()
    agent.config_manager = type("CM", (), {"config": {}, "reload_if_changed": lambda self: None})()
    agent._state = {
        "config": dict(TradeRadarAgent.DEFAULT_CONFIG),
        "suppressed": {},
        "candidates": candidates,
        "meta": {},
    }
    monkeypatch.setattr(agent, "_collect_scanner_candidates", lambda: scanner)
    monkeypatch.setattr(agent, "_load_watchlist_map", lambda: {})
    monkeypatch.setattr(agent, "_persist_locked", lambda: None)
    monkeypatch.setattr(mod, "is_agent_enabled", lambda *a, **k: True)
    monkeypatch.setattr(mod, "update_agent_status", lambda *a, **k: None)
    enriched = []

    def fake_enrich(**kwargs):
        enriched.append(kwargs["symbol"])
        out = dict(kwargs["candidate"])
        out["last_processed"] = _now_utc().isoformat()
        return out

    agent._enrich_candidate = fake_enrich
    return agent, enriched


def _scored(symbol, change, minutes_ago=30):
    ts = (_now_utc() - timedelta(minutes=minutes_ago)).isoformat()
    return {"symbol": symbol, "source": "binance", "change_pct": change, "price": 1.0, "volume": 1.0,
            "last_seen": ts, "last_processed": ts, "cooldown_until": ts,
            "analysis": {"verdict": "SHORT", "confidence": 55}, "red_flags": []}


def test_tr15_07_faded_move_gets_live_numbers_and_is_not_reanalysed(monkeypatch):
    stale = _scored("BERAUSDT", -16.5)
    seen_before = stale["last_seen"]
    agent, enriched = _cycle_agent(
        [stale], [{"symbol": "BERAUSDT", "change_pct": -9.2, "price": 0.0991, "source": "binance"}], monkeypatch,
    )
    out = agent.run_cycle()
    cand = out["candidates"][0]
    assert cand["change_pct"] == -9.2 and cand["price"] == 0.0991, "card kept the frozen snapshot"
    assert cand["last_seen"] == seen_before, "a non-qualifying sighting must not reset the age"
    assert enriched == []
    served = agent.get_state(limit=10)["candidates"][0]
    assert "move_faded_below_threshold" in served["red_flags"]


def test_tr15_07_vanished_mover_is_not_reanalysed_by_the_automatic_cycle(monkeypatch):
    gone = _scored("PUMPUSDT", -15.1)
    live = {"symbol": "KAVAUSDT", "change_pct": -58.4, "price": 0.0284, "source": "binance"}
    agent, enriched = _cycle_agent([gone], [live], monkeypatch)
    agent.run_cycle()
    assert enriched == ["KAVAUSDT"]

    agent2, enriched2 = _cycle_agent([_scored("PUMPUSDT", -15.1)], [live], monkeypatch)
    agent2.run_cycle(force_refresh=True)  # Force still re-scores everything
    assert sorted(enriched2) == ["KAVAUSDT", "PUMPUSDT"]


def test_tr15_07_failed_enrichment_does_not_leave_ready_behind(monkeypatch):
    ready = _scored("VEON", 6.0)
    ready["analysis"] = {"verdict": "LONG", "confidence": 80, "entry": 10.0, "tp": 10.6, "sl": 9.7}
    ready["readiness"] = {"status": "READY", "can_open": True}
    agent, _ = _cycle_agent([ready], [{"symbol": "VEON", "change_pct": 6.0, "price": 10.0}], monkeypatch)

    def boom(**kwargs):
        raise RuntimeError("provider down")

    agent._enrich_candidate = boom
    cand = agent.run_cycle()["candidates"][0]
    assert "enrichment_failed" in cand["red_flags"]
    assert cand["readiness"]["status"] == "BLOCKED"
    assert cand["readiness"]["can_open"] is False


def test_tr15_07_hard_blockers_survive_flag_truncation():
    agent = _agent()
    agent._get_ohlc_cached = lambda **kw: (
        None, None,
        ["ohlc_local_cache_insufficient", "ohlc_local_cache_empty", "ohlc_local_cache_invalid_cols",
         "ohlc_local_reader_missing:x", "connection reset", "No historical data for X", "ohlc_fetch_error:y", "weird"],
    )
    agent._build_news_pack = lambda **kw: ({"available": False, "top": []}, {"news_ts": None})
    agent._build_calendar_pack = lambda **kw: ({"available": False, "earnings": {}, "macro_next_72h": []}, {"calendar_ts": None})
    out = _enrich(agent, "TESTUSDT", 0.0, -25.0, cfg_extra={"local_only_mode": True})
    assert len(out["red_flags"]) <= 14
    for blocker in ("ohlc_local_cache_insufficient", "missing_price", "missing_ohlc", "missing_indicators"):
        assert blocker in out["red_flags"], f"{blocker} was cut from the flag list"
        assert blocker in out["readiness"]["blockers"] or len(out["readiness"]["blockers"]) == 6
