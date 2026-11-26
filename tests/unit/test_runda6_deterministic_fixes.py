"""tests/unit/test_runda6_deterministic_fixes.py
=================================================
Regression guards for the Runda-6 CONFIRMATED deterministic fixes (all 10 audit
findings CONFIRMED, 0 debunked). The 5 fixes here are win-rate-neutral
correctness / thread-safety / determinism fixes; the other 5 findings are
deferred (need A/B or tick data) -- see tasks/todo.md "Stare Runda 6".

  D1 -- core/engine.py _calculate_exit_levels: NaN/0 ATR flat-pct fallback
        (mirror strategy_backtester.py:369 guard). Pre-fix ATR=0/NaN -> sl=entry
        (zero risk, instant stop-out). Live now matches the backtester's safe
        flat fallback.
  D2 -- core/strategy_v2/selector.py: snapshot perf_cache ONCE per select()
        (get_symbol_entries) instead of get_entry() per candidate. Pre-fix each
        candidate re-read the JSON from disk; a concurrent writer between the
        LONG and SHORT reads gave them different perf -> non-deterministic
        best_long-vs-best_short. The snapshot makes the per-select perf view
        atomic (and cuts 2N disk reads to 1).
  D3 -- core/signals.py _load_locked: RESET last_signal_time before merge in
        BOTH branches (mirror open_signals full-replace). Pre-fix a cross-process
        clear_all (writes {} to disk) did NOT propagate the cooldown reset to the
        engine subprocess (merge loop was a no-op on {}) -> stale cooldowns
        blocked signals the user had just cleared.
  D4 -- core/strategy_v2/performance_cache.py: process-wide RLock around the
        load-modify-save writers (upsert_entry + upsert_from_trade_result). Pre-fix
        two concurrent trade closes both read the same `existing` and the second
        save clobbered the first (lost update -- a trade dropped from win_rate /
        profit_factor / the equity curve).
  D5 -- core/strategy_v2/evaluators.py _news_catalyst_score: sign-flip the
        opposing-sentiment fallback (LONG+bearish / SHORT+bullish). Pre-fix
        `30 + base*0.2` made a STRONGER opposing news ADD to the catalyst score
        (bearish headline on a LONG boosted the signal) -- an inverted sign.
        Post-fix `30 - base*0.3` reduces catalyst with magnitude (clamp 0).
"""
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
__import__("os").environ.setdefault("TEST_MODE", "1")
__import__("os").environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")


# ─────────────────────────────────────────────────────────────────────────────
# D1 -- engine _calculate_exit_levels NaN/0 ATR flat fallback
# ─────────────────────────────────────────────────────────────────────────────

def _engine_no_init():
    """_calculate_exit_levels reads self.config.signal.min_tp_pct only; build a
    bare MarketEngine via __new__ to skip state load + background threads."""
    from core.engine import MarketEngine
    eng = MarketEngine.__new__(MarketEngine)
    eng.config = MagicMock()
    eng.config.signal.get.return_value = 0.0  # min_tp_pct = 0
    return eng


def test_d1_nan_atr_falls_back_to_flat_pct_long():
    """D1: ATR=NaN in ATR mode -> flat FIXED_PCT levels (tp=entry*(1+tp_pct),
    sl=entry*(1-sl_pct)), NOT sl=entry (zero risk / instant stop-out)."""
    eng = _engine_no_init()
    tp, sl = eng._calculate_exit_levels(
        entry=100.0, direction="LONG", exit_mode="ATR",
        atr=float("nan"), tp_mult=3.0, sl_mult=1.5,
        tp_pct=0.05, sl_pct=0.02,
    )
    assert tp == pytest.approx(105.0, abs=1e-9), f"D1 NaN atr: LONG tp must be flat 105, got {tp}"
    assert sl == pytest.approx(98.0, abs=1e-9), (
        f"D1 NaN atr: LONG sl must be flat 98 (entry*(1-0.02)), got {sl}; "
        "pre-fix sl=entry=100 (zero risk -> instant stop-out)"
    )


def test_d1_zero_atr_falls_back_to_flat_pct_short():
    """D1: ATR=0.0 SHORT -> flat levels (tp=entry*(1-tp_pct), sl=entry*(1+sl_pct))."""
    eng = _engine_no_init()
    tp, sl = eng._calculate_exit_levels(
        entry=100.0, direction="SHORT", exit_mode="ATR",
        atr=0.0, tp_mult=3.0, sl_mult=1.5,
        tp_pct=0.05, sl_pct=0.02,
    )
    assert tp == pytest.approx(95.0, abs=1e-9), f"D1 zero atr: SHORT tp must be flat 95, got {tp}"
    assert sl == pytest.approx(102.0, abs=1e-9), f"D1 zero atr: SHORT sl must be flat 102, got {sl}"


def test_d1_positive_atr_uses_atr_math_unchanged():
    """D1 backward-compat: a valid ATR still uses the ATR branch (tp=entry+atr*tp_mult,
    sl=entry-atr*sl_mult), NOT the flat fallback."""
    eng = _engine_no_init()
    tp, sl = eng._calculate_exit_levels(
        entry=100.0, direction="LONG", exit_mode="ATR",
        atr=2.0, tp_mult=3.0, sl_mult=1.5,
        tp_pct=0.05, sl_pct=0.02,
    )
    assert tp == pytest.approx(106.0, abs=1e-9), f"valid atr tp must be 100+2*3=106, got {tp}"
    assert sl == pytest.approx(97.0, abs=1e-9), f"valid atr sl must be 100-2*1.5=97, got {sl}"
    # Must differ from the flat levels (proves the ATR branch was taken).
    assert tp != pytest.approx(105.0, abs=1e-3)


# ─────────────────────────────────────────────────────────────────────────────
# D2 -- selector snapshots perf_cache once per select()
# ─────────────────────────────────────────────────────────────────────────────

def _base_cfg():
    return {
        "min_rr": 2.0, "target_win_rate": 0.60, "event_block_hours": 6,
        "min_sample_size": 30, "min_validation_score": 60,
        "allow_low_sample_fallback": True, "allow_low_validation_fallback": True,
        "enabled_strategies_by_asset_class": {"US_STOCKS": ["S2_BREAKOUT_RETEST"]},
        "quality_thresholds": {
            "US_STOCKS": {"min_volume": 1000, "max_spread_bps": 40, "min_atr_pct": 0.001}
        },
    }


def _ctx_long():
    return {
        "symbol": "AAPL", "asset_class": "US_STOCKS", "price": 110.0,
        "resistance": 108.0, "support": 102.0,
        "retest_hold_long": True, "retest_hold_short": False,
        "volume": 1_000_000, "volume_avg": 700_000, "adx": 28.0,
        "atr": 1.5, "atr_pct": 0.013, "spread_bps": 8.0,
        "news_sentiment": "bullish", "news_pulse_score": 72,
        "event_risk": False, "hours_to_event": 999,
        "ema_slope": 0.002, "macd_hist": 0.03, "bb_width": 0.03,
        "swing_low": 106.0, "swing_high": 111.0,
    }


def test_d2_selector_uses_perf_snapshot_not_per_candidate_get_entry(monkeypatch):
    """D2: select() must snapshot perf once via get_symbol_entries and read each
    candidate's perf from that snapshot, NOT call perf_cache.get_entry() per
    candidate (which re-reads the JSON from disk per call -> a concurrent writer
    between the LONG and SHORT reads produces a non-deterministic best_long-vs-
    best_short comparison)."""
    from core.strategy_v2.selector import StrategySelector
    from core.strategy_v2.performance_cache import build_perf_key

    selector = StrategySelector(config=_base_cfg())

    # Seed a perf entry on disk for the LONG candidate so it survives the
    # min_sample_size / validation gate (sample_size=50, win_rate=0.6).
    perf_key_long = build_perf_key("AAPL", "US_STOCKS", "S2_BREAKOUT_RETEST", "LONG")
    snapshot = {
        perf_key_long: {
            "win_rate": 0.60, "profit_factor": 1.5, "expectancy_R": 0.2,
            "avg_R": 1.0, "max_drawdown": 0.05, "sample_size": 50,
            "last_run": "2026-07-03T10:00:00+00:00", "costs_used": {},
        }
    }

    get_entry_calls = []

    def _get_entry(self, symbol, asset_class, strategy_id, direction):
        # D2: if select() still calls get_entry per candidate, this records it
        # AND would re-read disk -> the non-determinism source. Post-fix this
        # must NEVER be called during select().
        get_entry_calls.append((symbol, strategy_id, direction))
        return snapshot.get(build_perf_key(symbol, asset_class, strategy_id, direction), {})

    get_symbol_entries_calls = []

    def _get_symbol_entries(self, symbol):
        get_symbol_entries_calls.append(symbol)
        return dict(snapshot)

    monkeypatch.setattr(selector.perf_cache, "get_entry",
                        lambda symbol, asset_class, strategy_id, direction: _get_entry(selector.perf_cache, symbol, asset_class, strategy_id, direction))
    monkeypatch.setattr(selector.perf_cache, "get_symbol_entries",
                        lambda symbol: _get_symbol_entries(selector.perf_cache, symbol))

    out = selector.select(symbol="AAPL", asset_class="US_STOCKS", context=_ctx_long())

    # D2: the snapshot API must be used exactly once per select().
    assert len(get_symbol_entries_calls) == 1, (
        f"D2: select() must call get_symbol_entries once per select (snapshot); "
        f"got {len(get_symbol_entries_calls)} calls"
    )
    # D2: get_entry (the per-candidate disk re-read) must NOT be called at all.
    assert get_entry_calls == [], (
        f"D2: select() must NOT call perf_cache.get_entry per candidate "
        f"(non-deterministic disk re-read); got {get_entry_calls}"
    )
    # The candidate still resolves (the snapshot's perf was used).
    assert out.get("best_candidate") is not None


# ─────────────────────────────────────────────────────────────────────────────
# D3 -- signals _load_locked resets cooldown before merge (clear_all propagates)
# ─────────────────────────────────────────────────────────────────────────────

def test_d3_clear_all_propagates_cooldown_reset_on_reload(tmp_path):
    """D3: after a signal sets a cooldown, clear_all writes {} to disk; a
    reload_if_changed must RESET last_signal_time to {} so the engine subprocess
    stops blocking the ticker. Pre-fix the merge loop was a no-op on {} and the
    stale cooldown survived the reload."""
    import json as _json
    from core.signals import SignalManager, BUCHAREST_TZ

    path = tmp_path / "signals.json"
    path.write_text(_json.dumps({"open": {}, "closed": [], "last_signal_time": {}}))

    mock_cm = MagicMock()
    mock_cm.get_balance.return_value = 10_000.0

    with patch("core.signals.get_capital_manager", return_value=mock_cm), \
         patch("core.signals.save_signals_payload"), \
         patch("core.signals.load_signals_payload", side_effect=lambda default: default):
        sm = SignalManager(path=path)
        # Simulate a signal having fired recently -> cooldown active.
        from datetime import datetime
        recent = datetime.now(BUCHAREST_TZ)
        sm.last_signal_time["AAPL"] = recent
        assert sm.can_open_new_signal_for_ticker("AAPL", min_minutes=15) is False, (
            "fixture: AAPL must be on cooldown before clear"
        )

        # Cross-process clear_all: writes {} to disk (as clear_all does at :359-366).
        _json.dump({"open": {}, "closed": [], "last_signal_time": {}}, open(path, "w"))
        # Bump mtime so reload_if_changed picks it up.
        import os as _os, time as _t
        _t.sleep(0.01)
        _os.utime(path, None)

        reloaded = sm.reload_if_changed()
        assert reloaded, "fixture: reload_if_changed must detect the mtime change"

    # D3: the cooldown must be GONE after the reload (disk last_signal_time={}).
    assert "AAPL" not in sm.last_signal_time, (
        f"D3: clear_all (empty dict on disk) must propagate -- stale AAPL cooldown "
        f"survived reload; last_signal_time={sm.last_signal_time}"
    )
    assert sm.can_open_new_signal_for_ticker("AAPL", min_minutes=15) is True, (
        "D3: after clear_all propagates, AAPL must be eligible again"
    )


def test_d3_cooldown_preserved_when_disk_still_has_it(tmp_path):
    """D3 negative: a legitimate on-disk cooldown must still be honored after a
    reload (the reset is from disk, not a blanket wipe). Guards against the fix
    over-clearning cooldowns that should persist."""
    import json as _json
    from core.signals import SignalManager, BUCHAREST_TZ
    from datetime import datetime

    path = tmp_path / "signals.json"
    recent_iso = datetime.now(BUCHAREST_TZ).isoformat()
    path.write_text(_json.dumps({
        "open": {}, "closed": [],
        "last_signal_time": {"AAPL": recent_iso},
    }))

    mock_cm = MagicMock()
    mock_cm.get_balance.return_value = 10_000.0
    with patch("core.signals.get_capital_manager", return_value=mock_cm), \
         patch("core.signals.save_signals_payload"), \
         patch("core.signals.load_signals_payload", side_effect=lambda default: default):
        sm = SignalManager(path=path)
        # Force a reload from disk (mtime may be equal; load() always re-reads).
        sm.load()

    assert "AAPL" in sm.last_signal_time, (
        "D3 negative: an on-disk cooldown must survive reload (reset is from disk)"
    )
    assert sm.can_open_new_signal_for_ticker("AAPL", min_minutes=15) is False


# ─────────────────────────────────────────────────────────────────────────────
# D4 -- performance_cache upsert is thread-safe (no lost update)
# ─────────────────────────────────────────────────────────────────────────────

def test_d4_concurrent_upserts_no_lost_update(tmp_path):
    """D4: N concurrent upsert_from_trade_result calls on the same (ticker,
    strategy, direction) must all be counted -- pre-fix the read-modify-save had
    no lock, so two threads both read sample_size=k and the second save clobbered
    the first, losing a trade (sample_size < N). Post-fix sample_size == N."""
    from core.strategy_v2.performance_cache import StrategyPerformanceCache

    cache = StrategyPerformanceCache(path=tmp_path / "perf.json")
    base_trade = {
        "ticker": "AAPL", "strategy_type": "S2_BREAKOUT_RETEST",
        "direction": "LONG", "asset_class": "US_STOCKS",
        "net_pnl": 1.0, "pnl_pct_notional": 0.5, "stop_loss_pct": 2.0,
    }

    N = 25
    threads = [
        threading.Thread(target=cache.upsert_from_trade_result, args=({**base_trade, "net_pnl": 1.0},))
        for _ in range(N)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entry = cache.get_entry("AAPL", "US_STOCKS", "S2_BREAKOUT_RETEST", "LONG")
    assert entry["sample_size"] == N, (
        f"D4: {N} concurrent upserts must all be counted (no lost update); "
        f"got sample_size={entry['sample_size']} (pre-fix < {N} due to clobbered saves)"
    )
    # All N were wins (net_pnl>0) -> win_rate is observable, while a finite
    # profit factor is unavailable because the sample has no losses.
    assert entry["win_rate"] == pytest.approx(1.0, abs=1e-6), (
        f"D4: all wins -> win_rate=1.0; got {entry['win_rate']}"
    )
    assert entry["profit_factor"] is None


def test_d4_concurrent_mixed_wins_losses_counted(tmp_path):
    """D4: mixed wins/losses concurrently -- both wins and losses must be counted
    (pre-fix a clobbered save could drop either)."""
    from core.strategy_v2.performance_cache import StrategyPerformanceCache

    cache = StrategyPerformanceCache(path=tmp_path / "perf.json")
    wins, losses = 15, 10

    def _trade(pnl):
        return {
            "ticker": "AAPL", "strategy_type": "S1", "direction": "LONG",
            "asset_class": "US_STOCKS", "net_pnl": pnl,
            "pnl_pct_notional": (0.5 if pnl > 0 else -0.4), "stop_loss_pct": 2.0,
        }

    threads = [threading.Thread(target=cache.upsert_from_trade_result, args=(_trade(1.0),)) for _ in range(wins)]
    threads += [threading.Thread(target=cache.upsert_from_trade_result, args=(_trade(-1.0),)) for _ in range(losses)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entry = cache.get_entry("AAPL", "US_STOCKS", "S1", "LONG")
    assert entry["sample_size"] == wins + losses, (
        f"D4: sample_size must be {wins + losses}; got {entry['sample_size']}"
    )
    # Wins/losses accumulators must total sample_size (breakeven excluded, none here).
    assert entry["_wins_accum"] == wins, f"D4: wins_accum must be {wins}; got {entry['_wins_accum']}"
    assert entry["_losses_accum"] == losses, f"D4: losses_accum must be {losses}; got {entry['_losses_accum']}"


# ─────────────────────────────────────────────────────────────────────────────
# D5 -- evaluators _news_catalyst_score opposing-sentiment sign-flip
# ─────────────────────────────────────────────────────────────────────────────

def test_d5_opposing_news_reduces_catalyst_with_magnitude():
    """D5: a LONG+bearish (opposing) catalyst must DECREASE as the news magnitude
    grows. Pre-fix `30 + base*0.2` INCREASED it (bearish headline boosted the
    long). Post-fix `30 - base*0.3` decreases."""
    from core.strategy_v2.evaluators import _news_catalyst_score

    weak = _news_catalyst_score("bearish", "LONG", news_score=20.0)
    strong = _news_catalyst_score("bearish", "LONG", news_score=80.0)
    assert strong < weak, (
        f"D5: stronger opposing news must REDUCE catalyst; weak={weak} strong={strong} "
        f"(pre-fix strong > weak -- inverted sign)"
    )
    # Quantitative: base=80 -> 30 - 80*0.3 = 6.0
    assert strong == pytest.approx(6.0, abs=1e-6), (
        f"D5: bearish+LONG base=80 -> 30-24=6.0; got {strong}"
    )


def test_d5_opposing_news_full_neutralization_clamps_to_zero():
    """D5: base=100 -> 30 - 100*0.3 = 0 (full neutralization, clamp 0 not 15)."""
    from core.strategy_v2.evaluators import _news_catalyst_score
    full = _news_catalyst_score("bearish", "LONG", news_score=100.0)
    assert full == pytest.approx(0.0, abs=1e-6), (
        f"D5: bearish+LONG base=100 must clamp to 0 (full neutralization); got {full}"
    )


def test_d5_opposing_short_bullish_symmetric():
    """D5 SHORT mirror: SHORT+bullish (opposing) reduces catalyst too."""
    from core.strategy_v2.evaluators import _news_catalyst_score
    res = _news_catalyst_score("bullish", "SHORT", news_score=80.0)
    assert res == pytest.approx(6.0, abs=1e-6), f"D5 SHORT+bullish base=80 -> 6.0; got {res}"


def test_d5_opposing_below_neutral_at_zero_magnitude():
    """D5: at base=0, opposing (30) stays just below neutral (35) -- the existing
    invariant in test_news_catalyst_reactivation.py:115 must still hold."""
    from core.strategy_v2.evaluators import _news_catalyst_score
    opposing = _news_catalyst_score("bearish", "LONG", news_score=0.0)
    neutral = _news_catalyst_score("neutral", "LONG", news_score=0.0)
    assert neutral == pytest.approx(35.0, abs=0.5)
    assert opposing == pytest.approx(30.0, abs=0.5)
    assert neutral > opposing
