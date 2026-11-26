"""Build execution-ready signals from autonomous market discovery agents."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.config import get_bucharest_time, is_crypto_ticker, normalize_market_ticker
from core.market_hours import is_pre_earnings_entry_window


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str):
            value = value.strip().replace("%", "").replace(",", "")
            if not value:
                return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(_safe_float(value, float(default))))
    except Exception:
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _scanner_symbol_to_ticker(symbol: Any) -> str:
    raw = str(symbol or "").upper().strip()
    if not raw:
        return ""
    if raw.endswith("USDT"):
        base = raw[:-4]
        # Stablecoin itself (USDT/USDC has no base asset). Pre-fix this produced
        # "-USD" (empty base) — garbage ticker that polluted signals & failed
        # execution. Skip it instead.
        raw = f"{base}-USD" if base else ""
        return normalize_market_ticker(raw) if raw else ""
    elif raw.endswith("USDC"):
        base = raw[:-4]
        raw = f"{base}-USD" if base else ""
        return normalize_market_ticker(raw) if raw else ""
    return normalize_market_ticker(raw)


# ---------------------------------------------------------------------------
# Instrument exclusions for autonomous paper entries
# ---------------------------------------------------------------------------
# The scanner tags Binance pairs that are tokenised US stocks
# (`instrument_type: "tokenized_stock"`, with `underlying`) and stablecoin
# pairs (`"stablecoin"`). Untagged they flowed into autonomous candidates as
# ordinary crypto: SPCXB-USD, MSTRB-USD and NVDABUSDT were traded 24/7 although
# they track US stocks, and the bot could hold both the token and its
# underlying. Both kinds are excluded from NEW autonomous entries by default;
# positions that are already open keep their normal exit handling (nothing
# here touches them).
INSTRUMENT_EXCLUSION_RULES: Dict[str, Dict[str, str]] = {
    "tokenized_stock": {
        "config_key": "exclude_tokenized_stocks",
        "reason": "tokenized_stock_excluded",
        "label": "tokenised stock",
    },
    "stablecoin": {
        "config_key": "exclude_stablecoins",
        "reason": "stablecoin_excluded",
        "label": "stablecoin pair",
    },
}


def instrument_exclusion_policy(root_config: Any) -> Dict[str, bool]:
    """`autonomous.exclude_tokenized_stocks` / `autonomous.exclude_stablecoins` (default True)."""
    section = root_config.get("autonomous") if isinstance(root_config, dict) else None
    section = section if isinstance(section, dict) else {}
    policy: Dict[str, bool] = {}
    for rule in INSTRUMENT_EXCLUSION_RULES.values():
        key = rule["config_key"]
        raw = section.get(key, True)
        policy[key] = True if raw is None else bool(raw)
    return policy


def instrument_tags_from_scanner_state(state: Any, known_tokenized: Any = None) -> Dict[str, Dict[str, Any]]:
    """Map normalised ticker -> {"instrument_type", "underlying"} from scanner rows.

    Reads every list of rows in the scanner snapshot (hot, liquid, watchlist),
    plus the scanner's remembered tokenised-stock map (symbol -> underlying).
    """
    tags: Dict[str, Dict[str, Any]] = {}
    if isinstance(state, dict):
        for rows in state.values():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                kind = str(row.get("instrument_type") or "").strip().lower()
                if kind not in INSTRUMENT_EXCLUSION_RULES:
                    continue
                ticker = _scanner_symbol_to_ticker(row.get("symbol") or row.get("ticker"))
                if ticker:
                    tags[ticker] = {
                        "instrument_type": kind,
                        "underlying": str(row.get("underlying") or "").upper().strip() or None,
                    }
    if isinstance(known_tokenized, dict):
        for symbol, underlying in known_tokenized.items():
            ticker = _scanner_symbol_to_ticker(symbol)
            if ticker and underlying and ticker not in tags:
                tags[ticker] = {
                    "instrument_type": "tokenized_stock",
                    "underlying": str(underlying).upper().strip(),
                }
    return tags


def classify_candidate_instrument(
    candidate: Any,
    tags: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """{"instrument_type", "underlying"} for a scanner row or a candidate signal.

    The row's / signal's own tag wins; otherwise the scanner tag for the same
    ticker. A stablecoin pair is also recognised from its name and price, so a
    candidate built from a source that carries no tag is still caught. A
    tokenised stock is never guessed from the name (ARB, BNB, TRB are ordinary
    tokens): only a scanner-verified tag counts.
    """
    payload = candidate if isinstance(candidate, dict) else {}
    context = payload.get("scanner_context") if isinstance(payload.get("scanner_context"), dict) else {}
    kind = str(payload.get("instrument_type") or context.get("instrument_type") or "").strip().lower()
    underlying = str(payload.get("underlying") or context.get("underlying") or "").upper().strip()
    raw_symbol = payload.get("ticker") or payload.get("symbol") or context.get("symbol")
    ticker = _scanner_symbol_to_ticker(raw_symbol)
    if kind not in INSTRUMENT_EXCLUSION_RULES and isinstance(tags, dict) and ticker in tags:
        tagged = tags[ticker] if isinstance(tags[ticker], dict) else {}
        kind = str(tagged.get("instrument_type") or "").strip().lower()
        underlying = underlying or str(tagged.get("underlying") or "").upper().strip()
    if kind not in INSTRUMENT_EXCLUSION_RULES:
        kind = ""
        if payload.get("is_stablecoin") is True:
            kind = "stablecoin"
        elif payload.get("is_tokenized_stock") is True and underlying:
            kind = "tokenized_stock"
        else:
            try:
                from agents.scanner_agent import is_stablecoin_pair

                price = payload.get("price") or payload.get("entry") or payload.get("entry_price")
                if is_stablecoin_pair(str(raw_symbol or ticker or ""), price):
                    kind = "stablecoin"
            except Exception:
                kind = ""
    return {"ticker": ticker, "instrument_type": kind or None, "underlying": underlying or None}


def autonomous_instrument_exclusion(
    candidate: Any,
    policy: Optional[Dict[str, bool]] = None,
    tags: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """The named exclusion for an autonomous candidate, or None when it may proceed."""
    info = classify_candidate_instrument(candidate, tags)
    kind = info.get("instrument_type")
    rule = INSTRUMENT_EXCLUSION_RULES.get(str(kind or ""))
    if rule is None:
        return None
    active = policy if isinstance(policy, dict) else instrument_exclusion_policy(None)
    if not bool(active.get(rule["config_key"], True)):
        return None
    underlying = info.get("underlying")
    detail = f"{info.get('ticker') or 'instrument'} is a {rule['label']}"
    if kind == "tokenized_stock" and underlying:
        detail += f" tracking {underlying}"
    detail += f"; excluded from autonomous paper entries (autonomous.{rule['config_key']}=true)"
    return {
        "reason": rule["reason"],
        "instrument_type": kind,
        "underlying": underlying,
        "ticker": info.get("ticker"),
        "config_key": f"autonomous.{rule['config_key']}",
        "detail": detail,
    }


def _build_signal_id(source: str, ticker: str, action: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    digest = hashlib.sha1(f"{source}:{ticker}:{action}:{now}".encode("utf-8")).hexdigest()[:10]
    return f"auto_{source}_{ticker.replace('-', '').replace('=', '')}_{action.lower()}_{digest}"


def _levels(entry: float, action: str, stop_pct: float, rr_ratio: float) -> Dict[str, float]:
    stop_pct = _clamp(float(stop_pct or 0.0), 0.001, 0.25)
    rr_ratio = _clamp(float(rr_ratio or 0.0), 1.0, 5.0)
    if action == "SHORT":
        sl = entry * (1.0 + stop_pct)
        tp = entry * (1.0 - (stop_pct * rr_ratio))
    else:
        sl = entry * (1.0 - stop_pct)
        tp = entry * (1.0 + (stop_pct * rr_ratio))
    return {
        "entry": round(entry, 8),
        "sl": round(sl, 8),
        "tp": round(tp, 8),
        "risk_reward_ratio": round(rr_ratio, 4),
    }


def _headline_age_label(published_at: Any) -> str:
    """Compact age label ("3h", "2d") from a published_at value, "" if unknown."""
    raw = str(published_at or "").strip()
    if not raw:
        return ""
    try:
        from datetime import datetime, timezone

        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        hours = max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds() / 3600.0)
    except (TypeError, ValueError):
        return ""
    if hours < 1.0:
        return f"{int(hours * 60)}m"
    if hours < 48.0:
        return f"{int(hours)}h"
    return f"{int(hours / 24)}d"


def _headline_with_meta(row: Dict[str, Any], title: str, max_len: int = 240) -> str:
    """Prefix the source + age so the AI reviewer can weigh the outlet instead
    of seeing bare titles (audit 2026-09-02: the §41 model complained it saw
    "press releases with no publisher/URL/timestamp" — the metadata existed on
    the article but was stripped before the prompt). Rows without metadata
    degrade to the bare title, so older fixtures stay byte-identical."""
    source = str(row.get("source") or "").strip()
    # pre-earnings rows carry the timestamp as published_utc (audit 2026-09-05:
    # the age component was silently empty for 902/902 top_news rows).
    age = _headline_age_label(
        row.get("published_at") or row.get("published_utc") or row.get("time")
    )
    meta = " | ".join(part for part in (f"src={source}" if source else "", f"age={age}" if age else "") if part)
    if not meta:
        return title[:220]
    return f"[{meta}] {title}"[:max_len]


def _news_sentiment_from_bias(news_ai: Any) -> str:
    """Signed pre-earnings news bias component → coarse sentiment label."""
    try:
        value = float(news_ai)
    except (TypeError, ValueError):
        return "unknown"
    if value >= 10:
        return "bullish"
    if value <= -10:
        return "bearish"
    return "neutral"


def _top_news_titles(item: Dict[str, Any], limit: int = 5) -> list[str]:
    headlines: list[str] = []
    for row in item.get("top_news") or item.get("latest_news") or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or row.get("headline") or "").strip()
        if title:
            headlines.append(_headline_with_meta(row, title))
        if len(headlines) >= limit:
            break
    return headlines


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _has_late_momentum_confirmation(item: Dict[str, Any], cfg: Dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []

    pullback_pct = abs(_safe_float(
        item.get("pullback_pct")
        or item.get("pullback_from_high_pct")
        or item.get("bounce_from_low_pct"),
        0.0,
    ))
    min_pullback = float(cfg.get("scanner_late_min_pullback_pct", 0.6) or 0.6)
    max_pullback = float(cfg.get("scanner_late_max_pullback_pct", 6.0) or 6.0)
    if min_pullback <= pullback_pct <= max_pullback:
        reasons.append(f"pullback={pullback_pct:.2f}%")

    consolidation_score = _safe_float(item.get("consolidation_score"), 0.0)
    if consolidation_score >= float(cfg.get("scanner_late_min_consolidation_score", 60.0) or 60.0):
        reasons.append(f"consolidation_score={consolidation_score:.1f}")

    if _truthy(item.get("is_consolidating")) or _truthy(item.get("base_breakout")):
        reasons.append("consolidation_flag")

    trade_radar_opportunity = _safe_float(item.get("trade_radar_opportunity"), 0.0)
    if trade_radar_opportunity >= float(cfg.get("scanner_late_min_trade_radar_opportunity", 60.0) or 60.0):
        reasons.append(f"trade_radar_opportunity={trade_radar_opportunity:.1f}")

    news_score = _safe_float(item.get("news_score") or item.get("news_pulse_score"), 0.0)
    if news_score >= float(cfg.get("scanner_late_min_news_score", 65.0) or 65.0):
        reasons.append(f"news_score={news_score:.1f}")

    if _truthy(item.get("has_news_catalyst")) or _truthy(item.get("confirmed_catalyst")):
        reasons.append("news_catalyst")

    rel_volume = _safe_float(item.get("relative_volume") or item.get("rel_volume"), 0.0)
    if rel_volume >= float(cfg.get("scanner_late_min_relative_volume", 2.5) or 2.5):
        reasons.append(f"relative_volume={rel_volume:.1f}")

    source = str(item.get("source") or item.get("exchange") or "").lower().strip()
    symbol = str(item.get("symbol") or "").upper().strip()
    is_crypto_mover = (
        source in {"binance", "kucoin"}
        or symbol.endswith("USDT")
        or symbol.endswith("USDC")
    )
    quote_volume = _safe_float(
        item.get("quoteVolume")
        or item.get("quote_volume")
        or item.get("turnover")
        or item.get("volume"),
        0.0,
    )
    min_crypto_liquidity = float(
        cfg.get(
            "scanner_late_min_crypto_quote_volume",
            cfg.get("scanner_min_volume_24h", 1_000_000),
        )
        or 0.0
    )
    if is_crypto_mover and quote_volume > 0 and quote_volume >= min_crypto_liquidity:
        reasons.append(f"crypto_liquidity=${quote_volume:,.0f}")

    return bool(reasons), reasons


def infer_pre_earnings_action(item: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[str]:
    """Return BUY/SHORT when the pre-earnings item has a directional edge."""
    if not isinstance(item, dict):
        return None
    min_abs_bias = _safe_float(cfg.get("pre_earnings_min_abs_bias"), 20.0)
    direction_label = str(item.get("direction_label") or "").upper().strip()
    bias_score = _safe_float(item.get("direction_bias_score"), 0.0)

    directional_action: Optional[str] = None
    if direction_label == "LONG" and abs(bias_score) >= min_abs_bias:
        directional_action = "BUY"
    elif direction_label == "SHORT" and abs(bias_score) >= min_abs_bias:
        directional_action = "SHORT"
    elif not direction_label and bias_score >= min_abs_bias:
        directional_action = "BUY"
    elif not direction_label and bias_score <= -min_abs_bias:
        directional_action = "SHORT"

    if bool(cfg.get("pre_earnings_require_canonical_decision", False)):
        decision = item.get("pre_earnings_decision")
        if not isinstance(decision, dict) or not bool(decision.get("available")):
            return None
        decision_action = str(decision.get("action") or "WATCH").upper().strip()
        if decision_action == "OPEN_LONG" and directional_action == "BUY":
            return directional_action
        if decision_action == "OPEN_SHORT" and directional_action == "SHORT":
            return directional_action
        return None

    return directional_action


def assess_pre_earnings_candidate(
    item: Dict[str, Any],
    price: float,
    cfg: Dict[str, Any],
    *,
    require_price: bool = True,
    require_entry_window: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Explain the deterministic gate before a candidate reaches Risk/AI.

    This is the authoritative admission check used by both the autonomous
    manager and the Cockpit read model.  Keeping one evaluator prevents the UI
    from calling a recommendation ``ELIGIBLE`` while the execution path silently
    rejects it on a different set of thresholds.
    """
    row = item if isinstance(item, dict) else {}
    execution_cfg = cfg if isinstance(cfg, dict) else {}
    symbol = normalize_market_ticker(str(row.get("symbol") or "").upper().strip())
    resolved_price = _safe_float(price, 0.0)
    gap_risk = _safe_int(row.get("gap_risk_numeric", row.get("gap_risk")), 0)
    coverage_score = _safe_int(row.get("coverage_score"), 0)
    bias_score = _safe_float(row.get("direction_bias_score"), 0.0)
    data_confidence = str(row.get("data_confidence") or "").upper().strip()
    min_gap = _safe_float(execution_cfg.get("pre_earnings_min_gap_risk"), 65.0)
    min_coverage = _safe_float(execution_cfg.get("pre_earnings_min_coverage_score"), 50.0)
    min_abs_bias = _safe_float(execution_cfg.get("pre_earnings_min_abs_bias"), 20.0)
    min_score = _safe_float(execution_cfg.get("pre_earnings_min_score"), 78.0)
    action = infer_pre_earnings_action(row, execution_cfg)
    direction_label = str(row.get("direction_label") or "").upper().strip()
    directional_edge_passes = (
        direction_label in {"LONG", "SHORT"}
        and abs(bias_score) >= min_abs_bias
    )
    canonical_required = bool(
        execution_cfg.get("pre_earnings_require_canonical_decision", False)
    )
    require_directional_edge = bool(
        execution_cfg.get("pre_earnings_require_directional_edge", False)
    )
    direction_track = row.get("direction_track_record")
    if not isinstance(direction_track, dict):
        direction_track = {}
    directional_sample = _safe_int(direction_track.get("n"), 0)
    directional_hit_rate = _safe_float(direction_track.get("hit_rate"), -1.0)
    min_directional_sample = max(
        1, _safe_int(execution_cfg.get("pre_earnings_min_directional_sample"), 100)
    )
    min_directional_hit_rate = _safe_float(
        execution_cfg.get("pre_earnings_min_directional_hit_rate"), 50.0
    )

    # 2-hour pre-close opening window check. 2026-09-03 audit P2: prefer the
    # SAME shared key the Pre-Earnings agent and the UI badge use
    # (pre_earnings.entry_window_hours); the execution-scoped key stays as a
    # compatibility fallback so existing configs keep working.
    earnings_time_utc = row.get("earnings_time_utc") or row.get("earnings_date")
    session = row.get("session") or row.get("when")
    window_hours = float(
        execution_cfg.get("pre_earnings_window_hours")
        or row.get("entry_window_hours")
        or 2.0
    )
    window_open, window_reason, window_meta = is_pre_earnings_entry_window(
        earnings_time_utc, session, now=now, window_hours=window_hours
    )

    confidence_adjustment = {"HIGH": 3.0, "MEDIUM": 1.0, "LOW": -3.0}.get(data_confidence, 0.0)
    score = 70.0 + min(gap_risk, 100) * 0.12 + min(abs(bias_score), 100) * 0.10 + coverage_score * 0.03
    score = round(_clamp(score + confidence_adjustment, 0.0, 95.0), 2)

    blockers: list[Dict[str, Any]] = []

    def _block(code: str, message: str, *, actual: Any = None, required: Any = None) -> None:
        blockers.append({
            "code": code,
            "message": message,
            "actual": actual,
            "required": required,
        })

    if not symbol:
        _block("ticker_missing", "Ticker is missing or invalid.")
    event_status = str(row.get("event_status") or "").upper().strip()
    flags = row.get("flags") if isinstance(row.get("flags"), dict) else {}
    if event_status == "REPORTED" or bool(flags.get("already_reported")):
        _block("earnings_already_reported", "The tracked earnings event is already reported.", actual=event_status or "REPORTED", required="UPCOMING")

    # Missing means a legacy/test fixture that predates the recommendation gate;
    # an explicit False is authoritative and must fail closed.
    if row.get("recommendation_eligible") is False:
        reasons = [
            str(value).strip()
            for value in (row.get("recommendation_blockers") or [])
            if str(value).strip()
        ]
        _block(
            "recommendation_ineligible",
            "Pre-Earnings recommendation is not eligible"
            + (f": {', '.join(reasons[:4])}." if reasons else "."),
            actual=reasons or False,
            required=True,
        )
    if abs(bias_score) < min_abs_bias:
        _block(
            "directional_edge_below_minimum",
            f"Directional bias {abs(bias_score):.1f} is below the {min_abs_bias:.1f} minimum.",
            actual=round(abs(bias_score), 4),
            required=min_abs_bias,
        )
    elif direction_label not in {"LONG", "SHORT"}:
        _block(
            "directional_direction_missing",
            "Directional bias clears the threshold, but no structured LONG/SHORT direction is available.",
            actual=direction_label or None,
            required="LONG_OR_SHORT",
        )
    elif canonical_required and directional_edge_passes and action is None:
        decision = row.get("pre_earnings_decision")
        if not isinstance(decision, dict) or not bool(decision.get("available")):
            _block(
                "canonical_decision_unavailable",
                "Directional edge passes, but the canonical Pre-Earnings decision is unavailable.",
                actual=(decision or {}).get("action") if isinstance(decision, dict) else None,
                required=("OPEN_LONG" if direction_label == "LONG" else "OPEN_SHORT"),
            )
        else:
            decision_action = str(decision.get("action") or "WATCH").upper().strip()
            required_action = "OPEN_LONG" if direction_label == "LONG" else "OPEN_SHORT"
            code = (
                "canonical_decision_not_open"
                if decision_action not in {"OPEN_LONG", "OPEN_SHORT"}
                else "canonical_direction_mismatch"
            )
            message = (
                "RiskManager entry thresholds pass, but the owner-enabled "
                f"canonical AI gate returned {decision_action or 'WATCH'} instead of an entry approval."
                if code == "canonical_decision_not_open"
                else "RiskManager entry thresholds pass, but the owner-enabled canonical AI gate "
                f"returned {decision_action} for a {direction_label} setup."
            )
            _block(
                code,
                message,
                actual=decision_action,
                required=required_action,
            )
    if require_directional_edge and directional_sample < min_directional_sample:
        _block(
            "directional_track_record_insufficient",
            "The selected pre-earnings direction has insufficient settled calibration history.",
            actual=directional_sample,
            required=min_directional_sample,
        )
    elif require_directional_edge and directional_hit_rate < 0:
        # -1.0 is the "no hit-rate recorded" sentinel, not a measurement: the
        # blocker used to read "hit-rate -1.0% is below 50.0%".
        _block(
            "directional_track_record_below_minimum",
            f"{row.get('direction_label') or 'The selected direction'} has no settled track record "
            f"(hit-rate unavailable); {min_directional_hit_rate:.1f}% is required.",
            actual=None,
            required=min_directional_hit_rate,
        )
    elif require_directional_edge and directional_hit_rate < min_directional_hit_rate:
        _block(
            "directional_track_record_below_minimum",
            f"Settled {row.get('direction_label') or 'direction'} hit-rate {directional_hit_rate:.1f}% is below {min_directional_hit_rate:.1f}%.",
            actual=round(directional_hit_rate, 4),
            required=min_directional_hit_rate,
        )
    if gap_risk < min_gap:
        _block(
            "gap_risk_below_minimum",
            f"Gap-risk {gap_risk} is below the autonomous-entry minimum {min_gap:g}.",
            actual=gap_risk,
            required=min_gap,
        )
    if coverage_score < min_coverage:
        _block(
            "coverage_below_minimum",
            f"Coverage {coverage_score} is below the autonomous-entry minimum {min_coverage:g}.",
            actual=coverage_score,
            required=min_coverage,
        )
    if score < min_score:
        _block(
            "candidate_score_below_minimum",
            f"Execution candidate score {score:.2f} is below the minimum {min_score:.2f}.",
            actual=score,
            required=min_score,
        )
    if require_price and resolved_price <= 0:
        _block("price_unavailable", "No valid execution price could be resolved.", actual=resolved_price, required="> 0")

    ranking_eligible = not blockers
    enforce_window = require_entry_window or bool(execution_cfg.get("pre_earnings_require_entry_window", False))
    if enforce_window and not window_open:
        _block(
            "outside_pre_earnings_window",
            window_reason,
            actual=window_meta,
            required=f"{window_hours:g}h_before_close_on_target_date",
        )

    return {
        "eligible": not blockers,
        "ranking_eligible": ranking_eligible,
        "executable_now": bool(ranking_eligible and window_open),
        "eligibility_scope": "execution" if enforce_window else "ranking",
        "stage": "AUTONOMOUS_PREFILTER",
        "symbol": symbol,
        "action": action,
        "direction": "LONG" if action == "BUY" else ("SHORT" if action == "SHORT" else None),
        "price": resolved_price if resolved_price > 0 else None,
        "score": score,
        "entry_window": window_meta,
        "window_open": window_open,
        "window_reason": window_reason,
        "metrics": {
            "gap_risk": gap_risk,
            "directional_bias_abs": round(abs(bias_score), 4),
            "coverage_score": coverage_score,
            "candidate_score": score,
            "window_open": window_open,
            "directional_sample": directional_sample,
            "directional_hit_rate": directional_hit_rate if directional_hit_rate >= 0 else None,
        },
        "thresholds": {
            "gap_risk": min_gap,
            "directional_bias_abs": min_abs_bias,
            "coverage_score": min_coverage,
            "candidate_score": min_score,
            "window_hours": window_hours,
            "directional_sample": min_directional_sample,
            "directional_hit_rate": min_directional_hit_rate,
        },
        "policies": {
            "canonical_ai_open_required": canonical_required,
            "directional_track_record_required": require_directional_edge,
        },
        "blockers": blockers,
        "first_blocker": blockers[0] if blockers else None,
    }


MARKET_DIP_ENTRY_STRATEGY = "market_dip"
MARKET_DIP_STRATEGY_TYPE = "SCANNER_AI_DIP_LONG"

# Research snapshot behind the "market_dip" crypto regime: a backtest of the
# entry rule, not live results. The two-year test found NO edge; the rule only
# looked good on the most recent 150 days. The regime stays available as an
# explicit paper experiment and is off unless configured.
# The target is 7.5% (1.25 x the stop), not the 6% first studied: every gate of the app
# requires a reward at least 1.2 x the risk, so a 6%/6% rule could never trade (on
# 2026-11-24, BTC -5%, all 35 candidates were turned down for that). On the same data the
# 7.5% target is no worse: profit factor 1.01 vs 0.99 over two years.
MARKET_DIP_BACKTEST = {
    "kind": "backtest",
    "as_of": "2026-11-24",
    "verdict": "no demonstrated edge",
    "rule": (
        "long a liquid USDT pair that is down 8-25% over 24h while BTC is down 2% or more "
        "over 24h; stop 6%, target 7.5%, exit after 24h"
    ),
    "costs": "4 bps commission per side, 2.5 bps entry slippage, 5 bps stop slippage",
    "two_years": {
        "data": "88 Binance USDT pairs, hourly bars, 2024-11-22..2026-11-22",
        "trades": 4269, "win_rate_pct": 49.1, "mean_net_pct": 0.015, "profit_factor": 1.01,
        "profitable_quarters": "5 of 9",
    },
    "last_150_days": {
        "data": "107 pairs, 2026-06-25..2026-11-22",
        "first_60pct": {"trades": 142, "win_rate_pct": 64.1, "profit_factor": 1.98},
        "last_40pct": {"trades": 475, "win_rate_pct": 61.1, "profit_factor": 1.51},
    },
    "note": (
        "Backtest of the entry rule, not live results. Over two years the rule is break-even "
        "after costs (profit factor 0.99, 5 of 9 quarters profitable); it was profitable only in "
        "the most recent 150 days, so treat it as unproven. Entries cluster on market-wide "
        "selloff days, so trades of one day win or lose together. The pair list is today's "
        "most-traded set (survivorship bias). No news catalyst is expected for this rule."
    ),
}


def market_dip_ai_is_advisory(signal: Any, exec_cfg: Any) -> bool:
    """True when the AI's rejection of a market-dip candidate is recorded but not obeyed.

    Only the dip rule itself (scanner source, "market_dip" regime) and only in paper
    mode, as the owner asked on 2026-11-24: a paper experiment has to trade to be
    measured, and the AI turned every dip down because the backtest is break-even.
    The rule's own budget stops it after 100 closed trades without an edge;
    ``scanner_dip_ai_advisory: false`` gives the AI its veto back.
    """
    if not isinstance(signal, dict):
        return False
    cfg = exec_cfg if isinstance(exec_cfg, dict) else {}
    if str(cfg.get("mode", "paper")).lower() != "paper" or not bool(cfg.get("scanner_dip_ai_advisory", True)):
        return False
    return (
        str(signal.get("strategy_type") or "").upper() == MARKET_DIP_STRATEGY_TYPE
        and str(signal.get("entry_strategy") or "").lower() == MARKET_DIP_ENTRY_STRATEGY
        and str(signal.get("autonomous_source") or "").lower() == "scanner"
    )


def market_btc_change_pct(scanner_state: Any) -> Optional[float]:
    """BTC's 24h change from the scanner's liquid crypto universe, or None."""
    rows = scanner_state.get("liquid_crypto_tickers") if isinstance(scanner_state, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or "").upper().replace("-", "").replace("/", "") != "BTCUSDT":
            continue
        try:
            value = float(row.get("change_pct"))
        except (TypeError, ValueError):
            return None
        return value if value == value and abs(value) != float("inf") else None
    return None


# Replay of this system's stock rule on 57 trading days of 5-minute bars, 393 liquid stocks,
# costs included (scripts/research/market_direction_study/fade_by_market_day.py, 2026-11-23).
MOVER_REPLAY_EVIDENCE: Dict[str, Any] = {
    "kind": "backtest",
    "as_of": "2026-11-23",
    "data": "393 liquid US stocks, 5-minute bars, 2026-09-01..2026-11-20; a stock 8-40% away from the previous close",
    "short_a_stock_that_is_up": {"all_days_pct": 0.05, "broad_up_days_pct": 0.03, "win_rate_pct": 52},
    "buy_a_stock_that_is_up": {"all_days_pct": -0.22, "broad_up_days_pct": -0.37},
    "buy_a_stock_that_is_down": {"all_days_pct": -0.51, "win_rate_pct": 41},
    "short_a_stock_that_is_down": {"all_days_pct": 0.28, "note": "not consistent from day to day"},
    "note": (
        "Mean net result per trade to the close, with the live stop and target. Two months only, and the "
        "index fell 9% in that window. Reading: a green market was not a reason to buy a stock that had "
        "already run, and buying a stock that had already fallen lost money on most trades."
    ),
}


def market_context(scanner_state: Any) -> Optional[Dict[str, Any]]:
    """How broad today's move is, from the scanner's own snapshot. None when there is nothing to read.

    The stock figures describe the scanner's list of movers and the watchlist,
    not the whole market: a list of gainers and losers is not a breadth index.
    """
    if not isinstance(scanner_state, dict):
        return None

    def changes(rows: Any, crypto: bool) -> List[float]:
        out: List[float] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            ticker = _scanner_symbol_to_ticker(row.get("symbol") or row.get("ticker"))
            if not ticker or is_crypto_ticker(ticker) != crypto:
                continue
            try:
                value = float(row.get("change_pct"))
            except (TypeError, ValueError):
                continue
            if value == value and abs(value) != float("inf"):
                out.append(value)
        return out

    def reading(share_up: float) -> str:
        return "broad up" if share_up >= 0.65 else ("broad down" if share_up <= 0.35 else "mixed")

    context: Dict[str, Any] = {}
    stocks = changes(scanner_state.get("hot_tickers"), False) + changes(scanner_state.get("watchlist_stock_tickers"), False)
    if len(stocks) >= 10:
        share = sum(1 for value in stocks if value > 0) / len(stocks)
        context["stocks"] = {
            "sample": "the scanner's movers and the watchlist, not the whole market",
            "count": len(stocks), "share_up": round(share, 2),
            "up_5pct_or_more": sum(1 for value in stocks if value >= 5.0),
            "down_5pct_or_more": sum(1 for value in stocks if value <= -5.0),
            "reading": reading(share),
        }
    coins = changes(scanner_state.get("liquid_crypto_tickers"), True)
    if len(coins) >= 10:
        share = sum(1 for value in coins if value > 0) / len(coins)
        context["crypto"] = {
            "sample": "the most traded USDT pairs", "count": len(coins), "share_up_24h": round(share, 2),
            "btc_change_24h_pct": market_btc_change_pct(scanner_state), "reading": reading(share),
        }
    if not context:
        return None
    context["as_of"] = str(scanner_state.get("as_of_utc") or "")
    context["replay_evidence"] = dict(MOVER_REPLAY_EVIDENCE)
    return context


def position_time_stop_minutes(position: Any) -> float:
    """Time stop a position carries from its entry regime, in minutes (0 = none)."""
    if not isinstance(position, dict):
        return 0.0
    context = position.get("decision_context") if isinstance(position.get("decision_context"), dict) else {}
    execution = context.get("execution_context") if isinstance(context.get("execution_context"), dict) else {}
    for raw in (position.get("time_stop_minutes"), context.get("time_stop_minutes"),
                execution.get("time_stop_minutes")):
        if raw is None or isinstance(raw, bool):
            continue
        try:
            minutes = float(raw)
        except (TypeError, ValueError):
            continue
        if minutes == minutes and 0 < minutes < float("inf"):
            return minutes
    return 0.0


def _market_dip_setup(item: Dict[str, Any], cfg: Dict[str, Any], change_pct: float) -> Optional[Dict[str, Any]]:
    """Parameters of a market-dip long, or None when the tested rule does not hold.

    Fail closed: without a BTC reading the market-wide condition cannot be
    checked, so there is no candidate.
    """
    try:
        btc_change = float(item.get("market_btc_change_pct"))
    except (TypeError, ValueError):
        return None
    if btc_change != btc_change:
        return None
    min_drop = float(cfg.get("scanner_dip_min_drop_pct", 8.0) or 8.0)
    max_drop = float(cfg.get("scanner_dip_max_drop_pct", 25.0) or 25.0)
    btc_max = float(cfg.get("scanner_dip_btc_max_change_pct", -2.0))
    if not (-max_drop < change_pct <= -min_drop) or btc_change > btc_max:
        return None
    return {
        "btc_change_pct": round(btc_change, 4),
        "stop_pct": float(cfg.get("scanner_dip_stop_pct", 0.06) or 0.06),
        "rr_ratio": float(cfg.get("scanner_dip_rr_ratio", 1.25) or 1.25),
        "time_stop_minutes": int(float(cfg.get("scanner_dip_time_stop_minutes", 1440) or 1440)),
    }


def build_scanner_candidate_signal(item: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert a Scanner hot ticker into a paper-trading signal candidate."""
    if not isinstance(item, dict):
        return None

    ticker = _scanner_symbol_to_ticker(item.get("symbol"))
    price = _safe_float(item.get("price") or item.get("lastPrice") or item.get("last_price"), 0.0)
    change_pct = _safe_float(item.get("change_pct") or item.get("priceChangePercent"), 0.0)
    volume = _safe_float(item.get("volume") or item.get("quoteVolume"), 0.0)
    min_abs_change = float(cfg.get("scanner_min_abs_change_pct", 5.0) or 5.0)
    min_volume = float(cfg.get("scanner_min_volume_24h", 0.0) or 0.0)

    if not ticker or price <= 0 or abs(change_pct) < min_abs_change:
        return None
    if min_volume > 0 and volume > 0 and volume < min_volume:
        return None

    crypto = is_crypto_ticker(ticker)
    # Stock scanner volume is reported in shares, while crypto volume is quote
    # currency. Convert only equities to a dollar proxy so a thin stock cannot
    # qualify merely because its share price is small.
    dollar_volume = volume if crypto else price * volume
    min_dollar_volume = float(cfg.get("scanner_min_dollar_volume", 0.0) or 0.0)
    if min_dollar_volume > 0 and (dollar_volume <= 0 or dollar_volume < min_dollar_volume):
        return None

    # Entry mode selects how we act on a detected mover:
    #   "momentum" (legacy) — chase the move (BUY spikes up / SHORT spikes down).
    #   "fade"              — mean-reversion: fade the extended move with a tight 1:1 target.
    #   "off"               — disable autonomous scanner entries entirely (stand aside).
    # Fade is an experimental post-news mean-reversion hypothesis. Its admission band and
    # stop are research parameters, not evidence of an edge. Autonomous execution is
    # separately gated by realized, strategy-specific Capital results and fresh news.
    #   "market_dip"        — crypto only: buy liquid coins that fell hard while the whole
    #                         market sold off (BTC down), fixed stop/target, 24h time stop.
    entry_mode = str(cfg.get("scanner_entry_mode", "fade")).lower().strip()
    # Crypto may run its own regime; "" keeps the general one.
    crypto_mode = str(cfg.get("scanner_crypto_entry_mode") or "").lower().strip()
    if crypto and crypto_mode:
        entry_mode = crypto_mode
    if entry_mode == "off":
        return None
    dip = None
    if entry_mode == MARKET_DIP_ENTRY_STRATEGY:
        dip = _market_dip_setup(item, cfg, change_pct) if crypto else None
        if dip is None:
            return None

    max_unconfirmed_change = float(cfg.get("scanner_max_unconfirmed_abs_change_pct", 12.0) or 12.0)
    require_late_confirmation = bool(cfg.get("scanner_require_late_momentum_confirmation", True))
    has_late_confirmation, late_confirmation_reasons = _has_late_momentum_confirmation(item, cfg)
    is_late_momentum = abs(change_pct) >= max_unconfirmed_change

    if dip is not None:
        action = "BUY"
        rr_ratio = dip["rr_ratio"]
    elif entry_mode == "fade":
        fade_min = float(cfg.get("scanner_fade_min_move_pct", 6.0) or 6.0)
        fade_max = float(cfg.get("scanner_fade_max_move_pct", 40.0) or 40.0)
        if abs(change_pct) < fade_min or abs(change_pct) > fade_max:
            return None  # only fade genuinely exhausted moves within the band
        action = "SHORT" if change_pct > 0 else "BUY"
        rr_ratio = float(cfg.get("scanner_fade_rr_ratio", 1.0) or 1.0)
    else:  # momentum (legacy)
        raw_max_momentum_move = cfg.get("scanner_momentum_max_move_pct")
        max_momentum_move = float(raw_max_momentum_move or 0.0)
        if max_momentum_move > 0 and abs(change_pct) > max_momentum_move:
            return None
        if is_late_momentum and require_late_confirmation and not has_late_confirmation:
            return None
        action = "BUY" if change_pct > 0 else "SHORT"
        rr_ratio = float(cfg.get("scanner_rr_ratio", 2.0) or 2.0)

    stop_pct = float(
        cfg.get("scanner_crypto_stop_pct" if crypto else "scanner_stop_pct", 0.03 if crypto else 0.025)
        or (0.03 if crypto else 0.025)
    )
    if entry_mode == "fade":
        # A fade can need room for the first adverse wiggle. This configured value is a
        # research parameter, not a promise of profitable performance.
        stop_pct = float(cfg.get("scanner_fade_stop_pct", 0.04) or 0.04)
    if dip is not None:
        stop_pct = dip["stop_pct"]
    min_score = float(cfg.get("scanner_min_score", 78.0) or 78.0)

    volume_boost = 0.0
    if min_volume > 0 and volume > 0:
        volume_boost = _clamp((volume / min_volume) - 1.0, 0.0, 3.0)
    if dip is not None:
        # Ranking heuristic only: deeper coin drop and deeper market selloff rank first.
        score = 80.0 + min(abs(change_pct), 25.0) * 0.4 + min(abs(dip["btc_change_pct"]), 8.0) * 0.5 + volume_boost
    elif entry_mode == "fade":
        # Bigger over-extension = better fade setup, so reward the move size (no late penalty).
        score = 74.0 + min(abs(change_pct), 25.0) * 1.0 + volume_boost
    else:
        late_penalty = 0.0 if has_late_confirmation else max(0.0, abs(change_pct) - 9.0) * 1.8
        score = 72.0 + min(abs(change_pct), 15.0) * 1.4 + volume_boost - late_penalty
    score = round(_clamp(score, 0.0, 95.0), 2)
    if score < min_score:
        return None

    level_data = _levels(price, action, stop_pct, rr_ratio)
    direction = "LONG" if action == "BUY" else "SHORT"
    source = str(item.get("source") or "scanner").strip()
    watchlist_member = bool(item.get("watchlist_member")) or source.lower() in {
        "watchlist",
        "yahoo_watchlist",
    }
    candidate_origin = "watchlist" if watchlist_member else "scanner"
    created_at = get_bucharest_time().isoformat()
    strategy_type = MARKET_DIP_STRATEGY_TYPE if dip is not None else f"SCANNER_AI_{direction}"
    if dip is not None:
        reasons = [
            f"Scanner {ticker}: down {change_pct:+.2f}% in 24h while BTC is {dip['btc_change_pct']:+.2f}% "
            "-> LONG (market-wide selloff rebound).",
            f"Entry {price:.4f}; stop {stop_pct * 100:.2f}%, target {stop_pct * rr_ratio * 100:.2f}%, "
            f"exit after {dip['time_stop_minutes'] / 60:.0f}h at the latest.",
            "Rule backtested on hourly Binance data (see regime_evidence); it expects no news catalyst.",
            "Ollama must confirm direction, leverage, and news/risk conflicts before execution.",
        ]
    elif entry_mode == "fade":
        reasons = [
            f"Scanner {ticker}: fading a {change_pct:+.2f}% over-extension -> {direction} (mean-reversion).",
            f"Entry {price:.4f}; stop {stop_pct * 100:.2f}% and tight R:R {rr_ratio:.2f} to capture the bounce.",
            "Ollama must confirm direction, leverage, and news/risk conflicts before execution.",
        ]
    else:
        reasons = [
            f"Scanner detected {ticker} {direction} momentum: {change_pct:.2f}% move.",
            f"Entry derived from scanner price {price:.4f}; stop {stop_pct * 100:.2f}% and R:R {rr_ratio:.2f}.",
            "Ollama must confirm direction, leverage, and news/risk conflicts before execution.",
        ]
    if volume > 0:
        reasons.append(f"Reported scanner volume: {volume:,.0f}.")
    if entry_mode != "fade" and is_late_momentum:
        reasons.append(
            "Late momentum confirmation: "
            + (", ".join(late_confirmation_reasons) if late_confirmation_reasons else "missing")
        )

    return {
        "id": _build_signal_id("scanner", ticker, action),
        "ticker": ticker,
        "action": action,
        "type": action,
        "status": "Open",
        "created_at": created_at,
        "source": "autonomous_monitor",
        "autonomous_source": "scanner",
        "autonomous_candidate_origin": candidate_origin,
        "strategy_type": strategy_type,
        "strategy_id": strategy_type,
        # This price-first candidate is an explicitly unvalidated experiment.
        # Carry that lifecycle fact into Risk so an owner policy that requires
        # APPROVED/CHAMPION returns a structured strategy-evidence veto instead
        # of the misleading "missing evidence" adapter error.
        "strategy_status": "RESEARCH",
        "strategy_evidence": {
            "lifecycle_status": "RESEARCH",
            "source": "autonomous_candidate_definition",
            "strategy_id": strategy_type,
        },
        # Tag the entry regime (fade|momentum) so live results can be measured per
        # strategy — the multi-day fade test must be separable from the legacy
        # momentum trades that share the SCANNER_AI_* strategy_type.
        "entry_strategy": entry_mode,
        "signal_mode": "AUTONOMOUS_SCANNER",
        "variant": "AUTO",
        # Separate the prospective price-first experiment from old V1 scanner
        # output. The tag is used by the research journal and never qualifies
        # a signal for Capital's V2 validation gate.
        "strategy_source": "SCANNER_PRICE_FIRST_V1",
        "entry_policy": "price_first_news_contextual",
        "asset_class": "CRYPTO_FUTURES" if crypto else "US_STOCKS",
        # Scanner instrument tag ("tokenized_stock" / "stablecoin" / None) and,
        # for a tokenised stock, the US ticker it tracks.
        "instrument_type": str(item.get("instrument_type") or "").strip().lower() or None,
        "underlying": str(item.get("underlying") or "").upper().strip() or None,
        "signal_score": score,
        "score": score,
        "raw_signal_score": score,
        # What `signal_score` is made of: for this candidate it is the size of
        # the 24 h move plus a volume term - a ranking heuristic, not a
        # measured (backtested / validated) setup quality.
        "signal_score_source": "move_size_heuristic",
        "probability_score": round(score / 100.0, 4),
        "quality_score": round(min(100.0, 55.0 + abs(change_pct) * 2.0), 2),
        # No validation (backtest / walk-forward) exists for this price-first
        # candidate. It used to carry a literal 55.0, which the AI reviewer and
        # the critic read as a measured score ("validation_score 55 vs
        # threshold 78") and the confidence gate read as "validated above 30".
        # The key is deliberately absent: every reader's missing-value default
        # (0) fails a validation gate instead of passing it.
        "validation_score_status": "unavailable",
        "signal_threshold": min_score,
        "gap_risk": 0,
        # Scanner normalisation exposes quote-currency 24h volume for crypto
        # and price * shares for equities.  That is the usable USD-liquidity
        # observation; keeping it only inside scanner_context previously made
        # Risk see ADV=0 and reject every candidate as liquidity-unverifiable.
        "average_daily_volume_usd": round(dollar_volume, 4),
        # News context for the AI decision layer (ollama_decision._compact_signal
        # whitelists news_score/news_sentiment/news_headlines/top_headlines). The
        # scanner item already exposes news_score/news_pulse_score (used by the
        # late-momentum gate) and top_news/latest_news -- attach them so the AI
        # reviewer sees news on the scanner autonomous path too (directive:
        # integrate AI via news analysis). Pre-fix this path carried zero news.
        "news_score": _safe_float(item.get("news_score") or item.get("news_pulse_score"), 0.0),
        "news_sentiment": str(
            item.get("news_sentiment_trend") or item.get("sentiment_trend") or "unknown"
        ).lower(),
        "news_headlines": _top_news_titles(item),
        "top_headlines": _top_news_titles(item),
        "scanner_context": {
            "symbol": item.get("symbol"),
            "source": source,
            "watchlist_member": watchlist_member,
            "candidate_origin": candidate_origin,
            "change_pct": round(change_pct, 4),
            "price": round(price, 8),
            "volume": round(volume, 4),
            "dollar_volume": round(dollar_volume, 4),
            "late_momentum": bool(is_late_momentum),
            "late_confirmation": late_confirmation_reasons,
            **({"btc_change_pct": dip["btc_change_pct"]} if dip is not None else {}),
        },
        "decision_reasons": reasons,
        "reasons": reasons,
        **level_data,
        # Regime-specific fields: the exit the rule was tested with, and the test itself.
        **({"time_stop_minutes": dip["time_stop_minutes"], "regime_evidence": dict(MARKET_DIP_BACKTEST)}
           if dip is not None else {}),
    }


def build_pre_earnings_candidate_signal(
    item: Dict[str, Any],
    price: float,
    cfg: Dict[str, Any],
    *,
    assessment: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Convert a Pre-Earnings opportunity into a paper-trading signal candidate."""
    if not isinstance(item, dict):
        return None

    assessment = dict(assessment) if isinstance(assessment, dict) else assess_pre_earnings_candidate(
        item,
        price,
        cfg,
        now=now,
    )
    if not assessment["eligible"]:
        return None

    symbol = str(assessment["symbol"])
    price = float(assessment["price"])
    action = str(assessment["action"])
    gap_risk = int(assessment["metrics"]["gap_risk"])
    coverage_score = int(assessment["metrics"]["coverage_score"])
    data_confidence = str(item.get("data_confidence") or "").upper().strip()

    direction = "LONG" if action == "BUY" else "SHORT"
    bias_score = _safe_float(item.get("direction_bias_score"), 0.0)
    expected_move = _safe_float(
        # Past earnings moves size the stop. `expected_move_pct` is only one
        # day's high-low range, so it is the last resort.
        item.get("historical_median_move_pct")
        or item.get("avg_move_historical")
        or item.get("expected_move_pct"),
        0.0,
    )
    min_stop = float(cfg.get("pre_earnings_stop_pct_min", 0.025) or 0.025)
    max_stop = float(cfg.get("pre_earnings_stop_pct_max", 0.08) or 0.08)
    stop_pct = _clamp((expected_move / 100.0) * 0.40 if expected_move > 0 else min_stop, min_stop, max_stop)
    rr_ratio = float(cfg.get("pre_earnings_rr_ratio", 1.8) or 1.8)
    min_score = float(assessment["thresholds"]["candidate_score"])
    score = float(assessment["score"])

    level_data = _levels(price, action, stop_pct, rr_ratio)
    created_at = get_bucharest_time().isoformat()
    headlines = _top_news_titles(item)
    average_volume_shares = _safe_float(
        item.get("avg_volume")
        or item.get("average_volume")
        or item.get("average_daily_volume"),
        0.0,
    )
    average_daily_volume_usd = price * average_volume_shares if average_volume_shares > 0 else 0.0
    reasons = [
        f"Pre-earnings AI candidate: {symbol} {direction}, GapRisk {gap_risk}, bias {bias_score:.1f}.",
        f"Coverage {coverage_score} and confidence {data_confidence or 'UNKNOWN'}; event {item.get('earnings_time_utc') or 'n/a'}.",
        f"Expected move proxy {expected_move:.2f}% sets stop {stop_pct * 100:.2f}% and R:R {rr_ratio:.2f}.",
        "Ollama must confirm news, event risk, direction, and leverage before execution.",
    ]
    if headlines:
        reasons.append(f"Top headline: {headlines[0]}")

    return {
        "id": _build_signal_id("preearnings", symbol, action),
        "ticker": symbol,
        "action": action,
        "type": action,
        "status": "Open",
        "created_at": created_at,
        "source": "autonomous_monitor",
        "autonomous_source": "pre_earnings",
        "strategy_type": f"PRE_EARNINGS_AI_{direction}",
        "strategy_id": f"PRE_EARNINGS_AI_{direction}",
        "strategy_status": "RESEARCH",
        "strategy_evidence": {
            "lifecycle_status": "RESEARCH",
            "source": "autonomous_candidate_definition",
            "strategy_id": f"PRE_EARNINGS_AI_{direction}",
        },
        "signal_mode": "AUTONOMOUS_PRE_EARNINGS",
        "variant": "AUTO",
        "strategy_source": "V1",
        "asset_class": "US_STOCKS",
        "signal_score": score,
        "score": score,
        "raw_signal_score": score,
        "probability_score": round(score / 100.0, 4),
        "quality_score": round(min(100.0, coverage_score), 2),
        # No validation score exists for a pre-earnings candidate either; see
        # the scanner builder above. Absent, not an invented 55.0.
        "validation_score_status": "unavailable",
        "signal_threshold": min_score,
        "gap_risk": gap_risk,
        "average_daily_volume_usd": round(average_daily_volume_usd, 4),
        "pre_earnings_gap_risk": gap_risk,
        "pre_earnings_entry_window": assessment.get("entry_window"),
        "pre_earnings_window_open": bool(assessment.get("window_open")),
        "direction_bias_score": round(bias_score, 4),
        "direction_label": item.get("direction_label"),
        "direction_track_record": dict(item.get("direction_track_record") or {}),
        "coverage_score": coverage_score,
        "expected_move_pct": expected_move,
        "gap_risk_level": item.get("gap_risk_level"),
        "news_headlines": headlines,
        # News pulse context for the AI decision layer. Pre-fix this path carried
        # headline text only (news_headlines) but not the numeric pulse score /
        # sentiment trend / top_headlines whitelist key -- so the AI reviewer had
        # partial news. Attach the full set (directive: integrate AI via news).
        # 2026-09-03 audit P1: pre-earnings items never expose news_score /
        # news_pulse_score / news_sentiment_trend, so the read below fabricated
        # `news_score: 0.0, news_sentiment: "unknown"` even for tickers with
        # rich news — feeding the §41 reviewer a false zero. Map the fields the
        # item actually carries (news_digest counts + signed news_ai bias).
        "news_score": (
            round(min(100.0, float((item.get("news_digest") or {}).get("article_count") or 0) * 10.0), 1)
            if isinstance(item.get("news_digest"), dict)
            and _safe_int((item.get("news_digest") or {}).get("article_count"), 0) > 0
            else _safe_float(item.get("news_score") or item.get("news_pulse_score"), 0.0)
        ),
        "news_sentiment": (
            _news_sentiment_from_bias((item.get("components") or {}).get("news_ai"))
            if isinstance(item.get("components"), dict) and item["components"].get("news_ai") is not None
            else str(
                item.get("news_sentiment_trend") or item.get("sentiment_trend") or "unknown"
            ).lower()
        ),
        # 2026-09-03 audit P2 (unused info): the canonical score was computed on
        # the item but never copied onto the signal, while the §44 critic and
        # the Decision Ledger recorder both read `pre_earnings_score` — it was
        # always empty on exactly the path it was meant for.
        "pre_earnings_score": _safe_float(item.get("pre_earnings_score"), None),
        "top_headlines": headlines,
        "entry_window": assessment.get("entry_window"),
        "pre_earnings_context": {
            "symbol": symbol,
            "earnings_time_utc": item.get("earnings_time_utc"),
            "session": item.get("session"),
            "gap_risk": gap_risk,
            "gap_risk_level": item.get("gap_risk_level"),
            "direction_bias_score": round(bias_score, 4),
            "direction_label": item.get("direction_label"),
            "direction_track_record": dict(item.get("direction_track_record") or {}),
            "canonical_decision": dict(item.get("pre_earnings_decision") or {}),
            "coverage_score": coverage_score,
            "coverage_label": item.get("coverage_label"),
            "data_confidence": data_confidence,
            "expected_move_pct": expected_move,
            "opportunity_score": item.get("opportunity_score"),
            "top_news": headlines,
            "entry_window": assessment.get("entry_window"),
        },
        "etoro_context": item.get("etoro_context"),
        "decision_reasons": reasons,
        "reasons": reasons,
        **level_data,
    }
