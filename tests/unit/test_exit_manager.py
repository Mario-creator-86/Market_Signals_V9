"""Unit tests for ExitManager."""

import pytest
from datetime import datetime, timezone, timedelta
from core.exit_manager import ExitManager


@pytest.fixture
def manager():
    return ExitManager()


@pytest.fixture
def time_exit_manager():
    return ExitManager({
        "trailing_enabled": True,
        "trailing_activation_r": 1.0,
        "trailing_factor": 0.5,
        "time_exit_enabled": True,
        "max_holding_minutes": 120,
    })


def _make_position(
    direction="LONG",
    entry_price=100.0,
    sl=95.0,
    tp=110.0,
    trailing_sl=0.0,
    opened_at=None,
):
    pos = {
        "direction": direction,
        "entry_price": entry_price,
        "stop_loss_price": sl,
        "take_profit_price": tp,
        "trailing_sl": trailing_sl,
    }
    if opened_at:
        pos["opened_at"] = opened_at
    return pos


class TestFixedTPSL:
    def test_long_hit_sl(self, manager):
        pos = _make_position("LONG", entry_price=100, sl=95, tp=110)
        result = manager.evaluate_position(pos, current_price=94.0)
        assert result["action"] == "close"
        assert result["reason"] == "stop_loss"

    def test_long_hit_tp(self, manager):
        pos = _make_position("LONG", entry_price=100, sl=95, tp=110)
        result = manager.evaluate_position(pos, current_price=111.0)
        assert result["action"] == "close"
        assert result["reason"] == "take_profit"

    def test_short_hit_sl(self, manager):
        pos = _make_position("SHORT", entry_price=100, sl=105, tp=90)
        result = manager.evaluate_position(pos, current_price=106.0)
        assert result["action"] == "close"
        assert result["reason"] == "stop_loss"

    def test_short_hit_tp(self, manager):
        pos = _make_position("SHORT", entry_price=100, sl=105, tp=90)
        result = manager.evaluate_position(pos, current_price=89.0)
        assert result["action"] == "close"
        assert result["reason"] == "take_profit"

    def test_no_hit_hold(self, manager):
        pos = _make_position("LONG", entry_price=100, sl=95, tp=110)
        result = manager.evaluate_position(pos, current_price=102.0)
        # Not hitting SL/TP — either hold or trailing action
        assert result is None or result.get("action") in ("hold", "trail")


class TestTrailingStop:
    def test_trailing_sl_hit_long(self, manager):
        pos = _make_position("LONG", entry_price=100, sl=95, tp=120, trailing_sl=103.0)
        result = manager.evaluate_position(pos, current_price=102.0)
        assert result["action"] == "close"
        assert result["reason"] == "trailing_stop"

    def test_trailing_sl_hit_short(self, manager):
        pos = _make_position("SHORT", entry_price=100, sl=105, tp=80, trailing_sl=97.0)
        result = manager.evaluate_position(pos, current_price=98.0)
        assert result["action"] == "close"
        assert result["reason"] == "trailing_stop"

    def test_manual_position_ignores_legacy_trailing_stop(self, manager):
        pos = _make_position("LONG", entry_price=100, sl=95, tp=120, trailing_sl=103.0)
        pos.update({"position_type": "manual", "strategy_type": "MANUAL_CHART"})

        result = manager.evaluate_position(pos, current_price=102.0)

        assert result is None

    def test_manual_position_does_not_create_trailing_stop(self, manager):
        pos = _make_position("LONG", entry_price=100, sl=95, tp=120)
        pos.update({"position_type": "manual", "strategy_type": "MANUAL_CHART"})

        result = manager.evaluate_position(pos, current_price=106.0)

        assert result is None

    def test_manual_position_still_honors_fixed_stop_loss(self, manager):
        pos = _make_position("LONG", entry_price=100, sl=95, tp=120, trailing_sl=103.0)
        pos.update({"position_type": "manual", "strategy_type": "MANUAL_CHART"})

        result = manager.evaluate_position(pos, current_price=94.0)

        assert result["action"] == "close"
        assert result["reason"] == "stop_loss"

    def test_manual_position_can_explicitly_enable_trailing(self, manager):
        pos = _make_position("LONG", entry_price=100, sl=95, tp=120, trailing_sl=103.0)
        pos.update(
            {
                "position_type": "manual",
                "strategy_type": "MANUAL_CHART",
                "automatic_trailing_enabled": True,
            }
        )

        result = manager.evaluate_position(pos, current_price=102.0)

        assert result["action"] == "close"
        assert result["reason"] == "trailing_stop"


class TestTimeExit:
    def test_time_exit_disabled_by_default(self, manager):
        old_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        pos = _make_position("LONG", entry_price=100, sl=95, tp=110, opened_at=old_time)
        result = manager.evaluate_position(pos, current_price=102.0)
        assert result is None or result.get("reason") != "time_exit"

    def test_time_exit_triggered(self, time_exit_manager):
        old_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        pos = _make_position("LONG", entry_price=100, sl=95, tp=110, opened_at=old_time)
        result = time_exit_manager.evaluate_position(pos, current_price=102.0)
        assert result["action"] == "close"
        assert result["reason"] == "time_exit"

    def test_time_exit_does_not_close_negative_r_trade(self, time_exit_manager):
        old_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        pos = _make_position("LONG", entry_price=100, sl=95, tp=110, opened_at=old_time)
        result = time_exit_manager.evaluate_position(pos, current_price=98.0)
        assert result["action"] == "hold"
        assert result["reason"] == "time_exit_blocked_negative_r"
        assert result["current_r"] < 0

    def test_time_exit_not_triggered(self, time_exit_manager):
        recent_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        pos = _make_position("LONG", entry_price=100, sl=95, tp=110, opened_at=recent_time)
        result = time_exit_manager.evaluate_position(pos, current_price=102.0)
        # Should NOT be a time exit
        assert result is None or result.get("reason") != "time_exit"

    def test_time_exit_uses_timezone_offset(self, time_exit_manager):
        opened_local = "2026-04-30T07:30:00+03:00"
        just_before_limit = datetime(2026, 4, 30, 6, 29, 0, tzinfo=timezone.utc)
        pos = _make_position("LONG", entry_price=100, sl=95, tp=110, opened_at=opened_local)

        result = time_exit_manager.evaluate_position(pos, current_price=102.0, current_time=just_before_limit)
        assert result is None or result.get("reason") != "time_exit"

        after_limit = datetime(2026, 4, 30, 6, 31, 0, tzinfo=timezone.utc)
        result = time_exit_manager.evaluate_position(pos, current_price=102.0, current_time=after_limit)
        assert result["action"] == "close"
        assert result["reason"] == "time_exit"


class TestEdgeCases:
    def test_empty_position(self, manager):
        result = manager.evaluate_position({}, current_price=100.0)
        assert result is None

    def test_zero_entry_price(self, manager):
        pos = _make_position("LONG", entry_price=0, sl=0, tp=0)
        result = manager.evaluate_position(pos, current_price=100.0)
        assert result is None

    def test_none_position(self, manager):
        result = manager.evaluate_position(None, current_price=100.0)


class TestTrailingBreakevenShort:
    def test_short_trailing_sl_is_below_entry_at_1r(self, manager):
        """At 1R favorable move, the SHORT trailing SL should already be below
        entry_price without needing an explicit breakeven guard."""
        pos = _make_position("SHORT", entry_price=100, sl=105, tp=90)
        # At 1R: price dropped by risk = 5, so current_price = 95
        result = manager._compute_trailing(pos, current_price=95.0, cfg={
            "trailing_enabled": True,
            "trailing_activation_r": 1.0,
            "trailing_factor": 0.5,
        })
        assert result is not None
        new_sl = result.get("new_sl", 0.0)
        # new_sl = entry - favorable_move * factor = 100 - 5 * 0.5 = 97.5
        assert new_sl < 100.0
        assert new_sl == pytest.approx(97.5)

    def test_short_trailing_sl_stays_below_entry_with_tight_sl(self, manager):
        """Even with a tight original SL, SHORT trailing stays below entry."""
        pos = _make_position("SHORT", entry_price=100, sl=102, tp=90)
        # 1R = 2 (distance to SL), so at current_price=98 we're at 1R
        result = manager._compute_trailing(pos, current_price=98.0, cfg={
            "trailing_enabled": True,
            "trailing_activation_r": 1.0,
            "trailing_factor": 0.5,
        })
        assert result is not None
        new_sl = result.get("new_sl", 0.0)
        # new_sl = 100 - 2 * 0.5 = 99, clamped by min(99, sl=102) = 99
        assert new_sl == pytest.approx(99.0)
        assert new_sl < 100.0


class TestScannerTimeStop:
    """Fade/scanner trades get cut at market if they haven't reverted in N minutes."""

    def _mgr(self, minutes=120):
        return ExitManager({
            "trailing_enabled": False,
            "time_exit_enabled": False,
            "scanner_time_stop_minutes": minutes,
        })

    def _scanner_pos(self, opened_at, strategy="SCANNER_AI_SHORT"):
        return {
            "direction": "SHORT",
            "entry_price": 100.0,
            "stop_loss_price": 103.0,
            "take_profit_price": 97.0,
            "trailing_sl": 0.0,
            "opened_at": opened_at,
            "strategy_type": strategy,
        }

    def test_scanner_position_cut_after_limit(self):
        opened = datetime.now(timezone.utc) - timedelta(minutes=130)
        pos = self._scanner_pos(opened.isoformat())
        # price is between SL and TP (not reverted) -> time-stop fires
        result = self._mgr(120).evaluate_position(pos, current_price=100.5, current_time=datetime.now(timezone.utc))
        assert result is not None
        assert result["action"] == "close"
        assert result["reason"] == "scanner_time_stop"

    def test_scanner_position_held_within_limit(self):
        opened = datetime.now(timezone.utc) - timedelta(minutes=60)
        pos = self._scanner_pos(opened.isoformat())
        result = self._mgr(120).evaluate_position(pos, current_price=100.5, current_time=datetime.now(timezone.utc))
        assert result is None  # trailing disabled, no exit yet

    def test_non_scanner_position_not_cut(self):
        opened = datetime.now(timezone.utc) - timedelta(minutes=300)
        pos = self._scanner_pos(opened.isoformat(), strategy="PRE_EARNINGS_LONG")
        result = self._mgr(120).evaluate_position(pos, current_price=100.5, current_time=datetime.now(timezone.utc))
        assert result is None  # non-scanner strategy is untouched by the fade time-stop

    def test_tp_still_takes_precedence(self):
        opened = datetime.now(timezone.utc) - timedelta(minutes=300)
        pos = self._scanner_pos(opened.isoformat())
        # SHORT TP at 97; price 96 -> take_profit wins over the time-stop
        result = self._mgr(120).evaluate_position(pos, current_price=96.0, current_time=datetime.now(timezone.utc))
        assert result["reason"] == "take_profit"

    def test_disabled_when_zero(self):
        opened = datetime.now(timezone.utc) - timedelta(minutes=300)
        pos = self._scanner_pos(opened.isoformat())
        result = self._mgr(0).evaluate_position(pos, current_price=100.5, current_time=datetime.now(timezone.utc))
        assert result is None


class TestPreEarningsPostEventReview:
    def _position(self, event, session="BMO"):
        position = _make_position("LONG", entry_price=100, sl=90, tp=120)
        position.update({
            "strategy_type": "PRE_EARNINGS_AI_LONG",
            "decision_context": {
                "autonomous": {
                    "pre_earnings_context": {
                        "earnings_time_utc": event,
                        "session": session,
                    },
                },
            },
        })
        return position

    def test_bmo_position_requests_ai_review_after_report_grace(self):
        manager = ExitManager({
            "trailing_enabled": False,
            "pre_earnings_post_event_ai_review": True,
            "pre_earnings_exit_grace_minutes": 30,
        })
        position = self._position("2026-09-01T00:00:00+00:00", "BMO")

        before = manager.evaluate_position(
            position, 101.0, datetime(2026, 9, 1, 13, 59, tzinfo=timezone.utc)
        )
        after = manager.evaluate_position(
            position, 101.0, datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)
        )

        assert before is None
        assert after["action"] == "review"
        assert after["reason"] == "pre_earnings_post_event_ai_review"

    def test_past_event_requests_review_even_when_session_is_ambiguous(self):
        manager = ExitManager({"trailing_enabled": False, "pre_earnings_post_event_ai_review": True})
        position = self._position("2026-08-27T00:00:00+00:00", "UNKNOWN")

        result = manager.evaluate_position(
            position, 101.0, datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        )

        assert result["action"] == "review"
        assert result["reason"] == "pre_earnings_post_event_ai_review"

    def test_non_pre_earnings_position_is_untouched(self):
        manager = ExitManager({"trailing_enabled": False, "pre_earnings_post_event_ai_review": True})
        position = self._position("2026-08-27T00:00:00+00:00")
        position["strategy_type"] = "SWING_LONG"

        assert manager.evaluate_position(
            position, 101.0, datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        ) is None

    def test_hard_take_profit_still_precedes_post_event_review(self):
        manager = ExitManager({"trailing_enabled": False, "pre_earnings_post_event_ai_review": True})
        position = self._position("2026-08-27T00:00:00+00:00", "UNKNOWN")

        result = manager.evaluate_position(
            position, 121.0, datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        )

        assert result["action"] == "close"
        assert result["reason"] == "take_profit"


class TestPostEventReviewDoesNotFreezeMechanicalExits:
    """Audit 2026-09-02: a pending or unavailable post-event AI review must
    never freeze the mechanical safety net (trailing stop, time exit)."""

    def _position(self, event, session="BMO", opened_at=None):
        position = _make_position("LONG", entry_price=100, sl=90, tp=120)
        position.update({
            "strategy_type": "PRE_EARNINGS_AI_LONG",
            "decision_context": {
                "autonomous": {
                    "pre_earnings_context": {
                        "earnings_time_utc": event,
                        "session": session,
                    },
                },
            },
        })
        if opened_at:
            position["opened_at"] = opened_at
        return position

    def _trailing_manager(self):
        return ExitManager({
            "trailing_enabled": True,
            "trailing_activation_r": 1.0,
            "trailing_factor": 0.5,
            "pre_earnings_post_event_ai_review": True,
        })

    def test_trailing_update_emits_while_review_pending(self):
        manager = self._trailing_manager()
        position = self._position("2026-09-01T00:00:00+00:00", "BMO")

        result = manager.evaluate_position(
            position, 115.0, datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)
        )

        # 1.5R favorable: the trailing stop must keep tightening exactly when
        # post-earnings volatility hits, instead of returning "review".
        assert result["action"] == "trail"
        assert result["new_sl"] == pytest.approx(107.5)

    def test_review_fires_on_tick_without_trail_update(self):
        manager = self._trailing_manager()
        position = self._position("2026-09-01T00:00:00+00:00", "BMO")
        position["trailing_sl"] = 107.0

        result = manager.evaluate_position(
            position, 114.0, datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)
        )

        # No new trail extreme this tick -> the AI review is emitted.
        assert result["action"] == "review"
        assert result["reason"] == "pre_earnings_post_event_ai_review"

    def test_allow_event_review_false_restores_mechanical_time_exit(self):
        manager = ExitManager({
            "trailing_enabled": False,
            "time_exit_enabled": True,
            "max_holding_minutes": 120,
            "pre_earnings_post_event_ai_review": True,
        })
        opened = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
        position = self._position("2026-09-01T00:00:00+00:00", "BMO",
                                  opened_at=opened.isoformat())
        now = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)

        with_review = manager.evaluate_position(position, 101.0, current_time=now)
        assert with_review["action"] == "review"

        mechanical = manager.evaluate_position(
            position, 101.0, current_time=now, allow_event_review=False
        )
        assert mechanical["action"] == "close"
        assert mechanical["reason"] == "time_exit"
