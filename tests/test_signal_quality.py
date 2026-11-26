"""
tests/test_signal_quality.py  —  Stage 7 Signal Quality Tests

Tests for core/signal_quality.py:
  - R-multiple cost model (WIN/LOSS/BREAKEVEN/unknown)
  - Windowed filtering (7d / 30d)
  - win_rate, profit_factor, avg_R, expectancy_R
  - max_drawdown on cumulative-R equity curve
  - Alert generation (win_rate_degraded, profit_factor_low, drawdown_excessive)
  - Empty / one-trade edge cases
  - compute_quality_report top-level aggregation
  - Determinism: same input → same output
"""
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.signal_quality import (
    compute_quality_metrics,
    compute_quality_report,
    _r_multiple,
    _compute_max_drawdown,
    ALERT_WIN_RATE_THRESHOLD,
    ALERT_PROFIT_FACTOR_MIN,
    ALERT_MAX_DRAWDOWN_PCT,
)


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────

def _iso(days_ago: float = 0.0) -> str:
    """ISO timestamp N days ago."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _sig(status: str, tp: float = 4.0, sl: float = 2.0,
         risk_mode: str = "MEDIUM", days_ago: float = 1.0,
         ticker: str = "BTC-USD", action: str = "BUY") -> Dict[str, Any]:
    return {
        "status": status,
        "tp_pct": tp,
        "sl_pct": sl,
        "risk_mode": risk_mode,
        "action": action,
        "ticker": ticker,
        "created_at": _iso(days_ago),
        "closed_at": _iso(days_ago),
    }


SAMPLE_WINS = [_sig("WIN", tp=4, sl=2) for _ in range(6)]    # R=2.0 each
SAMPLE_LOSSES = [_sig("LOSS", tp=4, sl=2) for _ in range(4)]  # R=-1.0 each
BALANCED_10 = SAMPLE_WINS + SAMPLE_LOSSES


# ────────────────────────────────────────────────────────────
# 1. R-multiple cost model
# ────────────────────────────────────────────────────────────

class TestRMultiple:
    def test_win_tp_sl_ratio(self):
        sig = _sig("WIN", tp=3.0, sl=1.5)
        assert _r_multiple(sig) == pytest.approx(2.0)

    def test_win_capped_at_5R(self):
        sig = _sig("WIN", tp=20.0, sl=1.0)
        assert _r_multiple(sig) == 5.0

    def test_loss_is_minus_one(self):
        sig = _sig("LOSS")
        assert _r_multiple(sig) == -1.0

    def test_live_lost_status_is_minus_one(self):
        sig = _sig("LOST")
        assert _r_multiple(sig) == -1.0

    def test_result_takes_precedence_over_generic_closed_status(self):
        sig = _sig("CLOSED")
        sig["result"] = "WIN"
        assert _r_multiple(sig) == pytest.approx(2.0)

    def test_absolute_prices_produce_reward_risk_ratio(self):
        sig = {"status": "WIN", "entry": 100.0, "tp": 106.0, "sl": 97.0}
        assert _r_multiple(sig) == pytest.approx(2.0)

    def test_breakeven_zero(self):
        sig = _sig("BREAKEVEN")
        assert _r_multiple(sig) == 0.0

    def test_unknown_status_nan(self):
        import math
        sig = _sig("OPEN")
        assert math.isnan(_r_multiple(sig))

    def test_win_no_sl_defaults_to_1R(self):
        sig = {"status": "WIN", "tp_pct": 4.0, "sl_pct": 0}
        assert _r_multiple(sig) == 1.0


# ────────────────────────────────────────────────────────────
# 2. Edge cases: empty / single trade
# ────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_input_returns_zeros(self):
        result = compute_quality_metrics([], 30)
        assert result["sample_size"] == 0
        assert result["win_rate"] is None
        assert result["profit_factor"] is None
        assert result["alerts"] == []

    def test_single_win_no_drawdown(self):
        result = compute_quality_metrics([_sig("WIN")], 30)
        assert result["wins"] == 1
        assert result["losses"] == 0
        assert result["win_rate"] == pytest.approx(1.0)
        assert result["max_drawdown"] is None   # need >= 2 trades

    def test_single_loss(self):
        result = compute_quality_metrics([_sig("LOSS")], 30)
        assert result["wins"] == 0
        assert result["losses"] == 1
        assert result["win_rate"] == pytest.approx(0.0)


# ────────────────────────────────────────────────────────────
# 3. Win rate
# ────────────────────────────────────────────────────────────

class TestWinRate:
    def test_win_rate_60pct(self):
        result = compute_quality_metrics(BALANCED_10, 30)
        assert result["win_rate"] == pytest.approx(0.60, abs=1e-3)

    def test_win_rate_100pct(self):
        all_wins = [_sig("WIN") for _ in range(5)]
        r = compute_quality_metrics(all_wins, 30)
        assert r["win_rate"] == pytest.approx(1.0)

    def test_win_rate_0pct(self):
        all_losses = [_sig("LOSS") for _ in range(5)]
        r = compute_quality_metrics(all_losses, 30)
        assert r["win_rate"] == pytest.approx(0.0)

    def test_live_payload_vocabulary_counts_lost_and_excludes_cancelled_closed(self):
        live_shaped = (
            [_sig("WIN") for _ in range(2)]
            + [_sig("LOST") for _ in range(3)]
            + [{"status": "CLOSED", "close_reason": "Win rate below minimum", "closed_at": _iso()}]
        )

        result = compute_quality_metrics(live_shaped, 30)

        assert result["sample_size"] == 5
        assert result["wins"] == 2
        assert result["losses"] == 3
        assert result["win_rate"] == pytest.approx(0.4)


# ────────────────────────────────────────────────────────────
# 4. Profit factor
# ────────────────────────────────────────────────────────────

class TestProfitFactor:
    def test_profit_factor_balanced_10(self):
        # 6 wins @ R=2.0, 4 losses @ R=-1.0
        # gross_profit = 12, gross_loss = 4 → PF = 3.0
        r = compute_quality_metrics(BALANCED_10, 30)
        assert r["profit_factor"] == pytest.approx(3.0, abs=0.01)

    def test_profit_factor_all_losses_below_1(self):
        r = compute_quality_metrics([_sig("LOSS")] * 5, 30)
        assert r["profit_factor"] == pytest.approx(0.0, abs=0.01)

    def test_profit_factor_all_wins_is_unavailable(self):
        r = compute_quality_metrics([_sig("WIN")] * 5, 30)
        assert r["profit_factor"] is None


# ────────────────────────────────────────────────────────────
# 5. avg_R and expectancy_R
# ────────────────────────────────────────────────────────────

class TestExpectancy:
    def test_avg_R_positive_for_balanced(self):
        # 6×2.0 + 4×(-1.0) / 10 = (12-4)/10 = 0.8
        r = compute_quality_metrics(BALANCED_10, 30)
        assert r["avg_R"] == pytest.approx(0.8, abs=0.01)

    def test_expectancy_R_positive(self):
        r = compute_quality_metrics(BALANCED_10, 30)
        # win_rate=0.6, avg_win=2.0, avg_loss=1.0
        # expectancy = 0.6*2.0 - 0.4*1.0 = 1.2 - 0.4 = 0.8
        assert r["expectancy_R"] == pytest.approx(0.8, abs=0.01)

    def test_expectancy_R_negative_for_losers(self):
        losses = [_sig("LOSS")] * 8 + [_sig("WIN", tp=1, sl=2)] * 2
        r = compute_quality_metrics(losses, 30)
        assert r["expectancy_R"] < 0


# ────────────────────────────────────────────────────────────
# 6. Max drawdown
# ────────────────────────────────────────────────────────────

class TestMaxDrawdown:
    def test_no_drawdown_for_all_wins(self):
        """All wins → equity curve always increasing → drawdown = 0."""
        dd = _compute_max_drawdown([2.0, 2.0, 2.0])
        assert dd == pytest.approx(0.0, abs=0.01)

    def test_drawdown_with_losses(self):
        # Equity: 0 → 2 → 1 → 3 → 2 → 4
        dd = _compute_max_drawdown([2.0, -1.0, 2.0, -1.0, 2.0])
        assert dd is not None
        assert dd > 0

    def test_single_trade_returns_none(self):
        dd = _compute_max_drawdown([2.0])
        assert dd is None

    def test_all_losses_large_drawdown(self):
        dd = _compute_max_drawdown([-1.0] * 10)
        assert dd is not None
        assert dd == pytest.approx(100.0)


# ────────────────────────────────────────────────────────────
# 7. Windowed filtering
# ────────────────────────────────────────────────────────────

class TestWindowFiltering:
    def test_30d_includes_recent(self):
        recent = [_sig("WIN", days_ago=1)]
        r = compute_quality_metrics(recent, 30)
        assert r["sample_size"] == 1

    def test_30d_excludes_old(self):
        old = [_sig("WIN", days_ago=60)]
        r = compute_quality_metrics(old, 30)
        assert r["sample_size"] == 0

    def test_7d_excludes_8_days_old(self):
        sigs = [_sig("WIN", days_ago=8)] * 5
        r = compute_quality_metrics(sigs, 7)
        assert r["sample_size"] == 0

    def test_7d_includes_5_days_old(self):
        sigs = [_sig("WIN", days_ago=5)] * 3
        r = compute_quality_metrics(sigs, 7)
        assert r["sample_size"] == 3


# ────────────────────────────────────────────────────────────
# 8. Alert generation
# ────────────────────────────────────────────────────────────

class TestAlerts:
    def test_no_alerts_for_good_metrics(self):
        r = compute_quality_metrics(BALANCED_10, 30)
        assert r["win_rate"] >= ALERT_WIN_RATE_THRESHOLD
        assert "win_rate_degraded" not in r["alerts"]

    def test_win_rate_alert_triggered(self):
        mostly_losses = [_sig("LOSS")] * 8 + [_sig("WIN")] * 2
        r = compute_quality_metrics(mostly_losses, 30)
        assert "win_rate_degraded" in r["alerts"]

    def test_profit_factor_alert_triggered(self):
        # Many losses → PF < 1
        bad = [_sig("LOSS")] * 7 + [_sig("WIN", tp=1.0, sl=1.0)] * 3
        r = compute_quality_metrics(bad, 30)
        assert "profit_factor_low" in r["alerts"]

    def test_no_win_rate_alert_for_good_win_rate(self):
        r = compute_quality_metrics([_sig("WIN")] * 10, 30)
        assert "win_rate_degraded" not in r["alerts"]


# ────────────────────────────────────────────────────────────
# 9. compute_quality_report aggregation
# ────────────────────────────────────────────────────────────

class TestQualityReport:
    def test_report_has_global_windows(self):
        r = compute_quality_report(BALANCED_10)
        assert "global" in r
        assert "7d" in r["global"]
        assert "30d" in r["global"]
        assert "all_time" in r["global"]

    def test_report_has_breakdown(self):
        sigs = (
            [_sig("WIN", risk_mode="MEDIUM")] * 3 +
            [_sig("LOSS", risk_mode="STRONG")] * 3
        )
        r = compute_quality_report(sigs, breakdown_by="risk_mode")
        assert "breakdown" in r
        assert "MEDIUM" in r["breakdown"]
        assert "STRONG" in r["breakdown"]

    def test_report_as_of_is_iso(self):
        r = compute_quality_report([])
        assert "as_of" in r
        datetime.fromisoformat(r["as_of"].replace("Z", "+00:00"))

    def test_report_active_alerts_list(self):
        r = compute_quality_report([_sig("LOSS")] * 10)
        assert isinstance(r["active_alerts"], list)

    def test_report_thresholds_match_constants(self):
        r = compute_quality_report([])
        t = r["alert_thresholds"]
        assert t["min_win_rate"] == ALERT_WIN_RATE_THRESHOLD
        assert t["min_profit_factor"] == ALERT_PROFIT_FACTOR_MIN
        assert t["max_drawdown_pct"] == ALERT_MAX_DRAWDOWN_PCT


# ────────────────────────────────────────────────────────────
# 10. Determinism
# ────────────────────────────────────────────────────────────

class TestDeterminism:
    def test_same_input_same_output(self):
        sigs = BALANCED_10.copy()
        r1 = compute_quality_metrics(sigs, 30)
        r2 = compute_quality_metrics(sigs, 30)
        assert r1["win_rate"] == r2["win_rate"]
        assert r1["profit_factor"] == r2["profit_factor"]
        assert r1["avg_R"] == r2["avg_R"]
        assert r1["max_drawdown"] == r2["max_drawdown"]

    def test_does_not_mutate_input(self):
        sigs = [_sig("WIN"), _sig("LOSS")]
        original_len = len(sigs)
        compute_quality_metrics(sigs, 30)
        assert len(sigs) == original_len
