from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from core.config import is_crypto_ticker, normalize_ticker
from core.data_model.entities import TradingSession, EXCHANGES


EASTERN_TZ = ZoneInfo("America/New_York")
US_EQUITY_OPEN = time(9, 30)
US_EQUITY_CLOSE = time(16, 0)
US_EQUITY_EARLY_CLOSE = time(13, 0)
US_EQUITY_PRE_OPEN = time(4, 0)       # pre-market starts 04:00 ET (§28)
US_EQUITY_AFTER_CLOSE = time(20, 0)   # after-hours ends 20:00 ET (§28)
# On 13:00 early-close days the late trading session ends at 17:00 ET (NYSE
# holiday calendar: "late trading sessions will close at 5:00 p.m.").
US_EQUITY_EARLY_AFTER_CLOSE = time(17, 0)

# Earnings session reporting thresholds in the exchange's local timezone.
# UTC constants silently retain BMO rows for an extra hour during US daylight
# saving time.  Session semantics belong to America/New_York.
EARNINGS_BMO_REPORTED_ET = time(9, 30)
EARNINGS_AMC_REPORTED_ET = time(18, 0)


class MarketHoursError(ValueError):
    """Raised when an equity trade is attempted outside regular market hours."""


@dataclass(frozen=True)
class MarketHoursStatus:
    is_open: bool
    reason: str
    now_et: datetime
    open_time_et: datetime
    close_time_et: datetime
    session: TradingSession = TradingSession.CLOSED
    is_half_day: bool = False
    exchange: str = "XNYS"


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + (n - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    return cursor - timedelta(days=(cursor.weekday() - weekday) % 7)


def _observed_fixed_holiday(year: int, month: int, day: int) -> date:
    raw = date(year, month, day)
    if raw.weekday() == 5:
        return raw - timedelta(days=1)
    if raw.weekday() == 6:
        return raw + timedelta(days=1)
    return raw


def _easter_date(year: int) -> date:
    # Anonymous Gregorian algorithm.
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _new_years_day_observed(year: int) -> Optional[date]:
    """New Year's Day closure for `year`, or None when it is not observed.

    NYSE Rule 7.2: a holiday falling on a Saturday is observed the preceding
    Friday *unless* that Friday is the last business day of the year (books
    close). So Jan 1 on a Saturday (2022, 2028, 2033) is NOT observed on
    Fri Dec 31 — the market is open that day."""
    raw = date(year, 1, 1)
    if raw.weekday() == 5:
        return None
    if raw.weekday() == 6:
        return raw + timedelta(days=1)
    return raw


def _us_equity_holidays(year: int) -> set[date]:
    holidays = {
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_date(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed_fixed_holiday(year, 7, 4),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed_fixed_holiday(year, 12, 25),
    }
    new_year = _new_years_day_observed(year)
    if new_year is not None:
        holidays.add(new_year)
    if year >= 2022:
        holidays.add(_observed_fixed_holiday(year, 6, 19))
    return holidays


def _us_equity_early_close_date(day: date) -> bool:
    """True if `day` is a 13:00 ET early-close TRADING day.

    A half day is by definition a trading day. A weekend or a full holiday
    (Fri Jul 3 when Jul 4 is a Saturday, Fri Dec 24 when Christmas is a
    Saturday) is closed, not "half day" — it used to be reported as both.
    The former "Jul 2 when Jul 4 is a Sunday" rule was wrong: NYSE trades a
    full session on Fri Jul 2 2027 (holiday observed Mon Jul 5)."""
    if day.weekday() >= 5 or day in _us_equity_holidays(day.year):
        return False
    thanksgiving = _nth_weekday(day.year, 11, 3, 4)
    if day == thanksgiving + timedelta(days=1):
        return True
    if day.month == 12 and day.day == 24:
        return True
    if day.month == 7 and day.day == 3:
        return True
    return False


def _override_status() -> Optional[bool]:
    override = str(os.getenv("MS_MARKET_HOURS_OVERRIDE", "")).strip().lower()
    if override in {"open", "1", "true", "yes", "on"}:
        return True
    if override in {"closed", "0", "false", "no", "off"}:
        return False
    return None


def _classify_session(
    now_et: datetime,
    open_dt: datetime,
    close_dt: datetime,
    after_close: time = US_EQUITY_AFTER_CLOSE,
) -> TradingSession:
    """Classify a timestamp into a US-equity trading session (§28)."""
    if now_et < open_dt:
        if now_et.time() >= US_EQUITY_PRE_OPEN:
            return TradingSession.PRE_MARKET
        return TradingSession.CLOSED
    if now_et <= close_dt:
        return TradingSession.REGULAR
    if now_et.time() <= after_close:
        return TradingSession.AFTER_HOURS
    return TradingSession.CLOSED


def _extended_close_time(day: date) -> time:
    """End of the after-hours session on trading day `day` (ET)."""
    return US_EQUITY_EARLY_AFTER_CLOSE if _us_equity_early_close_date(day) else US_EQUITY_AFTER_CLOSE


def us_equity_market_status(now: Optional[datetime] = None) -> MarketHoursStatus:
    raw_now = now or datetime.now(EASTERN_TZ)
    now_et = raw_now.astimezone(EASTERN_TZ) if raw_now.tzinfo else raw_now.replace(tzinfo=EASTERN_TZ)
    day = now_et.date()
    open_dt = datetime.combine(day, US_EQUITY_OPEN, EASTERN_TZ)
    early = _us_equity_early_close_date(day)
    close_t = US_EQUITY_EARLY_CLOSE if early else US_EQUITY_CLOSE
    close_dt = datetime.combine(day, close_t, EASTERN_TZ)

    override = _override_status()
    if override is not None:
        return MarketHoursStatus(override, "override", now_et, open_dt, close_dt,
                                 TradingSession.REGULAR if override else TradingSession.CLOSED,
                                 early, "XNYS")
    if now_et.weekday() >= 5:
        return MarketHoursStatus(False, "weekend", now_et, open_dt, close_dt,
                                 TradingSession.CLOSED, early, "XNYS")
    if day in _us_equity_holidays(day.year):
        return MarketHoursStatus(False, "us_equity_holiday", now_et, open_dt, close_dt,
                                 TradingSession.CLOSED, early, "XNYS")
    session = _classify_session(
        now_et, open_dt, close_dt,
        US_EQUITY_EARLY_AFTER_CLOSE if early else US_EQUITY_AFTER_CLOSE,
    )
    is_open = session is TradingSession.REGULAR
    if now_et < open_dt:
        reason = "before_regular_session"
    elif now_et > close_dt:
        reason = "after_regular_session"
    else:
        reason = "regular_session"
    return MarketHoursStatus(is_open, reason, now_et, open_dt, close_dt, session, early, "XNYS")


def earnings_session_reported(session_hint: str, now_utc: datetime) -> bool:
    """Same-day earnings reported check by BMO/AMC session hint (§28).

    BMO is reported once the actual New York market open is reached; AMC once
    past the conservative 18:00 ET after-hours bulk print. Unknown session -> not
    reported (keep — do not silently drop an ambiguous catalyst)."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)
    now_et = now_utc.astimezone(EASTERN_TZ)
    s = str(session_hint or "").lower().strip()
    if s in ("bmo", "pre-market", "premarket"):
        return now_et.time() >= EARNINGS_BMO_REPORTED_ET
    if s in ("amc", "after_hours", "afterhours", "aft"):
        return now_et.time() >= EARNINGS_AMC_REPORTED_ET
    return False


def is_us_equity_market_open(now: Optional[datetime] = None) -> bool:
    return us_equity_market_status(now).is_open


def is_24x5_instrument(ticker: str) -> bool:
    try:
        y_ticker = normalize_ticker(ticker, provider="yahoo")
    except Exception:
        y_ticker = str(ticker or "").upper().strip()
    return ("=F" in y_ticker) or ("=X" in y_ticker) or ("/" in y_ticker)


def is_equity_market_hours_enforced(ticker: str) -> bool:
    symbol = str(ticker or "").upper().strip()
    if not symbol:
        return False
    if is_crypto_ticker(symbol):
        return False
    if is_24x5_instrument(symbol):
        return False
    return True


def assert_trade_allowed(ticker: str, action: str, now: Optional[datetime] = None) -> None:
    if not is_equity_market_hours_enforced(ticker):
        return
    status = us_equity_market_status(now)
    if status.is_open:
        return
    action_text = str(action or "trade").strip().lower() or "trade"
    raise MarketHoursError(
        f"Cannot {action_text} stock position for {str(ticker or '').upper().strip()} "
        f"outside US regular market hours "
        f"({status.reason}; now {status.now_et.strftime('%Y-%m-%d %H:%M:%S %Z')}, "
        f"session {status.open_time_et.strftime('%H:%M')}-{status.close_time_et.strftime('%H:%M')} ET). "
        "Crypto positions are allowed 24/7."
    )


def is_us_equity_trading_day(day: date) -> bool:
    """True if `day` is a regular US equity trading day (Mon-Fri and not a market holiday)."""
    if day.weekday() >= 5:
        return False
    if day in _us_equity_holidays(day.year):
        return False
    return True


def get_previous_us_equity_trading_day(day: date) -> date:
    """Return the closest previous US equity trading day before `day`."""
    cursor = day - timedelta(days=1)
    while not is_us_equity_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def get_next_us_equity_trading_day(day: date) -> date:
    """Return the closest next US equity trading day after `day`."""
    cursor = day + timedelta(days=1)
    while not is_us_equity_trading_day(cursor):
        cursor += timedelta(days=1)
    return cursor


def is_pre_earnings_entry_window(
    earnings_time_utc: Any,
    session: Any,
    now: Optional[datetime] = None,
    *,
    window_hours: float = 2.0,
) -> tuple[bool, str, dict]:
    """Determine whether the pre-close opening window is currently active for an earnings candidate.

    Timing rules:
      - AMC (After Market Close) earnings: Position must open TODAY during the last `window_hours`
        (default 2h, e.g. 14:00-16:00 ET) before market close.
      - BMO / BFO (Before Market Open) earnings: Position must open on the PREVIOUS TRADING DAY
        during the last `window_hours` before market close (e.g. today before close if reporting tomorrow BMO).
      - On early close days (13:00 close), the window automatically aligns to 11:00-13:00 ET.
    """
    if earnings_time_utc is None:
        return False, "missing_earnings_time", {"status": "missing_earnings_time"}

    if isinstance(earnings_time_utc, datetime):
        event_dt = earnings_time_utc
    else:
        raw_str = str(earnings_time_utc).strip()
        if not raw_str:
            return False, "missing_earnings_time", {"status": "missing_earnings_time"}
        try:
            event_dt = datetime.fromisoformat(raw_str.replace("Z", "+00:00"))
        except Exception:
            try:
                parsed_d = date.fromisoformat(raw_str[:10])
                event_dt = datetime.combine(parsed_d, time(0, 0), timezone.utc)
            except Exception:
                return False, f"invalid_earnings_time:{raw_str}", {"status": "invalid_earnings_time"}

    if event_dt.tzinfo is None:
        event_dt = event_dt.replace(tzinfo=timezone.utc)

    # Date resolution in US/Eastern
    if event_dt.hour == 0 and event_dt.minute == 0:
        event_date_et = event_dt.date()
    else:
        event_date_et = event_dt.astimezone(EASTERN_TZ).date()

    raw_now = now or datetime.now(EASTERN_TZ)
    now_et = raw_now.astimezone(EASTERN_TZ) if raw_now.tzinfo else raw_now.replace(tzinfo=EASTERN_TZ)
    today_et = now_et.date()

    raw_sess = str(session or "").strip().upper()
    if raw_sess in {"BMO", "PRE-MARKET", "PREMARKET", "BEFORE MARKET OPEN", "BEFORE OPEN", "BFO"}:
        norm_sess = "BMO"
    elif raw_sess in {"AMC", "AFTER_HOURS", "AFTERHOURS", "AFTER MARKET CLOSE", "AFT"}:
        norm_sess = "AMC"
    else:
        norm_sess = "UNKNOWN"

    # Determine target entry trading date
    if norm_sess == "BMO":
        target_entry_date = get_previous_us_equity_trading_day(event_date_et)
    elif norm_sess == "AMC":
        target_entry_date = event_date_et
    else:
        if event_date_et == today_et:
            target_entry_date = today_et
        elif event_date_et > today_et:
            prev_day = get_previous_us_equity_trading_day(event_date_et)
            if today_et == prev_day and (event_dt.hour < 12 if (event_dt.hour or event_dt.minute) else True):
                target_entry_date = today_et
            else:
                target_entry_date = event_date_et
        else:
            target_entry_date = event_date_et

    market_status = us_equity_market_status(now_et)
    meta = {
        "event_date": event_date_et.isoformat(),
        "target_entry_date": target_entry_date.isoformat(),
        "today": today_et.isoformat(),
        "session": norm_sess,
        "window_hours": window_hours,
        "now_et": now_et.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "is_market_open": market_status.is_open,
        "market_session": market_status.session.value if hasattr(market_status.session, "value") else str(market_status.session),
    }

    if today_et < target_entry_date:
        reason = f"outside_pre_earnings_window: target entry date is {target_entry_date.isoformat()} (for {norm_sess} earnings on {event_date_et.isoformat()})"
        meta["status"] = "future_entry_date"
        return False, reason, meta

    if today_et > target_entry_date:
        if norm_sess == "BMO" and today_et == event_date_et:
            reason = f"outside_pre_earnings_window: BMO earnings is today morning; entry window was on {target_entry_date.isoformat()} before close"
        else:
            reason = f"outside_pre_earnings_window: target entry date {target_entry_date.isoformat()} has passed"
        meta["status"] = "passed_entry_date"
        return False, reason, meta

    if not market_status.is_open:
        open_s = market_status.open_time_et.strftime("%H:%M")
        close_s = market_status.close_time_et.strftime("%H:%M")
        if now_et < market_status.open_time_et:
            reason = f"outside_pre_earnings_window: market opens at {open_s} ET today"
            meta["status"] = "before_market_open"
        elif now_et > market_status.close_time_et:
            reason = f"outside_pre_earnings_window: market closed at {close_s} ET today"
            meta["status"] = "after_market_close"
        else:
            reason = f"outside_pre_earnings_window: market is closed today ({market_status.reason})"
            meta["status"] = f"market_closed_{market_status.reason}"
        return False, reason, meta

    close_time_et = market_status.close_time_et
    window_start_et = close_time_et - timedelta(hours=window_hours)
    w_start_s = window_start_et.strftime("%H:%M")
    w_close_s = close_time_et.strftime("%H:%M")
    meta["window_start_et"] = w_start_s
    meta["window_close_et"] = w_close_s

    if now_et < window_start_et:
        reason = (
            f"outside_pre_earnings_window: waiting for 2h pre-close entry window "
            f"({w_start_s}-{w_close_s} ET today) for {norm_sess} earnings on {event_date_et.isoformat()}"
        )
        meta["status"] = "waiting_for_pre_close_window"
        return False, reason, meta

    if now_et > close_time_et:
        reason = f"outside_pre_earnings_window: 2h pre-close window closed at {w_close_s} ET"
        meta["status"] = "window_expired"
        return False, reason, meta

    reason = (
        f"pre_earnings_window_open: 2h pre-close window is active "
        f"({w_start_s}-{w_close_s} ET) for {norm_sess} earnings on {event_date_et.isoformat()}"
    )
    meta["status"] = "window_open"
    return True, reason, meta


def _closed_kind(day: date) -> str:
    if day.weekday() >= 5:
        return "weekend"
    if day in _us_equity_holidays(day.year):
        return "holiday"
    return "overnight"


def us_equity_session_snapshot(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Describe the current US-equity session AND the session the latest prints belong to.

    `us_equity_market_status` answers "is the regular session open now?". A
    scanner or UI that shows prices outside regular hours also needs to know
    *which* session those prices come from, otherwise a Sunday screen presents
    Friday's last prints as a live pre-market. Pure function of `now`
    (JSON-serialisable result):

      state                    PRE_MARKET | REGULAR | AFTER_HOURS | CLOSED
      reason                   reason from us_equity_market_status
      closed_kind              weekend | holiday | overnight | override | None (not closed)
      is_trading_day           today (ET) is a trading day
      is_half_day              today closes at 13:00 ET
      now_et / now_utc         ISO timestamps
      quote_session_date       ET date of the most recent session with any trading
                               (pre-market counts) — the day the newest prints belong to
      last_regular_session_date / last_regular_close_utc
                               most recent COMPLETED regular session
      last_extended_close_utc  end of after-hours on quote_session_date; only once it
                               has passed (i.e. nothing has traded since), else None
      next_premarket_open_utc / next_regular_open_utc / next_session_date
                               next 04:00 ET / 09:30 ET strictly after `now`
      seconds_since_last_trade seconds since last_extended_close_utc when CLOSED, else 0
      label                    short human description
    """
    status = us_equity_market_status(now)
    now_et = status.now_et
    today = now_et.date()
    state = status.session.value if hasattr(status.session, "value") else str(status.session)
    trading_today = is_us_equity_trading_day(today)
    pre_open_today = datetime.combine(today, US_EQUITY_PRE_OPEN, EASTERN_TZ)

    if trading_today and now_et >= pre_open_today:
        quote_day = today
    else:
        quote_day = get_previous_us_equity_trading_day(today)

    if trading_today and now_et > status.close_time_et:
        last_regular_day = today
    else:
        last_regular_day = get_previous_us_equity_trading_day(today)
    last_regular_close = datetime.combine(
        last_regular_day,
        US_EQUITY_EARLY_CLOSE if _us_equity_early_close_date(last_regular_day) else US_EQUITY_CLOSE,
        EASTERN_TZ,
    )

    extended_close = datetime.combine(quote_day, _extended_close_time(quote_day), EASTERN_TZ)
    last_extended_close = extended_close if now_et > extended_close else None

    if trading_today and now_et < pre_open_today:
        next_pre_day = today
    else:
        next_pre_day = get_next_us_equity_trading_day(today)
    next_premarket_open = datetime.combine(next_pre_day, US_EQUITY_PRE_OPEN, EASTERN_TZ)

    if trading_today and now_et < status.open_time_et:
        next_open_day = today
    else:
        next_open_day = get_next_us_equity_trading_day(today)
    next_regular_open = datetime.combine(next_open_day, US_EQUITY_OPEN, EASTERN_TZ)

    closed_kind: Optional[str] = None
    if state == TradingSession.CLOSED.value:
        closed_kind = "override" if status.reason == "override" else _closed_kind(today)

    seconds_since_last_trade = 0
    if state == TradingSession.CLOSED.value and last_extended_close is not None:
        seconds_since_last_trade = int(max(0.0, (now_et - last_extended_close).total_seconds()))

    if state == TradingSession.PRE_MARKET.value:
        label = "Pre-market session (04:00-09:30 ET)"
    elif state == TradingSession.REGULAR.value:
        label = "Regular session open"
    elif state == TradingSession.AFTER_HOURS.value:
        label = "After-hours session"
    elif closed_kind == "weekend":
        label = "Market closed (weekend)"
    elif closed_kind == "holiday":
        label = "Market closed (US market holiday)"
    elif closed_kind == "override":
        label = "Market closed (override)"
    else:
        label = "Market closed (overnight)"

    def _utc_iso(value: Optional[datetime]) -> Optional[str]:
        return value.astimezone(timezone.utc).isoformat() if value is not None else None

    return {
        "state": state,
        "reason": status.reason,
        "closed_kind": closed_kind,
        "is_trading_day": bool(trading_today),
        "is_half_day": bool(status.is_half_day),
        "now_et": now_et.isoformat(),
        "now_utc": _utc_iso(now_et),
        "quote_session_date": quote_day.isoformat(),
        "last_regular_session_date": last_regular_day.isoformat(),
        "last_regular_close_utc": _utc_iso(last_regular_close),
        "last_extended_close_utc": _utc_iso(last_extended_close),
        "next_premarket_open_utc": _utc_iso(next_premarket_open),
        "next_regular_open_utc": _utc_iso(next_regular_open),
        "next_session_date": next_open_day.isoformat(),
        "seconds_since_last_trade": seconds_since_last_trade,
        "label": label,
    }


def us_eastern_date(value: Any, now: Optional[datetime] = None) -> Optional[date]:
    """US/Eastern calendar date of an event timestamp or of `now`.

    Earnings calendars publish date-only rows (parsed as midnight UTC): those
    already ARE the exchange-local date and must not be shifted. Full
    timestamps are converted to America/New_York. `value=None` returns the
    ET date of `now` (default: the current time)."""
    if value is None:
        raw = now or datetime.now(EASTERN_TZ)
        raw = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        return raw.astimezone(EASTERN_TZ).date()
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if dt.astimezone(timezone.utc).time() == time(0, 0):
            return dt.astimezone(timezone.utc).date()
        return dt.astimezone(EASTERN_TZ).date()
    if isinstance(value, date):
        return value
    return None
