# core/exit_manager.py - Position exit strategy engine

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from core.autonomous_candidates import position_time_stop_minutes
from core.market_hours import EASTERN_TZ, earnings_session_reported

logger = logging.getLogger("ExitManager")


class ExitManager:
    def __init__(self, config_manager=None):
        self.config_manager = config_manager

    @staticmethod
    def _parse_timestamp(value) -> Optional[datetime]:
        if not value:
            return None
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # Historical capital timestamps were stored as local time without
            # an offset. Treat them as local time instead of UTC.
            dt = dt.astimezone()
        return dt

    @staticmethod
    def _normalize_current_time(current_time: Optional[datetime]) -> datetime:
        if current_time is None:
            return datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            return current_time.astimezone()
        return current_time

    def _get_config(self) -> dict:
        if self.config_manager is not None:
            if isinstance(self.config_manager, dict):
                return self.config_manager
            if hasattr(self.config_manager, "reload_if_changed"):
                try:
                    self.config_manager.reload_if_changed()
                except Exception:
                    pass
            return self.config_manager.bot
        return {
            "trailing_enabled": True,
            "trailing_activation_r": 1.0,
            "trailing_factor": 0.5,
            "time_exit_enabled": False,
            "max_holding_minutes": 720,
            "max_holding_bars": 0,
            "bar_seconds": 60,
            "scanner_time_stop_minutes": 120,
            "pre_earnings_post_event_ai_review": True,
            "pre_earnings_exit_grace_minutes": 30,
        }

    @staticmethod
    def _pre_earnings_context(position: dict) -> dict:
        direct = (position or {}).get("pre_earnings_context")
        if isinstance(direct, dict):
            return direct
        decision = (position or {}).get("decision_context")
        if not isinstance(decision, dict):
            return {}
        nested = decision.get("pre_earnings_context")
        if isinstance(nested, dict):
            return nested
        autonomous = decision.get("autonomous")
        if isinstance(autonomous, dict) and isinstance(autonomous.get("pre_earnings_context"), dict):
            return autonomous["pre_earnings_context"]
        return {}

    @classmethod
    def _pre_earnings_event_review(cls, position: dict, cfg: dict, now: datetime) -> Optional[dict]:
        strategy = str((position or {}).get("strategy_type") or "").upper()
        if not strategy.startswith("PRE_EARNINGS") or not bool(cfg.get("pre_earnings_post_event_ai_review", True)):
            return None
        context = cls._pre_earnings_context(position)
        raw_event = context.get("earnings_time_utc") or (position or {}).get("earnings_time_utc")
        session = context.get("session") or (position or {}).get("earnings_session")
        try:
            event_dt = cls._parse_timestamp(raw_event)
        except (TypeError, ValueError):
            return None
        if event_dt is None:
            return None

        now_utc = now.astimezone(timezone.utc)
        now_et = now_utc.astimezone(EASTERN_TZ)
        event_date = event_dt.date() if event_dt.hour == 0 and event_dt.minute == 0 else event_dt.astimezone(EASTERN_TZ).date()
        if now_et.date() < event_date:
            return None
        grace_minutes = max(0.0, float(cfg.get("pre_earnings_exit_grace_minutes", 30) or 0))
        session_reported = earnings_session_reported(
            str(session or ""),
            now_utc - timedelta(minutes=grace_minutes),
        )
        # Once the event day has passed, an ambiguous/missing session must not
        # strand catalyst risk indefinitely. On the event day we require either
        # the conservative BMO/AMC threshold or a precise non-midnight timestamp.
        precise_event_passed = (
            (event_dt.hour != 0 or event_dt.minute != 0)
            and now_utc >= event_dt.astimezone(timezone.utc) + timedelta(minutes=grace_minutes)
        )
        if now_et.date() > event_date or session_reported or precise_event_passed:
            return {
                "action": "review",
                "reason": "pre_earnings_post_event_ai_review",
                "new_sl": 0.0,
                "earnings_time_utc": event_dt.astimezone(timezone.utc).isoformat(),
                "earnings_session": str(session or "UNKNOWN").upper(),
            }
        return None

    @staticmethod
    def _is_manual_position(position: dict) -> bool:
        position_type = str((position or {}).get("position_type") or "").lower().strip()
        strategy_type = str((position or {}).get("strategy_type") or "").upper().strip()
        return position_type == "manual" or strategy_type.startswith("MANUAL")

    @classmethod
    def _automatic_trailing_enabled(cls, position: dict, cfg: dict) -> bool:
        explicit = (position or {}).get("automatic_trailing_enabled")
        if explicit is not None:
            return bool(explicit)
        if cls._is_manual_position(position):
            return False
        return bool(cfg.get("trailing_enabled", True))

    def evaluate_position(
        self,
        position: dict,
        current_price: float,
        current_time: Optional[datetime] = None,
        allow_event_review: bool = True,
    ) -> Optional[dict]:
        """
        Evaluate whether a position should be closed or trailed.

        Returns None (hold), or dict with:
          - action: "close" | "trail" | "hold"
          - reason: str
          - new_sl: float (for trail actions)
        """
        if not isinstance(position, dict) or not position:
            return None

        cfg = self._get_config()
        direction = str(position.get("direction", "LONG")).upper()
        entry_price = float(position.get("entry_price", 0.0))
        sl_price = float(position.get("stop_loss_price", 0.0))
        tp_price = float(position.get("take_profit_price", 0.0))
        trailing_sl = float(position.get("trailing_sl", 0.0))
        opened_at = position.get("opened_at")
        automatic_trailing_enabled = self._automatic_trailing_enabled(position, cfg)

        if entry_price <= 0 or current_price <= 0:
            return None

        # 1. Fixed TP/SL — hard floor, always checked first
        if direction == "LONG":
            if sl_price > 0 and current_price <= sl_price:
                return {"action": "close", "reason": "stop_loss", "new_sl": 0.0}
            if tp_price > 0 and current_price >= tp_price:
                return {"action": "close", "reason": "take_profit", "new_sl": 0.0}
        else:
            if sl_price > 0 and current_price >= sl_price:
                return {"action": "close", "reason": "stop_loss", "new_sl": 0.0}
            if tp_price > 0 and current_price <= tp_price:
                return {"action": "close", "reason": "take_profit", "new_sl": 0.0}

        # 2. Trailing stop — check if current trailing SL is hit
        if automatic_trailing_enabled and trailing_sl > 0:
            if direction == "LONG" and current_price <= trailing_sl:
                return {"action": "close", "reason": "trailing_stop", "new_sl": 0.0}
            if direction == "SHORT" and current_price >= trailing_sl:
                return {"action": "close", "reason": "trailing_stop", "new_sl": 0.0}

        # 2b. Once earnings are reported, request an evidence-backed AI review.
        # This is deliberately not a mechanical close: favorable price, News,
        # and flow confirmation may justify holding or tightening the stop.
        event_exit = (
            self._pre_earnings_event_review(
                position,
                cfg,
                self._normalize_current_time(current_time),
            )
            if allow_event_review
            else None
        )
        if event_exit is not None:
            # A pending or unavailable AI review must never freeze the
            # mechanical safety net. A trailing-stop update only tightens
            # risk, so it stays compatible with whatever the review later
            # decides: emit it this tick and let the review fire on a tick
            # without a trail update. Returning the review unconditionally
            # froze the stop and MFE exactly when post-earnings volatility
            # hits.
            if automatic_trailing_enabled:
                trail_result = self._compute_trailing(position, current_price, cfg)
                if trail_result is not None and trail_result.get("action") == "trail":
                    return trail_result
            return event_exit

        # 2c. Scanner/fade time-stop — mean-reversion trades that have not reverted
        # within N minutes have failed (a trend is running them over); cut at market
        # regardless of R rather than wait for the full stop. Scoped to SCANNER
        # strategy positions so longer-horizon trades are untouched.
        scanner_stop_minutes = float(cfg.get("scanner_time_stop_minutes", 0) or 0)
        strategy_type = str(position.get("strategy_type", "")).upper()
        if scanner_stop_minutes > 0 and opened_at and strategy_type.startswith("SCANNER"):
            try:
                opened_dt = self._parse_timestamp(opened_at)
                if opened_dt is not None:
                    now_dt = self._normalize_current_time(current_time)
                    elapsed_min = max(0.0, (now_dt - opened_dt).total_seconds() / 60.0)
                    if elapsed_min >= scanner_stop_minutes:
                        return {"action": "close", "reason": "scanner_time_stop", "new_sl": 0.0}
            except (ValueError, TypeError):
                pass

        # 2d. Regime time-stop — a position whose entry rule was only tested with
        # an exit after N minutes carries that limit itself (crypto "market_dip":
        # the rebound edge is gone after the first day).
        regime_minutes = position_time_stop_minutes(position)
        if regime_minutes > 0 and opened_at:
            try:
                opened_dt = self._parse_timestamp(opened_at)
                if opened_dt is not None:
                    now_dt = self._normalize_current_time(current_time)
                    if (now_dt - opened_dt).total_seconds() / 60.0 >= regime_minutes:
                        return {"action": "close", "reason": "regime_time_stop", "new_sl": 0.0}
            except (ValueError, TypeError):
                pass

        # 3. Time exit — close if position held too long
        time_exit_enabled = bool(cfg.get("time_exit_enabled", False))
        max_minutes = float(cfg.get("max_holding_minutes", 0) or 0)
        if max_minutes <= 0:
            max_bars = float(cfg.get("max_holding_bars", 0) or 0)
            bar_seconds = float(cfg.get("bar_seconds", 60) or 60)
            max_minutes = (max_bars * bar_seconds) / 60.0
        if time_exit_enabled and opened_at and max_minutes > 0:
            try:
                opened_dt = self._parse_timestamp(opened_at)
                if opened_dt is None:
                    return None
                current_time = self._normalize_current_time(current_time)
                elapsed_s = (current_time - opened_dt).total_seconds()
                if elapsed_s < 0:
                    elapsed_s = 0
                elapsed_minutes = elapsed_s / 60.0
                if elapsed_minutes >= max_minutes:
                    min_profit_r = float(cfg.get("time_exit_min_profit_r", 0.0) or 0.0)
                    risk = abs(entry_price - sl_price) if sl_price > 0 else entry_price * 0.02
                    if direction == "LONG":
                        favorable_move = current_price - entry_price
                    else:
                        favorable_move = entry_price - current_price
                    current_r = favorable_move / risk if risk > 0 else 0.0
                    if current_r < min_profit_r:
                        return {
                            "action": "hold",
                            "reason": "time_exit_blocked_negative_r",
                            "new_sl": 0.0,
                            "current_r": round(current_r, 4),
                        }
                    return {"action": "close", "reason": "time_exit", "new_sl": 0.0}
            except (ValueError, TypeError):
                pass

        # 4. Compute trailing stop update
        if automatic_trailing_enabled:
            trail_result = self._compute_trailing(position, current_price, cfg)
            if trail_result is not None:
                return trail_result

        return None

    def _compute_trailing(self, position: dict, current_price: float, cfg: dict) -> Optional[dict]:
        """Compute trailing stop update if conditions are met."""
        direction = str(position.get("direction", "LONG")).upper()
        entry_price = float(position.get("entry_price", 0.0))
        sl_price = float(position.get("stop_loss_price", 0.0))
        current_trailing = float(position.get("trailing_sl", 0.0))
        max_favorable = float(position.get("max_favorable_excursion", 0.0))

        if entry_price <= 0:
            return None

        activation_r = float(cfg.get("trailing_activation_r", 1.0))
        trail_factor = float(cfg.get("trailing_factor", 0.5))

        # Compute risk (distance from entry to SL)
        risk = abs(entry_price - sl_price) if sl_price > 0 else entry_price * 0.02
        if risk <= 0:
            return None

        # Compute favorable excursion in R terms
        if direction == "LONG":
            favorable_move = current_price - entry_price
        else:
            favorable_move = entry_price - current_price

        current_r = favorable_move / risk

        # Update max favorable excursion
        new_max_favorable = max(max_favorable, favorable_move)

        # Only activate trailing after activation_r
        if current_r < activation_r:
            return {"action": "hold", "reason": "below_trailing_activation", "new_sl": 0.0,
                    "max_favorable_excursion": new_max_favorable}

        # Compute new trailing SL
        if direction == "LONG":
            # Trail at entry + (favorable_move * trail_factor)
            # But never below the original SL or current trailing SL
            new_sl = entry_price + favorable_move * trail_factor
            if sl_price > 0:
                new_sl = max(new_sl, sl_price)
            if current_trailing > 0:
                new_sl = max(new_sl, current_trailing)
            # Breakeven after 1R — ensure trailing SL doesn't stay below entry
            if current_r >= 1.0 and new_sl < entry_price:
                new_sl = entry_price
        else:
            new_sl = entry_price - favorable_move * trail_factor
            if sl_price > 0:
                new_sl = min(new_sl, sl_price)
            if current_trailing > 0:
                new_sl = min(new_sl, current_trailing)
            # No explicit breakeven guard for SHORT: the trailing formula
            # already produces new_sl < entry_price at 1R+ favorable move,
            # and min() guards above can only push it further below entry.

        # Only emit trail action if SL actually moved
        if direction == "LONG":
            if new_sl > current_trailing and new_sl > sl_price:
                return {"action": "trail", "reason": "trailing_update",
                        "new_sl": round(new_sl, 8),
                        "max_favorable_excursion": new_max_favorable}
        else:
            if (current_trailing <= 0 or new_sl < current_trailing) and new_sl < sl_price:
                return {"action": "trail", "reason": "trailing_update",
                        "new_sl": round(new_sl, 8),
                        "max_favorable_excursion": new_max_favorable}

        return {"action": "hold", "reason": "no_trail_update", "new_sl": 0.0,
                "max_favorable_excursion": new_max_favorable}
