"""
tests/unit/test_signal_store.py
===============================
Unit tests for core/signals.py :: SignalManager

Strategy: use a temp file path so no real signals_state.json is touched.
Capital manager and state_persistence are patched to avoid heavy deps.
All tests are pure-logic / arithmetic — no network, no FMP, no yfinance.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── ensure project root on path ──────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")
os.environ.setdefault("MS_DISABLE_WARMUP", "1")
os.environ.setdefault("MS_DISABLE_STATE_PREFETCH", "1")
os.environ.setdefault("MS_DISABLE_BG_WORKER", "1")


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_signal(
    sid="sig-001",
    ticker="AAPL",
    action="BUY",
    entry=150.0,
    tp=160.0,
    sl=145.0,
    score=80.0,
    threshold=70.0,
    status="Pending",
    **extra,
) -> dict:
    return {
        "id": sid,
        "ticker": ticker,
        "action": action,
        "entry": entry,
        "tp": tp,
        "sl": sl,
        "signal_score": score,
        "signal_threshold": threshold,
        "status": status,
        "invested_amount": 500.0,
        **extra,
    }


def test_capital_reset_archives_executed_signal_without_fabricating_outcome(sm_factory):
    sm, _path, _capital = sm_factory()
    signal = _make_signal(
        sid="sig-reset",
        status="Open",
        paper_trade=True,
        paper_position_id="pos-reset",
    )
    sm.open_signals[signal["id"]] = signal

    archived = sm.archive_positions_removed_by_reset(
        [{"position_id": "pos-reset", "signal_id": "sig-reset", "status": "OPEN"}],
        reason="Manual Reset via UI",
        reset_at="2026-08-27T00:00:00+03:00",
    )

    assert archived == 1
    assert "sig-reset" not in sm.open_signals
    closed = sm.closed_signals[0]
    assert closed["status"] == "CANCELLED"
    assert closed["paper_position_id"] == "pos-reset"
    assert closed["paper_position_status"] == "RESET"
    assert closed["outcome_data_valid"] is False
    assert closed["outcome_validation"] == "excluded_capital_reset"
    assert closed["capital_settled"] is True


def _sm_with_temp_file():
    """Return a SignalManager backed by a fresh temp JSON file."""
    tmp = tempfile.NamedTemporaryFile(
        suffix=".json", delete=False, mode="w", encoding="utf-8"
    )
    json.dump({"open": {}, "closed": [], "last_signal_time": {}}, tmp)
    tmp.close()
    path = Path(tmp.name)

    # Patch capital manager so add_signal doesn't call real capital math
    mock_cm = MagicMock()
    mock_cm.get_balance.return_value = 10_000.0

    with patch("core.signals.get_capital_manager", return_value=mock_cm), \
         patch("core.signals.load_signals_payload", side_effect=lambda default: default), \
         patch("core.signals.save_signals_payload"):
        from core.signals import SignalManager
        sm = SignalManager(path=path)
    return sm, path, mock_cm


@pytest.fixture
def sm_factory():
    """Fixture: factory that yields (SignalManager, path). Cleans up after test."""
    created_paths = []

    def _factory():
        sm, path, mock_cm = _sm_with_temp_file()
        created_paths.append(path)
        return sm, path, mock_cm

    yield _factory

    for p in created_paths:
        try:
            p.unlink(missing_ok=True)
            bak = p.with_suffix(".bak.json")
            bak.unlink(missing_ok=True)
        except Exception:
            pass


@pytest.fixture
def sm(sm_factory):
    """Convenience: a single SignalManager with temp file."""
    manager, _, _ = sm_factory()
    return manager


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Class-level predicates (pure, no I/O)
# ═══════════════════════════════════════════════════════════════════════════════

class TestPredicates:
    def test_is_active_signal_pending(self):
        from core.signals import SignalManager
        assert SignalManager._is_active_signal({"status": "Pending"}) is True

    def test_is_active_signal_open(self):
        from core.signals import SignalManager
        assert SignalManager._is_active_signal({"status": "Open"}) is True

    def test_is_active_signal_closed(self):
        from core.signals import SignalManager
        assert SignalManager._is_active_signal({"status": "CLOSED"}) is False

    def test_is_active_signal_win(self):
        from core.signals import SignalManager
        assert SignalManager._is_active_signal({"status": "WIN"}) is False

    def test_is_active_signal_empty(self):
        from core.signals import SignalManager
        assert SignalManager._is_active_signal({}) is False

    def test_should_be_open_high_score(self):
        from core.signals import SignalManager
        # score=85, threshold=70 → open_cutoff=max(70, 80)=80 → 85>=80 → True
        assert SignalManager._should_be_open({"signal_score": 85, "signal_threshold": 70}) is True

    def test_should_be_open_low_score(self):
        from core.signals import SignalManager
        # score=60, threshold=70 → open_cutoff=80 → 60<80 → False
        assert SignalManager._should_be_open({"signal_score": 60, "signal_threshold": 70}) is False

    def test_should_be_open_boundary(self):
        from core.signals import SignalManager
        # score=80, threshold=70 → open_cutoff=80 → 80>=80 → True
        assert SignalManager._should_be_open({"signal_score": 80, "signal_threshold": 70}) is True

    def test_should_be_open_uses_score_alias(self):
        from core.signals import SignalManager
        # 'score' field instead of 'signal_score'
        assert SignalManager._should_be_open({"score": 90}) is True

    def test_non_v2_signal_is_research_only(self):
        from core.signals import SignalManager

        result = SignalManager._mark_research_only_when_unvalidated({"strategy_source": "V1"})
        assert result["research_only"] is True
        assert result["capital_execution_eligible"] is False

    def test_v2_signal_keeps_its_execution_provenance_unmodified(self):
        from core.signals import SignalManager

        result = SignalManager._mark_research_only_when_unvalidated({"strategy_source": "V2"})
        assert "research_only" not in result


# ═══════════════════════════════════════════════════════════════════════════════
# 2. add_signal — validation & happy paths
# ═══════════════════════════════════════════════════════════════════════════════

class TestAddSignal:
    def test_add_valid_buy_signal(self, sm):
        with patch("core.signals.get_capital_manager") as mock_gcm, \
             patch("core.signals.ConfigManager") as mock_cfg_cls:
            mock_cm = MagicMock()
            mock_cm.get_balance.return_value = 10_000.0
            mock_gcm.return_value = mock_cm
            mock_cfg = MagicMock()
            mock_cfg.agents = {"risk": {"position_size_pct": 2.0}}
            mock_cfg_cls.return_value = mock_cfg

            sig = _make_signal()
            result = sm.add_signal(sig)

        assert result == "sig-001"
        assert "sig-001" in sm.open_signals

    def test_add_signal_no_id_raises(self, sm):
        sig = _make_signal(sid=None)
        sig.pop("id")
        with pytest.raises(ValueError, match="ID"):
            sm.add_signal(sig)

    def test_add_signal_invalid_tp_raises(self, sm):
        sig = _make_signal(tp=0)
        with pytest.raises(ValueError, match="TP/SL"):
            sm.add_signal(sig)

    def test_add_signal_invalid_sl_raises(self, sm):
        sig = _make_signal(sl=-1)
        with pytest.raises(ValueError, match="TP/SL"):
            sm.add_signal(sig)

    def test_add_buy_signal_tp_below_entry_raises(self, sm):
        # BUY: TP must be above entry
        sig = _make_signal(entry=150, tp=140, sl=145)
        with pytest.raises(ValueError, match="TP"):
            sm.add_signal(sig)

    def test_add_buy_signal_sl_above_entry_raises(self, sm):
        # BUY: SL must be below entry
        sig = _make_signal(entry=150, tp=160, sl=155)
        with pytest.raises(ValueError, match="SL"):
            sm.add_signal(sig)

    def test_add_short_signal_tp_above_entry_raises(self, sm):
        sig = _make_signal(action="SHORT", entry=150, tp=160, sl=155)
        with pytest.raises(ValueError, match="TP"):
            sm.add_signal(sig)

    def test_add_short_signal_sl_below_entry_raises(self, sm):
        sig = _make_signal(action="SHORT", entry=150, tp=140, sl=145)
        with pytest.raises(ValueError, match="SL"):
            sm.add_signal(sig)

    def test_add_signal_insufficient_balance_raises(self, sm):
        with patch("core.signals.get_capital_manager") as mock_gcm, \
             patch("core.signals.ConfigManager") as mock_cfg_cls:
            mock_cm = MagicMock()
            mock_cm.get_balance.return_value = 5.0  # below $10 threshold
            mock_gcm.return_value = mock_cm
            mock_cfg = MagicMock()
            mock_cfg.agents = {"risk": {"position_size_pct": 2.0}}
            mock_cfg_cls.return_value = mock_cfg

            sig = _make_signal(sid="sig-low")
            with pytest.raises(ValueError, match="Insufficient"):
                sm.add_signal(sig)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. update_outcomes — TP/SL hit detection
# ═══════════════════════════════════════════════════════════════════════════════

class TestUpdateOutcomes:
    def _add_raw(self, sm, sig):
        """Bypass add_signal validation — insert directly into open_signals."""
        sm.open_signals[sig["id"]] = sig

    def test_buy_tp_hit(self, sm):
        sig = _make_signal(status="Open", entry=150, tp=160, sl=145)
        self._add_raw(sm, sig)
        result = sm.update_outcomes("AAPL", 161.0)
        assert result == "WIN"
        assert "sig-001" not in sm.open_signals
        assert any(s["status"] == "WIN" for s in sm.closed_signals)

    def test_buy_sl_hit(self, sm):
        sig = _make_signal(status="Open", entry=150, tp=160, sl=145)
        self._add_raw(sm, sig)
        result = sm.update_outcomes("AAPL", 144.0)
        assert result == "LOST"
        assert any(s["status"] == "LOST" for s in sm.closed_signals)

    def test_short_tp_hit(self, sm):
        sig = _make_signal(
            sid="s2", action="SHORT", status="Open",
            entry=150, tp=140, sl=155
        )
        self._add_raw(sm, sig)
        result = sm.update_outcomes("AAPL", 139.0)
        assert result == "WIN"

    def test_short_sl_hit(self, sm):
        sig = _make_signal(
            sid="s3", action="SHORT", status="Open",
            entry=150, tp=140, sl=155
        )
        self._add_raw(sm, sig)
        result = sm.update_outcomes("AAPL", 156.0)
        assert result == "LOST"

    def test_no_hit_returns_none(self, sm):
        sig = _make_signal(status="Open", entry=150, tp=160, sl=145)
        self._add_raw(sm, sig)
        result = sm.update_outcomes("AAPL", 152.0)
        assert result is None
        assert "sig-001" in sm.open_signals

    def test_pending_signal_promoted_to_open_when_score_sufficient(self, sm):
        # score=85 > open_cutoff=80 → should flip to Open
        sig = _make_signal(status="Pending", signal_score=85, signal_threshold=70)
        self._add_raw(sm, sig)
        sm.update_outcomes("OTHER", 999.0)  # different ticker, won't close
        assert sm.open_signals["sig-001"]["status"] == "Open"

    def test_buy_pnl_calculated_correctly(self, sm):
        sig = _make_signal(status="Open", entry=100, tp=110, sl=95, invested_amount=1000)
        self._add_raw(sm, sig)
        sm.update_outcomes("AAPL", 110.0)
        closed = sm.closed_signals[-1]
        # pnl_pct = (110-100)/100 * 100 = 10%
        assert closed["pnl_pct"] == pytest.approx(10.0, abs=0.01)

    def test_short_pnl_calculated_correctly(self, sm):
        sig = _make_signal(
            sid="s4", action="SHORT", status="Open",
            entry=100, tp=90, sl=105, invested_amount=1000
        )
        self._add_raw(sm, sig)
        sm.update_outcomes("AAPL", 90.0)  # exactly TP
        closed = sm.closed_signals[-1]
        # pnl_pct = (100-90)/100 * 100 = 10%
        assert closed["pnl_pct"] == pytest.approx(10.0, abs=0.01)

    def test_signals_with_zero_tp_skipped(self, sm):
        # Invalid signal — should be skipped, no result
        sig = _make_signal(status="Open", tp=0, sl=145)
        # bypass add_signal so it doesn't raise ValueError
        sm.open_signals["sig-001"] = sig
        result = sm.update_outcomes("AAPL", 200.0)
        assert result is None
        assert "sig-001" in sm.open_signals  # not closed


# ═══════════════════════════════════════════════════════════════════════════════
# 4. has_open_signal_for_ticker
# ═══════════════════════════════════════════════════════════════════════════════

class TestHasOpenSignal:
    def test_returns_signal_when_exists(self, sm):
        sig = _make_signal(status="Open")
        sm.open_signals["sig-001"] = sig
        result = sm.has_open_signal_for_ticker("AAPL")
        assert result is not None
        assert result["ticker"] == "AAPL"

    def test_returns_none_for_closed_signal(self, sm):
        sig = _make_signal(status="CLOSED")
        sm.open_signals["sig-001"] = sig
        result = sm.has_open_signal_for_ticker("AAPL")
        assert result is None

    def test_returns_none_when_no_signal(self, sm):
        result = sm.has_open_signal_for_ticker("TSLA")
        assert result is None

    def test_ignores_different_ticker(self, sm):
        sig = _make_signal(ticker="MSFT", status="Open")
        sm.open_signals["sig-001"] = sig
        result = sm.has_open_signal_for_ticker("AAPL")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# 5. can_open_new_signal_for_ticker — cooldown logic
# ═══════════════════════════════════════════════════════════════════════════════

class TestCooldown:
    def test_no_previous_signal_allowed(self, sm):
        assert sm.can_open_new_signal_for_ticker("AAPL") is True

    def test_within_cooldown_blocked(self, sm):
        from core.signals import get_bucharest_time
        sm.last_signal_time["AAPL"] = get_bucharest_time() - timedelta(minutes=5)
        assert sm.can_open_new_signal_for_ticker("AAPL", min_minutes=15) is False

    def test_after_cooldown_allowed(self, sm):
        from core.signals import get_bucharest_time
        sm.last_signal_time["AAPL"] = get_bucharest_time() - timedelta(minutes=20)
        assert sm.can_open_new_signal_for_ticker("AAPL", min_minutes=15) is True

    def test_custom_cooldown_respected(self, sm):
        from core.signals import get_bucharest_time
        sm.last_signal_time["TSLA"] = get_bucharest_time() - timedelta(minutes=3)
        assert sm.can_open_new_signal_for_ticker("TSLA", min_minutes=5) is False
        assert sm.can_open_new_signal_for_ticker("TSLA", min_minutes=2) is True


# ═══════════════════════════════════════════════════════════════════════════════
# 6. delete_signal
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeleteSignal:
    def test_delete_open_signal(self, sm):
        sm.open_signals["sig-001"] = _make_signal(status="Open")
        result = sm.delete_signal("sig-001")
        assert result is True
        assert "sig-001" not in sm.open_signals

    def test_delete_closed_signal(self, sm):
        sm.closed_signals = [_make_signal(sid="closed-1", status="CLOSED")]
        result = sm.delete_signal("closed-1")
        assert result is True
        assert all(s.get("id") != "closed-1" for s in sm.closed_signals)

    def test_delete_nonexistent_returns_false(self, sm):
        result = sm.delete_signal("does-not-exist")
        assert result is False

    def test_delete_does_not_affect_other_signals(self, sm):
        sm.open_signals["sig-A"] = _make_signal(sid="sig-A", status="Open")
        sm.open_signals["sig-B"] = _make_signal(sid="sig-B", ticker="TSLA", status="Open")
        sm.delete_signal("sig-A")
        assert "sig-B" in sm.open_signals


# ═══════════════════════════════════════════════════════════════════════════════
# 7. clear_all
# ═══════════════════════════════════════════════════════════════════════════════

class TestClearAll:
    def test_clear_all(self, sm):
        sm.open_signals["s1"] = _make_signal(status="Open")
        sm.closed_signals = [_make_signal(sid="s2", status="CLOSED")]
        sm.clear_all()
        assert sm.open_signals == {}
        assert sm.closed_signals == []
        assert sm.last_signal_time == {}


# ═══════════════════════════════════════════════════════════════════════════════
# 8. get_stats
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetStats:
    def test_empty_stats(self, sm):
        stats = sm.get_stats()
        assert stats["open"] == 0
        assert stats["closed"] == 0
        assert stats["wins"] == 0
        assert stats["losses"] == 0
        assert stats["win_rate"] == 0.0

    def test_stats_with_signals(self, sm):
        sm.open_signals["s1"] = _make_signal(status="Open")
        sm.closed_signals = [
            _make_signal(sid="c1", status="WIN", outcome_data_valid=True),
            _make_signal(sid="c2", status="LOST", outcome_data_valid=True),
            _make_signal(sid="c3", status="WIN", outcome_data_valid=True),
        ]
        stats = sm.get_stats()
        assert stats["open"] == 1
        assert stats["wins"] == 2
        assert stats["losses"] == 1
        assert stats["win_rate"] == pytest.approx(66.7, abs=0.1)
        assert stats["unverified_outcomes"] == 0

    def test_legacy_outcomes_are_quarantined_from_performance_metrics(self, sm):
        sm.closed_signals = sm._normalize_closed_signals([
            _make_signal(sid="legacy-win", status="WIN"),
            _make_signal(sid="legacy-loss", status="LOST"),
        ])

        stats = sm.get_stats()
        assert stats["wins"] == 0
        assert stats["losses"] == 0
        assert stats["verified_outcomes"] == 0
        assert stats["unverified_outcomes"] == 2
        assert all(signal["outcome_data_valid"] is False for signal in sm.closed_signals)


# ═══════════════════════════════════════════════════════════════════════════════
# 9. get_variant_stats
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetVariantStats:
    def _closed(self, sid, variant, status, days_ago=1):
        from core.signals import get_bucharest_time
        t = (get_bucharest_time() - timedelta(days=days_ago)).isoformat()
        return {"id": sid, "variant": variant, "status": status, "created_at": t}

    def test_variant_stats_basic(self, sm):
        sm.closed_signals = [
            self._closed("a1", "A", "WIN"),
            self._closed("a2", "A", "WIN"),
            self._closed("a3", "A", "LOST"),
            self._closed("b1", "B", "LOST"),
        ]
        stats = sm.get_variant_stats("A")
        assert stats["wins"] == 2
        assert stats["losses"] == 1
        assert stats["total"] == 3
        assert stats["win_pct"] == pytest.approx(66.7, abs=0.1)

    def test_variant_stats_empty(self, sm):
        stats = sm.get_variant_stats("C")
        assert stats["total"] == 0
        assert stats["win_pct"] == 0.0

    def test_variant_stats_time_filter(self, sm):
        # Signal older than 30d should not be counted
        from core.signals import get_bucharest_time
        old_t = (get_bucharest_time() - timedelta(days=40)).isoformat()
        sm.closed_signals = [
            {"id": "old", "variant": "A", "status": "WIN", "created_at": old_t},
            self._closed("new", "A", "LOST", days_ago=1),
        ]
        stats = sm.get_variant_stats("A", days=30)
        assert stats["wins"] == 0
        assert stats["losses"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 10. update_signal_status (batch update)
# ═══════════════════════════════════════════════════════════════════════════════

class TestUpdateSignalStatus:
    def test_batch_update_closes_on_tp(self, sm):
        sig = _make_signal(status="Open", entry=100, tp=110, sl=95, invested_amount=1000)
        sm.open_signals["sig-001"] = sig
        outcomes = sm.update_signal_status({
            "AAPL": {
                "price": 115.0,
                "price_verified": True,
                "price_integrity": {"status": "verified", "is_verified": True},
            }
        })
        assert "AAPL" in outcomes
        assert outcomes["AAPL"] == "WIN"
        assert sm.closed_signals[0]["outcome_data_valid"] is True

    def test_batch_update_rejects_unverified_price(self, sm):
        sig = _make_signal(status="Open", entry=100, tp=110, sl=95)
        sm.open_signals["sig-001"] = sig
        outcomes = sm.update_signal_status({
            "AAPL": {
                "price": 115.0,
                "price_verified": False,
                "price_integrity": {"status": "conflict", "is_verified": False},
            }
        })
        assert outcomes == {}
        assert "sig-001" in sm.open_signals

    def test_batch_update_skips_none_price(self, sm):
        sig = _make_signal(status="Open", entry=100, tp=110, sl=95)
        sm.open_signals["sig-001"] = sig
        outcomes = sm.update_signal_status({"AAPL": {"price": None}})
        assert "AAPL" not in outcomes


# ═══════════════════════════════════════════════════════════════════════════════
# 11. cleanup_orphaned_signals
# ═══════════════════════════════════════════════════════════════════════════════

class TestCleanupOrphanedSignals:
    def test_removes_signal_not_in_watchlist(self, sm):
        sig = _make_signal(ticker="DOGE", status="Open")
        sm.open_signals["sig-001"] = sig
        removed = sm.cleanup_orphaned_signals({"AAPL", "TSLA"})
        assert removed == 1
        assert "sig-001" not in sm.open_signals
        assert any(s["ticker"] == "DOGE" for s in sm.closed_signals)

    def test_keeps_signals_in_watchlist(self, sm):
        sig = _make_signal(ticker="AAPL", status="Open")
        sm.open_signals["sig-001"] = sig
        removed = sm.cleanup_orphaned_signals({"AAPL", "TSLA"})
        assert removed == 0
        assert "sig-001" in sm.open_signals

    def test_keeps_autonomous_scanner_signal_outside_watchlist(self, sm):
        sig = _make_signal(ticker="WOLF", status="Open")
        sig["source"] = "autonomous_monitor"
        sig["autonomous_source"] = "scanner"
        sm.open_signals["auto-scanner"] = sig
        removed = sm.cleanup_orphaned_signals({"AAPL", "TSLA"})
        assert removed == 0
        assert "auto-scanner" in sm.open_signals

    def test_orphaned_signal_close_reason(self, sm):
        sig = _make_signal(ticker="DOGE", status="Open")
        sm.open_signals["sig-001"] = sig
        sm.cleanup_orphaned_signals(set())
        closed = sm.closed_signals[-1]
        assert "watchlist" in closed.get("close_reason", "").lower()

    def test_orphaned_uses_tickers_data_exit_price(self, sm):
        sig = _make_signal(ticker="DOGE", status="Open", entry=1.0)
        sm.open_signals["sig-001"] = sig
        sm.cleanup_orphaned_signals(set(), tickers_data={"DOGE": {"price": 1.5}})
        closed = sm.closed_signals[-1]
        assert closed["exit_price"] == pytest.approx(1.5)


# ═══════════════════════════════════════════════════════════════════════════════
# 12. cleanup_invalid_signals — win-rate gate (critical invariant)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCleanupInvalidSignals:
    def _mock_config(self, min_win_rate=0, signals_enabled=True, allowed_strategies=None):
        cfg = MagicMock()
        settings = {"signals_enabled": signals_enabled, "min_win_rate": min_win_rate}
        if allowed_strategies is not None:
            settings["allowed_strategies"] = allowed_strategies
        cfg.get_effective_ticker_settings.return_value = settings
        return cfg

    def test_signals_disabled_removes_signal(self, sm):
        sig = _make_signal(ticker="AAPL", status="Open")
        sm.open_signals["sig-001"] = sig
        cfg = self._mock_config(signals_enabled=False)
        removed = sm.cleanup_invalid_signals(cfg)
        assert removed == 1
        assert "sig-001" not in sm.open_signals

    def test_min_win_rate_gate_pending_backtest_not_removed(self, sm):
        """Critical invariant: pending backtest must NOT be removed by win-rate gate."""
        sig = _make_signal(ticker="AAPL", status="Open", quick_backtest_30d={"pending": True})
        sm.open_signals["sig-001"] = sig
        cfg = self._mock_config(min_win_rate=60)
        removed = sm.cleanup_invalid_signals(cfg)
        # Must NOT be removed — pending backtest bypasses win-rate gate
        assert removed == 0
        assert "sig-001" in sm.open_signals

    def test_min_win_rate_gate_low_win_rate_removes(self, sm):
        sig = _make_signal(
            ticker="AAPL", status="Open",
            quick_backtest_30d={"win_rate": 40.0, "total": 10}
        )
        sm.open_signals["sig-001"] = sig
        cfg = self._mock_config(min_win_rate=60)
        removed = sm.cleanup_invalid_signals(cfg)
        assert removed == 1

    def test_min_win_rate_gate_sufficient_win_rate_keeps(self, sm):
        sig = _make_signal(
            ticker="AAPL", status="Open",
            quick_backtest_30d={"win_rate": 70.0, "total": 10}
        )
        sm.open_signals["sig-001"] = sig
        cfg = self._mock_config(min_win_rate=60)
        removed = sm.cleanup_invalid_signals(cfg)
        assert removed == 0

    def test_no_backtest_history_removes_when_min_wr_set(self, sm):
        # total=0 → no history → remove
        sig = _make_signal(
            ticker="AAPL", status="Open",
            quick_backtest_30d={"win_rate": None, "total": 0}
        )
        sm.open_signals["sig-001"] = sig
        cfg = self._mock_config(min_win_rate=50)
        removed = sm.cleanup_invalid_signals(cfg)
        assert removed == 1

    def test_backtest_absent_entirely_not_removed(self, sm):
        """Old signal without quick_backtest_30d key → don't close."""
        sig = _make_signal(ticker="AAPL", status="Open")
        # ensure no backtest key at all
        sig.pop("quick_backtest_30d", None)
        sm.open_signals["sig-001"] = sig
        cfg = self._mock_config(min_win_rate=60)
        removed = sm.cleanup_invalid_signals(cfg)
        assert removed == 0

    def test_strategy_filter_removes_not_allowed(self, sm):
        sig = _make_signal(
            ticker="AAPL", status="Open",
            risk_mode="MEDIUM", signal_mode="PULLBACK"
        )
        sm.open_signals["sig-001"] = sig
        cfg = self._mock_config(allowed_strategies=["STRONG/BREAKOUT"])
        removed = sm.cleanup_invalid_signals(cfg)
        assert removed == 1

    def test_strategy_filter_keeps_autonomous_scanner_signal(self, sm):
        sig = _make_signal(
            ticker="CGPT-USD",
            status="Open",
            risk_mode="MEDIUM",
            signal_mode="AUTONOMOUS_SCANNER",
            source="autonomous_monitor",
            autonomous_source="scanner",
            strategy_type="SCANNER_AI_SHORT",
        )
        sm.open_signals["auto-cgpt"] = sig
        cfg = self._mock_config(allowed_strategies=["STRONG/BREAKOUT"])
        removed = sm.cleanup_invalid_signals(cfg)
        assert removed == 0
        assert "auto-cgpt" in sm.open_signals

    def test_strategy_filter_keeps_signal_with_open_paper_position(self, sm):
        sig = _make_signal(
            ticker="CGPT-USD",
            status="Open",
            risk_mode="MEDIUM",
            signal_mode="PULLBACK",
            paper_position_id="pos-open",
        )
        sm.open_signals["auto-cgpt"] = sig
        mock_cm = MagicMock()
        # _find_open_paper_position iterates cm.get_positions() (plural) and matches
        # by paper_position_id == position["position_id"] with status OPEN — wire
        # the plural list form (same pattern as TestSyncOpenPaperPositions).
        mock_cm.get_positions.return_value = [{"position_id": "pos-open", "status": "OPEN"}]
        cfg = self._mock_config(allowed_strategies=["STRONG/BREAKOUT"])
        with patch("core.signals.get_capital_manager", return_value=mock_cm):
            removed = sm.cleanup_invalid_signals(cfg)
        assert removed == 0
        assert "auto-cgpt" in sm.open_signals

    def test_strategy_filter_keeps_allowed(self, sm):
        sig = _make_signal(
            ticker="AAPL", status="Open",
            risk_mode="MEDIUM", signal_mode="PULLBACK"
        )
        sm.open_signals["sig-001"] = sig
        cfg = self._mock_config(allowed_strategies=["MEDIUM/PULLBACK"])
        removed = sm.cleanup_invalid_signals(cfg)
        assert removed == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 13. close_signal_manual
# ═══════════════════════════════════════════════════════════════════════════════

class TestRestoreOpenPaperSignals:
    def test_restores_closed_signal_with_open_paper_position(self, sm):
        sig = _make_signal(
            sid="auto-cgpt",
            ticker="CGPT-USD",
            action="SHORT",
            entry=0.03146,
            tp=0.02957,
            sl=0.0324,
            status="CLOSED",
            paper_position_id="pos-open",
            close_reason="Strategy MEDIUM/AUTONOMOUS_SCANNER not allowed",
            closed_at="2026-05-16T17:28:16+03:00",
            source="autonomous_monitor",
            autonomous_source="scanner",
            strategy_type="SCANNER_AI_SHORT",
        )
        sm.closed_signals.append(sig)
        mock_cm = MagicMock()
        # restore_open_paper_signals fetches cm.get_positions() (plural) once and
        # matches by paper_position_id; wire the plural list form (matches the
        # production detection path and the sibling sync test).
        mock_cm.get_positions.return_value = [
            {
                "position_id": "pos-open",
                "status": "OPEN",
                "entry_price": 0.03136,
                "qty": 157.5,
                "margin_used": 4.94,
                "commission_usd": 0.01235,
                "opened_at": "2026-05-16T17:27:40+03:00",
                "strategy_type": "SCANNER_AI_SHORT",
                "entry_reason": "Scanner detected CGPT-USD SHORT momentum",
                "decision_context": {"ticker": "CGPT-USD", "action": "SHORT"},
            }
        ]
        with patch("core.signals.get_capital_manager", return_value=mock_cm):
            restored = sm.restore_open_paper_signals()
        assert restored == 1
        assert "auto-cgpt" in sm.open_signals
        assert not sm.closed_signals
        restored_sig = sm.open_signals["auto-cgpt"]
        assert restored_sig["status"] == "Open"
        assert restored_sig["paper_status"] == "OPEN"
        assert restored_sig["executed_entry_price"] == pytest.approx(0.03136)
        assert "close_reason" not in restored_sig


class TestSyncOpenPaperPositions:
    def test_sync_attaches_open_position_by_signal_id(self, sm):
        sig = _make_signal(
            sid="auto-osmo",
            ticker="OSMO-USD",
            action="BUY",
            entry=0.0781,
            tp=0.0828,
            sl=0.0758,
            status="Open",
            source="autonomous_monitor",
            autonomous_source="scanner",
            strategy_type="SCANNER_AI_LONG",
        )
        sm.open_signals["auto-osmo"] = sig

        mock_cm = MagicMock()
        mock_cm.get_positions.return_value = [
            {
                "position_id": "pos-osmo",
                "signal_id": "auto-osmo",
                "source_signal_id": "auto-osmo",
                "ticker": "OSMO-USD",
                "status": "OPEN",
                "entry_price": 0.0768,
                "qty": 63.7,
                "margin_used": 4.89,
                "commission_usd": 0.0122,
                "opened_at": "2026-05-16T17:50:16+03:00",
                "strategy_type": "SCANNER_AI_LONG",
                "entry_reason": "Scanner detected OSMO-USD LONG momentum",
                "decision_context": {"ticker": "OSMO-USD", "action": "BUY"},
            }
        ]

        with patch("core.signals.get_capital_manager", return_value=mock_cm):
            sm.sync_paper_positions()

        synced = sm.open_signals["auto-osmo"]
        assert synced["paper_status"] == "OPEN"
        assert synced["paper_position_id"] == "pos-osmo"
        assert synced["executed_entry_price"] == pytest.approx(0.0768)
        assert synced["paper_margin_used"] == pytest.approx(4.89)


class TestCloseSignalManual:
    def test_close_moves_to_closed(self, sm):
        sig = _make_signal(status="Open")
        sm.open_signals["sig-001"] = sig
        sm.close_signal_manual("AAPL", reason="User closed", exit_price=155.0)
        assert "sig-001" not in sm.open_signals
        assert len(sm.closed_signals) == 1

    def test_close_calculates_pnl(self, sm):
        sig = _make_signal(status="Open", entry=100.0, action="BUY", invested_amount=1000)
        sm.open_signals["sig-001"] = sig
        sm.close_signal_manual("AAPL", reason="Manual", exit_price=110.0)
        closed = sm.closed_signals[-1]
        assert closed["pnl_pct"] == pytest.approx(10.0, abs=0.01)

    def test_close_without_exit_price(self, sm):
        sig = _make_signal(status="Open")
        sm.open_signals["sig-001"] = sig
        sm.close_signal_manual("AAPL", reason="No price")
        assert len(sm.closed_signals) == 1

    def test_close_only_affects_target_ticker(self, sm):
        sm.open_signals["s1"] = _make_signal(sid="s1", ticker="AAPL", status="Open")
        sm.open_signals["s2"] = _make_signal(sid="s2", ticker="TSLA", status="Open")
        sm.close_signal_manual("AAPL", reason="Manual")
        assert "s2" in sm.open_signals
        assert "s1" not in sm.open_signals

    # ───────────────────────────────────────────────────────────────────────
    # EX1 (Runda-5): close_signal_manual must settle capital (release margin)
    # even when exit_price is None. The engine REPLACED/REVERSAL paths
    # (engine.py:2983/3042) pass exit_price=None when their own price fetch
    # failed; pre-fix that skipped _sync_closed_signal_capital entirely -> the
    # linked paper position stayed OPEN -> margin reserved forever.
    # ───────────────────────────────────────────────────────────────────────

    def test_ex1_none_exit_price_still_syncs_capital_with_entry_fallback(self, sm):
        sig = _make_signal(
            sid="sig-ex1a", status="Open", entry=100.0, action="BUY",
            invested_amount=1000.0, paper_position_id="pos-ex1a",
        )
        sm.open_signals["sig-ex1a"] = sig
        calls = []

        def fake_sync(*, sid, signal, exit_price, fallback_reason, ticker, pnl_pct):
            calls.append({"sid": sid, "exit_price": exit_price})
            return None  # no paper position dict -> to_close path

        sm._sync_closed_signal_capital = fake_sync  # type: ignore[method-assign]
        sm.close_signal_manual("AAPL", reason="REPLACED_BY_STRICT", exit_price=None)

        assert len(calls) == 1, (
            "EX1: _sync_closed_signal_capital MUST be called even when exit_price "
            f"is None (margin leak); got {len(calls)} calls"
        )
        assert calls[0]["exit_price"] == pytest.approx(100.0), (
            "EX1: when exit_price is None, fall back to the signal's entry price so "
            "capital is settled (~0 P&L, no phantom gain/loss)"
        )
        assert "sig-ex1a" not in sm.open_signals
        assert len(sm.closed_signals) == 1

    def test_ex1_none_exit_and_zero_entry_passes_zero_so_close_fetches(self, sm):
        sig = _make_signal(
            sid="sig-ex1b", status="Open", entry=0.0, action="BUY",
            invested_amount=1000.0, paper_position_id="pos-ex1b",
        )
        sm.open_signals["sig-ex1b"] = sig
        calls = []

        def fake_sync(*, sid, signal, exit_price, fallback_reason, ticker, pnl_pct):
            calls.append({"exit_price": exit_price})
            return None

        sm._sync_closed_signal_capital = fake_sync  # type: ignore[method-assign]
        sm.close_signal_manual("AAPL", reason="REPLACED_BY_STRICT", exit_price=None)

        assert len(calls) == 1
        # entry=0 -> effective_exit=0 -> close_position will fetch a live price
        # itself (the position must not stay open because the reason label drifted).
        assert calls[0]["exit_price"] == 0.0

    def test_ex1_explicit_exit_price_unchanged(self, sm):
        """When exit_price IS provided, behavior is unchanged: sync called with
        that exact price (no fallback). Guards against over-eager fallback."""
        sig = _make_signal(
            sid="sig-ex1c", status="Open", entry=100.0, action="BUY",
            invested_amount=1000.0, paper_position_id="pos-ex1c",
        )
        sm.open_signals["sig-ex1c"] = sig
        calls = []

        def fake_sync(*, sid, signal, exit_price, fallback_reason, ticker, pnl_pct):
            calls.append({"exit_price": exit_price})
            return None

        sm._sync_closed_signal_capital = fake_sync  # type: ignore[method-assign]
        sm.close_signal_manual("AAPL", reason="Manual", exit_price=110.0)

        assert len(calls) == 1
        assert calls[0]["exit_price"] == pytest.approx(110.0)
        closed = sm.closed_signals[-1]
        assert closed["pnl_pct"] == pytest.approx(10.0, abs=0.01)


# ═══════════════════════════════════════════════════════════════════════════════
# 14. back_signal_with_strict
# ═══════════════════════════════════════════════════════════════════════════════

class TestBackSignalWithStrict:
    def test_marks_signal_as_backed(self, sm):
        sig = _make_signal(status="Open")
        sm.open_signals["sig-001"] = sig
        strict = {
            "variant": "A",
            "risk_mode": "STRONG",
            "signal_mode": "BREAKOUT",
            "signal_score": 95,
            "probability_score": 0.88,
            "mtf_alignment": True,
        }
        result = sm.back_signal_with_strict("AAPL", strict)
        assert result is True
        assert sm.open_signals["sig-001"]["strict_backed"] is True
        assert sm.open_signals["sig-001"]["strict_variant"] == "A"

    def test_returns_false_no_signal(self, sm):
        result = sm.back_signal_with_strict("AAPL", {})
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════════
# 15. persist / load roundtrip (temp file path, no state_persistence mock)
# ═══════════════════════════════════════════════════════════════════════════════

class TestPersistRoundtrip:
    def test_save_and_load(self, sm_factory):
        sm, path, _ = sm_factory()
        sig = _make_signal(status="Open")
        sm.open_signals["sig-001"] = sig
        sm.closed_signals = [_make_signal(sid="c1", status="WIN")]
        sm.save()

        # Verify file on disk
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "sig-001" in data["open"]
        assert any(s["id"] == "c1" for s in data["closed"])

    def test_reload_if_changed(self, sm_factory):
        from core.signals import SignalManager
        sm, path, _ = sm_factory()

        # Write new data directly to disk
        new_data = {
            "open": {"x1": _make_signal(sid="x1", status="Open")},
            "closed": [],
            "last_signal_time": {},
        }
        path.write_text(json.dumps(new_data), encoding="utf-8")
        # Force mtime difference
        import time
        time.sleep(0.05)
        import os
        os.utime(path, None)
        sm.last_mtime = 0.0  # pretend it's stale

        reloaded = sm.reload_if_changed()
        assert reloaded is True
        assert "x1" in sm.open_signals
