"""Backtest tab audit (2026-11-15) regression tests.

Everything here runs on synthetic OHLCV frames: no network, no live data, no
engine threads. Each test pins one defect found while auditing the Backtest
tab end to end (frontend -> /api/backtest* -> actions_service -> engines).
"""
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")


# --------------------------------------------------------------------------- #
# Shared synthetic fixtures
# --------------------------------------------------------------------------- #

def _signal_cfg(**overrides):
    cfg = {
        "rsi_period": 14, "rsi_oversold": 45, "rsi_overbought": 55,
        "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
        "atr_period": 14,
        "profit_target_pct": 1.0, "stop_loss_pct": 0.5,
        "exit_mode": "ATR",
        "atr_multiplier_tp": 2.0, "atr_multiplier_sl": 1.5,
        "backtest_min_trades_for_validation": 30,
    }
    cfg.update(overrides)
    return cfg


class _FakeConfig:
    """Minimal ConfigManager stand-in: only what the legacy engine reads."""

    def __init__(self, **signal_overrides):
        self.signal = _signal_cfg(**signal_overrides)
        # Any MACD histogram sign qualifies, so the rule reduces to RSI + MACD.
        self.signal_variants = {
            "variant_a_macd_hist_min_pos": 1e9, "variant_b_macd_hist_min_pos": 1e9,
            "variant_c_macd_hist_min_pos": 0.0,
            "variant_a_macd_hist_min_neg": -1e9, "variant_b_macd_hist_min_neg": -1e9,
            "variant_c_macd_hist_min_neg": 0.0,
        }
        self.backtest = {"cache_ttl_seconds": 0, "max_tickers": 10}
        self.watchlist = {"yahoo": [], "binance": []}
        self.strategy_v2 = {
            "costs_by_asset": {
                "CRYPTO_FUTURES": {"commission_bps": 4.0, "entry_slippage_bps": 2.5,
                                   "slippage_sl_bps": 5.0, "slippage_tp_bps": 2.5},
                "US_STOCKS": {"commission_bps": 2.0, "entry_slippage_bps": 1.5,
                              "slippage_sl_bps": 3.0, "slippage_tp_bps": 1.5},
            },
        }
        self.bot = {}

    def get_ticker_settings(self, _ticker):
        return {}


def _random_walk_df(n=3000, seed=7, start="2026-01-05 00:00", freq="1min"):
    """Mean-reverting noisy walk: produces both oversold and overbought RSI
    readings with MACD turns, i.e. real BUY and SHORT signals."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 0.0012, size=n)
    wave = 0.004 * np.sin(np.arange(n) / 45.0)
    close = 100.0 * np.exp(np.cumsum(steps) * 0.6 + wave)
    open_ = np.concatenate([[close[0]], close[:-1]])
    spread = np.abs(rng.normal(0.0, 0.0006, size=n)) + 0.0002
    high = np.maximum(open_, close) * (1 + spread)
    low = np.minimum(open_, close) * (1 - spread)
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": 1000.0}, index=idx)


RISK_MODES = ["PRO", "STRONG", "MEDIUM", "RELAX"]
TRADE_MODES = ["PULLBACK", "BREAKOUT", "CONTRA"]


def _quick(config=None):
    from core.quick_backtester import QuickBacktester
    return QuickBacktester(config or _FakeConfig())


# --------------------------------------------------------------------------- #
# Legacy RSI/MACD engine (ALL / PRO / MEDIUM / STRONG / RELAX)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("risk_mode", RISK_MODES)
@pytest.mark.parametrize("trade_mode", TRADE_MODES)
def test_every_mode_applies_costs_and_timestamps_trades(risk_mode, trade_mode):
    df = _random_walk_df()
    res = _quick().quick_backtest("BTC-USD", risk_mode=risk_mode, is_crypto=True,
                                  days=2, trade_mode=trade_mode, df=df)
    assert not res.get("error")
    trades = res["signals"]
    assert len(trades) >= 5, "fixture must produce a usable sample"
    for t in trades:
        # Costs: commission both sides, and net is never better than gross.
        assert t["commission"] > 0
        assert t["net_pnl"] < t["gross_pnl"]
        assert t["slippage_pct"] > 0
        # Every trade is locatable in time (the Trades tab showed "-").
        entry_time = pd.Timestamp(t["entry_time"])
        exit_time = pd.Timestamp(t["exit_time"])
        assert exit_time > entry_time
        assert entry_time == df.index[t["index"]]
        assert t["exit_reason"] in {"stop_loss", "take_profit"}
    assert res["closed_trades"] == len(trades)
    assert res["insufficient_data"] is False
    assert res["bars"] == len(df)
    assert res["actual_span_days"] == pytest.approx(len(df) / 1440, abs=0.01)


@pytest.mark.parametrize("risk_mode", RISK_MODES)
def test_legacy_engine_has_no_look_ahead(risk_mode):
    """Causality: a trade that is closed by bar k must be identical whether or
    not the bars after k exist. Any indicator or level that peeks at later bars
    breaks this."""
    df = _random_walk_df()
    full = _quick().quick_backtest("BTC-USD", risk_mode=risk_mode, is_crypto=True,
                                   days=2, trade_mode="PULLBACK", df=df)["signals"]
    cut = 1700
    cut_time = df.index[cut - 1]
    partial = _quick().quick_backtest("BTC-USD", risk_mode=risk_mode, is_crypto=True,
                                      days=2, trade_mode="PULLBACK", df=df.iloc[:cut])["signals"]
    closed_before_cut = [t for t in full if pd.Timestamp(t["exit_time"]) <= cut_time]
    assert len(closed_before_cut) >= 3
    assert partial == closed_before_cut

    # And rewriting the future must not move any earlier trade.
    tampered = df.copy()
    tampered.iloc[cut:, :4] *= 1.5
    again = _quick().quick_backtest("BTC-USD", risk_mode=risk_mode, is_crypto=True,
                                    days=2, trade_mode="PULLBACK", df=tampered)["signals"]
    assert [t for t in again if pd.Timestamp(t["exit_time"]) <= cut_time] == closed_before_cut


def test_stop_is_assumed_first_when_stop_and_target_share_a_bar():
    """A bar that spans both levels is booked as the stop (conservative)."""
    from core.backtester import Backtester
    df = _random_walk_df(n=400)
    engine = Backtester(_FakeConfig(exit_mode="FIXED_PCT"))
    base = engine.run_backtest("BTC-USD", days=1, is_crypto=True, df=df)
    first = base["signals"][0]
    entry_bar = first["index"]
    wide = df.copy()
    nxt = wide.index[entry_bar + 1]
    wide.loc[nxt, "high"] = max(first["tp"], first["sl"]) * 1.01
    wide.loc[nxt, "low"] = min(first["tp"], first["sl"]) * 0.99
    wide.loc[nxt, "open"] = first["entry"]
    again = Backtester(_FakeConfig(exit_mode="FIXED_PCT")).run_backtest(
        "BTC-USD", days=1, is_crypto=True, df=wide)["signals"][0]
    assert again["index"] == entry_bar
    assert again["exit_reason"] == "stop_loss"
    assert again["result"] == "LOSS"


def test_too_few_bars_is_reported_as_insufficient_data_not_zero_trades():
    from core.backtester import Backtester
    df = _random_walk_df(n=80)
    res = Backtester(_FakeConfig()).run_backtest("BTC-USD", days=1, is_crypto=True, df=df)
    assert res["insufficient_data"] is True
    assert res["bars"] == 80
    assert "Insufficient data" in res["warnings"][0]
    quick = _quick().quick_backtest("BTC-USD", is_crypto=True, days=1, df=df)
    assert quick["insufficient_data"] is True
    assert quick["bars"] == 80


def test_position_open_on_the_last_bar_is_reported_not_dropped():
    from core.backtester import Backtester
    df = _random_walk_df(n=600)
    engine = Backtester(_FakeConfig(exit_mode="FIXED_PCT", profit_target_pct=50.0, stop_loss_pct=50.0))
    res = engine.run_backtest("BTC-USD", days=1, is_crypto=True, df=df)
    # Exits 50% away are never reached: the single entry stays open.
    assert res["signals"] == []
    assert res["open_trades"] == 1
    assert res["closed_trades"] == 0
    assert res["open_signal"]["result"] == "OPEN"
    assert res["wins"] == 0 and res["losses"] == 0


def test_custom_tp_sl_are_honoured_when_config_exit_mode_is_atr():
    """Cust TP % / Cust SL % were dead controls in ATR exit mode: the engine
    sized exits from ATR multipliers and never read the percentages."""
    df = _random_walk_df()
    qb = _quick(_FakeConfig(exit_mode="ATR"))
    default = qb.quick_backtest("BTC-USD", risk_mode="MEDIUM", is_crypto=True, days=2,
                                trade_mode="PULLBACK", df=df)
    custom = qb.quick_backtest("BTC-USD", risk_mode="MEDIUM", is_crypto=True, days=2,
                               trade_mode="PULLBACK", df=df,
                               custom_tp_pct=0.8, custom_sl_pct=0.4)
    assert default["effective_exit_mode"] == "ATR"
    assert default["custom_exit_applied"] is False
    assert custom["effective_exit_mode"] == "FIXED_PCT"
    assert custom["custom_exit_applied"] is True
    first = custom["signals"][0]
    sign = 1 if first["type"] == "BUY" else -1
    assert first["tp"] == pytest.approx(first["entry"] * (1 + sign * 0.008), rel=1e-9)
    assert first["sl"] == pytest.approx(first["entry"] * (1 - sign * 0.004), rel=1e-9)
    assert custom["signals"] != default["signals"]


# --------------------------------------------------------------------------- #
# OPENING RANGE (FVG)
# --------------------------------------------------------------------------- #

def _orb_cost_model():
    from core.trading_costs import resolve_trading_cost_model
    return resolve_trading_cost_model("AAPL", strategy_v2_config=_FakeConfig().strategy_v2)


def _five_min_day(rows, day="2026-03-02"):
    """rows: list of (HH:MM, open, high, low, close) in New York time."""
    import pytz
    et = pytz.timezone("America/New_York")
    idx = pd.DatetimeIndex([et.localize(pd.Timestamp(f"{day} {hhmm}").to_pydatetime()) for hhmm, *_ in rows])
    return pd.DataFrame(
        {"open": [r[1] for r in rows], "high": [r[2] for r in rows],
         "low": [r[3] for r in rows], "close": [r[4] for r in rows], "volume": 1000.0},
        index=idx,
    )


def _bullish_fvg(df, signal_hhmm="09:55"):
    signal_time = [t for t in df.index if t.strftime("%H:%M") == signal_hhmm][0]
    # Gap 100.0 .. 101.0, midpoint 100.5, stop below 99.0
    return {"type": "BULLISH", "fvg_high": 101.0, "fvg_low": 100.0, "midpoint": 100.5,
            "c1_low": 99.0, "c1_time": signal_time, "c3_time": signal_time,
            "signal_time": signal_time}


def test_orb_stop_hit_on_the_fill_bar_is_a_loss():
    """The fill bar was never checked for the stop: a bar that traded through
    the limit AND the stop was carried forward as an open winner."""
    from core.orb_fvg_backtester import simulate_trade
    df = _five_min_day([
        ("09:55", 101.5, 101.8, 101.2, 101.6),
        ("10:00", 101.4, 101.5, 98.5, 98.8),    # fills at 100.5, then trades through the 98.99 stop
        ("10:05", 99.0, 104.0, 98.9, 103.9),    # would have reached the target
        ("15:45", 103.9, 104.0, 103.5, 103.8),
    ])
    trade = simulate_trade(_bullish_fvg(df), df, "MIDPOINT", 2.0, df.index[0].date(),
                           cost_model=_orb_cost_model())
    assert trade["result"] == "LOSS"
    assert trade["exit_time"] == trade["entry_time"]
    assert trade["r_gross"] == pytest.approx(-1.0)
    assert trade["r_multiple"] < -1.0  # costs make the loss slightly larger than 1R


def test_orb_gap_through_the_stop_fills_at_the_open_and_costs_are_applied():
    from core.orb_fvg_backtester import simulate_trade
    df = _five_min_day([
        ("09:55", 101.5, 101.8, 101.2, 101.6),
        ("10:00", 101.4, 101.5, 100.4, 100.9),  # fills at 100.5
        ("10:05", 97.0, 97.5, 96.5, 97.2),      # opens far below the 98.99 stop
        ("15:45", 97.2, 97.3, 97.0, 97.1),
    ])
    gross_only = simulate_trade(_bullish_fvg(df), df, "MIDPOINT", 2.0, df.index[0].date())
    assert gross_only["exit_level"] == pytest.approx(97.0)   # the open, not the 98.99 stop
    assert gross_only["r_gross"] < -2.0                      # worse than a clean -1R stop
    assert gross_only["costs_applied"] is False
    assert gross_only["r_multiple"] == pytest.approx(gross_only["r_gross"])

    net = simulate_trade(_bullish_fvg(df), df, "MIDPOINT", 2.0, df.index[0].date(),
                         cost_model=_orb_cost_model())
    assert net["costs_applied"] is True
    assert net["commission"] > 0
    assert net["exit_price"] < net["exit_level"]             # adverse exit slippage
    assert net["r_multiple"] < net["r_gross"]
    assert net["pnl_pct"] == pytest.approx(net["pnl"] / net["entry_price"] * 100.0)


def test_orb_trade_never_rolls_into_the_next_session():
    from core.orb_fvg_backtester import simulate_trade
    day1 = _five_min_day([
        ("09:55", 101.5, 101.8, 101.2, 101.6),
        ("10:00", 101.4, 101.5, 100.4, 100.9),  # fills at 100.5
        ("12:55", 100.9, 101.0, 100.6, 100.7),  # half day: no 15:45 bar
    ])
    day2 = _five_min_day([("09:45", 110.0, 111.0, 109.0, 110.5)], day="2026-03-03")
    df = pd.concat([day1, day2])
    trade = simulate_trade(_bullish_fvg(df), df, "MIDPOINT", 2.0, day1.index[0].date())
    assert trade["result"] == "TIME_EXIT"
    assert trade["exit_time"] == day1.index[-1]              # same session
    assert trade["exit_level"] == pytest.approx(100.7)       # not next morning's 110.5


def test_orb_run_reports_net_expectancy_and_flags_small_samples():
    from core.orb_fvg_backtester import run_orb_fvg_backtest
    from tests.unit.test_orb_fvg_reactivated import _synthetic_intraday_df
    df = _synthetic_intraday_df(days=5)
    res = run_orb_fvg_backtest("AAPL", days=5, entry_type="MIDPOINT", df_1min=df,
                               cost_model=_orb_cost_model())
    assert "error" not in res
    assert res["costs_applied"] is True
    assert res["cost_model"]["asset_class"] == "US_STOCKS"
    assert res["evidence"]["sufficient"] is False
    assert res["evidence"]["minimum_sample"] == 30
    assert any("Insufficient evidence" in w for w in res["warnings"])
    assert res["actual_span_days"] > 3
    for t in res["trades"]:
        assert t["r_multiple"] < t["r_gross"]
    # Nothing Romanian leaks into an English UI.
    assert not any("limitat" in w or "Eroare" in w for w in res["warnings"])


# --------------------------------------------------------------------------- #
# api.actions_service: request validation, history, error states
# --------------------------------------------------------------------------- #

@pytest.fixture
def svc(monkeypatch, tmp_path):
    """actions_service wired to a fake config and a private history file."""
    from api import actions_service as module
    from core.quick_backtester import QuickBacktester

    config = _FakeConfig()
    monkeypatch.setattr(module, "get_shared_config", lambda: config)
    monkeypatch.setattr(module, "BACKTEST_RESULTS_PATH", tmp_path / "backtest_results.json")

    def fetch(self, ticker, is_crypto=False, days=30, ui_interval="5min"):
        frame = module._test_frames.get(str(ticker).upper())
        if frame is None:
            return None, [f"No historical data for {ticker}"]
        out = frame.copy()
        out["timestamp"] = out.index
        return out, []

    monkeypatch.setattr(module, "_test_frames", {}, raising=False)
    monkeypatch.setattr(QuickBacktester, "fetch_backtest_data", fetch)
    return module


def _history(module):
    path = module.BACKTEST_RESULTS_PATH
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


@pytest.mark.parametrize("tp,sl,fragment", [
    (0, None, "take profit must be greater than 0"),
    (-1.5, None, "take profit must be greater than 0"),
    (None, 0, "stop loss must be greater than 0"),
    (None, -2, "stop loss must be greater than 0"),
    (None, 150, "stop loss must be at most"),
    (10_000, None, "take profit must be at most"),
    (float("nan"), None, "finite"),
    (None, float("inf"), "finite"),
])
def test_custom_tp_sl_that_cannot_be_meant_are_refused(svc, tp, sl, fragment):
    res = svc.run_backtest(days=7, tickers=["BTC-USD"], compare_all=True,
                           custom_tp_pct=tp, custom_sl_pct=sl)
    assert fragment in res["error"].lower()
    assert _history(svc) == []


@pytest.mark.parametrize("days", [0, -5, 5000])
def test_days_outside_the_supported_range_are_refused(svc, days):
    res = svc.run_backtest(days=days, tickers=["BTC-USD"], compare_all=True)
    assert "days must be between" in res["error"].lower()
    wf = svc.run_walk_forward_backtest("BTC-USD", days=days)
    assert wf["status"] == "error" and "days must be between" in wf["error"].lower()


def test_blank_ticker_is_refused(svc):
    assert "ticker" in svc.run_backtest(days=7, tickers=["   "], compare_all=True)["error"].lower()
    assert svc.run_walk_forward_backtest("  ")["status"] == "error"


def test_compare_all_with_no_data_is_an_error_and_is_not_saved(svc):
    """An unknown ticker used to come back as a normal comparison with zero
    rows, was stored under Recent simulations and rendered an empty top row."""
    res = svc.run_backtest(days=30, tickers=["NOSUCH"], compare_all=True)
    assert "No historical price data was returned for NOSUCH" in res["error"]
    assert "results" not in res
    assert _history(svc) == []


def test_compare_all_reports_real_coverage_evidence_and_canonical_symbol(svc):
    svc._test_frames["BTC-USD"] = _random_walk_df()          # ~2.1 days of 1m bars
    res = svc.run_backtest(days=90, tickers=["btcusdt"], compare_all=True)
    assert "error" not in res
    assert res["ticker"] == "BTC-USD"                        # BTCUSDT == BTC-USD
    assert len(res["results"]) == 12                         # 4 risk modes x 3 exit profiles
    coverage = res["data_coverage"]
    assert coverage["requested_days"] == 90
    assert coverage["actual_span_days"] == pytest.approx(2.08, abs=0.02)
    assert coverage["truncated"] is True
    assert any("Requested 90 days" in w for w in res["warnings"])
    for row in res["results"]:
        assert row["evidence"]["minimum_sample"] == 30
        assert row["evidence"]["sample_size"] == row["closed_trades"] == len(row["trades"])
        assert row["evidence"]["sufficient"] == (row["closed_trades"] >= 30)
        assert row["effective_exit_mode"] == "ATR"
        assert row["simulation_scope"] == "RSI_MACD_ENTRIES_WITH_EXIT_PROFILE_VARIANTS"
    # Rows with a usable sample are listed before thin ones.
    flags = [row["evidence"]["sufficient"] for row in res["results"]]
    assert flags == sorted(flags, reverse=True)
    assert len(_history(svc)) == 1


def test_standard_run_with_no_data_is_an_error_not_zero_trades(svc, monkeypatch):
    from core.backtester import Backtester
    monkeypatch.setattr(Backtester, "_fetch_stock_historical", lambda self, t, d: None)
    res = svc.run_backtest(days=30, risk_mode="MEDIUM", trade_mode="PULLBACK", tickers=["NOSUCH"])
    assert "Backtest could not run for NOSUCH" in res["error"]
    assert res["failed_tickers"] == [{"ticker": "NOSUCH", "reason": "no historical price data was returned"}]
    assert _history(svc) == []


def test_standard_run_applies_typed_tp_sl_even_when_config_exit_mode_is_atr(svc, monkeypatch):
    from core.backtester import Backtester
    monkeypatch.setattr(Backtester, "_fetch_crypto_historical", lambda self, t, d: _random_walk_df())
    res = svc.run_backtest(days=2, risk_mode="MEDIUM", trade_mode="PULLBACK", tickers=["BTC-USD"],
                           custom_tp_pct=0.8, custom_sl_pct=0.4)
    assert "error" not in res
    row = res["signals"][0]
    assert row["effective_exit_mode"] == "FIXED_PCT"
    assert row["custom_exit_applied"] is True
    assert row["effective_tp_pct"] == pytest.approx(0.8)
    assert row["effective_sl_pct"] == pytest.approx(0.4)
    assert res["ticker"] == "BTC-USD"
    assert res["evidence"]["sample_size"] == len(res["trade_list"])
    assert res["data_coverage"]["bars"] == 3000


def test_history_limit_keeps_the_newest_and_strips_nested_trades(svc):
    for n in (1, 2, 3):
        svc.save_backtest_result({
            "mode": "comparison", "ticker": f"T{n}", "days": 7,
            "results": [{"risk_mode": "MEDIUM", "trade_mode": "PULLBACK",
                         "signals": [{"entry": 1.0}], "trades": [{"entry": 1.0}]}],
        })
    assert [r["ticker"] for r in svc.get_backtest_history()] == ["T3", "T2", "T1"]
    assert [r["ticker"] for r in svc.get_backtest_history(limit=1)] == ["T3"]   # was T1 (oldest)
    light = svc.get_backtest_history(include_trades=False)
    assert "signals" not in light[0]["results"][0]
    assert "trades" not in light[0]["results"][0]
    assert "trade_list" not in light[0]["results"][0]
    assert light[0]["results"][0]["risk_mode"] == "MEDIUM"


def test_failed_runs_are_not_history_and_orb_results_are_storable(svc):
    import datetime as dt
    svc.save_backtest_result({"mode": "standard", "error": "boom"})
    assert _history(svc) == []
    # date / Timestamp / numpy scalars / NaN: this is what an ORB result holds
    # and what used to make json.dump fail, so ORB runs were never listed.
    svc.save_backtest_result({
        "mode": "orb_fvg", "ticker": "AAPL", "days": 5, "avg_r": np.float64(0.25),
        "wins": np.int64(2), "bad": float("nan"),
        "trades": [{"date": dt.date(2026, 3, 2), "entry_time": pd.Timestamp("2026-03-02 10:00", tz="UTC"),
                    "r_multiple": np.float64(1.9)}],
    })
    stored = _history(svc)
    assert len(stored) == 1
    assert stored[0]["trades"][0]["date"] == "2026-03-02"
    assert stored[0]["trades"][0]["entry_time"].startswith("2026-03-02T10:00")
    assert stored[0]["wins"] == 2 and stored[0]["avg_r"] == 0.25
    assert stored[0]["bad"] is None


def test_faithful_variant_mode_layers_the_variant_overrides():
    from api import actions_service as module
    config = _FakeConfig(
        scoring={"min_probability": 0.9, "mtf_alignment_min": 6},
        variant_strict={"scoring": {"min_probability": 0.7}},
        variant_custom={"scoring": {"min_probability": 0.5}, "regime": {"allow_volatile": True}},
    )
    strict, applied = module._variant_replay_config(config, "STRICT")
    assert applied is True
    assert strict.signal["scoring"] == {"min_probability": 0.7, "mtf_alignment_min": 6}
    custom, applied = module._variant_replay_config(config, "custom")
    assert applied is True
    assert custom.signal["scoring"]["min_probability"] == 0.5
    assert custom.signal["regime"] == {"allow_volatile": True}
    assert custom.signal["variant_mode"] == "CUSTOM"
    assert custom.strategy_v2 is config.strategy_v2          # everything else delegated
    assert config.signal["scoring"]["min_probability"] == 0.9  # base untouched
    same, applied = module._variant_replay_config(_FakeConfig(), "CUSTOM")
    assert applied is False and isinstance(same, _FakeConfig)


def test_faithful_no_data_is_one_fetch_and_a_readable_error(svc, monkeypatch):
    import core.strategy_backtester as sb
    calls = []
    monkeypatch.setattr(sb, "build_dataframe", lambda *a, **k: calls.append(a) or None)
    monkeypatch.setattr(sb, "backtest", lambda *a, **k: pytest.fail("must not replay without data"))
    res = svc.run_backtest(days=7, risk_mode="FAITHFUL", tickers=["BTC-USD"])
    assert len(calls) == 1
    assert res["mode"] == "faithful"
    assert "No usable Binance 1-minute history" in res["error"]
    assert _history(svc) == []


# --------------------------------------------------------------------------- #
# Walk-forward
# --------------------------------------------------------------------------- #

def test_walk_forward_selects_on_train_only_and_tests_out_of_sample(svc):
    df = _random_walk_df(n=6 * 1440, seed=11)                # 6 days of 1m bars
    svc._test_frames["BTC-USD"] = df
    kwargs = dict(days=6, train_days=2, test_days=1, max_windows=3,
                  risk_modes=["MEDIUM", "RELAX"], trade_modes=["PULLBACK", "CONTRA"])
    res = svc.run_walk_forward_backtest("BTC-USD", **kwargs)
    assert res["status"] == "ok"
    assert len(res["windows"]) == 3
    for w in res["windows"]:
        # train and test never overlap, and test follows train
        assert pd.Timestamp(w["train_end"]) == pd.Timestamp(w["test_start"])
        assert pd.Timestamp(w["train_start"]) < pd.Timestamp(w["train_end"]) < pd.Timestamp(w["test_end"])
        # the selection is a real, evidence-based score (it used to be 0 for
        # every candidate, so the first combination always "won")
        assert w["train_summary"]["closed_trades"] > 0
        assert w["best_strategy"]["train_score"] != 0
    summary = res["summary"]
    assert summary["total_trades"] == sum(
        w["test_summary"]["wins"] + w["test_summary"]["losses"] for w in res["windows"])
    weighted = sum(w["test_summary"]["expectancy_r"] * w["test_summary"]["closed_trades"]
                   for w in res["windows"]) / summary["total_trades"]
    assert summary["expectancy_r"] == pytest.approx(weighted, abs=1e-3)
    assert res["evidence"]["sample_size"] == summary["total_trades"]

    # Leakage: rewriting everything after the first training window must not
    # change what was selected on it.
    first = res["windows"][0]
    tampered = df.copy()
    after_train = tampered.index >= pd.Timestamp(first["train_end"])
    tampered.loc[after_train, ["open", "high", "low", "close"]] *= 1.3
    svc._test_frames["BTC-USD"] = tampered
    again = svc.run_walk_forward_backtest("BTC-USD", **kwargs)["windows"][0]
    assert again["best_strategy"]["risk_mode"] == first["best_strategy"]["risk_mode"]
    assert again["best_strategy"]["trade_mode"] == first["best_strategy"]["trade_mode"]
    assert again["train_summary"] == first["train_summary"]


def test_walk_forward_without_enough_history_says_so(svc):
    svc._test_frames["BTC-USD"] = _random_walk_df(n=6 * 1440)   # 6 days, window needs 30
    res = svc.run_walk_forward_backtest("BTC-USD", days=60, train_days=20, test_days=10)
    assert res["status"] == "insufficient_data"
    assert "Only 6.0 days of price history" in res["reason"]
    assert res["windows"] == []
    assert res["summary"]["win_rate_trades"] is None          # not a confident 0%
    assert res["summary"]["expectancy_r"] is None
    assert res["evidence"]["sufficient"] is False


def test_walk_forward_windows_with_too_few_bars_are_skipped_not_scored(svc):
    # Hourly bars: a 2-day slice has 48 bars, under the engine minimum of 100.
    svc._test_frames["AAPL"] = _random_walk_df(n=24 * 20, freq="1h")
    res = svc.run_walk_forward_backtest("AAPL", days=20, train_days=2, test_days=2)
    assert res["status"] == "insufficient_data"
    assert res["summary"]["skipped_reasons"]["too_few_bars"] >= 1
    assert "fewer than 100 price bars" in res["reason"]


def test_walk_forward_rejects_a_period_shorter_than_one_window(svc):
    res = svc.run_walk_forward_backtest("BTC-USD", days=10, train_days=20, test_days=10)
    assert res["status"] == "error"
    assert "shorter than one train + test window" in res["error"]


# --------------------------------------------------------------------------- #
# Strategy performance cache (expectancy must be net, like win rate / PF)
# --------------------------------------------------------------------------- #

def test_performance_cache_expectancy_is_net_of_costs(tmp_path):
    from core.strategy_v2.performance_cache import StrategyPerformanceCache
    cache = StrategyPerformanceCache(tmp_path / "perf.json")
    # +0.10% gross move on a 10,000 notional = +10 gross, but 12 of commission
    # makes it a 2 net LOSS. It was booked as a loss with a POSITIVE R.
    cache.upsert_from_trade_result({
        "ticker": "BTC-USD", "strategy_type": "S1", "direction": "LONG",
        "asset_class": "CRYPTO_FUTURES", "net_pnl": -2.0, "gross_pnl": 10.0,
        "pnl_pct_notional": 0.10, "stop_loss_pct": 1.0,
    })
    entry = cache.get_entry("BTC-USD", "CRYPTO_FUTURES", "S1", "LONG")
    assert entry["win_rate"] == 0.0
    assert entry["expectancy_R"] == pytest.approx(-0.02)      # was +0.10
    assert entry["expectancy_basis"] == "net"

    # With nothing but the raw move the old (gross) figure is kept and labelled.
    legacy = StrategyPerformanceCache(tmp_path / "legacy.json")
    legacy.upsert_from_trade_result({
        "ticker": "BTC-USD", "strategy_type": "S1", "direction": "LONG",
        "asset_class": "CRYPTO_FUTURES", "net_pnl": -2.0,
        "pnl_pct_notional": 0.10, "stop_loss_pct": 1.0,
    })
    old = legacy.get_entry("BTC-USD", "CRYPTO_FUTURES", "S1", "LONG")
    assert old["expectancy_R"] == pytest.approx(0.10)
    assert old["expectancy_basis"] == "gross_price_move"


def test_one_minute_bars_are_not_annualised_as_daily_bars():
    from core.backtest_engine import BacktestEngine
    assert BacktestEngine._bars_per_year("1m") == pytest.approx(252 * 390)
    assert BacktestEngine._bars_per_year("1wk") == 52
    assert BacktestEngine._bars_per_year("1d") == 252


# --------------------------------------------------------------------------- #
# agents.backtest_agent
# --------------------------------------------------------------------------- #

def _agent(monkeypatch, quick_result):
    from agents.backtest_agent import BacktestAgent

    class _Quick:
        def quick_backtest(self, **_kwargs):
            return dict(quick_result)

    agent = BacktestAgent(config_manager=_FakeConfig())
    monkeypatch.setattr(agent, "_get_quick_backtester", lambda: _Quick())
    return agent


def test_agent_metrics_use_net_returns_not_the_bare_price_move():
    from agents.backtest_agent import BacktestAgent
    # +0.05% price move, but commission turns it into a net loss.
    net_loser = {"type": "BUY", "entry": 100.0, "exit_price": 100.05, "qty": 10.0,
                 "net_pnl": -0.30, "result": "LOSS"}
    winner = {"type": "BUY", "entry": 100.0, "exit_price": 102.0, "qty": 10.0,
              "net_pnl": 19.2, "result": "WIN"}
    assert BacktestAgent._trade_return_pct(net_loser, 100.0, 100.05) == pytest.approx(-0.03)
    agent = BacktestAgent(config_manager=_FakeConfig())
    # The losing trade used to be averaged into the losses as +0.05 (abs of a
    # positive move) and counted as profit in the profit factor.
    avg_win, avg_loss = agent._calculate_avg_returns([winner, net_loser])
    assert avg_win == pytest.approx(1.92)
    assert avg_loss == pytest.approx(0.03)
    assert agent._calculate_profit_factor([winner, net_loser]) == pytest.approx(1.92 / 0.03)
    # Records without sizing fall back to the price move (unchanged behaviour).
    bare = {"type": "SHORT", "entry": 100.0, "exit_price": 98.0}
    assert BacktestAgent._trade_return_pct(bare, 100.0, 98.0) == pytest.approx(2.0)


def test_agent_scenario_without_data_fails_instead_of_passing(monkeypatch):
    from agents.backtest_agent import BacktestScenario
    agent = _agent(monkeypatch, {"wins": 0, "losses": 0, "total_signals": 0, "win_rate": 0.0,
                                 "signals": [], "insufficient_data": True, "bars": 12,
                                 "min_bars_required": 100})
    scenario = BacktestScenario(name="no-data", ticker="NOSUCH", is_crypto=False,
                                days=7, risk_mode="MEDIUM", trade_mode="PULLBACK")
    result = agent._run_scenario(scenario)
    assert "Insufficient price data (12 bars" in result.error
    assert any("Scenario error" in e for e in agent._validate_result(result))
    quick = agent.quick_test("NOSUCH")
    assert quick["status"] == "FAILED"
    assert "Insufficient price data" in quick["error"]


# --------------------------------------------------------------------------- #
# Variant gate must not depend on the price scale of the instrument
# --------------------------------------------------------------------------- #

def _default_variant_config(**signal_overrides):
    """Fake config with the PRODUCTION variant thresholds (0.034 / 0.032 / 0.0001)."""
    from core.config import DEFAULT_CONFIG
    config = _FakeConfig(exit_mode="FIXED_PCT", **signal_overrides)
    config.signal_variants = dict(DEFAULT_CONFIG["signal_variants"])
    return config


@pytest.mark.parametrize("scale", [0.0005, 0.2, 600.0])
def test_variant_gate_is_scale_invariant_so_cheap_coins_can_trade(scale):
    """The MACD histogram is in price units and was compared raw against
    thresholds chosen for ~$20 instruments: a coin under ~$0.1 could never
    pass even the lowest one (0 backtest trades -> Trade Radar blocked it with
    backtest_no_trades), while for BTC any histogram passed the strictest."""
    from core.backtester import Backtester
    from core.scoring_system import MACD_REFERENCE_PRICE

    reference = _random_walk_df()
    reference[["open", "high", "low", "close"]] *= MACD_REFERENCE_PRICE / 100.0   # ~$20
    scaled = reference.copy()
    scaled[["open", "high", "low", "close"]] *= scale                             # $0.01 .. $12,000

    base = Backtester(_default_variant_config()).run_backtest(
        "REF-USD", days=2, is_crypto=True, df=reference)["signals"]
    other = Backtester(_default_variant_config()).run_backtest(
        "PENGU-USD", days=2, is_crypto=True, df=scaled)["signals"]

    assert len(base) >= 5, "fixture must trade at the reference price"
    # Same candles at another price level: same entries, same variants.
    assert [t["index"] for t in other] == [t["index"] for t in base]
    assert [t["variant"] for t in other] == [t["variant"] for t in base]
    assert [t["type"] for t in other] == [t["type"] for t in base]


def test_variant_gate_thresholds_keep_their_meaning_at_the_reference_price():
    from core.backtester import Backtester
    from core.config import DEFAULT_CONFIG
    from core.scoring_system import MACD_REFERENCE_PRICE

    engine = Backtester(_FakeConfig())
    cfg = dict(DEFAULT_CONFIG["signal_variants"])
    ref = MACD_REFERENCE_PRICE
    # At the reference price nothing changes.
    assert engine._check_variant(0.05, "BUY", cfg, price=ref) == "A"
    assert engine._check_variant(0.033, "BUY", cfg, price=ref) == "B"
    assert engine._check_variant(0.001, "BUY", cfg, price=ref) == "C"
    assert engine._check_variant(0.00005, "BUY", cfg, price=ref) is None
    assert engine._check_variant(-0.05, "SHORT", cfg, price=ref) == "A"
    # A $0.02 coin: the same RELATIVE histogram gets the same variant
    # (compared raw, 0.00005 never reached 0.0001 and the coin never traded).
    assert engine._check_variant(0.05 / 1000, "BUY", cfg, price=ref / 1000) == "A"
    assert engine._check_variant(0.001 / 1000, "BUY", cfg, price=ref / 1000) == "C"
    assert engine._check_variant(-0.001 / 1000, "SHORT", cfg, price=ref / 1000) == "C"
    # A $60,000 coin: a histogram that is negligible relative to price no
    # longer passes the strictest threshold just because it is a big number.
    assert engine._check_variant(0.05, "BUY", cfg, price=60_000.0) is None
    assert engine._check_variant(0.05 * 3000, "BUY", cfg, price=60_000.0) == "A"
    # Callers without a price keep the raw comparison.
    assert engine._check_variant(0.05, "BUY", cfg) == "A"
    assert engine._check_variant(0.05, "BUY", cfg, price=float("nan")) == "A"
