from core.strategy_v2.performance_cache import (
    StrategyPerformanceCache,
    calculate_validation_score,
)

import pytest
from datetime import datetime, timedelta, timezone


def test_strategy_performance_cache_uses_real_gross_profit_and_loss(tmp_path):
    cache = StrategyPerformanceCache(tmp_path / "strategy_performance_cache.json")

    cache.upsert_from_trade_result(
        {
            "ticker": "AAPL",
            "asset_class": "US_STOCKS",
            "strategy_type": "BREAKOUT",
            "direction": "LONG",
            "net_pnl": 100.0,
            "pnl_pct_notional": 2.0,
            "stop_loss_pct": 2.0,
        }
    )
    cache.upsert_from_trade_result(
        {
            "ticker": "AAPL",
            "asset_class": "US_STOCKS",
            "strategy_type": "BREAKOUT",
            "direction": "LONG",
            "net_pnl": -50.0,
            "pnl_pct_notional": -1.0,
            "stop_loss_pct": 2.0,
        }
    )

    entry = cache.get_entry("AAPL", "US_STOCKS", "BREAKOUT", "LONG")
    assert entry["sample_size"] == 2
    assert entry["win_rate"] == 0.5
    assert entry["profit_factor"] == 2.0
    assert entry["expectancy_R"] == 0.25
    assert entry["_gross_profit_accum"] == 100.0
    assert entry["_gross_loss_accum"] == 50.0
    assert entry["_wins_accum"] == 1
    assert entry["_losses_accum"] == 1


def test_trade_update_preserves_validation_provenance_and_costs(tmp_path):
    cache = StrategyPerformanceCache(tmp_path / "validated_cache.json")
    validation_as_of = datetime.now(timezone.utc).isoformat()
    cache.upsert_entry(
        "BTC-USD",
        "CRYPTO_FUTURES",
        "S2_BREAKOUT_RETEST",
        "LONG",
        {
            "win_rate": 0.6,
            "profit_factor": 1.4,
            "expectancy_R": 0.2,
            "avg_R": 0.2,
            "max_drawdown": 8.0,
            "sample_size": 30,
            "validation_status": "validated",
            "validation_method": "faithful_v2_walk_forward",
            "validation_reason": "OOS thresholds passed",
            "validation_as_of": validation_as_of,
            "costs_used": {"commission_bps": 4.0},
        },
    )

    cache.upsert_from_trade_result(
        {
            "ticker": "BTC-USD",
            "asset_class": "CRYPTO_FUTURES",
            "strategy_type": "S2_BREAKOUT_RETEST",
            "direction": "LONG",
            "net_pnl": -10.0,
            "pnl_pct_notional": -1.0,
            "stop_loss_pct": 2.0,
        }
    )

    entry = cache.get_entry("BTC-USD", "CRYPTO_FUTURES", "S2_BREAKOUT_RETEST", "LONG")
    assert entry["validation_status"] == "validated"
    assert entry["validation_method"] == "faithful_v2_walk_forward"
    assert entry["validation_reason"] == "OOS thresholds passed"
    assert entry["validation_as_of"] == validation_as_of
    assert entry["costs_used"] == {"commission_bps": 4.0}


def test_degraded_and_suspended_flags_survive_allowlist(tmp_path):
    cache = StrategyPerformanceCache(tmp_path / "flags_cache.json")
    cache.upsert_entry(
        "AAPL",
        "US_STOCKS",
        "BREAKOUT",
        "LONG",
        {
            "sample_size": 30,
            "win_rate": 0.3,
            "profit_factor": 0.8,
            "degraded": True,
            "suspended": True,
        },
    )

    entry = cache.get_entry("AAPL", "US_STOCKS", "BREAKOUT", "LONG")
    assert entry["degraded"] is True
    assert entry["suspended"] is True


def test_validated_freshness_uses_validation_timestamp_not_recent_trade(tmp_path):
    cache = StrategyPerformanceCache(tmp_path / "freshness_cache.json")
    stale_validation = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    cache.upsert_entry(
        "BTC-USD",
        "CRYPTO_FUTURES",
        "S2_BREAKOUT_RETEST",
        "LONG",
        {
            "sample_size": 30,
            "win_rate": 0.6,
            "profit_factor": 1.4,
            "validation_status": "validated",
            "validation_method": "faithful_v2_walk_forward",
            "validation_as_of": stale_validation,
            "last_run": datetime.now(timezone.utc).isoformat(),
        },
    )
    entry = cache.get_entry("BTC-USD", "CRYPTO_FUTURES", "S2_BREAKOUT_RETEST", "LONG")

    assert cache.is_fresh(entry, 6 * 3600) is False


# ─────────────────────────────────────────────────────────────────────────────
# Critical #2: V2 cold-start deadlock — calculate_validation_score neutrality
# ─────────────────────────────────────────────────────────────────────────────
# Pre-fix, a cold performance cache (sample_size==0) collapsed
# validation_score to 0 (wr=0, pf=0, sample_penalty=45 -> max(0,15-45)=0).
# validation carries 25% of the V2 total_score, so 0 capped total_score at
# 75 even with every other component at 100 -> no cold V2 candidate cleared
# min_edge_score (75) -> V2 never fired -> never accrued trades -> deadlock.
# Fix: cold (no dict / sample==0) returns NEUTRAL 50 (mirrors
# news-unknown->neutral, finding #4). Evidence path (sample>0) unchanged.


def test_validation_score_cold_empty_dict_is_neutral_not_zero():
    """Cold cache (empty perf dict) must score neutral 50, not punitive 0."""
    assert calculate_validation_score({}) == 50.0


def test_validation_score_cold_explicit_zero_sample_is_neutral():
    """Explicit sample_size=0 (fresh entry, no trades) must be neutral 50."""
    assert calculate_validation_score({"sample_size": 0, "win_rate": 0.0}) == 50.0


def test_validation_score_non_dict_is_neutral_not_zero():
    """None / non-dict perf (no entry at all) must be neutral 50, not 0.
    Pre-fix this branch returned 0.0 — same cold-deadlock root cause."""
    assert calculate_validation_score(None) == 50.0
    assert calculate_validation_score("not-a-dict") == 50.0


def test_validation_score_cold_neutral_unblocks_v2_total_score():
    """The deadlock fix must let a strong-setup cold V2 candidate clear
    min_edge_score (75). With validation neutral 50 and setup=95, catalyst=75,
    risk=80 -> total_score = 0.45*95 + 0.15*75 + 0.15*80 + 0.25*50 = 78.5 >= 75.
    Pre-fix (validation 0) the same candidate scored 66.0 < 75 -> dropped."""
    neutral = calculate_validation_score({})
    assert neutral == 50.0
    setup, catalyst, risk = 95.0, 75.0, 80.0
    total_post = 0.45 * setup + 0.15 * catalyst + 0.15 * risk + 0.25 * neutral
    total_pre = 0.45 * setup + 0.15 * catalyst + 0.15 * risk + 0.25 * 0.0
    assert total_post >= 75.0  # post-fix: V2 can fire on cold start
    assert total_pre < 75.0    # pre-fix: V2 deadlocked below the gate


def test_validation_score_evidence_path_unchanged_for_real_performance():
    """A proven 60% win-rate, PF 1.8, positive expectancy, sufficient sample
    must score the SAME as before the cold-start fix (sample>0 path intact).
    This guards that neutrality did not inflate proven-performance scoring."""
    perf = {
        "sample_size": 50,
        "win_rate": 0.60,
        "profit_factor": 1.8,
        "expectancy_R": 0.3,
        "max_drawdown": 8.0,
    }
    score = calculate_validation_score(perf, target_win_rate=0.60, min_sample=30)
    # Reproduce the documented formula (sample>=min_sample -> no sample penalty):
    wr_score = (0.60 / 0.60) * 100.0            # 100
    exp_score = 50.0 + (0.3 * 100.0)            # 80
    pf_score = (1.8 / 2.0) * 100.0              # 90
    dd_penalty = max(0.0, min(60.0, 8.0 * 1.2))  # 9.6
    expected = max(0.0, min(100.0, 0.45 * wr_score + 0.30 * exp_score + 0.25 * pf_score - dd_penalty))
    assert score == pytest.approx(expected, abs=0.01)
    # And it must beat the cold-neutral 50 (proven > unknown).
    assert score > 50.0


# ─────────────────────────────────────────────────────────────────────────────
# M7: rolling max_drawdown must be peak-to-trough, not worst-single-loss
# ─────────────────────────────────────────────────────────────────────────────
# Pre-fix upsert_from_trade_result set max_drawdown = max(old_dd, |single-loss
# pnl_pct|) -- the worst single trade, NOT a drawdown -- so clustered losses
# understated risk and inflated validation_score / dodged drawdown_high reject.
# Post-fix: running equity curve (sum of pnl_pct) with peak-to-trough drop,
# matching strategy_backtester.py:394-406 semantics (absolute %-points).


def test_max_drawdown_is_peak_to_trough_not_worst_single_loss(tmp_path):
    """Three consecutive -2% losses: worst-single-loss = 2, but the true
    peak-to-trough drawdown is 6 (equity 0 -> -2 -> -4 -> -6, peak 0)."""
    cache = StrategyPerformanceCache(tmp_path / "pc_m7a.json")
    for _ in range(3):
        cache.upsert_from_trade_result({
            "ticker": "AAPL", "asset_class": "US_STOCKS",
            "strategy_type": "BREAKOUT", "direction": "LONG",
            "net_pnl": -100.0, "pnl_pct_notional": -2.0, "stop_loss_pct": 2.0,
        })
    entry = cache.get_entry("AAPL", "US_STOCKS", "BREAKOUT", "LONG")
    assert entry["max_drawdown"] == pytest.approx(6.0, abs=0.01), (
        "max_drawdown must be the cumulative peak-to-trough (6), not the "
        f"worst single loss (2). Got {entry['max_drawdown']}."
    )
    assert entry["_equity_accum"] == pytest.approx(-6.0, abs=0.01)


def test_max_drawdown_mixed_sequence_uses_peak_to_trough(tmp_path):
    """+5, -2, -2, -2: equity 5 -> 3 -> 1 -> -1, peak 5. Drawdown series
    0, 2, 4, 6 -> max_drawdown 6 (worst-single-loss would be 2)."""
    cache = StrategyPerformanceCache(tmp_path / "pc_m7b.json")
    seq = [(100.0, 5.0), (-40.0, -2.0), (-40.0, -2.0), (-40.0, -2.0)]
    for net, pct in seq:
        cache.upsert_from_trade_result({
            "ticker": "AAPL", "asset_class": "US_STOCKS",
            "strategy_type": "BREAKOUT", "direction": "LONG",
            "net_pnl": net, "pnl_pct_notional": pct, "stop_loss_pct": 2.0,
        })
    entry = cache.get_entry("AAPL", "US_STOCKS", "BREAKOUT", "LONG")
    assert entry["max_drawdown"] == pytest.approx(6.0, abs=0.01)
    assert entry["_peak_accum"] == pytest.approx(5.0, abs=0.01)


def test_max_drawdown_zero_when_only_wins(tmp_path):
    """All wins -> equity monotonically up -> no drawdown (0), not |win|."""
    cache = StrategyPerformanceCache(tmp_path / "pc_m7c.json")
    for _ in range(3):
        cache.upsert_from_trade_result({
            "ticker": "AAPL", "asset_class": "US_STOCKS",
            "strategy_type": "BREAKOUT", "direction": "LONG",
            "net_pnl": 80.0, "pnl_pct_notional": 2.0, "stop_loss_pct": 2.0,
        })
    entry = cache.get_entry("AAPL", "US_STOCKS", "BREAKOUT", "LONG")
    assert entry["max_drawdown"] == pytest.approx(0.0, abs=0.01)


def test_max_drawdown_migration_from_old_entry_without_equity_keys(tmp_path):
    """Old cache entries (pre-fix) lack _equity_accum/_peak_accum. The rolling
    path must migrate gracefully (equity/peak start at 0) and still produce a
    sane drawdown on the next trade, not crash."""
    cache = StrategyPerformanceCache(tmp_path / "pc_m7d.json")
    # Seed a pre-fix-style entry with max_drawdown set but no equity accumulators.
    cache.upsert_entry("AAPL", "US_STOCKS", "BREAKOUT", "LONG", {
        "win_rate": 0.5, "profit_factor": 1.0, "expectancy_R": 0.0, "avg_R": 0.0,
        "max_drawdown": 3.0, "sample_size": 2,
        "_gross_profit_accum": 100.0, "_gross_loss_accum": 100.0,
        "_wins_accum": 1, "_losses_accum": 1,
    })
    # Next trade: a -4% loss. Migrated equity 0 -> -4, peak 0, dd_now 4, max(3,4)=4.
    cache.upsert_from_trade_result({
        "ticker": "AAPL", "asset_class": "US_STOCKS",
        "strategy_type": "BREAKOUT", "direction": "LONG",
        "net_pnl": -200.0, "pnl_pct_notional": -4.0, "stop_loss_pct": 2.0,
    })
    entry = cache.get_entry("AAPL", "US_STOCKS", "BREAKOUT", "LONG")
    assert entry["max_drawdown"] == pytest.approx(4.0, abs=0.01)
    assert entry["_equity_accum"] == pytest.approx(-4.0, abs=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# H2: thin-sample neutrality — 0 < sample < min_sample must not deadlock below 50
# ─────────────────────────────────────────────────────────────────────────────
# Pre-fix, a single losing trade (sample=1) collapsed validation_score to 0
# (wr_score=0, exp_score=0, pf_score=0, sample_penalty=min(45,29*1.5)=43.5,
# dd_penalty>0 -> max(0, 0-0-43.5)=0 < 50), permanently deadlocking the
# strategy so it could never accrue the trades needed to compute a real
# validation_score -- defeating the Runda-4 cold-start (sample==0 -> 50) fix.
# Fix: clamp score >= 50 when 0 < sample < min_sample (thin = neutral, not
# punitive). Evidence path (sample >= min_sample) unchanged.


def test_validation_score_single_loss_does_not_deadlock_below_neutral():
    """sample=1, one loss -> validation must be >= 50 (neutral), not 0."""
    perf = {
        "win_rate": 0.0,        # 1 loss
        "profit_factor": 0.0,
        "expectancy_R": -1.0,
        "max_drawdown": 2.0,    # dd_penalty = 2.4
        "sample_size": 1,
    }
    score = calculate_validation_score(perf, min_sample=30)
    assert score >= 50.0, (
        "thin sample (1 loss) must not dip below cold-start neutral 50 (H2)"
    )


def test_validation_score_thin_sample_clamped_to_neutral():
    """sample=5, all losses, meaningful drawdown -> still clamped to >= 50."""
    perf = {
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "expectancy_R": -1.0,
        "max_drawdown": 10.0,
        "sample_size": 5,
    }
    score = calculate_validation_score(perf, min_sample=30)
    assert score == 50.0


def test_validation_score_proven_regime_not_clamped():
    """sample >= min_sample: the real math governs (a bad proven strategy
    scores BELOW 50 -- evidence-driven, NOT clamped to neutral)."""
    perf = {
        "win_rate": 0.20,        # well below 0.60 target -> wr_score ~33
        "profit_factor": 0.5,    # pf_score = 25
        "expectancy_R": -0.5,   # exp_score = 0
        "max_drawdown": 15.0,   # dd_penalty = 18
        "sample_size": 30,       # >= min_sample
    }
    score = calculate_validation_score(perf, min_sample=30)
    # 0.45*33 + 0.30*0 + 0.25*25 = 14.85 + 0 + 6.25 = 21.1; - 18 dd = 3.1
    assert score < 50.0, "proven-bad strategy must score below neutral (H2 only clamps thin samples)"
    assert score >= 0.0


# ─────────────────────────────────────────────────────────────────────────────
# L1: breakeven trade (net_pnl == 0) is NEITHER win nor loss
# ─────────────────────────────────────────────────────────────────────────────
# Pre-fix `is_win = pnl > 0` made breakeven count as a loss (losses += 1),
# deflating win_rate while profit_factor was unaffected (gross_loss += 0) --
# inconsistent metrics. Fix: breakeven excluded from both; win_rate over
# DECIDED trades (wins + losses); sample_size (n) still counts every trade.


def test_breakeven_trade_excluded_from_win_loss_counts(tmp_path):
    cache = StrategyPerformanceCache(tmp_path / "pc.json")
    # 1 win (+2%), 1 breakeven (0), 1 loss (-1%)
    cache.upsert_from_trade_result({
        "ticker": "AAPL", "asset_class": "US_STOCKS",
        "strategy_type": "BREAKOUT", "direction": "LONG",
        "net_pnl": 100.0, "pnl_pct_notional": 2.0, "stop_loss_pct": 2.0,
    })
    cache.upsert_from_trade_result({
        "ticker": "AAPL", "asset_class": "US_STOCKS",
        "strategy_type": "BREAKOUT", "direction": "LONG",
        "net_pnl": 0.0, "pnl_pct_notional": 0.0, "stop_loss_pct": 2.0,
    })
    cache.upsert_from_trade_result({
        "ticker": "AAPL", "asset_class": "US_STOCKS",
        "strategy_type": "BREAKOUT", "direction": "LONG",
        "net_pnl": -50.0, "pnl_pct_notional": -1.0, "stop_loss_pct": 2.0,
    })
    entry = cache.get_entry("AAPL", "US_STOCKS", "BREAKOUT", "LONG")
    assert entry["sample_size"] == 3          # breakeven still counts as a closed trade
    assert entry["_wins_accum"] == 1
    assert entry["_losses_accum"] == 1         # breakeven NOT a loss (was 2 pre-fix)
    assert entry["win_rate"] == pytest.approx(0.5, abs=1e-6)  # 1 / (1+1), not 1/3
    assert entry["_gross_loss_accum"] == 50.0  # breakeven added 0 to gross loss
    assert entry["profit_factor"] == pytest.approx(2.0, abs=1e-6)
