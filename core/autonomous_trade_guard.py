"""Evidence-based gates for autonomous paper-trade execution.

An autonomous opportunity is not executable until it has strategy-specific
realized evidence and the asset-appropriate context. Major crypto assets use
fresh News AI evidence; non-major crypto is assessed from live market and risk
controls instead. This module does not predict prices; it reports the evidence
present at decision time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional
import uuid


def _number(value: Any, default: float = 0.0) -> float:
    """float(value), or `default` for anything that is not a finite number.

    Every comparison with NaN is False, so a NaN metric used to slip through
    each "below the minimum" check of the evidence gates.
    """
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def _text(value: Any) -> str:
    return str(value or "").strip()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_autonomous_candidate(signal: Dict[str, Any]) -> bool:
    payload = signal or {}
    strategy_type = _text(payload.get("strategy_type")).upper()
    signal_mode = _text(payload.get("signal_mode")).upper()
    return bool(
        payload.get("_autonomous_candidate")
        or _text(payload.get("autonomous_source"))
        or signal_mode.startswith("AUTONOMOUS_")
        or strategy_type.startswith(("SCANNER_AI_", "PRE_EARNINGS_AI_"))
    )


def _direction(signal: Dict[str, Any]) -> str:
    raw = _text(signal.get("direction") or signal.get("action") or signal.get("type")).upper()
    return "LONG" if raw in {"BUY", "LONG"} else "SHORT" if raw in {"SELL", "SHORT"} else ""


def _crypto_base_asset(ticker: str) -> str:
    """Return the base asset from a normalized crypto pair without guessing stocks."""
    token = _text(ticker).upper().replace("_", "-").replace("/", "-").replace(" ", "")
    if not token:
        return ""
    if "-" in token:
        return token.split("-", 1)[0]
    for quote in ("USDT", "USDC", "BUSD", "FDUSD", "TUSD", "USD", "EUR"):
        if token.endswith(quote) and len(token) > len(quote):
            return token[: -len(quote)]
    return token


def _crypto_news_policy(signal: Dict[str, Any], gate_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Return the configured News AI policy for a signal's crypto asset class.

    Asset class is deliberately required for the non-major exception.  A ticker
    suffix such as ``-USD`` is also used by equities, so inferring crypto from
    the symbol would weaken the stock News gate.
    """
    payload = signal or {}
    asset_class = _text(payload.get("asset_class") or payload.get("market_type")).upper()
    is_crypto = "CRYPTO" in asset_class
    configured_majors = gate_cfg.get("major_crypto_bases_requiring_news")
    # An explicitly empty list means News is contextual for every crypto asset.
    # Keep the historical major-asset default only when the option is absent.
    raw_majors = configured_majors if isinstance(configured_majors, list) else ["BTC", "ETH", "SOL", "BNB", "XRP"]
    major_bases = {
        _text(value).upper()
        for value in raw_majors
        if _text(value)
    }
    base_asset = _crypto_base_asset(_text(payload.get("ticker"))) if is_crypto else ""
    requires_non_major_news = bool(gate_cfg.get("require_news_for_non_major_crypto", False))
    return {
        "asset_class": asset_class or "unclassified",
        "is_crypto": is_crypto,
        "base_asset": base_asset or None,
        "is_major_crypto": bool(is_crypto and base_asset in major_bases),
        "major_crypto_bases_requiring_news": sorted(major_bases),
        "require_news_for_non_major_crypto": requires_non_major_news,
        "news_required": bool(not is_crypto or base_asset in major_bases or requires_non_major_news),
    }


def _source_backed_ai_headline(article: Any) -> bool:
    if not isinstance(article, dict):
        return False
    has_source = all(
        _text(article.get(field))
        for field in ("title", "url", "source")
    ) and _text(article.get("time") or article.get("published_at"))
    if not has_source:
        return False
    for field in ("ai_micro_summary", "ai_deep_analysis"):
        analysis = article.get(field)
        if not isinstance(analysis, dict):
            continue
        sentiment = _text(
            analysis.get("overall_sentiment") or analysis.get("sentiment")
        ).lower()
        if sentiment in {"bullish", "bearish", "neutral"}:
            return True
    return False


def _entry_strategy(payload: Dict[str, Any]) -> str:
    context = payload.get("decision_context") if isinstance(payload, dict) else None
    context = context if isinstance(context, dict) else {}
    execution = context.get("execution_context") if isinstance(context.get("execution_context"), dict) else {}
    return _text(payload.get("entry_strategy") or context.get("entry_strategy") or execution.get("entry_strategy")).lower()


def _closed_at_sort_key(position: Dict[str, Any]) -> tuple[int, str]:
    raw = _text(position.get("closed_at"))
    if not raw:
        return (1, "")
    try:
        return (0, datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat())
    except ValueError:
        return (0, raw)


def _position_matches(position: Dict[str, Any], *, strategy_type: str, direction: str, entry_strategy: str) -> bool:
    if _text(position.get("status")).upper() != "CLOSED":
        return False
    if _text(position.get("strategy_type")).upper() != strategy_type:
        return False
    if direction and _text(position.get("direction")).upper() != direction:
        return False
    # Never use legacy/unlabelled trades to validate a new entry regime.
    return not entry_strategy or _entry_strategy(position) == entry_strategy


def _has_verified_execution_provenance(position: Dict[str, Any]) -> bool:
    """Only use realized P&L when both fills have audited, verified prices."""
    if not isinstance(position, dict):
        return False
    for prefix, price_field in (("entry", "entry_price"), ("exit", "exit_price")):
        try:
            if float(position.get(price_field, 0.0) or 0.0) <= 0:
                return False
        except (TypeError, ValueError):
            return False
        source = _text(position.get(f"{prefix}_price_source")).lower()
        if not source or source in {"fallback", "unavailable", "unknown"}:
            return False
        quality = position.get(f"{prefix}_price_quality")
        if not isinstance(quality, dict) or quality.get("is_valid") is False:
            return False
        level = _text(quality.get("level") or quality.get("quality")).upper()
        if level != "VERIFIED":
            return False
    return True


def evaluate_autonomous_performance(
    signal: Dict[str, Any],
    positions: Iterable[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Evaluate net Capital results for the exact autonomous setup."""
    cfg = dict(config or {})
    gate_cfg = cfg.get("autonomous_profitability_gate") if isinstance(cfg.get("autonomous_profitability_gate"), dict) else {}
    enabled = bool(gate_cfg.get("enabled", True))
    strategy_type = _text((signal or {}).get("strategy_type")).upper()
    direction = _direction(signal or {})
    entry_strategy = _entry_strategy(signal or {})
    min_sample = max(1, int(_number(gate_cfg.get("min_sample"), 30)))
    min_profit_factor = max(0.0, _number(gate_cfg.get("min_profit_factor"), 1.2))
    min_expectancy = _number(gate_cfg.get("min_expectancy_usd"), 0.0)
    max_drawdown_pct = max(0.0, _number(gate_cfg.get("max_drawdown_pct"), 25.0))
    base = {
        "gate": "autonomous_profitability",
        "enabled": enabled,
        "strategy_type": strategy_type,
        "direction": direction,
        "entry_strategy": entry_strategy or "unclassified",
        "scope": "closed Capital positions with the same strategy, direction, and entry regime",
        "thresholds": {
            "min_sample": min_sample,
            "min_profit_factor": min_profit_factor,
            "min_expectancy_usd": min_expectancy,
            "max_drawdown_pct": max_drawdown_pct,
        },
    }
    if not is_autonomous_candidate(signal):
        return {**base, "allowed": True, "status": "not_autonomous", "reason": "Manual/non-autonomous signal"}
    if not enabled:
        return {**base, "allowed": True, "status": "disabled", "reason": "Profitability gate disabled by configuration"}
    if not strategy_type or not direction:
        return {**base, "allowed": False, "status": "invalid_signal", "reason": "Missing strategy type or direction"}

    matching_candidates = [
        dict(position)
        for position in positions or []
        if isinstance(position, dict)
        and _position_matches(position, strategy_type=strategy_type, direction=direction, entry_strategy=entry_strategy)
    ]
    matching = [
        position for position in matching_candidates
        if _has_verified_execution_provenance(position)
    ]
    matching.sort(key=_closed_at_sort_key)
    pnl = [_number(position.get("net_pnl", position.get("pnl")), 0.0) for position in matching]
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = None if gross_loss == 0 and gross_profit > 0 else (gross_profit / gross_loss if gross_loss else 0.0)
    cumulative_pct = peak_pct = max_drawdown = 0.0
    for position in matching:
        cumulative_pct += _number(position.get("pnl_pct_notional"), 0.0)
        peak_pct = max(peak_pct, cumulative_pct)
        max_drawdown = max(max_drawdown, peak_pct - cumulative_pct)
    metrics = {
        "sample_size": len(matching),
        "matching_closed_positions": len(matching_candidates),
        "excluded_unverified_execution": max(0, len(matching_candidates) - len(matching)),
        "wins": len(wins),
        "losses": len(losses),
        "net_pnl_usd": round(sum(pnl), 6),
        "expectancy_usd": round(sum(pnl) / len(pnl), 6) if pnl else 0.0,
        "profit_factor": round(profit_factor, 6) if profit_factor is not None else None,
        "profit_factor_display": "infinite" if profit_factor is None else round(profit_factor, 6),
        "max_drawdown_pct": round(max_drawdown, 6),
    }
    if len(matching) < min_sample:
        return {
            **base,
            "allowed": False,
            "status": "insufficient_evidence",
            "reason": (
                f"Need {min_sample} matching closed trades with verified entry and exit prices; "
                f"only {len(matching)} qualify"
            ),
            "metrics": metrics,
        }
    if profit_factor is not None and profit_factor < min_profit_factor:
        return {**base, "allowed": False, "status": "negative_edge", "reason": f"Net profit factor {profit_factor:.2f} is below required {min_profit_factor:.2f}", "metrics": metrics}
    if metrics["expectancy_usd"] <= min_expectancy:
        return {**base, "allowed": False, "status": "negative_edge", "reason": f"Net expectancy ${metrics['expectancy_usd']:.2f} is not above ${min_expectancy:.2f}", "metrics": metrics}
    if metrics["max_drawdown_pct"] > max_drawdown_pct:
        return {**base, "allowed": False, "status": "drawdown_exceeded", "reason": f"Observed drawdown {metrics['max_drawdown_pct']:.2f}% exceeds {max_drawdown_pct:.2f}%", "metrics": metrics}
    return {**base, "allowed": True, "status": "validated", "reason": "Matching closed-trade history satisfies net profitability thresholds", "metrics": metrics}


def evaluate_autonomous_news(
    signal: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
    *,
    news_pulse: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Require fresh, source-backed news context and reject conflicts."""
    cfg = dict(config or {})
    gate_cfg = cfg.get("autonomous_news_gate") if isinstance(cfg.get("autonomous_news_gate"), dict) else {}
    enabled = bool(gate_cfg.get("enabled", True))
    required = bool(gate_cfg.get("require_fresh_context", True))
    min_articles = max(0, int(_number(gate_cfg.get("min_articles_24h"), 1)))
    require_source_urls = bool(gate_cfg.get("require_source_urls", True))
    require_ai_analysis = bool(gate_cfg.get("require_ai_analysis", True))
    min_ai_articles = max(0, int(_number(gate_cfg.get("min_ai_analyzed_articles_24h"), 1)))
    require_source_backed_ai = bool(
        gate_cfg.get(
            "require_source_backed_ai_analysis",
            require_source_urls and require_ai_analysis,
        )
    )
    min_source_backed_ai_articles = max(
        0,
        int(_number(
            gate_cfg.get("min_source_backed_ai_analyzed_articles_24h"),
            min_ai_articles,
        )),
    )
    ticker = _text((signal or {}).get("ticker")).upper()
    direction = _direction(signal or {})
    crypto_policy = _crypto_news_policy(signal or {}, gate_cfg)
    base = {
        "gate": "autonomous_news",
        "evaluated_at": _utc_now_iso(),
        "enabled": enabled,
        "ticker": ticker,
        "direction": direction,
        "required": required,
        "min_articles_24h": min_articles,
        "require_source_urls": require_source_urls,
        "require_ai_analysis": require_ai_analysis,
        "min_ai_analyzed_articles_24h": min_ai_articles,
        "require_source_backed_ai_analysis": require_source_backed_ai,
        "min_source_backed_ai_analyzed_articles_24h": min_source_backed_ai_articles,
        "crypto_policy": crypto_policy,
    }
    if not is_autonomous_candidate(signal):
        return {**base, "allowed": True, "status": "not_autonomous", "reason": "Manual/non-autonomous signal"}
    if not enabled:
        return {**base, "allowed": True, "status": "disabled", "reason": "News gate disabled by configuration"}
    # Price-first crypto still fetches and evaluates News whenever it is
    # available.  The contextual policy only prevents *missing* evidence from
    # vetoing a technical setup; a verified directional conflict remains a veto.
    contextual_crypto = bool(crypto_policy["is_crypto"] and not crypto_policy["news_required"])
    try:
        if news_pulse is None:
            from core.news_pulse import build_news_pulse
            news_pulse = build_news_pulse(ticker, ttl_seconds=max(5, int(_number(gate_cfg.get("ttl_seconds"), 45))), allow_blocking=bool(gate_cfg.get("allow_blocking_fetch", True)))
    except Exception as exc:
        if contextual_crypto:
            return {
                **base,
                "allowed": True,
                "status": "news_context_unavailable_price_first",
                "reason": "News context unavailable; crypto entry remains price-first and AI will retry during position management",
                "context": {
                    "source": "unavailable",
                    "asset_policy": "crypto_price_first_news_contextual",
                    "base_asset": crypto_policy["base_asset"],
                    "error": str(exc),
                    "score": 0,
                    "sentiment": "unknown",
                    "ai_sentiment": "unknown",
                    "article_count_24h": 0,
                    "article_count_7d": 0,
                    "source_backed_count_24h": 0,
                    "ai_analyzed_count_24h": 0,
                    "source_backed_ai_analyzed_count_24h": 0,
                    "top_headlines": [],
                },
            }
        return {**base, "allowed": False, "status": "unavailable", "reason": f"News context unavailable: {exc}", "context": {}}

    pulse = dict(news_pulse or {})
    window_24h = pulse.get("24h") if isinstance(pulse.get("24h"), dict) else {}
    window_7d = pulse.get("7d") if isinstance(pulse.get("7d"), dict) else {}
    headlines = pulse.get("top_headlines") if isinstance(pulse.get("top_headlines"), list) else []
    sentiment = _text(pulse.get("sentiment_trend")).lower() or "unknown"
    ai_sentiment = _text(pulse.get("ai_sentiment_trend")).lower() or "unknown"
    article_count = int(_number(window_24h.get("article_count"), 0))
    source_backed_count = int(_number(window_24h.get("source_backed_count"), 0))
    ai_analyzed_count = int(_number(window_24h.get("ai_analyzed_count"), 0))
    source_backed_ai_analyzed_count = int(
        _number(window_24h.get("source_backed_ai_analyzed_count"), 0)
    )
    source_backed_ai_headlines = [
        dict(item) for item in headlines if _source_backed_ai_headline(item)
    ][:3]
    evidence_headlines = (
        source_backed_ai_headlines
        if require_source_backed_ai
        else [dict(item) for item in headlines if isinstance(item, dict)][:3]
    )
    context = {
        "source": "news_pulse",
        "pulse_generated_at": _text(pulse.get("generated_at")),
        "score": int(_number(pulse.get("score"), 0)),
        "sentiment": sentiment,
        "ai_sentiment": ai_sentiment,
        "article_count_24h": article_count,
        "article_count_7d": int(_number(window_7d.get("article_count"), 0)),
        "source_backed_count_24h": source_backed_count,
        "ai_analyzed_count_24h": ai_analyzed_count,
        "source_backed_ai_analyzed_count_24h": source_backed_ai_analyzed_count,
        "top_headlines": evidence_headlines,
    }
    def _missing_context(status: str, reason: str) -> Dict[str, Any]:
        if contextual_crypto:
            contextual_context = dict(context)
            contextual_context["asset_policy"] = "crypto_price_first_news_contextual"
            contextual_context["missing_context_status"] = status
            return {
                **base,
                "allowed": True,
                "status": "news_context_missing_price_first",
                "reason": (
                    f"{reason}; crypto entry remains price-first. News/AI is retained "
                    "for ongoing position review and verified conflicts."
                ),
                "context": contextual_context,
            }
        return {**base, "allowed": False, "status": status, "reason": reason, "context": context}

    if required and article_count < min_articles:
        return _missing_context("insufficient_evidence", f"Need {min_articles} verified article(s) in the last 24h; found {article_count}")
    if required and require_source_urls and source_backed_count < min_articles:
        return _missing_context("unverifiable_sources", f"Need {min_articles} source-backed article(s) with URL and publisher; found {source_backed_count}")
    if required and require_ai_analysis and ai_analyzed_count < min_ai_articles:
        return _missing_context("ai_analysis_unavailable", f"Need {min_ai_articles} AI-analyzed article(s) in the last 24h; found {ai_analyzed_count}")
    if (
        required
        and require_source_backed_ai
        and source_backed_ai_analyzed_count < min_source_backed_ai_articles
    ):
        return _missing_context(
            "ai_analysis_unverifiable",
            (
                f"Need {min_source_backed_ai_articles} source-backed AI-analyzed "
                f"article(s) in the last 24h; found {source_backed_ai_analyzed_count}"
            ),
        )
    decision_sentiment = ai_sentiment if require_ai_analysis else sentiment
    conflicts = (direction == "LONG" and decision_sentiment == "bearish") or (direction == "SHORT" and decision_sentiment == "bullish")
    if bool(gate_cfg.get("block_conflicting_sentiment", True)) and conflicts:
        source_label = "AI news sentiment" if require_ai_analysis else "Fresh news sentiment"
        return {**base, "allowed": False, "status": "direction_conflict", "reason": f"{source_label} ({decision_sentiment}) conflicts with {direction} direction", "context": context}
    return {**base, "allowed": True, "status": "aligned" if decision_sentiment in {"bullish", "bearish"} else "neutral", "reason": "Fresh source-backed news context has no directional conflict", "context": context}


def attach_news_context(signal: Dict[str, Any], decision: Dict[str, Any]) -> None:
    """Attach only verified news fields for downstream AI and Capital audit."""
    context = decision.get("context") if isinstance(decision, dict) else None
    if not isinstance(context, dict):
        return
    signal["news_score"] = context.get("score", 0)
    signal["news_sentiment"] = context.get("sentiment", "unknown")
    signal["top_headlines"] = list(context.get("top_headlines") or [])
    signal["news_headlines"] = [_text(item.get("title")) for item in context.get("top_headlines") or [] if isinstance(item, dict) and _text(item.get("title"))]


def _news_evidence_headlines(decision: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Whitelist source fields before persisting a prospective News decision."""
    context = decision.get("context") if isinstance(decision, dict) else None
    headlines = context.get("top_headlines") if isinstance(context, dict) else []
    records: list[Dict[str, Any]] = []
    for article in headlines if isinstance(headlines, list) else []:
        if not isinstance(article, dict):
            continue
        records.append({
            "article_id": _text(article.get("article_id")),
            "title": _text(article.get("title")),
            "published_at": _text(article.get("time") or article.get("published_at")),
            "url": _text(article.get("url")),
            "source": _text(article.get("source")),
            "impact": _text(article.get("impact")),
            "impact_score": _number(article.get("impact_score")),
            "sentiment": _text(article.get("sentiment")),
            "sentiment_score": article.get("sentiment_score"),
            "sentiment_confidence": article.get("sentiment_confidence"),
            "ai_micro_summary": dict(article.get("ai_micro_summary")) if isinstance(article.get("ai_micro_summary"), dict) else None,
            "ai_deep_analysis": dict(article.get("ai_deep_analysis")) if isinstance(article.get("ai_deep_analysis"), dict) else None,
        })
    return records


def record_autonomous_news_evidence(
    signal: Dict[str, Any],
    decision: Dict[str, Any],
    *,
    evaluation_stage: str,
) -> Optional[Dict[str, Any]]:
    """Persist an uncapped, prospective snapshot of a News gate decision.

    The record intentionally contains only source-provided article metadata and
    previously generated AI analysis.  It is not historical News reconstruction
    and does not turn a gate result into a profitability claim.
    """
    if not is_autonomous_candidate(signal) or not isinstance(decision, dict):
        return None
    context = decision.get("context") if isinstance(decision.get("context"), dict) else {}
    record = {
        "event_id": f"news_{uuid.uuid4().hex}",
        "timestamp": _text(decision.get("evaluated_at")) or _utc_now_iso(),
        "event_type": "autonomous_news_evaluated",
        "status": _text(decision.get("status")) or "unknown",
        "ticker": _text(signal.get("ticker")).upper(),
        "signal_id": _text(signal.get("id")),
        "position_id": "",
        "strategy_type": _text(signal.get("strategy_type")).upper(),
        "summary": _text(decision.get("reason")),
        "payload": {
            "schema": "autonomous_news_evidence.v1",
            "prospective_only": True,
            "evaluation_stage": _text(evaluation_stage),
            "decision": {
                "allowed": bool(decision.get("allowed")),
                "status": _text(decision.get("status")),
                "reason": _text(decision.get("reason")),
                "direction": _text(decision.get("direction")),
                "thresholds": {
                    "min_articles_24h": decision.get("min_articles_24h"),
                    "require_source_urls": bool(decision.get("require_source_urls")),
                    "require_ai_analysis": bool(decision.get("require_ai_analysis")),
                    "min_ai_analyzed_articles_24h": decision.get("min_ai_analyzed_articles_24h"),
                    "require_source_backed_ai_analysis": bool(decision.get("require_source_backed_ai_analysis")),
                    "min_source_backed_ai_analyzed_articles_24h": decision.get("min_source_backed_ai_analyzed_articles_24h"),
                },
            },
            "news_context": {
                "source": _text(context.get("source")),
                "pulse_generated_at": _text(context.get("pulse_generated_at")),
                "score": _number(context.get("score")),
                "sentiment": _text(context.get("sentiment")),
                "ai_sentiment": _text(context.get("ai_sentiment")),
                "article_count_24h": int(_number(context.get("article_count_24h"))),
                "source_backed_count_24h": int(_number(context.get("source_backed_count_24h"))),
                "ai_analyzed_count_24h": int(_number(context.get("ai_analyzed_count_24h"))),
                "source_backed_ai_analyzed_count_24h": int(_number(context.get("source_backed_ai_analyzed_count_24h"))),
                "headlines": _news_evidence_headlines(decision),
            },
        },
    }
    try:
        from core.execution_archive import append_execution

        append_execution(record)
    except Exception:
        # Audit durability must not open a fail-open path or crash a trade path.
        return None
    return record
