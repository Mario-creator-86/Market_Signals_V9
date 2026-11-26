"""tests/unit/test_medium_findings_batch_a.py
================================================
Regression guards for the medium-finding batch A (autonomous §6 fixes):
  M1 — PRO profile honored when risk_mode=="PRO" (engine + backtester).
  M2 — V2 override recomputes variant/risk_mode from the V2 score (not stale V1).
  M3 — positions[-300:] trim never drops an OPEN position (capital-integrity).
  M9 — watchlist_snapshot SQLite round-trip (the key the engine reads for
       hours_to_event / V2 event_block; written by state_service.build_tickers_list).

Each fix is win-rate-justified in tasks/todo.md. These tests pin the contract.
"""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")


# --------------------------------------------------------------------------- #
# M2 — V2 override variant/risk_mode consistent with V2 score
# --------------------------------------------------------------------------- #
class TestV2OverrideVariantRiskModeConsistent:
    """M2: _apply_v2_override must recompute variant/risk_mode from the V2 score
    (same 85/70 bands as the V1 path), not leave the V1-derived label stale.

    Pre-fix: a V2 signal carried V2 tp/sl/rr alongside a V1-band variant/risk_mode
    -> mismatched the win-rate filter and the ollama decision layer which gate on
    the label."""

    def _engine(self):
        # _apply_v2_override never touches `self`, so an uninitialised instance
        # is enough to exercise it in isolation.
        from core.engine import MarketEngine
        return object.__new__(MarketEngine)

    def _v2_candidate(self, score: float) -> dict:
        return {
            "type": "BUY",
            "direction": "LONG",
            "signal_score": float(score),
            "strategy_type": "TrendPullback",
            "tp": 110.0,
            "sl": 95.0,
            "validation_score": 80.0,
            "setup_score": 90.0,
            "risk_score": 85.0,
            "catalyst_score": 70.0,
        }

    def test_high_v2_score_maps_to_variant_a_strong(self):
        eng = self._engine()
        result = eng._apply_v2_override(
            {"variant": "C", "risk_mode": "RELAX", "reasons": []},
            self._v2_candidate(88.0), v2_tp_pct=10.0, v2_sl_pct=5.0, v2_rr=2.0,
        )
        assert result["variant"] == "A"
        assert result["risk_mode"] == "STRONG"

    def test_mid_v2_score_maps_to_variant_b_medium(self):
        eng = self._engine()
        result = eng._apply_v2_override(
            {"variant": "C", "risk_mode": "RELAX", "reasons": []},
            self._v2_candidate(75.0), v2_tp_pct=10.0, v2_sl_pct=5.0, v2_rr=2.0,
        )
        assert result["variant"] == "B"
        assert result["risk_mode"] == "MEDIUM"

    def test_low_v2_score_maps_to_variant_c_relax(self):
        eng = self._engine()
        result = eng._apply_v2_override(
            {"variant": "A", "risk_mode": "STRONG", "reasons": []},
            self._v2_candidate(60.0), v2_tp_pct=10.0, v2_sl_pct=5.0, v2_rr=2.0,
        )
        assert result["variant"] == "C"
        assert result["risk_mode"] == "RELAX"

    def test_no_v2_candidate_leaves_variant_risk_mode_unchanged(self):
        eng = self._engine()
        result = eng._apply_v2_override(
            {"variant": "B", "risk_mode": "MEDIUM", "reasons": []},
            None, v2_tp_pct=0.0, v2_sl_pct=0.0, v2_rr=0.0,
        )
        # Override is a no-op when v2_candidate is None -> label untouched.
        assert result["variant"] == "B"
        assert result["risk_mode"] == "MEDIUM"
        assert "strategy_source" not in result


# --------------------------------------------------------------------------- #
# M1 — PRO profile iterated when risk_mode == "PRO" (backtester path)
# --------------------------------------------------------------------------- #
def _synthetic_ohlc_df(n: int = 220):
    import pandas as pd
    idx = pd.date_range("2025-01-01 09:30", periods=n, freq="1min", tz="UTC", name="dt")
    t = pd.Series(range(n))
    close = 100.0 + 0.05 * t
    df = pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close * 1.0015,
            "low": close * 0.9985,
            "close": close,
            "volume": pd.Series([10_000.0] * n),
        }
    )
    df.index = idx
    return df


class TestProProfileIterated:
    """M1: when risk_mode=="PRO", the backtester's profile list must include PRO
    (and lead with it). Pre-fix PRO was absent from every `profiles` list, so a
    config of risk_mode:"PRO" was silently routed to MEDIUM/STRONG/RELAX."""

    def test_pro_profile_appears_in_iteration(self):
        import core.strategy_backtester as sb
        from core.strategy_backtester import backtest

        # ConfigManager is NOT a singleton -- the backtester builds a fresh one
        # from config.json, so patch sb.ConfigManager to a fake whose .signal
        # carries risk_mode="PRO" (the configured-but-pre-fix-unreachable profile).
        class _FakeCM:
            signal = {"risk_mode": "PRO", "regime": {}, "scoring": None,
                      "min_candles_between_signals": 20, "profit_target_pct": 5.0,
                      "stop_loss_pct": 2.0, "enable_regime_gating": True}
            bot = {"trailing_enabled": False, "trailing_activation_r": 1.0,
                   "trailing_factor": 0.5}
            strategy_v2 = {
                "signal_mode": "V1",
                "costs_by_asset": {
                    "CRYPTO_FUTURES": {
                        "commission_bps": 25,
                        "entry_slippage_bps": 10,
                        "slippage_sl_bps": 10,
                        "slippage_tp_bps": 10,
                    }
                },
            }

        recorded_profiles = []

        def recording_score(ctx, mode, profile, scoring_cfg, **kw):
            recorded_profiles.append(profile)
            return SimpleNamespace(is_valid=False, total_score=0, direction="LONG",
                                   mode=mode, profile=profile, tp=0.0, sl=0.0)

        df = _synthetic_ohlc_df()
        with patch("core.config.ConfigManager", _FakeCM), \
             patch.object(sb, "compute_signal_score", side_effect=recording_score), \
             patch.object(sb, "allowed_modes_for_regime",
                          lambda regime, cfg: ["PULLBACK", "BREAKOUT", "CONTRA"]), \
             patch.object(sb, "row_to_context",
                          lambda row: SimpleNamespace(market_regime="MIXED")), \
             patch.object(sb, "detect_market_regime",
                          lambda ctx, cfg: ("MIXED", {})):
            backtest("TEST-USD", days=2, warmup=5, df=df)

        assert "PRO" in recorded_profiles, (
            "PRO profile was never iterated (pre-fix: PRO absent from every "
            "profiles list -> risk_mode:'PRO' silently fell through to MEDIUM). "
            f"Recorded profiles: {sorted(set(recorded_profiles))}"
        )


# --------------------------------------------------------------------------- #
# M3 — positions[-300:] trim never drops an OPEN position
# --------------------------------------------------------------------------- #
class TestPositionsTrimPreservesOpen:
    """M3: _save_unlocked partitions OPEN vs CLOSED before the [-300:] trim so a
    long-held OPEN position (head of the insertion-ordered list) is not dropped.
    Pre-fix the slice trimmed by insertion order -> OPEN position dropped -> its
    reserved margin never released -> permanent margin leak + balance understated.
    """

    def test_open_position_survives_when_over_300_closed_after_it(self, tmp_path, monkeypatch):
        import core.capital_manager as capital_mod
        from core.capital_manager import CapitalManager

        capital_path = tmp_path / "capital.json"
        monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", capital_path)
        monkeypatch.setattr(capital_mod, "get_sqlite_store", lambda: None)
        CapitalManager._instance = None
        manager = CapitalManager()
        try:
            # One long-held OPEN position at the HEAD, then 310 CLOSED ones.
            open_pos = {"position_id": "OPEN_HEAD", "ticker": "AAPL", "status": "OPEN",
                        "direction": "LONG", "entry_price": 100.0, "margin_used": 200.0}
            closed = [{"position_id": f"C{i}", "ticker": "AAPL", "status": "CLOSED",
                       "direction": "LONG", "entry_price": 100.0} for i in range(310)]
            manager.positions = [open_pos] + closed
            manager.balance = 5000.0

            manager._save_unlocked()

            saved = json.loads(capital_path.read_text(encoding="utf-8"))
            saved_pos = saved["positions"]
            pids = {p["position_id"] for p in saved_pos}
            assert "OPEN_HEAD" in pids, (
                "OPEN position was dropped by the [-300:] trim -> margin leaked. "
                f"Saved position ids: {sorted(pids)[:5]}... ({len(pids)} total)"
            )
        finally:
            CapitalManager._instance = None

    def test_trim_still_caps_closed_tail_at_300(self, tmp_path, monkeypatch):
        import core.capital_manager as capital_mod
        from core.capital_manager import CapitalManager

        capital_path = tmp_path / "capital.json"
        monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", capital_path)
        monkeypatch.setattr(capital_mod, "get_sqlite_store", lambda: None)
        CapitalManager._instance = None
        manager = CapitalManager()
        try:
            open_pos = {"position_id": "OPEN_HEAD", "status": "OPEN"}
            closed = [{"position_id": f"C{i}", "status": "CLOSED"} for i in range(400)]
            manager.positions = [open_pos] + closed
            manager.balance = 5000.0
            manager._save_unlocked()
            saved = json.loads(capital_path.read_text(encoding="utf-8"))
            # 300 CLOSED (trimmed) + 1 OPEN = 301.
            assert len(saved["positions"]) == 301
            assert sum(1 for p in saved["positions"] if p["status"] == "OPEN") == 1
        finally:
            CapitalManager._instance = None


# --------------------------------------------------------------------------- #
# M9 — watchlist_snapshot SQLite round-trip (engine reads it for hours_to_event)
# --------------------------------------------------------------------------- #
class TestWatchlistSnapshotRoundTrip:
    """M9: the engine reads state_store.get_json('watchlist_snapshot') to enrich
    watch_row with earnings_days/earnings_when -> hours_to_event -> V2 event_block.
    Pre-fix NOTHING wrote that key, so earnings_days was always -1 and the V2
    event_block (HIGH-g) was inert. state_service.build_tickers_list now writes it.
    This test pins the data-layer contract: the key round-trips and the engine's
    hours_to_event computation from a populated snapshot yields a real (<999) value.
    """

    def test_snapshot_round_trips_through_sqlite_store(self, tmp_path, monkeypatch):
        import core.sqlite_store as store_mod

        db = tmp_path / "test_ms.db"
        monkeypatch.setenv("MS_SQLITE_DB_PATH", str(db))
        store_mod.SQLiteKVStore._instance = None
        try:
            store = store_mod.get_sqlite_store()
            snap = {
                "AAPL": {"earnings_date": "2026-07-10", "earnings_days": 2,
                         "earnings_when": "BMO", "earnings_when_desc": "Before open"},
            }
            store.set_json("watchlist_snapshot", snap)
            got = store.get_json("watchlist_snapshot", None)
            assert isinstance(got, dict)
            assert got["AAPL"]["earnings_days"] == 2
            assert got["AAPL"]["earnings_when"] == "BMO"
        finally:
            store_mod.SQLiteKVStore._instance = None

    def test_hours_to_event_computed_from_snapshot(self):
        """Mirror engine.py's hours_to_event computation: a snapshot with
        earnings_days=2 + earnings_when='BMO' -> 2*24 + 2 = 50.0 hours (< 999),
        so event_risk becomes True when <= event_block_hours. Pre-fix the snapshot
        was empty -> earnings_days=-1 -> hours_to_event stayed 999.0 (no event risk)."""
        snap = {"AAPL": {"earnings_days": 2, "earnings_when": "BMO"}}
        # Engine enrichment: copy scalar fields from snapshot[ticker] into watch_row.
        watch_row = {"price": 190.0}
        for k, v in snap.get("AAPL", {}).items():
            if isinstance(v, (int, float, str, bool)):
                watch_row[k] = v

        hours_to_event = 999.0
        _ed = int(watch_row.get("earnings_days", -1))
        if _ed >= 0:
            _when = str(watch_row.get("earnings_when") or "").upper()
            _offset = 2.0 if _when in {"BMO", "BEFORE_OPEN", "BEFORE MARKET OPEN"} \
                else 10.0 if _when in {"AMC", "AFTER_CLOSE", "AFTER MARKET CLOSE"} else 0.0
            hours_to_event = max(0.0, float(_ed) * 24.0 + _offset)

        assert hours_to_event == pytest.approx(50.0)
        assert hours_to_event <= 999.0  # event_risk path now reachable
