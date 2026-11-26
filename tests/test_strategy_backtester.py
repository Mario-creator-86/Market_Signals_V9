"""Fast, network-free unit tests for the faithful strategy backtester's pure logic."""
import pandas as pd
import pytest

from core import strategy_backtester as sb

pytestmark = pytest.mark.unit


def test_binance_symbol_mapping():
    assert sb._to_binance_symbol("BTC-USD") == "BTCUSDT"
    assert sb._to_binance_symbol("eth-usd") == "ETHUSDT"
    assert sb._to_binance_symbol("SOLUSDT") == "SOLUSDT"


def test_summarize_metrics():
    trades = [
        {"net_pct": 3.0, "r": 1.5, "reason": "take_profit"},
        {"net_pct": -1.0, "r": -1.0, "reason": "stop_loss"},
        {"net_pct": 2.0, "r": 1.0, "reason": "trailing_stop"},
        {"net_pct": -1.0, "r": -1.0, "reason": "stop_loss"},
    ]
    s = sb._summarize("X-USD", 10, trades)
    assert s["trades"] == 4
    assert s["win_rate"] == 50.0
    # gross win 5.0 / gross loss 2.0 -> PF 2.5
    assert s["profit_factor"] == 2.5
    assert s["total_net_pct"] == 3.0
    assert s["exit_mix"]["stop_loss"] == 2


def test_summarize_empty():
    s = sb._summarize("X-USD", 10, [])
    assert s["trades"] == 0 and s["win_rate"] == 0.0 and s["profit_factor"] is None


def test_summarize_avg_win_loss_r_uses_net_r():
    """avg_win_r/avg_loss_r must be NET R-multiples (post round-trip commission),
    consistent with the net_pct-based win/loss membership. Pre-fix they used the
    GROSS r, so a trade that grosses +0.4R but nets negative after commission was
    classified as a loss (net_pct<=0) yet carried a POSITIVE R into loss_rs,
    inflating avg_loss_r toward zero. Here trade B grosses +0.4R but its r_net
    is -0.1 (commission flips it to a net loss) — it must land in losses with a
    NEGATIVE r_net, not pollute loss_rs with a positive gross R.
    """
    trades = [
        # A: clean net winner (r_net 1.2)
        {"net_pct": 2.4, "r": 1.5, "r_net": 1.2, "reason": "take_profit"},
        # B: gross winner (r 0.4) but net loser after commission (r_net -0.1)
        {"net_pct": -0.2, "r": 0.4, "r_net": -0.1, "reason": "time_exit"},
        # C: clean net loser (r_net -1.0)
        {"net_pct": -2.0, "r": -1.1, "r_net": -1.0, "reason": "stop_loss"},
    ]
    s = sb._summarize("X-USD", 10, trades)
    # Membership is net_pct-based: only A is a win (net_pct>0); B and C are losses.
    assert s["wins"] == 1
    assert s["losses"] == 2
    # avg_win_r uses r_net of the single winner (1.2), not its gross r (1.5).
    assert s["avg_win_r"] == pytest.approx(1.2, abs=1e-6)
    # avg_loss_r uses r_net of B and C: mean(-0.1, -1.0) = -0.55 — negative,
    # NOT contaminated by B's positive gross r (0.4) which pre-fix gave
    # mean(0.4, -1.1) = -0.35 (a less-negative, wrong avg loss).
    assert s["avg_loss_r"] == pytest.approx(-0.55, abs=1e-6)
    assert s["avg_loss_r"] < -0.4  # guard: must be clearly negative, not near zero


def test_summarize_expectancy_r_uses_net_r_m3():
    """M3: expectancy_r must be the NET R-multiple expectancy (post round-trip
    commission), consistent with avg_win_r/avg_loss_r which are already net.
    Pre-fix expectancy_r aliased GROSS avg_r, so commission-heavy strategies
    showed an inflated headline expectancy while avg_win_r/avg_loss_r told the
    net story. avg_R stays gross for legacy/parity consumers."""
    trades = [
        # gross r sums to 0.4 -> gross avg_r = 0.4/3 = 0.1333
        # net r sums to 0.1 -> net expectancy = 0.1/3 = 0.0333
        {"net_pct": 2.0, "r": 1.2, "r_net": 0.9, "reason": "take_profit"},
        {"net_pct": -1.0, "r": -0.8, "r_net": -1.0, "reason": "stop_loss"},
        {"net_pct": 0.0, "r": 0.0, "r_net": 0.1, "reason": "time_exit"},
    ]
    s = sb._summarize("X-USD", 10, trades)
    # avg_R stays GROSS: mean(1.2, -0.8, 0.0) = 0.1333
    assert s["avg_R"] == pytest.approx(0.1333, abs=1e-3), (
        "avg_R must remain the GROSS R-multiple average (legacy alias)"
    )
    # expectancy_r is now NET: mean(0.9, -1.0, 0.1) = 0.0
    assert s["expectancy_r"] == pytest.approx(0.0, abs=1e-3), (
        "M3: expectancy_r must be the NET R-multiple expectancy (post-commission), "
        "not the gross avg_r"
    )
    assert s["expectancy_r"] != s["avg_R"], (
        "expectancy_r and avg_R must differ when commissions move net != gross"
    )


def test_summarize_expectancy_r_falls_back_to_gross_r_when_no_r_net():
    """Backward compat: trade dicts that predate the r_net field fall back to
    gross r for the net expectancy too (no KeyError, no crash)."""
    trades = [{"net_pct": 2.0, "r": 1.0, "reason": "take_profit"}]  # no r_net
    s = sb._summarize("X-USD", 10, trades)
    assert s["expectancy_r"] == pytest.approx(1.0, abs=1e-6)
    assert s["avg_R"] == pytest.approx(1.0, abs=1e-6)


def test_htf_trend_series_lookahead_safe():
    """Trend per TF bar is keyed by the bar's CLOSE time (open + interval), not open time."""
    idx = pd.date_range("2026-01-01", periods=60, freq="5min", tz="UTC")
    # rising then falling close -> MACD hist sign flips; we only assert structure + timing
    close = pd.Series(list(range(1, 41)) + list(range(40, 20, -1)), index=idx, dtype="float64")
    df_tf = pd.DataFrame({"close": close})
    cfg = {"macd_fast": 12, "macd_slow": 26, "macd_signal": 9}
    out = sb._htf_trend_series(df_tf, 5, cfg)
    assert set(out["trend"].unique()) <= {"BULL", "BEAR", "N/A"}
    # close_time must be 5 min AFTER the bar's open time (no look-ahead into a forming bar)
    assert (out["close_time"] - pd.Timedelta(minutes=5)).isin(idx).all()
