# core/agent_manager.py

import logging
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Load the whole AI package now, on the importing thread, before this module
# starts its worker threads. `core.ai/__init__` imports every submodule, and the
# code below imports single submodules lazily from several threads
# (`core.ai.decision_engine` in the review path, `core.pre_earnings.decision`
# -> `core.ai.decision` in the autonomous monitor). Two threads doing that for
# the first time take the package lock and a submodule lock in opposite order:
# Python then raises `_DeadlockError` in one and `KeyError('core.ai')` in the
# other. That was the "pre-earnings autonomous load failed: 'core.ai'" warning
# at startup, and it cost the first AI review of the session as well.
import core.ai as _ai_package

# Referenced so static checkers do not flag (or "repair") the import away.
_AI_PACKAGE_PRELOADED = _ai_package.__name__

from agents.base_agent import BaseAgent
from agents.execution_agent import ExecutionAgent
from agents.risk_agent import RiskAgent, resolve_position_limits
from agents.scanner_agent import get_scanner_agent
from agents.sentiment_agent import SentimentAgent
from core.autonomous_candidates import (
    assess_pre_earnings_candidate,
    autonomous_instrument_exclusion,
    build_pre_earnings_candidate_signal,
    build_scanner_candidate_signal,
    infer_pre_earnings_action,
    instrument_exclusion_policy,
    instrument_tags_from_scanner_state,
    market_btc_change_pct,
    market_context,
)
from core.config import ConfigManager, is_crypto_ticker, normalize_market_ticker
from core.confidence_gates import ConfidenceGates
from core.delist_watcher import delist_short_config, get_delist_watcher
from core.strategy_lab.live import (
    SOURCE_NAME as STRATEGY_LAB_SOURCE,
    get_lab_baskets,
    is_lab_signal,
    lab_ai_is_advisory,
    lab_live_config,
)
from core.learning_mode.book import LearningBook
from core.learning_mode.settings import LEARNING_SOURCE, learning_config
from core.learning_mode.worker import LearningWorker, get_learning_worker
from core.xs_momentum_basket import (
    SOURCE_NAME as XS_MOMENTUM_SOURCE,
    get_xs_momentum_basket,
    is_xs_momentum_signal,
    xs_momentum_ai_is_advisory,
    xs_momentum_config,
)
from core.market_hours import MarketHoursError, assert_trade_allowed
from core.ollama_decision import evaluate_trade_decision_with_ollama, should_veto_trade
from core.autonomy import (
    kill_switch_active,
    mode_allows_live_orders,
    mode_allows_paper_orders,
    resolve_mode,
)
from core.price_integrity import validate_price_consistency
from core.ticker_learning import get_ticker_adjustment
from core.trade_audit import record_trade_event
from core.autonomous_trade_guard import is_autonomous_candidate, record_autonomous_news_evidence
from core.autonomous_control import (
    consume_autonomous_cycle_request,
    get_autonomous_allocation_state,
    get_autonomous_cycle_status,
    record_autonomous_allocation_state,
    record_autonomous_cycle_result,
)
from core.capital_entry_guard import (
    _paper_autonomous_experiment_source,
    capital_entry_requires_ai_review,
    evaluate_capital_entry,
    evaluate_paper_experiment_budget,
)
from core.trade_decision import build_signal_decision_context


logger = logging.getLogger("MarketEngine")


_AUTONOMOUS_ALLOCATION_BUCKETS = ("stocks", "crypto", "pre_earnings")


def _attach_learning_evidence(signal_data: dict) -> None:
    """Give the AI reviewer what the app has measured about itself and the market.

    ``ai_track_record``: how past candidates of this strategy did, approved or
    rejected, and whether stated confidence matched outcomes (core.ai.scorecard).
    ``positioning_evidence``: futures positioning of a crypto candidate, from the
    recorder's local database. Both are evidence only and read local files; a
    failure here must never break a decision.
    """
    try:
        from core.ai import scorecard as _scorecard

        record = _scorecard.track_record_for(signal_data)
        if record:
            signal_data["ai_track_record"] = record
    except Exception:
        pass
    try:
        ticker = str(signal_data.get("ticker") or "")
        if is_crypto_ticker(ticker):
            from core import positioning_evidence as _positioning

            evidence = _positioning.positioning_evidence(ticker)
            if evidence:
                signal_data["positioning_evidence"] = evidence
    except Exception:
        pass


def _candidate_allocation_bucket(signal: dict) -> str:
    """Classify one autonomous candidate into the owner's three allocation lanes."""
    payload = signal or {}
    source = str(payload.get("autonomous_source") or "").lower().strip()
    strategy = str(payload.get("strategy_type") or "").upper().strip()
    if source == "pre_earnings" or strategy.startswith("PRE_EARNINGS"):
        return "pre_earnings"
    ticker = str(payload.get("ticker") or payload.get("symbol") or "").upper().strip()
    asset_class = str(payload.get("asset_class") or "").upper().strip()
    if is_crypto_ticker(ticker) or "CRYPTO" in asset_class:
        return "crypto"
    return "stocks"


def _autonomous_allocation_policy(exec_cfg: dict) -> dict[str, Any]:
    raw = exec_cfg.get("autonomous_allocation") if isinstance(exec_cfg.get("autonomous_allocation"), dict) else {}
    configured = raw.get("buckets_pct") if isinstance(raw.get("buckets_pct"), dict) else {}
    weights: dict[str, float] = {}
    for bucket in _AUTONOMOUS_ALLOCATION_BUCKETS:
        try:
            weight = float(configured.get(bucket, 33.0) or 0.0)
        except (TypeError, ValueError):
            weight = 0.0
        weights[bucket] = max(0.0, weight)
    enabled = bool(raw.get("enabled", False)) and sum(weights.values()) > 0
    return {
        "enabled": enabled,
        "weights_pct": weights,
        "normalized_pct": {
            bucket: round((weight / sum(weights.values())) * 100.0, 4)
            if sum(weights.values()) > 0 else 0.0
            for bucket, weight in weights.items()
        },
        "fallback_when_empty": bool(raw.get("fallback_when_empty", True)),
    }


def _json_safe_ai_artifacts(eval_result: Any) -> tuple[Any, Any, Any, Any]:
    from core.decision_ledger.types import (
        critic_projection,
        decision_projection,
        validation_projection,
    )

    decision = decision_projection(getattr(eval_result, "decision", None))
    validation = validation_projection(getattr(eval_result, "validation", None))
    critic_value = getattr(eval_result, "critic", None)
    critic = critic_projection(critic_value) if critic_value is not None else None
    review = dict(getattr(eval_result, "review", {}) or {})
    if "decision" in review:
        review["decision"] = decision_projection(review["decision"])
    return decision, validation, critic, review


def _combine_independent_size_caps(
    confidence_factor: Any,
    review_factor: Any,
) -> float:
    """Combine independent sizing opinions without compounding punishment.

    Confidence and AI/Critic factors are both expressed as absolute fractions
    of the configured base allocation. Multiplying them applied the same setup
    uncertainty twice (for example 0.25 * 0.25 = 0.0625). The stricter cap is
    authoritative; a reviewer can still reduce a full-size setup, but cannot
    shrink an allocation that is already below its requested cap.
    """
    try:
        confidence = float(confidence_factor)
    except (TypeError, ValueError):
        confidence = 0.0
    try:
        review = float(review_factor)
    except (TypeError, ValueError):
        review = 1.0
    confidence = max(0.0, min(1.0, confidence))
    review = max(0.0, min(1.0, review))
    return round(min(confidence, review), 4)


def _is_scanner_signal(signal: dict) -> bool:
    payload = signal if isinstance(signal, dict) else {}
    source = str(payload.get("autonomous_source") or payload.get("source") or "").lower().strip()
    mode = str(payload.get("signal_mode") or "").upper().strip()
    strategy = str(payload.get("strategy_type") or payload.get("strategy_id") or "").upper().strip()
    return source == "scanner" or "SCANNER" in mode or strategy.startswith("SCANNER")


def _engine_process_role() -> bool:
    """True inside the engine subprocess (`MS_PROCESS_ROLE=engine`)."""
    return str(os.getenv("MS_PROCESS_ROLE", "") or "").strip().lower() == "engine"


def _attach_cached_news_context_for_ai(signal: dict, execution_cfg: dict) -> dict:
    """Fetch, persist, and attach ticker news before the AI review.

    Scanner candidates are intentionally admitted to the paper experiment after
    the data-integrity check, before the normal Capital news gate runs.  Their
    scanner rows do not necessarily contain News Agent fields.  A cache read by
    itself is insufficient for a newly discovered symbol, so Scanner explicitly
    asks the News Agent to hydrate its persistent bucket before the AI prompt is
    built.
    """
    payload = signal if isinstance(signal, dict) else {}
    scanner_signal = _is_scanner_signal(payload)
    if not scanner_signal and not is_autonomous_candidate(payload):
        return payload

    existing_headlines = payload.get("news_headlines") or payload.get("top_headlines") or []
    existing_sentiment = str(payload.get("news_sentiment") or "").lower().strip()
    existing_context = payload.get("news_context") if isinstance(payload.get("news_context"), dict) else {}
    existing_hydration = (
        existing_context.get("hydration")
        if isinstance(existing_context.get("hydration"), dict)
        else {}
    )
    # Titles-only headlines (e.g. the pre-earnings candidate builder carries
    # bare titles + a sentiment trend) are NOT "news already attached": the
    # §41 reviewer needs source/timestamp/summary metadata to weigh outlets.
    # Only a rich headline set (dicts with source/time/url) counts as present.
    rich_headlines_present = any(
        isinstance(item, dict)
        and (str(item.get("source") or "").strip() or str(item.get("time") or "").strip() or str(item.get("url") or "").strip())
        for item in (payload.get("top_headlines") or [])
        if isinstance(item, dict)
    )
    scanner_already_hydrated = bool(
        scanner_signal
        and existing_hydration.get("requested")
        and str(existing_context.get("source") or "") == "news_pulse"
        and not bool(existing_context.get("stale", False))
    )
    if scanner_already_hydrated or (
        not scanner_signal
        and rich_headlines_present
        and existing_headlines
        and existing_sentiment in {"bullish", "bearish", "neutral"}
    ):
        return payload

    ticker = str(payload.get("ticker") or payload.get("symbol") or "").upper().strip()
    if not ticker:
        return payload

    news_cfg = (
        execution_cfg.get("autonomous_news_gate")
        if isinstance(execution_cfg, dict)
        and isinstance(execution_cfg.get("autonomous_news_gate"), dict)
        else {}
    )
    try:
        ttl_seconds = max(5, int(float(news_cfg.get("ttl_seconds", 45) or 45)))
    except (TypeError, ValueError):
        ttl_seconds = 45
    allow_blocking = bool(news_cfg.get("allow_blocking_fetch", True))
    try:
        max_items = max(3, min(20, int(news_cfg.get("scanner_news_max_items", 5) or 5)))
    except (TypeError, ValueError):
        max_items = 5

    # The API process owns news refresh (network fetch, FinBERT sentiment
    # model, news backup file). In the engine subprocess a per-ticker refresh
    # loaded the sentiment model a second time and wrote the same backup from
    # a second process, so the engine reads what is already cached and never
    # asks for a remote refresh.
    engine_cached_only = _engine_process_role()
    try:
        if scanner_signal:
            from core.news_pulse import hydrate_news_pulse

            pulse = hydrate_news_pulse(
                ticker,
                ttl_seconds=ttl_seconds,
                max_items=max(40, max_items),
                allow_remote=(
                    bool(news_cfg.get("scanner_auto_fetch", True))
                    and allow_blocking
                    and not engine_cached_only
                ),
                force_refresh=(
                    bool(news_cfg.get("scanner_force_refresh_on_signal", False))
                    and not engine_cached_only
                ),
            )
            if engine_cached_only and isinstance(pulse, dict):
                receipt = pulse.get("hydration")
                if isinstance(receipt, dict):
                    receipt["cached_only"] = True
                    receipt["cached_only_reason"] = "engine_process_reads_cached_news_only"
        else:
            from core.news_pulse import build_news_pulse

            pulse = build_news_pulse(
                ticker,
                ttl_seconds=ttl_seconds,
                max_items=max(40, max_items),
                allow_blocking=allow_blocking,
            )
    except Exception as exc:
        logger.warning("AI news hydration failed for %s: %s", ticker, exc)
        payload["news_context"] = {
            "status": "unavailable",
            "source": "news_pulse",
            "error": str(exc),
        }
        return payload

    pulse = pulse if isinstance(pulse, dict) else {}
    top_headlines = [
        dict(item)
        for item in (pulse.get("top_headlines") or [])
        if isinstance(item, dict) and str(item.get("title") or "").strip()
    ][:max_items]
    # Compact titles keep the source + age metadata (fix #2, 2026-09-02):
    # the §41 reviewer must be able to weigh the outlet, not just the text.
    try:
        from core.autonomous_candidates import _headline_with_meta

        headline_titles = [
            _headline_with_meta(item, str(item.get("title") or "").strip())
            for item in top_headlines
        ]
    except Exception:
        headline_titles = [str(item.get("title") or "").strip()[:220] for item in top_headlines]
    if top_headlines:
        payload["top_headlines"] = top_headlines
        payload["news_headlines"] = headline_titles
        payload["latest_news"] = top_headlines

    ai_sentiment = str(pulse.get("ai_sentiment_trend") or "").lower().strip()
    measured_sentiment = str(pulse.get("sentiment_trend") or "").lower().strip()
    sentiment = (
        ai_sentiment
        if ai_sentiment in {"bullish", "bearish", "neutral"}
        else measured_sentiment
    )
    if sentiment in {"bullish", "bearish", "neutral"}:
        payload["news_sentiment"] = sentiment
    try:
        payload["news_score"] = round(float(pulse.get("score", 0) or 0), 2)
    except (TypeError, ValueError):
        payload["news_score"] = 0.0

    window_24h = pulse.get("24h") if isinstance(pulse.get("24h"), dict) else {}
    window_7d = pulse.get("7d") if isinstance(pulse.get("7d"), dict) else {}
    hydration = pulse.get("hydration") if isinstance(pulse.get("hydration"), dict) else {}
    payload["news_context"] = {
        "status": "available" if top_headlines else "empty",
        "source": "news_pulse",
        "generated_at": pulse.get("generated_at"),
        "stale": bool(pulse.get("stale", False)),
        "article_count_24h": int(window_24h.get("article_count", 0) or 0),
        "article_count_7d": int(window_7d.get("article_count", 0) or 0),
        "source_backed_count_24h": int(window_24h.get("source_backed_count", 0) or 0),
        "ai_analyzed_count_24h": int(window_24h.get("ai_analyzed_count", 0) or 0),
        "source_backed_ai_analyzed_count_24h": int(
            window_24h.get("source_backed_ai_analyzed_count", 0) or 0
        ),
        "sentiment": payload.get("news_sentiment", "unknown"),
        "score": payload.get("news_score", 0.0),
        "top_headlines": top_headlines,
        "hydration": dict(hydration),
    }
    if scanner_signal:
        logger.info(
            "Scanner news hydrated for %s: status=%s fetched=%s persisted=%s articles_24h=%s",
            ticker,
            hydration.get("status", "unknown"),
            bool(hydration.get("fetched", False)),
            bool(hydration.get("persisted", False)),
            payload["news_context"]["article_count_24h"],
        )
    return payload


def _attach_etoro_context_for_ai(signal: dict) -> dict:
    """Attach eToro crowd positioning, sentiment & rates to signal before AI review."""
    if not isinstance(signal, dict):
        return signal
    if signal.get("etoro_context"):
        return signal
    ticker = str(signal.get("ticker") or signal.get("symbol") or "").upper().strip()
    if not ticker:
        return signal
    try:
        from core.etoro_client import get_etoro_client
        client = get_etoro_client()
        if not client.is_available():
            return signal
        meta = client.search_instrument(ticker)
        if not meta:
            return signal
        iid = meta.get("instrument_id")
        rates = client.get_live_rates([iid]) if iid else {}
        rate_info = rates.get(iid, {}) if iid else {}

        feed = client.get_feed_for_instrument(iid, limit=2) if iid else []
        headlines = [p.get("text") for p in feed if p.get("text")]

        signal["etoro_context"] = {
            "instrument_id": iid,
            "holders": meta.get("holders"),
            "popularity_rank": meta.get("popularity_rank"),
            "buy_pct": meta.get("buy_pct"),
            "sell_pct": meta.get("sell_pct"),
            "pe_ratio": meta.get("pe_ratio"),
            "esg_score": meta.get("esg_score"),
            "bid": rate_info.get("bid"),
            "ask": rate_info.get("ask"),
            "spread": rate_info.get("spread"),
            "social_headlines": headlines[:2],
        }
    except Exception as exc:
        logger.debug("Failed to attach eToro context for %s: %s", ticker, exc)
    return signal


def _radar_hard_blockers(candidate: dict) -> list[str]:
    """Blocking readiness codes of a radar candidate (evidence notes excluded)."""
    readiness = candidate.get("readiness") if isinstance(candidate.get("readiness"), dict) else {}
    notes = {str(n).lower() for n in (readiness.get("non_blocking_notes") or [])} | {"backtest_thin_sample"}
    hard: list[str] = []
    for raw in readiness.get("blockers") or []:
        code = str((raw.get("code") if isinstance(raw, dict) else raw) or "").strip().lower()
        if code and code not in notes:
            hard.append(code)
    return hard


def _trade_radar_opportunity(candidates: Any, ticker: str) -> tuple[float, str]:
    """(opportunity 0..100, reason) for `ticker` from the Trade Radar candidates.

    A candidate the radar has listed but not scored yet (`analysis_pending`)
    carries only a placeholder analysis (NEUTRAL / confidence 0). That is "no
    radar opinion", so it is reported like a ticker the radar does not list:
    reading the placeholder as a scored opportunity of 0 made the confidence
    gate treat it as a radar rejection.
    """
    symbol = str(ticker or "").upper().strip()
    for candidate in candidates if isinstance(candidates, (list, tuple)) else []:
        if not isinstance(candidate, dict) or str(candidate.get("symbol", "")).upper().strip() != symbol:
            continue
        if candidate.get("analysis_pending"):
            return 0.0, "ticker_not_in_trade_radar (analysis_pending)"
        analysis = candidate.get("analysis") if isinstance(candidate.get("analysis"), dict) else {}
        try:
            opportunity = float(analysis.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            opportunity = 0.0
        if opportunity != opportunity or opportunity in (float("inf"), float("-inf")):
            opportunity = 0.0
        readiness = candidate.get("readiness") if isinstance(candidate.get("readiness"), dict) else {}
        # A candidate the radar itself blocks (negative backtest, missing
        # price history, ...) is a radar rejection: its confidence number must
        # not count as an opportunity towards an entry. Evidence notes such as
        # backtest_thin_sample are listed with the blockers but do not block.
        hard_blockers = _radar_hard_blockers(candidate)
        if hard_blockers:
            return 0.0, "trade_radar_blocked=" + ",".join(hard_blockers[:4])
        if readiness.get("can_open"):
            opportunity = max(opportunity, 60.0)
        return opportunity, f"trade_radar_opportunity={opportunity:.1f}"
    # The radar lists crypto as exchange pairs ("SHELLUSDT") while signals use
    # market tickers ("SHELL-USD"), so the exact match above never finds them.
    # A block must still reach the entry path. Only the block is taken from
    # this looser match: an unblocked pair keeps today's "not in radar" result,
    # so no candidate gains radar confirmation it did not have before.
    try:
        from core.autonomous_candidates import _scanner_symbol_to_ticker

        wanted = _scanner_symbol_to_ticker(symbol)
        for candidate in candidates if isinstance(candidates, (list, tuple)) else []:
            if not isinstance(candidate, dict) or candidate.get("analysis_pending"):
                continue
            if not wanted or _scanner_symbol_to_ticker(candidate.get("symbol")) != wanted:
                continue
            hard_blockers = _radar_hard_blockers(candidate)
            if hard_blockers:
                return 0.0, "trade_radar_blocked=" + ",".join(hard_blockers[:4])
    except Exception:
        logger.debug("normalised trade radar lookup failed", exc_info=True)
    return 0.0, "ticker_not_in_trade_radar"


def _pre_earnings_context_of(signal: dict) -> dict:
    pe = signal.get("pre_earnings_context")
    if isinstance(pe, dict):
        return pe
    dc = signal.get("decision_context") if isinstance(signal.get("decision_context"), dict) else {}
    autonomous = dc.get("autonomous") if isinstance(dc.get("autonomous"), dict) else {}
    pe = autonomous.get("pre_earnings_context")
    return pe if isinstance(pe, dict) else {}


def _release_confidence_reservation(gates: Any, signal_data: dict, confidence_result) -> None:
    """2026-09-03 audit P1: ConfidenceGates.evaluate() optimistically reserves a
    daily-trade slot (max_trades_per_day). Every rejection path that runs after
    the evaluation must release it, or a handful of AI/risk rejections would
    silence the bot until midnight UTC."""
    if gates is not None and confidence_result is not None:
        try:
            gates.release_trade_reservation(signal_data)
        except Exception:
            logger.debug("confidence reservation release failed", exc_info=True)


def _debate_should_run(signal: dict, review: Any, execution_cfg: dict) -> tuple[bool, str]:
    """Config-gated debate policy wrapper (core.ai.debate.should_debate)."""
    try:
        from core.ai.debate import should_debate

        return should_debate(signal, review if isinstance(review, dict) else {}, execution_cfg)
    except Exception:
        return False, "error"


def _pre_earnings_entry_gate(signal: dict, review: Any, execution_cfg: dict) -> str:
    """History-driven entry discipline for the binary earnings event (P1).

    Closed-trade history showed accepted pre-earnings entries at ~18% win rate
    with gap-through-stop losses, while approvals carried 0.42-0.55 confidence
    against the model's own explicit gap warnings. Two fail-closed rules:
      - gap_risk >= block level (default 60, high): no new pre-earnings entry;
      - AI confidence < min (default 0.6): no new pre-earnings entry.
    Only applies to executable approvals; watch/avoid are already blocked by
    the AI entry decision, and unavailability stays governed by the separate
    AI review availability policy (fail-closed there as well)."""
    strategy = str((signal or {}).get("strategy_type") or "").upper()
    if not strategy.startswith("PRE_EARNINGS"):
        return ""
    review = review if isinstance(review, dict) else {}
    if not review.get("available"):
        return ""
    if str(review.get("recommendation") or "").lower() not in {"approve", "reduce"}:
        return ""
    try:
        min_confidence = float(
            execution_cfg.get("pre_earnings_min_ai_confidence", 0.6)
        )
    except (TypeError, ValueError):
        min_confidence = 0.6
    try:
        block_level = float(
            execution_cfg.get("pre_earnings_gap_risk_block_level", 60)
        )
    except (TypeError, ValueError):
        block_level = 60.0
    try:
        confidence = float(review.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    pe = _pre_earnings_context_of(signal)
    try:
        gap_risk = float(pe.get("gap_risk") or 0.0)
    except (TypeError, ValueError):
        gap_risk = 0.0
    if gap_risk >= block_level > 0:
        return "pre_earnings_gap_risk_high"
    if confidence < min_confidence:
        return "pre_earnings_low_ai_confidence"
    return ""


DEFAULT_MIN_ENTRY_CONFIDENCE = 0.5


def _min_entry_confidence(root_config: Any) -> float:
    """`ai.decision.min_entry_confidence` on 0..1 (default 0.5; 0 disables)."""
    ai = root_config.get("ai") if isinstance(root_config, dict) else None
    decision = ai.get("decision") if isinstance(ai, dict) else None
    raw = decision.get("min_entry_confidence", DEFAULT_MIN_ENTRY_CONFIDENCE) if isinstance(decision, dict) else DEFAULT_MIN_ENTRY_CONFIDENCE
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = DEFAULT_MIN_ENTRY_CONFIDENCE
    if value != value:
        value = DEFAULT_MIN_ENTRY_CONFIDENCE
    return max(0.0, min(1.0, value))


def _ai_min_entry_confidence_gate(signal: dict, review: Any, root_config: Any) -> tuple[str, str]:
    """(reason, detail) when an approved non-pre-earnings entry is under-confident.

    Live: an entry was accepted at AI confidence 0.35. Pre-earnings entries
    keep their own stricter rule (`pre_earnings_min_ai_confidence`). Only an
    available review that approves/reduces is checked: watch/avoid are already
    blocked, and AI unavailability is governed by its own policy.
    """
    strategy = str((signal or {}).get("strategy_type") or "").upper()
    if strategy.startswith("PRE_EARNINGS"):
        return "", ""
    review = review if isinstance(review, dict) else {}
    if not review.get("available"):
        return "", ""
    if str(review.get("recommendation") or "").lower() not in {"approve", "reduce"}:
        return "", ""
    minimum = _min_entry_confidence(root_config)
    if minimum <= 0:
        return "", ""
    try:
        confidence = float(review.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence != confidence:
        confidence = 0.0
    if confidence < minimum:
        return (
            "ai_low_entry_confidence",
            f"AI confidence {confidence:.2f} is below the minimum {minimum:.2f} for an entry "
            "(ai.decision.min_entry_confidence)",
        )
    return "", ""


def _pre_earnings_gap_size_factor(signal: dict, execution_cfg: dict) -> float:
    """Size discipline vs the earnings gap: medium gap risk halves the size
    (stop cannot protect an overnight gap; sizing must reflect that), high gap
    risk is already blocked from entering. Returns a multiplier in [0, 1]."""
    strategy = str((signal or {}).get("strategy_type") or "").upper()
    if not strategy.startswith("PRE_EARNINGS"):
        return 1.0
    try:
        medium_factor = float(
            execution_cfg.get("pre_earnings_gap_risk_medium_size_factor", 0.5)
        )
    except (TypeError, ValueError):
        medium_factor = 0.5
    medium_factor = max(0.0, min(1.0, medium_factor))
    pe = _pre_earnings_context_of(signal)
    try:
        gap_risk = float(pe.get("gap_risk") or 0.0)
    except (TypeError, ValueError):
        return 1.0
    if 40.0 <= gap_risk < 60.0:
        return medium_factor
    return 1.0


def _ai_entry_block(eval_result: Any, review: Any) -> tuple[bool, str]:
    """Return the authoritative AI/critic block independently of confidence.

    An effective critic veto and a validated NO_TRADE/WATCH decision are final
    entry decisions. The legacy confidence threshold remains relevant only for
    legacy review payloads that have no structured EvaluationResult.
    """
    critic = getattr(eval_result, "critic", None) if eval_result is not None else None
    if bool(getattr(critic, "veto", False)) or bool((review or {}).get("critic_veto")):
        return True, "critic_veto"

    if eval_result is not None and bool(getattr(eval_result, "ai_available", False)):
        validation = getattr(eval_result, "validation", None)
        if validation is not None and not bool(getattr(validation, "ok", False)):
            stage = getattr(validation, "stage", None)
            stage_value = str(getattr(stage, "value", stage) or "validation").strip().lower()
            return True, f"ai_validation_{stage_value}"
        decision = getattr(eval_result, "decision", None)
        action = getattr(decision, "action", None)
        action_value = str(getattr(action, "value", action) or "").upper()
        if action_value in {"NO_TRADE", "WATCH"}:
            return True, f"ai_{action_value.lower()}"

    if eval_result is None and should_veto_trade(review if isinstance(review, dict) else {}, {}):
        return True, "legacy_ai_veto"
    return False, ""


def _ai_block_is_advisory(signal: Any, execution_cfg: Any) -> bool:
    """True when an AI rejection is recorded but does not stop the entry.

    Only rule experiments, and only in paper mode: the rule baskets (the weekly
    momentum basket, Strategy Lab rules), measured as a basket, where a veto on
    single members would change what is being measured, and the crypto
    market-dip rule (owner, 2026-11-24: a paper experiment has to trade). Every
    other candidate keeps the AI veto.
    """
    from core.autonomous_candidates import market_dip_ai_is_advisory
    from core.learning_mode.settings import learning_ai_is_advisory

    return (
        xs_momentum_ai_is_advisory(signal, execution_cfg)
        or lab_ai_is_advisory(signal, execution_cfg)
        or market_dip_ai_is_advisory(signal, execution_cfg)
        # Learning mode: Opus chose the trade itself; the review is a second opinion on record.
        or learning_ai_is_advisory(signal, execution_cfg)
    )


# Blocks that arrive through the AI pipeline but are not the AI's opinion: the second Risk Agent
# check (daily loss, exposure, positions, Risk Constitution) and the Capital entry gate (stale
# signal, unverified price). They stay binding for every candidate, rule baskets included.
_NON_AI_BLOCKS = ("ai_validation_policy", "ai_validation_authoritative_data_check")


def _advisory_override_applies(blocked: bool, block_reason: str, signal: Any, execution_cfg: Any) -> bool:
    """True when an AI-pipeline block of this candidate is recorded but does not stop the entry.

    Only an AI opinion (the reviewer's NO_TRADE / WATCH, low confidence, a critic veto) on a rule-basket
    member in paper mode. A Risk Agent or Capital-gate rejection that arrives through the same pipeline
    is not an opinion and stops the entry like for every other candidate.
    """
    return bool(blocked) and block_reason not in _NON_AI_BLOCKS and _ai_block_is_advisory(signal, execution_cfg)


def _learning_priority(signal: Any) -> int:
    """1 for a learning-mode trade Opus chose: it waits at most 10 minutes for the single review slot,
    while rule candidates come back every cycle, so it is reviewed first."""
    from core.learning_mode.settings import is_learning_signal

    return 1 if is_learning_signal(signal) else 0


def _is_learning(signal: Any) -> bool:
    from core.learning_mode.settings import is_learning_signal

    return is_learning_signal(signal)


def _is_rule_basket_in_paper(signal: Any, execution_cfg: Any) -> bool:
    """A member of a rule basket (weekly momentum basket, Strategy Lab rule) while the bot trades on paper."""
    mode = str((execution_cfg or {}).get("mode", "paper") if isinstance(execution_cfg, dict) else "paper").lower()
    return mode == "paper" and (is_lab_signal(signal) or is_xs_momentum_signal(signal))


def _ai_review_availability_policy(
    signal: dict,
    execution_cfg: dict,
    ollama_cfg: dict,
    review: Any,
) -> dict[str, bool]:
    """Resolve AI availability before the immutable decision ledger is written."""
    capital_required = capital_entry_requires_ai_review(execution_cfg)
    review_required = (
        bool(execution_cfg.get("require_ai_review", True))
        or capital_required
        or (
            bool((signal or {}).get("_autonomous_candidate"))
            and bool(execution_cfg.get("autonomous_require_ai_review", True))
        )
    )
    review_enabled = bool(ollama_cfg.get("decision_enabled", True)) if isinstance(ollama_cfg, dict) else True
    gate_active = review_required and (review_enabled or capital_required)
    available = isinstance(review, dict) and bool(review.get("available"))
    fallback_default = str(execution_cfg.get("mode", "paper")).lower() == "paper"
    fallback_allowed = (
        gate_active
        and not capital_required
        and bool(execution_cfg.get("allow_signal_only_ai_fallback", fallback_default))
    )
    return {
        "capital_required": capital_required,
        "review_required": review_required,
        "review_enabled": review_enabled,
        "available": available,
        "fallback": bool(gate_active and not available and fallback_allowed),
        "blocked": bool(gate_active and not available and not fallback_allowed),
    }


class AgentManager:
    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager
        self.agents: Dict[str, BaseAgent] = {}
        self.lock = threading.Lock()
        self.bot_enabled = False
        self.confidence_gates = ConfidenceGates(config_manager)
        self._reconcile_stop = threading.Event()
        self._reconcile_thread = None
        self._reconcile_run_lock = threading.Lock()
        self._reconcile_attempt_cache: Dict[str, float] = {}
        self._reconcile_last_circuit_log = 0.0
        self._autonomous_stop = threading.Event()
        self._autonomous_thread = None
        self._autonomous_cycle_lock = threading.Lock()
        self._autonomous_last_decisions: Dict[str, float] = {}
        self._autonomous_source_stats: Dict[str, Dict[str, Any]] = {}
        self._autonomous_allocation_current: Dict[str, float] = {}
        self._autonomous_allocation_state_loaded = False
        self._init_agents()

    def _init_agents(self):
        """Initialize agent instances."""
        self.agents["execution"] = ExecutionAgent(self.config_manager)
        self.agents["risk"] = RiskAgent(self.config_manager)
        self.agents["scanner"] = get_scanner_agent(self.config_manager)
        self.agents["sentiment"] = SentimentAgent(self.config_manager)

    @staticmethod
    def _effective_signal_score(signal: dict) -> float:
        if not isinstance(signal, dict):
            return 0.0
        for key in ("signal_score", "score", "raw_signal_score", "raw_score"):
            try:
                value = float(signal.get(key, 0.0) or 0.0)
                if value > 0:
                    return value
            except Exception:
                continue
        return 0.0

    def _next_autonomous_allocation_bucket(self, policy: dict[str, Any]) -> str | None:
        """Smooth weighted round-robin; equal 33/33/33 yields Stocks/Crypto/PE."""
        if not policy.get("enabled"):
            return None
        weights = policy.get("weights_pct") if isinstance(policy.get("weights_pct"), dict) else {}
        total = sum(max(0.0, float(weights.get(bucket, 0.0) or 0.0)) for bucket in _AUTONOMOUS_ALLOCATION_BUCKETS)
        if total <= 0:
            return None
        if not bool(getattr(self, "_autonomous_allocation_state_loaded", False)):
            restored: Dict[str, float] = {}
            try:
                state = get_autonomous_allocation_state()
                state_weights = state.get("weights_pct") if isinstance(state.get("weights_pct"), dict) else {}
                state_current = state.get("current") if isinstance(state.get("current"), dict) else {}
                weights_match = all(
                    abs(float(state_weights.get(bucket, -1.0)) - float(weights.get(bucket, 0.0))) < 1e-9
                    for bucket in _AUTONOMOUS_ALLOCATION_BUCKETS
                )
                if weights_match:
                    restored = {
                        bucket: float(state_current.get(bucket, 0.0) or 0.0)
                        for bucket in _AUTONOMOUS_ALLOCATION_BUCKETS
                    }
            except Exception as exc:
                logger.warning("AgentManager: allocation scheduler state could not be restored: %s", exc)

            # One-time migration for deployments that already recorded cycles
            # before durable scheduler state existed. Equal lanes have a fully
            # deterministic accumulator after each target, so reconstruct it
            # from the latest completed cycle instead of restarting at Stocks.
            if not restored:
                try:
                    equal_weights = len({round(float(weights.get(bucket, 0.0)), 9) for bucket in _AUTONOMOUS_ALLOCATION_BUCKETS}) == 1
                    status = get_autonomous_cycle_status()
                    cycle = (status.get("result") or {}).get("cycle") if isinstance(status.get("result"), dict) else {}
                    allocation = cycle.get("allocation") if isinstance(cycle, dict) and isinstance(cycle.get("allocation"), dict) else {}
                    previous_target = str(allocation.get("target_bucket") or "")
                    if equal_weights and previous_target in _AUTONOMOUS_ALLOCATION_BUCKETS:
                        weight = float(weights.get(previous_target, 0.0) or 0.0)
                        if previous_target == "stocks":
                            restored = {"stocks": -2.0 * weight, "crypto": weight, "pre_earnings": weight}
                        elif previous_target == "crypto":
                            restored = {"stocks": -weight, "crypto": -weight, "pre_earnings": 2.0 * weight}
                        else:
                            restored = {bucket: 0.0 for bucket in _AUTONOMOUS_ALLOCATION_BUCKETS}
                except Exception as exc:
                    logger.warning("AgentManager: legacy allocation state migration failed: %s", exc)
            self._autonomous_allocation_current = restored
            self._autonomous_allocation_state_loaded = True

        current = dict(getattr(self, "_autonomous_allocation_current", {}) or {})
        for bucket in _AUTONOMOUS_ALLOCATION_BUCKETS:
            current[bucket] = float(current.get(bucket, 0.0) or 0.0) + max(
                0.0,
                float(weights.get(bucket, 0.0) or 0.0),
            )
        selected = max(_AUTONOMOUS_ALLOCATION_BUCKETS, key=lambda bucket: current[bucket])
        current[selected] -= total
        self._autonomous_allocation_current = current
        try:
            record_autonomous_allocation_state(
                weights_pct={
                    bucket: float(weights.get(bucket, 0.0) or 0.0)
                    for bucket in _AUTONOMOUS_ALLOCATION_BUCKETS
                },
                current=current,
                last_target_bucket=selected,
            )
        except Exception as exc:
            logger.warning("AgentManager: allocation scheduler state could not be persisted: %s", exc)
        return selected

    def start_agents(self):
        """Start all enabled agents."""
        logger.info("AgentManager: checking agent status...")
        bot_active = self._bot_mode_active()
        for name, agent in self.agents.items():
            agent_config = self.config_manager.agents.get(name, {})
            is_enabled = agent_config.get("enabled", False)

            # Bot mode forces risk + execution agents on
            if bot_active and name in ("risk", "execution"):
                is_enabled = True

            if is_enabled:
                if not agent.is_running:
                    try:
                        agent.start()
                        logger.info(f"Agent {name.upper()} started")
                    except Exception as exc:
                        logger.error(f"Failed to start agent {name}: {exc}")
            elif agent.is_running:
                agent.stop()
                logger.info(f"Agent {name.upper()} stopped (disabled in config)")

        self._update_reconcile_loop()
        self._update_autonomous_monitor_loop()
        if bot_active:
            threading.Thread(
                target=self._reconcile_existing_signal_executions,
                daemon=True,
                name="SignalExecutionInitialReconcile",
            ).start()

    def _reconcile_existing_signal_executions(self):
        """Open paper positions for valid open signals that were created before execution was running."""
        if not self._bot_mode_active():
            return
        if not self._reconcile_run_lock.acquire(blocking=False):
            return
        try:
            self._reconcile_existing_signal_executions_unlocked()
        finally:
            self._reconcile_run_lock.release()

    @staticmethod
    def _signal_reconcile_key(signal: dict) -> str:
        sid = str((signal or {}).get("id") or "").strip()
        if sid:
            return sid
        ticker = str((signal or {}).get("ticker") or "").upper().strip()
        action = str((signal or {}).get("action") or (signal or {}).get("type") or "").upper().strip()
        created = str((signal or {}).get("created_at") or (signal or {}).get("timestamp") or "").strip()
        return f"{ticker}:{action}:{created}"

    @staticmethod
    def _numeric_ts(value: Any) -> float:
        try:
            return float(value or 0.0)
        except Exception:
            return 0.0

    def _reconcile_cooldown_active(self, signal: dict, cooldown_seconds: float) -> bool:
        now_ts = time.time()
        key = self._signal_reconcile_key(signal)
        if key:
            last_ts = float(self._reconcile_attempt_cache.get(key, 0.0) or 0.0)
            if last_ts > 0 and now_ts - last_ts < cooldown_seconds:
                return True
        retry_after_ts = self._numeric_ts((signal or {}).get("execution_retry_after_ts"))
        return retry_after_ts > now_ts

    def _mark_reconcile_attempt(self, signal_id: str, cooldown_seconds: float) -> None:
        sid = str(signal_id or "").strip()
        if not sid:
            return
        now_ts = time.time()
        self._reconcile_attempt_cache[sid] = now_ts

        try:
            from core.config import get_bucharest_time
            from core.signals import get_signal_manager

            sm = get_signal_manager()
            stored = sm.open_signals.get(sid)
            if not isinstance(stored, dict) or stored.get("paper_position_id"):
                return

            stored["execution_last_attempt_ts"] = now_ts
            stored["execution_retry_after_ts"] = now_ts + max(0.0, float(cooldown_seconds or 0.0))
            stored["execution_last_attempt_at"] = get_bucharest_time().isoformat()
            stored["execution_last_result"] = "attempted_without_position"
            try:
                stored["execution_attempt_count"] = int(stored.get("execution_attempt_count", 0) or 0) + 1
            except Exception:
                stored["execution_attempt_count"] = 1
            sm.save()
        except Exception as exc:
            logger.debug(f"AgentManager: could not persist reconcile cooldown for {sid}: {exc}")

    def _risk_daily_loss_circuit_open(self) -> bool:
        risk_cfg = self.config_manager.agents.get("risk", {}) or {}
        if not bool(risk_cfg.get("enabled", True)):
            return False

        try:
            from datetime import datetime, timezone

            from core.capital_manager import (
                get_capital_manager,
                is_research_position,
                resolve_capital_equity,
            )
            from core.config import BUCHAREST_TZ, get_bucharest_time
            from core.risk_limits import configured_daily_loss_pct

            cm = get_capital_manager()
            positions = cm.get_positions() or []
            equity = resolve_capital_equity(cm, positions)
            if equity <= 0:
                return False

            max_loss_pct = configured_daily_loss_pct(risk_cfg)
            today = get_bucharest_time().date()

            def closed_today(pos: dict) -> bool:
                closed_at = str((pos or {}).get("closed_at") or "").strip()
                if not closed_at:
                    return False
                try:
                    closed_dt = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
                    if closed_dt.tzinfo is None:
                        closed_dt = closed_dt.replace(tzinfo=timezone.utc)
                    return closed_dt.astimezone(BUCHAREST_TZ).date() == today
                except Exception:
                    return False

            daily_realized_pnl = sum(
                float((pos or {}).get("net_pnl", (pos or {}).get("pnl", 0.0)) or 0.0)
                for pos in positions
                if str((pos or {}).get("status", "")).upper() == "CLOSED"
                and not is_research_position(pos)
                and closed_today(pos)
            )
            daily_loss_pct = abs(min(0.0, daily_realized_pnl) / equity) * 100.0
            if daily_loss_pct < max_loss_pct:
                return False

            exec_cfg = self.config_manager.agents.get("execution", {}) or {}
            log_interval = max(30.0, float(exec_cfg.get("reconcile_circuit_log_interval_seconds", 120.0) or 120.0))
            now_ts = time.time()
            if now_ts - float(self._reconcile_last_circuit_log or 0.0) >= log_interval:
                self._reconcile_last_circuit_log = now_ts
                logger.warning(
                    "AgentManager: reconcile paused because daily loss circuit is open "
                    f"({daily_loss_pct:.1f}% >= {max_loss_pct:.1f}%)"
                )
            return True
        except Exception as exc:
            logger.debug(f"AgentManager: daily loss circuit check failed: {exc}")
            return False

    def _reconcile_existing_signal_executions_unlocked(self):
        """Open paper positions for valid open signals that were created before execution was running."""
        exec_agent = self.agents.get("execution")
        if not exec_agent or not exec_agent.is_running:
            return

        exec_cfg = self.config_manager.agents.get("execution", {}) or {}
        if not exec_cfg.get("auto_execute_existing_signals", True):
            return
        if bool(exec_cfg.get("reconcile_skip_when_risk_circuit_open", True)) and self._risk_daily_loss_circuit_open():
            return

        try:
            from core.signals import get_signal_manager
            from core.capital_manager import get_capital_manager, is_research_position

            sm = get_signal_manager()
            sm.sync_paper_positions()
            cm = get_capital_manager()
            open_positions = [
                pos for pos in cm.get_positions()
                if isinstance(pos, dict) and str(pos.get("status", "")).upper() == "OPEN"
                and not is_research_position(pos)
            ]
            max_open_raw = exec_cfg.get("max_open_trades", 0)
            max_open = int(max_open_raw if max_open_raw is not None else 0)
            slots_available = None if max_open <= 0 else max(0, max_open - len(open_positions))
            if slots_available == 0:
                return
            open_signals = sm.open_signals if isinstance(sm.open_signals, dict) else {}
            max_reconcile_raw = exec_cfg.get("max_reconcile_per_cycle", exec_cfg.get("max_reconcile_per_start", 3))
            max_reconcile = int(max_reconcile_raw if max_reconcile_raw is not None else 3)
            if max_reconcile <= 0:
                max_reconcile = max(1, len(open_signals))
            if slots_available is not None:
                max_reconcile = max(1, min(max_reconcile, slots_available))
            reject_cooldown = max(30.0, float(exec_cfg.get("reconcile_reject_cooldown_seconds", 900.0) or 900.0))
            processed = 0
            for signal in list(open_signals.values()):
                if processed >= max_reconcile:
                    break
                if not isinstance(signal, dict):
                    continue
                sid = str(signal.get("id") or "").strip()
                if signal.get("paper_position_id"):
                    continue
                status = str(signal.get("status", "") or "").upper()
                if status not in {"PENDING", "OPEN"}:
                    continue
                if self._reconcile_cooldown_active(signal, reject_cooldown):
                    continue
                effective_score = self._effective_signal_score(signal)
                min_score = float(exec_cfg.get("min_reconcile_score", 75.0) or 75.0)
                if effective_score < min_score:
                    continue
                signal_payload = dict(signal)
                signal_payload["signal_score"] = effective_score
                signal_payload["score"] = effective_score
                signal_payload.setdefault("raw_signal_score", signal_payload.get("raw_score", effective_score))
                self.on_new_signal(signal_payload)
                self._mark_reconcile_attempt(sid or self._signal_reconcile_key(signal_payload), reject_cooldown)
                processed += 1
            if processed:
                logger.info(f"AgentManager: reconciled {processed} existing signal(s) into execution flow")
        except Exception as exc:
            logger.error(f"AgentManager: existing signal execution reconciliation failed: {exc}")

    def _update_reconcile_loop(self):
        exec_agent = self.agents.get("execution")
        exec_cfg = self.config_manager.agents.get("execution", {}) or {}
        should_run = bool(
            self._bot_mode_active()
            and exec_agent
            and exec_agent.is_running
            and exec_cfg.get("auto_execute_existing_signals", True)
        )
        if not should_run:
            self._stop_reconcile_loop()
            return
        if self._reconcile_thread and self._reconcile_thread.is_alive():
            return

        self._reconcile_stop.clear()
        interval = max(10.0, float(exec_cfg.get("reconcile_interval_seconds", 30.0) or 30.0))

        def _loop():
            while not self._reconcile_stop.wait(interval):
                self._reconcile_existing_signal_executions()

        self._reconcile_thread = threading.Thread(
            target=_loop,
            daemon=True,
            name="SignalExecutionReconcile",
        )
        self._reconcile_thread.start()
        logger.info(f"AgentManager: signal execution reconcile loop started ({interval:.0f}s)")

    def _stop_reconcile_loop(self):
        self._reconcile_stop.set()

    def _execution_config(self) -> dict:
        try:
            if hasattr(self.config_manager, "reload_if_changed"):
                self.config_manager.reload_if_changed()
        except Exception:
            pass
        return self.config_manager.agents.get("execution", {}) or {}

    def _bot_mode_active(self) -> bool:
        """Return the authoritative master-switch state for automated execution."""
        config_manager = getattr(self, "config_manager", None)
        if config_manager is None:
            return bool(getattr(self, "bot_enabled", False))
        try:
            if hasattr(config_manager, "reload_if_changed"):
                config_manager.reload_if_changed()
            bot_cfg = getattr(config_manager, "bot", {}) or {}
            return bool(getattr(self, "bot_enabled", False) or bot_cfg.get("enabled", False))
        except Exception as exc:
            logger.warning("AgentManager: bot master switch unavailable; execution stays off: %s", exc)
            return False

    def _update_autonomous_monitor_loop(self):
        exec_agent = self.agents.get("execution")
        exec_cfg = self._execution_config()
        should_run = bool(
            self._bot_mode_active()
            and exec_agent
            and exec_agent.is_running
            and exec_cfg.get("autonomous_candidates_enabled", False)
        )
        if not should_run:
            self._stop_autonomous_monitor_loop()
            return
        if self._autonomous_thread and self._autonomous_thread.is_alive():
            return

        self._autonomous_stop.clear()
        interval = max(30.0, float(exec_cfg.get("autonomous_monitor_interval_seconds", 120.0) or 120.0))

        def _loop():
            next_scheduled_cycle = 0.0
            while not self._autonomous_stop.is_set():
                try:
                    manual_request = consume_autonomous_cycle_request()
                    now = time.monotonic()
                    ran_cycle = False
                    if manual_request:
                        result = self.run_autonomous_cycle_now()
                        record_autonomous_cycle_result(result, request=manual_request)
                        ran_cycle = True
                    if now >= next_scheduled_cycle and not ran_cycle:
                        result = self.run_autonomous_cycle_now()
                        record_autonomous_cycle_result(
                            result,
                            request={"request_type": "scheduled", "requested_at": None},
                        )
                    if now >= next_scheduled_cycle:
                        next_scheduled_cycle = time.monotonic() + interval
                except Exception as exc:
                    logger.error(f"AgentManager: autonomous monitor failed: {exc}")
                # Polling only checks a local command file. It never creates
                # additional candidates unless a request was received or the
                # configured monitor interval elapsed.
                if self._autonomous_stop.wait(min(5.0, interval)):
                    break

        self._autonomous_thread = threading.Thread(
            target=_loop,
            daemon=True,
            name="AutonomousCandidateMonitor",
        )
        self._autonomous_thread.start()
        logger.info(f"AgentManager: autonomous Scanner/Watchlist/Pre-Earnings monitor started ({interval:.0f}s)")

    def _stop_autonomous_monitor_loop(self):
        self._autonomous_stop.set()

    @staticmethod
    def _autonomous_key(signal: dict) -> str:
        return ":".join(
            [
                str((signal or {}).get("autonomous_source") or "unknown").lower(),
                str((signal or {}).get("ticker") or "").upper(),
                str((signal or {}).get("action") or (signal or {}).get("type") or "").upper(),
            ]
        )

    @staticmethod
    def _signal_direction(signal: dict) -> str:
        action = str((signal or {}).get("action") or (signal or {}).get("type") or "").upper().strip()
        return "SHORT" if action in {"SHORT", "SELL"} else "LONG"

    def _apply_signal_size_caps(self, signal_data: dict) -> None:
        """Keep pre-risk sizing aligned with the configured risk cap."""
        try:
            risk_cfg = self.config_manager.agents.get("risk", {}) or {}
            if "max_position_size_pct" not in risk_cfg:
                return
            max_pct = max(0.05, float(risk_cfg.get("max_position_size_pct") or 0.0))
            from core.capital_manager import get_capital_manager, resolve_deployable_balance

            balance = resolve_deployable_balance(get_capital_manager())
            if balance <= 0:
                return

            cap_amount = balance * (max_pct / 100.0)

            # Check ADV Liquidity Headroom
            adv = float(signal_data.get("average_daily_volume_usd") or signal_data.get("avg_daily_volume_usd") or 0.0)
            if adv <= 0:
                price = float(signal_data.get("entry") or signal_data.get("entry_price") or signal_data.get("price") or 0.0)
                vol = float(signal_data.get("volume") or 0.0)
                if price > 0 and vol > 0:
                    adv = price * vol

            try:
                from core.portfolio.constraints import resolve_confirmed_risk_constitution
                app_cfg = getattr(self.config_manager, "config", {}) or {}
                constitution = resolve_confirmed_risk_constitution(app_cfg) if isinstance(app_cfg, dict) else None
                max_liq_pct = float(constitution.max_liquidity_participation_pct) if constitution and getattr(constitution, "max_liquidity_participation_pct", None) is not None else 0.0
            except Exception:
                max_liq_pct = 0.0

            if adv > 0 and max_liq_pct > 0:
                adv_cap = adv * (max_liq_pct / 100.0)
                if adv_cap > 0:
                    cap_amount = min(cap_amount, adv_cap) if cap_amount > 0 else adv_cap

            invested_amount = float(signal_data.get("invested_amount", 0.0) or 0.0)
            position_pct = float(
                signal_data.get("position_size_pct", signal_data.get("risk_pct_used", 0.0)) or 0.0
            )
            if invested_amount <= 0 and position_pct <= 0:
                # Proposed sizing per the app's standard default — the risk
                # evaluation and the constitution need a notional to evaluate
                # (max_position/exposure); the capital engine still makes the
                # final sizing decision downstream.
                # The legacy `position_size_pct` ("Risk per trade (%)" in the
                # UI) is the BASE POSITION NOTIONAL in % of the deployable
                # balance, never above the max position notional. It is not a
                # risk-per-trade figure: loss at the stop is limited separately
                # by `max_risk_per_trade_pct` (Risk Agent + Execution Agent).
                limits = resolve_position_limits(risk_cfg, default_base_notional_pct=max_pct)
                default_pct = float(limits["base_position_notional_pct"])
                position_pct = max(0.0, min(default_pct, max_pct))
                if position_pct > 0:
                    invested_amount = balance * (position_pct / 100.0)
            if invested_amount <= 0 and position_pct > 0:
                invested_amount = balance * (position_pct / 100.0)

            capped = False
            if position_pct > max_pct:
                signal_data["position_size_pct"] = round(max_pct, 4)
                signal_data["risk_pct_used"] = round(max_pct, 4)
                position_pct = max_pct
                capped = True
            if invested_amount > cap_amount > 0:
                signal_data["invested_amount"] = round(cap_amount, 6)
                signal_data["usd_size"] = round(cap_amount, 6)
                if balance > 0:
                    position_pct = (cap_amount / balance) * 100.0
                    signal_data["position_size_pct"] = round(position_pct, 4)
                    signal_data["risk_pct_used"] = round(position_pct, 4)
                capped = True

            if invested_amount > 0 and not signal_data.get("invested_amount"):
                signal_data["invested_amount"] = round(invested_amount, 6)
            if position_pct > 0 and not signal_data.get("position_size_pct"):
                signal_data["position_size_pct"] = round(position_pct, 4)
            if not capped:
                return

            signal_data["position_size_capped"] = True
            signal_data["position_size_cap_pct"] = round(max_pct, 4)
            reason = (
                f"Position size capped to the max position size {max_pct:.2f}% of capital "
                "before execution"
            )
            existing_reasons = signal_data.get("decision_reasons")
            if isinstance(existing_reasons, list) and reason not in existing_reasons:
                existing_reasons.append(reason)

            sid = str(signal_data.get("id") or "").strip()
            if sid:
                try:
                    from core.signals import get_signal_manager

                    sm = get_signal_manager()
                    stored = sm.open_signals.get(sid)
                    if isinstance(stored, dict):
                        for key in (
                            "invested_amount",
                            "position_size_pct",
                            "risk_pct_used",
                            "position_size_capped",
                            "position_size_cap_pct",
                        ):
                            if key in signal_data:
                                stored[key] = signal_data[key]
                        sm.save()
                except Exception as exc:
                    logger.debug(f"AgentManager: could not persist capped sizing for {sid}: {exc}")
        except Exception as exc:
            logger.debug(f"AgentManager: size cap check skipped: {exc}")

    def _instrument_exclusion_inputs(self) -> tuple[dict, dict]:
        """(policy, scanner tags) for the tokenised-stock / stablecoin exclusion."""
        policy = instrument_exclusion_policy(
            getattr(getattr(self, "config_manager", None), "config", None)
        )
        tags: dict = {}
        if not any(policy.values()):
            return policy, tags
        try:
            scanner = (getattr(self, "agents", None) or {}).get("scanner")
            if scanner is not None:
                state = scanner.get_state()
                tags = instrument_tags_from_scanner_state(
                    state if isinstance(state, dict) else {},
                    getattr(scanner, "_tokenized_stock_map", None),
                )
        except Exception as exc:
            logger.debug("AgentManager: scanner instrument tags unavailable: %s", exc)
        return policy, tags

    def _record_instrument_exclusion(self, signal: dict, exclusion: dict, *, stage: str) -> None:
        """Write the exclusion to the decision trace, at most once an hour per ticker."""
        ticker = str(exclusion.get("ticker") or (signal or {}).get("ticker") or "").upper().strip()
        seen = getattr(self, "_instrument_exclusion_logged", None)
        if not isinstance(seen, dict):
            seen = {}
            self._instrument_exclusion_logged = seen
        key = f"{ticker}:{exclusion.get('reason')}"
        now_ts = time.time()
        if now_ts - float(seen.get(key, 0.0) or 0.0) < 3600.0:
            return
        seen[key] = now_ts
        try:
            record_trade_event(
                event_type="signal_skipped",
                status="instrument_excluded",
                ticker=ticker,
                signal_id=(signal or {}).get("id", ""),
                strategy_type=str((signal or {}).get("strategy_type") or ""),
                summary=str(exclusion.get("detail") or exclusion.get("reason") or "instrument excluded"),
                payload={"instrument_exclusion": dict(exclusion), "stage": stage},
            )
        except Exception:
            logger.debug("instrument exclusion event failed", exc_info=True)
        logger.info(
            "AgentManager: %s excluded from autonomous entries (%s, %s)",
            ticker, exclusion.get("reason"), stage,
        )

    def _cooldown_active(self, key: str, cooldown_minutes: float) -> bool:
        last_ts = float(self._autonomous_last_decisions.get(key, 0.0) or 0.0)
        if last_ts <= 0:
            return False
        return (time.time() - last_ts) < max(0.0, cooldown_minutes) * 60.0

    def _has_recent_close_lockout(self, signal: dict, lockout_hours: float) -> tuple[bool, str]:
        """Per-ticker post-close re-entry lockout (Capital profitability A/B,
        2026-07). Returns (blocked, reason). Derived from the CLOSED position
        history in capital state (source of truth) so it survives restarts --
        no in-memory registry to lose. Blocks re-entry on a ticker for
        ``lockout_hours`` after ANY close on that ticker.

        A/B on 276 autonomous closed trades (scripts/ab_capital_profitability.py):
        baseline WR 29.7% / PF 0.641 / net -1381; with a 6h post-close lockout
        the kept population is n=96 / WR 34.4% / PF 0.701 / net -376 -- a
        +1005 PnL / 73% loss reduction with a still-respectable sample. The
        existing 45-min ``autonomous_cooldown_minutes`` gate keys on candidate
        REVIEW, not on the actual close event, so it cannot prevent the
        repeat-offender pattern (VANRY 30 closes, EPIC 26, HMSTR 25 ...).
        """
        if lockout_hours <= 0:
            return False, ""
        try:
            from core.capital_manager import get_capital_manager, is_research_position

            ticker = str((signal or {}).get("ticker") or "").upper().strip()
            if not ticker:
                return False, ""
            cm = get_capital_manager()
            now_ts = time.time()
            horizon_s = max(0.0, lockout_hours) * 3600.0
            for pos in cm.get_positions():
                if not isinstance(pos, dict):
                    continue
                if str(pos.get("status", "")).upper() != "CLOSED":
                    continue
                if is_research_position(pos):
                    continue
                if str(pos.get("ticker") or "").upper().strip() != ticker:
                    continue
                closed_at = pos.get("closed_at")
                if not closed_at:
                    continue
                try:
                    # capital stores ISO-8601 with tz (e.g. 2026-07-04T16:07:08+03:00)
                    from datetime import datetime

                    cdt = datetime.fromisoformat(str(closed_at))
                    cts = cdt.timestamp()
                except Exception:
                    continue
                if cts <= 0:
                    continue
                if (now_ts - cts) < horizon_s:
                    remaining = (horizon_s - (now_ts - cts)) / 3600.0
                    return True, (
                        f"ticker_lockout: {ticker} closed "
                        f"{(now_ts - cts) / 3600.0:.1f}h ago, "
                        f"{remaining:.1f}h of {lockout_hours:.0f}h lockout remaining"
                    )
            return False, ""
        except Exception as exc:
            logger.warning(f"AgentManager: post-close lockout check failed: {exc}")
            # Fail-open: a check failure must not strand the bot. The lockout is
            # a profitability guard, not a safety gate.
            return False, ""

    def _has_open_ticker_conflict(self, signal: dict, *, allow_scale_in: bool) -> bool:
        try:
            from core.capital_manager import get_capital_manager, is_research_position

            ticker = str((signal or {}).get("ticker") or "").upper().strip()
            direction = self._signal_direction(signal)
            if not ticker:
                return True
            cm = get_capital_manager()
            open_positions = [
                pos for pos in cm.get_positions()
                if isinstance(pos, dict) and str(pos.get("status", "")).upper() == "OPEN"
                and not is_research_position(pos)
            ]
            matches = [
                pos for pos in open_positions
                if str(pos.get("ticker") or "").upper().strip() == ticker
            ]
            if not matches:
                return False
            if not allow_scale_in:
                return True
            return not any(str(pos.get("direction") or "").upper().strip() == direction for pos in matches)
        except Exception as exc:
            logger.warning(f"AgentManager: open-position conflict check failed: {exc}")
            return True

    @staticmethod
    def _autonomous_trade_allowed_now(signal: dict) -> tuple[bool, str]:
        ticker = str((signal or {}).get("ticker") or "").upper().strip()
        action = str((signal or {}).get("action") or (signal or {}).get("type") or "open").lower().strip()
        if not ticker:
            return False, "missing_ticker"
        try:
            assert_trade_allowed(ticker, action or "open")
            return True, ""
        except MarketHoursError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, str(exc)

    def _autonomy_order_gate(self, execution_cfg: dict) -> tuple[bool, str]:
        """§45/§46/§48/§74 — gate autonomous order placement by mode + kill switch + LIVE_AUTO.

        Fail-safe: an unreadable/missing autonomy config resolves to ANALYZE, which
        forbids both paper and live orders, so a misconfiguration can never silently
        enable autonomous trading. §74: kill switch blocks new automated risk while
        monitoring stays active. §45: LIVE_AUTO is owner-only; §46 strengthens the
        LIVE_AUTO branch via can_enable_live_auto (Task 6).
        """
        try:
            cm_cfg = getattr(self.config_manager, "config", {}) or {}
        except Exception:
            cm_cfg = {}
        if kill_switch_active(cm_cfg):
            return False, "kill_switch_active"  # §74 — no new automated risk
        mode = resolve_mode(cm_cfg)
        is_live = str((execution_cfg or {}).get("mode", "paper")).lower() == "live"
        if is_live:
            if not mode_allows_live_orders(mode):
                return False, f"mode_{mode.value}_forbids_live_orders"
            # §45/§46/§48 — LIVE_AUTO requires owner flag AND recorded Paper-Auto acceptance.
            # can_enable_live_auto reads the recorded flag at runtime (no live AI re-run).
            from core.autonomy import can_enable_live_auto
            _la_ok, _la_reason = can_enable_live_auto(cm_cfg)
            if not _la_ok:
                return False, f"live_auto_blocked:{_la_reason}"
        else:
            if not mode_allows_paper_orders(mode):
                return False, f"mode_{mode.value}_forbids_paper_orders"
        return True, "ok"

    def _load_pre_earnings_items(self) -> List[Dict[str, Any]]:
        # Kept for the cycle report: a failed load must not look like
        # "the pre-earnings source had no eligible candidates".
        self._pre_earnings_load_error = ""
        try:
            from agents.pre_earnings_agent import PRE_EARNINGS_OUTPUT_PATH, get_pre_earnings_agent_if_ready

            pea = get_pre_earnings_agent_if_ready()
            if pea is not None:
                state = pea.get_state()
                items = state.get("items", []) if isinstance(state, dict) else []
                return [item for item in items if isinstance(item, dict)]

            if PRE_EARNINGS_OUTPUT_PATH.exists():
                with PRE_EARNINGS_OUTPUT_PATH.open("r", encoding="utf-8") as f:
                    payload = json.load(f)
                items = payload.get("items", []) if isinstance(payload, dict) else []
                return [item for item in items if isinstance(item, dict)]
        except Exception as exc:
            # Name the exception type: a bare KeyError prints only its key
            # ("'core.ai'"), which hid an import failure for days.
            self._pre_earnings_load_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                f"AgentManager: pre-earnings autonomous load failed: {self._pre_earnings_load_error}"
            )
        return []

    @staticmethod
    def _item_price(item: Dict[str, Any]) -> float:
        for key in ("price", "last_price", "current_price", "regularMarketPrice"):
            try:
                value = item.get(key)
                if isinstance(value, dict):
                    value = value.get("raw")
                parsed = float(value or 0.0)
                if parsed > 0:
                    return parsed
            except Exception:
                continue
        return 0.0

    def _resolve_candidate_price(self, ticker: str, action: str) -> float:
        try:
            from core.capital_manager import get_capital_manager

            side = "BUY" if str(action or "").upper() == "BUY" else "SELL"
            cm = get_capital_manager()
            exec_data = cm._get_execution_price(ticker, side, use_cache=True)
            return float((exec_data or {}).get("execution_price", 0.0) or 0.0)
        except Exception as exc:
            logger.warning(f"AgentManager: could not resolve price for {ticker}: {exc}")
            return 0.0

    def _attach_candidate_price_integrity(self, signal: dict) -> dict:
        """Attach fresh, verified price provenance before autonomous review.

        Scanner candidates carry a technical snapshot price.  Before allowing
        one through the execution pipeline, compare it with the executable
        quote for the correct side.  This keeps the Capital data gate useful
        for autonomous signals while making one cache-bypassing lookup only for
        the candidate selected for review, not every scanner result.
        """
        payload = signal if isinstance(signal, dict) else {}
        ticker = str(payload.get("ticker") or "").upper().strip()
        action = str(payload.get("action") or payload.get("type") or "").upper().strip()
        try:
            technical_price = float(payload.get("entry", payload.get("entry_price", 0.0)) or 0.0)
        except (TypeError, ValueError):
            technical_price = 0.0
        asset_class = str(payload.get("asset_class") or "").upper().strip()
        is_crypto = is_crypto_ticker(ticker) or "CRYPTO" in asset_class
        asset_type = "crypto" if is_crypto else "stock"
        autonomous_source = str(payload.get("autonomous_source") or "").lower().strip()
        technical_source = (
            "v2_live_ohlcv"
            if autonomous_source == "v2_forward"
            else ("scanner_binance_24h" if is_crypto else "scanner_market_snapshot")
        )
        signal_config = getattr(getattr(self, "config_manager", None), "signal", {}) or {}
        base_evidence = {
            "technical_price": technical_price if technical_price > 0 else None,
            "technical_source": technical_source,
            "asset_type": asset_type,
        }

        def attach_integrity_evidence(integrity: dict) -> None:
            """Expose the price validator's conflict verdict to Risk.

            Missing an independent quote is not the same as two providers
            disagreeing: Capital's verified-price gate handles the former,
            while the Risk Constitution's conflict gate handles the latter.
            """
            status = str((integrity or {}).get("status") or "unavailable").lower()
            payload["price_integrity"] = integrity
            payload["has_data_conflict"] = status == "conflict"
            payload["data_conflict_evidence"] = {
                "status": status,
                "has_conflict": status == "conflict",
                "live_source": (integrity or {}).get("live_source"),
                "technical_source": (integrity or {}).get("technical_source"),
                "deviation_pct": (integrity or {}).get("deviation_pct"),
            }

        if not ticker or action not in {"BUY", "SHORT"} or technical_price <= 0:
            integrity = validate_price_consistency(
                live_price=None,
                technical_price=technical_price,
                asset_type=asset_type,
                live_source="unavailable",
                technical_source=technical_source,
                config=signal_config.get("price_integrity", {}),
            )
            attach_integrity_evidence(integrity)
            payload["price_verified"] = False
            payload["price_evidence"] = base_evidence
            return payload

        try:
            from core.capital_manager import get_capital_manager, resolve_deployable_balance

            side = "BUY" if action == "BUY" else "SELL"
            execution_data = get_capital_manager()._get_execution_price(
                ticker,
                side,
                use_cache=False,
            )
            execution_data = execution_data if isinstance(execution_data, dict) else {}
            live_price = float(execution_data.get("execution_price", 0.0) or 0.0)
            execution_verified = get_capital_manager()._is_verified_execution_payload(execution_data)
            integrity = validate_price_consistency(
                live_price=live_price if execution_verified else None,
                technical_price=technical_price,
                asset_type=asset_type,
                live_source=str(execution_data.get("source") or "unavailable"),
                technical_source=technical_source,
                config=signal_config.get("price_integrity", {}),
            )
            attach_integrity_evidence(integrity)
            payload["price_verified"] = bool(execution_verified and integrity.get("is_verified"))
            payload["technical_price"] = technical_price
            payload["live_price"] = live_price if execution_verified else None
            payload["price_evidence"] = {
                **base_evidence,
                "live_price": live_price if execution_verified else None,
                "live_source": str(execution_data.get("source") or "unavailable"),
                "live_timestamp": execution_data.get("price_timestamp"),
                "live_quality": dict(execution_data.get("execution_price_quality") or {}),
                "execution_price_verified": execution_verified,
            }
        except Exception as exc:
            integrity = validate_price_consistency(
                live_price=None,
                technical_price=technical_price,
                asset_type=asset_type,
                live_source="unavailable",
                technical_source=technical_source,
                config=getattr(self.config_manager, "signal", {}).get("price_integrity", {}),
            )
            integrity["reason"] = f"Live execution quote unavailable: {exc}"
            attach_integrity_evidence(integrity)
            payload["price_verified"] = False
            payload["price_evidence"] = base_evidence
            logger.warning("AgentManager: price integrity unavailable for %s: %s", ticker, exc)
        return payload

    def _scanner_candidates(self, exec_cfg: dict) -> List[dict]:
        scanner = self.agents.get("scanner")
        if scanner is None:
            return []
        try:
            state = scanner.get_state()
            max_age_seconds = max(
                1.0,
                float(exec_cfg.get("autonomous_scanner_max_data_age_seconds", 120.0) or 120.0),
            )
            data_age_seconds = state.get("data_age_seconds") if isinstance(state, dict) else None
            if isinstance(data_age_seconds, (int, float)) and data_age_seconds > max_age_seconds:
                logger.info(
                    "AgentManager: scanner snapshot is %.0fs old (limit %.0fs); no autonomous candidates created",
                    data_age_seconds,
                    max_age_seconds,
                )
                return []
            items = state.get("hot_tickers", []) if isinstance(state, dict) else []
            sources = exec_cfg.get("autonomous_sources") if isinstance(exec_cfg.get("autonomous_sources"), dict) else {}
            watchlist_enabled = bool(sources.get("watchlist", True))
            watchlist_stock_items = (
                state.get("watchlist_stock_tickers", [])
                if isinstance(state, dict) and watchlist_enabled
                else []
            )
            watchlist_crypto_items = (
                state.get("watchlist_crypto_tickers", [])
                if isinstance(state, dict) and watchlist_enabled
                else []
            )
            watchlist_items = [
                *(
                    watchlist_stock_items
                    if isinstance(watchlist_stock_items, list)
                    else []
                ),
                *(
                    watchlist_crypto_items
                    if isinstance(watchlist_crypto_items, list)
                    else []
                ),
            ]
            ranked = sorted(
                [item for item in items if isinstance(item, dict)],
                key=lambda item: abs(float(item.get("change_pct", 0.0) or 0.0)),
                reverse=True,
            )
            per_bucket_limit = max(
                1,
                int(exec_cfg.get("autonomous_scanner_max_symbols_per_bucket", 25) or 25),
            )
            selected_hot: List[dict] = []
            for crypto_bucket in (False, True):
                bucket_items = [
                    item
                    for item in ranked
                    if is_crypto_ticker(
                        normalize_market_ticker(str(item.get("symbol") or item.get("ticker") or ""))
                    ) is crypto_bucket
                ]
                selected_hot.extend(bucket_items[:per_bucket_limit])

            watchlist_limit = max(
                1,
                int(exec_cfg.get("autonomous_watchlist_max_symbols", 100) or 100),
            )
            watchlist_ranked = sorted(
                [item for item in watchlist_items if isinstance(item, dict)],
                key=lambda item: abs(float(item.get("change_pct", 0.0) or 0.0)),
                reverse=True,
            )[:watchlist_limit]
            watchlist_by_ticker = {
                normalize_market_ticker(str(item.get("symbol") or item.get("ticker") or "")): item
                for item in watchlist_ranked
            }
            merged_items: List[dict] = []
            seen_tickers: set[str] = set()
            # Market-wide reading for regimes that depend on it (crypto "market_dip").
            btc_change_pct = market_btc_change_pct(state)
            for item in [*selected_hot, *watchlist_ranked]:
                ticker = normalize_market_ticker(str(item.get("symbol") or item.get("ticker") or ""))
                if not ticker or ticker in seen_tickers:
                    continue
                enriched = dict(item)
                if btc_change_pct is not None and is_crypto_ticker(ticker):
                    enriched["market_btc_change_pct"] = btc_change_pct
                if ticker in watchlist_by_ticker:
                    enriched["watchlist_member"] = True
                seen_tickers.add(ticker)
                merged_items.append(enriched)
            candidates: List[dict] = []
            # What the whole list is doing today, for the AI reviewer (evidence, not a rule).
            day_context = market_context(state)
            for item in merged_items:
                signal = build_scanner_candidate_signal(item, exec_cfg)
                if signal:
                    if day_context is not None:
                        signal["market_context"] = day_context
                    candidates.append(signal)
            watchlist_candidate_count = sum(
                1
                for signal in candidates
                if str(signal.get("autonomous_candidate_origin") or "") == "watchlist"
            )
            self._autonomous_source_stats["scanner"] = {
                "enabled": True,
                "observation_count": len(selected_hot),
                "candidate_count": len(candidates),
            }
            self._autonomous_source_stats["watchlist"] = {
                "enabled": watchlist_enabled,
                "observation_count": len(watchlist_ranked),
                "stock_observation_count": len(watchlist_stock_items) if isinstance(watchlist_stock_items, list) else 0,
                "crypto_observation_count": len(watchlist_crypto_items) if isinstance(watchlist_crypto_items, list) else 0,
                "candidate_count": watchlist_candidate_count,
                "allocation_buckets": ["stocks", "crypto"],
            }
            return candidates
        except Exception as exc:
            logger.warning(f"AgentManager: scanner autonomous candidates failed: {exc}")
            return []

    def _forward_v2_candidates(self, exec_cfg: dict) -> List[dict]:
        """Build strict live-V2 research candidates from a fresh scanner snapshot."""
        source_stats = getattr(self, "_autonomous_source_stats", None)
        if not isinstance(source_stats, dict):
            source_stats = {}
            self._autonomous_source_stats = source_stats
        scanner = self.agents.get("scanner")
        if scanner is None:
            source_stats["v2_forward"] = {"enabled": True, "status": "scanner_unavailable", "evaluated": 0, "candidate_count": 0}
            return []
        try:
            from core.forward_v2 import build_forward_v2_candidate, forward_v2_config, is_forward_universe_item

            config = forward_v2_config(exec_cfg)
            if not bool(config.get("enabled", False)):
                source_stats["v2_forward"] = {"enabled": False, "status": "disabled", "evaluated": 0, "candidate_count": 0}
                return []
            state = scanner.get_state()
            data_age_seconds = state.get("data_age_seconds") if isinstance(state, dict) else None
            max_age = max(1.0, float(config.get("max_scanner_data_age_seconds", 120.0) or 120.0))
            scanner_stale = bool(isinstance(data_age_seconds, (int, float)) and data_age_seconds > max_age)
            if scanner_stale:
                logger.info(
                    "AgentManager: V2 forward scanner snapshot is %.0fs old (limit %.0fs)",
                    data_age_seconds,
                    max_age,
                )
            liquid_items = state.get("liquid_crypto_tickers", []) if isinstance(state, dict) and not scanner_stale else []
            using_liquid_universe = isinstance(liquid_items, list) and bool(liquid_items)
            hot_items = state.get("hot_tickers", []) if isinstance(state, dict) and not scanner_stale else []
            items: List[dict] = [item for item in liquid_items if isinstance(item, dict)]
            for item in hot_items if isinstance(hot_items, list) else []:
                raw_symbol = str(item.get("symbol") or item.get("ticker") or "").upper().strip()
                normalized = normalize_market_ticker(raw_symbol)
                if not using_liquid_universe or not is_crypto_ticker(normalized):
                    items.append(item)

            # Stocks are drawn from the active watchlist, while the explicitly
            # configured futures universe ensures COMMODITIES are evaluated even
            # when Yahoo's gainers/losers screen contains equities only.
            watchlist = getattr(self.config_manager, "watchlist", {})
            if isinstance(watchlist, dict):
                for ticker in watchlist.get("yahoo", []) or []:
                    items.append({"symbol": ticker, "source": "watchlist_v2_forward"})
            configured_symbols = config.get("universe_symbols_by_asset_class")
            if isinstance(configured_symbols, dict):
                for asset_class, symbols in configured_symbols.items():
                    if str(asset_class or "").upper().strip() not in config.get("asset_classes", set()):
                        continue
                    for ticker in symbols if isinstance(symbols, list) else []:
                        items.append({"symbol": ticker, "source": "configured_v2_forward"})

            deduped_items: List[dict] = []
            seen_tickers: set[str] = set()
            for item in items:
                raw_symbol = str(item.get("symbol") or item.get("ticker") or "").upper().strip()
                ticker = normalize_market_ticker(raw_symbol)
                if not ticker or ticker in seen_tickers or not is_forward_universe_item(item, config):
                    continue
                seen_tickers.add(ticker)
                deduped_items.append(item)
            from core.forward_v2 import select_forward_universe

            by_asset_class: Dict[str, List[dict]] = {}
            for item in deduped_items:
                ticker = normalize_market_ticker(str(item.get("symbol") or item.get("ticker") or ""))
                from core.strategy_v2.context_builder import infer_asset_class
                by_asset_class.setdefault(infer_asset_class(ticker), []).append(item)

            selected_items: List[dict] = []
            per_asset_limit = max(
                1,
                int(float(config.get("max_symbols_per_asset_class_per_cycle", config.get("max_symbols_per_cycle", 2)) or 2)),
            )
            for asset_class in sorted(config.get("asset_classes", set())):
                class_config = dict(config)
                class_config["max_symbols_per_cycle"] = per_asset_limit
                selected_items.extend(
                    select_forward_universe(
                        by_asset_class.get(asset_class, []),
                        config=class_config,
                        rotate=using_liquid_universe if asset_class == "CRYPTO_FUTURES" else True,
                    )
                )
            candidates: List[dict] = []
            evaluated_symbols: List[str] = []
            evaluations: List[dict] = []

            def _evaluate_forward_item(item: dict) -> tuple[str, Dict[str, Any], Optional[dict]]:
                symbol = str(item.get("symbol") or "").upper().strip()
                diagnostic: Dict[str, Any] = {}
                signal = build_forward_v2_candidate(
                    item,
                    strategy_v2_config=getattr(self.config_manager, "strategy_v2", {}) or {},
                    execution_config=exec_cfg,
                    diagnostic=diagnostic,
                )
                return symbol, diagnostic, signal

            workers = min(
                len(selected_items),
                max(1, int(float(config.get("max_fetch_workers", 6) or 6))),
            )
            if workers > 1:
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="forward-v2-ohlcv") as pool:
                    evaluation_rows = list(pool.map(_evaluate_forward_item, selected_items))
            else:
                evaluation_rows = [_evaluate_forward_item(item) for item in selected_items]
            for symbol, diagnostic, signal in evaluation_rows:
                if symbol:
                    evaluated_symbols.append(symbol)
                evaluations.append({"symbol": symbol, **diagnostic})
                if signal:
                    candidates.append(signal)
            source_stats["v2_forward"] = {
                "enabled": True,
                "status": "candidate_found" if candidates else "no_qualifying_v2_setup",
                "scanner_age_seconds": round(float(data_age_seconds), 3) if isinstance(data_age_seconds, (int, float)) else None,
                "max_scanner_age_seconds": round(max_age, 3),
                "scanner_snapshot_stale": scanner_stale,
                "universe_source": (
                    "binance_liquid"
                    if using_liquid_universe and config.get("asset_classes", set()) == {"CRYPTO_FUTURES"}
                    else ("multi_asset_with_binance_liquid" if using_liquid_universe else "multi_asset_scanner_watchlist")
                ),
                "universe_size": len(deduped_items),
                "universe_by_asset_class": {key: len(value) for key, value in by_asset_class.items()},
                "evaluated": len(evaluated_symbols),
                "evaluated_symbols": evaluated_symbols,
                "evaluations": evaluations,
                "candidate_count": len(candidates),
            }
            return candidates
        except Exception as exc:
            logger.warning("AgentManager: V2 forward candidate build failed: %s", exc)
            source_stats["v2_forward"] = {
                "enabled": True,
                "status": "error",
                "evaluated": 0,
                "candidate_count": 0,
                "reason": str(exc),
            }
            return []

    def _cross_sectional_momentum_candidates(self, exec_cfg: dict) -> List[dict]:
        """Build explicit forward-paper cross-sectional momentum observations.

        This source deliberately produces research-only candidates.  The normal
        Capital gate rejects its non-V2 provenance before a cohort position can
        be considered, and cohort outcomes never train the Capital cache.
        """
        source_stats = getattr(self, "_autonomous_source_stats", None)
        if not isinstance(source_stats, dict):
            source_stats = {}
            self._autonomous_source_stats = source_stats
        scanner = self.agents.get("scanner")
        if scanner is None:
            source_stats["cross_sectional_momentum"] = {
                "enabled": True,
                "status": "scanner_unavailable",
                "evaluated": 0,
                "candidate_count": 0,
            }
            return []
        try:
            from core.cross_sectional_momentum import build_cross_sectional_momentum_candidates

            configured = exec_cfg.get("cross_sectional_momentum") if isinstance(exec_cfg.get("cross_sectional_momentum"), dict) else {}
            if not bool(configured.get("enabled", False)):
                source_stats["cross_sectional_momentum"] = {
                    "enabled": False,
                    "status": "disabled",
                    "evaluated": 0,
                    "candidate_count": 0,
                }
                return []
            state = scanner.get_state()
            age = state.get("data_age_seconds") if isinstance(state, dict) else None
            max_age = max(1.0, float(configured.get("max_scanner_data_age_seconds", 120.0) or 120.0))
            if isinstance(age, (int, float)) and age > max_age:
                source_stats["cross_sectional_momentum"] = {
                    "enabled": True,
                    "status": "stale_scanner_snapshot",
                    "scanner_age_seconds": round(float(age), 3),
                    "max_scanner_age_seconds": round(max_age, 3),
                    "evaluated": 0,
                    "candidate_count": 0,
                }
                return []
            items = state.get("liquid_crypto_tickers", []) if isinstance(state, dict) else []
            universe = items if isinstance(items, list) and items else (state.get("hot_tickers", []) if isinstance(state, dict) else [])
            candidates, diagnostic = build_cross_sectional_momentum_candidates(
                universe if isinstance(universe, list) else [],
                execution_config=exec_cfg,
            )
            source_stats["cross_sectional_momentum"] = {
                **(diagnostic if isinstance(diagnostic, dict) else {}),
                "enabled": True,
                "scanner_age_seconds": round(float(age), 3) if isinstance(age, (int, float)) else None,
                "universe_source": "binance_liquid" if isinstance(items, list) and bool(items) else "scanner_hot_fallback",
            }
            return candidates
        except Exception as exc:
            logger.warning("AgentManager: cross-sectional momentum candidate build failed: %s", exc)
            source_stats["cross_sectional_momentum"] = {
                "enabled": True,
                "status": "error",
                "evaluated": 0,
                "candidate_count": 0,
                "reason": str(exc),
            }
            return []

    def _pre_earnings_candidates(
        self,
        exec_cfg: dict,
        *,
        now: Optional[datetime] = None,
    ) -> List[dict]:
        try:
            self._pre_earnings_load_error = ""
            items = self._load_pre_earnings_items()
            load_error = str(getattr(self, "_pre_earnings_load_error", "") or "")
            cycle_now = now or datetime.now(timezone.utc)
            # Shortlist by execution relevance before resolving market prices.
            # Sorting the entire calendar only by GapRisk allowed distant events
            # to crowd a valid same-day entry window out of the bounded top 25.
            preassessed: List[tuple[dict, dict]] = []
            for item in items:
                preassessment = assess_pre_earnings_candidate(
                    item,
                    self._item_price(item),
                    exec_cfg,
                    require_price=False,
                    require_entry_window=True,
                    now=cycle_now,
                )
                preassessed.append((item, preassessment))

            window_priority = {
                "window_open": 4,
                "waiting_for_pre_close_window": 3,
                "future_entry_date": 2,
                "before_market_open": 1,
            }

            def _shortlist_priority(row: tuple[dict, dict]) -> tuple:
                item, assessment = row
                window = assessment.get("entry_window") if isinstance(assessment.get("entry_window"), dict) else {}
                return (
                    1 if assessment.get("executable_now") else 0,
                    1 if assessment.get("ranking_eligible") else 0,
                    window_priority.get(str(window.get("status") or ""), 0),
                    float(assessment.get("score", 0.0) or 0.0),
                    int(float(item.get("gap_risk_numeric", item.get("gap_risk", 0)) or 0)),
                    abs(float(item.get("direction_bias_score", 0.0) or 0.0)),
                )

            ranked = sorted(preassessed, key=_shortlist_priority, reverse=True)
            candidates: List[dict] = []
            evaluations: List[dict] = []
            for item, _preassessment in ranked[:25]:
                action = infer_pre_earnings_action(item, exec_cfg)
                ticker = str(item.get("symbol") or "").upper().strip()
                price = self._item_price(item)
                if action and price <= 0 and ticker:
                    price = self._resolve_candidate_price(ticker, action)
                assessment = assess_pre_earnings_candidate(
                    item,
                    price,
                    exec_cfg,
                    require_entry_window=True,
                    now=cycle_now,
                )
                evaluations.append({
                    "ticker": ticker,
                    "eligible": bool(assessment.get("eligible")),
                    "ranking_eligible": bool(assessment.get("ranking_eligible")),
                    "executable_now": bool(assessment.get("executable_now")),
                    "eligibility_scope": assessment.get("eligibility_scope"),
                    "score": assessment.get("score"),
                    "entry_window": assessment.get("entry_window"),
                    "window_open": assessment.get("window_open"),
                    "window_reason": assessment.get("window_reason"),
                    "metrics": assessment.get("metrics"),
                    "thresholds": assessment.get("thresholds"),
                    "blockers": assessment.get("blockers"),
                    "first_blocker": assessment.get("first_blocker"),
                })
                if not assessment.get("eligible"):
                    continue
                signal = build_pre_earnings_candidate_signal(
                    item,
                    price,
                    exec_cfg,
                    assessment=assessment,
                    now=cycle_now,
                )
                if signal:
                    candidates.append(signal)
            pre_earnings_stats: Dict[str, Any] = {
                "enabled": True,
                "status": "ready" if candidates else "no_eligible_candidates",
                "evaluated": len(evaluations),
                "candidate_count": len(candidates),
                "evaluations": evaluations,
            }
            if load_error:
                # The source could not be read: say so instead of reporting an
                # empty, healthy-looking source.
                pre_earnings_stats["load_error"] = load_error
                if not items:
                    pre_earnings_stats["status"] = "error"
                    pre_earnings_stats["reason"] = load_error
            self._autonomous_source_stats["pre_earnings"] = pre_earnings_stats
            return candidates
        except Exception as exc:
            logger.warning(f"AgentManager: pre-earnings autonomous candidates failed: {exc}")
            self._autonomous_source_stats["pre_earnings"] = {
                "enabled": True,
                "status": "error",
                "evaluated": 0,
                "candidate_count": 0,
                "reason": str(exc),
            }
            return []

    def _delist_notice_candidates(self, exec_cfg: dict) -> List[dict]:
        """SHORT candidates for tokens named in a fresh Binance spot delisting notice.

        The watcher only reads public pages; every candidate still passes the
        normal pipeline (risk, AI review with veto, critic, Capital gates).
        """
        enabled = bool(delist_short_config(exec_cfg).get("enabled", True))
        stats: Dict[str, Any] = {"enabled": enabled, "candidate_count": 0}
        found: List[dict] = []
        if enabled:
            try:
                watcher = get_delist_watcher()
                found = list(watcher.candidates(exec_cfg) or [])
                stats.update(watcher.status())
                stats["candidate_count"] = len(found)
            except Exception as exc:
                logger.warning("AgentManager: delisting-notice candidates failed: %s", exc)
                stats.update({"status": "error", "reason": str(exc)})
                found = []
        self._autonomous_source_stats["delist_notice"] = stats
        return found

    def _xs_momentum_basket_candidates(self, exec_cfg: dict) -> List[dict]:
        """LONG and SHORT candidates of the weekly cross-sectional momentum basket.

        The module only reads public pages; every candidate still passes the
        normal pipeline (risk, AI review, critic, Capital gates). For this
        basket the AI's rejection is advisory (see ``_ai_block_is_advisory``).
        """
        enabled = bool(xs_momentum_config(exec_cfg).get("enabled", True))
        stats: Dict[str, Any] = {"enabled": enabled, "candidate_count": 0}
        found: List[dict] = []
        if enabled:
            try:
                policy, tags = self._instrument_exclusion_inputs()

                def excluded(symbol: str) -> bool:
                    probe = {"ticker": symbol, "symbol": symbol, "asset_class": "CRYPTO_FUTURES"}
                    return autonomous_instrument_exclusion(probe, policy, tags) is not None

                basket = get_xs_momentum_basket()
                concluded = self._xs_basket_concluded(exec_cfg)
                found = [] if concluded else list(basket.candidates(exec_cfg, exclude=excluded) or [])
                stats.update(basket.status(exec_cfg))
                if concluded:
                    stats["status"] = "experiment_concluded"
                    stats["reason"] = concluded
                stats["candidate_count"] = len(found)
            except Exception as exc:
                logger.warning("AgentManager: momentum-basket candidates failed: %s", exc)
                stats.update({"status": "error", "reason": str(exc)})
                found = []
        self._autonomous_source_stats[XS_MOMENTUM_SOURCE] = stats
        return found

    def _drop_concluded_experiments(self, candidates: List[dict]) -> tuple[List[dict], Dict[str, int]]:
        """(candidates that can still open a position, {strategy: how many were left out}).

        A paper experiment that has used up its budget (enough closed trades,
        no net edge) is refused by the Capital gate every time. Its candidates
        used to win the cycle's single review slot on score and be refused
        seconds later, cycle after cycle, so that rules which could still
        trade never got a turn. They are left out before the slot is given.
        """
        if not candidates:
            return candidates, {}
        try:
            from core.capital_manager import get_capital_manager

            positions = get_capital_manager().get_positions() or []
        except Exception as exc:
            logger.warning("AgentManager: experiment budgets unavailable, nothing left out: %s", exc)
            return candidates, {}
        config = getattr(getattr(self, "config_manager", None), "config", None) or self._execution_config()
        allowed_by_key: Dict[tuple, bool] = {}
        kept: List[dict] = []
        concluded: Dict[str, int] = {}
        for signal in candidates:
            strategy = str((signal or {}).get("strategy_type") or (signal or {}).get("strategy_id") or "").upper()
            key = (strategy, self._signal_direction(signal), str((signal or {}).get("entry_strategy") or ""))
            if key not in allowed_by_key:
                allowed = True
                try:
                    # Rule baskets ("factor") have no per-leg budget: their own stopping rules cover both legs.
                    # Learning mode runs toward 10,000 closed trades without a 100-trade stop.
                    if _paper_autonomous_experiment_source(signal, config) not in ("", "factor", "learning"):
                        probe = {**signal, "_autonomous_candidate": True}
                        allowed = bool(evaluate_paper_experiment_budget(probe, positions, config).get("allowed", True))
                except Exception as exc:
                    logger.warning("AgentManager: experiment budget check failed for %s: %s", strategy, exc)
                allowed_by_key[key] = allowed
            if allowed_by_key[key]:
                kept.append(signal)
            else:
                concluded[strategy] = concluded.get(strategy, 0) + 1
        return kept, concluded

    @staticmethod
    def _open_learning_positions() -> int:
        from core.capital_manager import get_capital_manager

        return sum(1 for p in (get_capital_manager().get_positions() or [])
                   if isinstance(p, dict) and str(p.get("status") or "").upper() == "OPEN" and _is_learning(p))

    def _learning_turn(self, signal: Any) -> int:
        """1 when a learning idea should take this cycle's review slot.

        Learning ideas expire within minutes while rule candidates come back every
        cycle, so an idea goes first - but never twice in a row, so that the weekly
        basket, the Strategy Lab and the dip rule keep getting reviewed.
        """
        return 1 if _learning_priority(signal) and not getattr(self, "_last_review_was_learning", False) else 0

    @staticmethod
    def _learning_equity() -> float:
        from core.capital_manager import get_capital_manager, resolve_capital_equity

        return float(resolve_capital_equity(get_capital_manager()) or 0.0)

    def _build_learning_worker(self) -> LearningWorker:
        """The learning-mode worker of this process, wired to the scanner, the news, Capital and Opus."""
        from core.autonomous_candidates import market_context as scanner_market_context
        from core.capital_manager import get_capital_manager
        from core.config import DATA_DIR

        def scanner_state() -> dict:
            scanner = (getattr(self, "agents", None) or {}).get("scanner")
            state = scanner.get_state() if scanner is not None else {}
            return state if isinstance(state, dict) else {}

        def news_rows() -> list:
            from core.news_agent_access import get_news_agent_cached

            agent = get_news_agent_cached()
            state = getattr(agent, "_state", None)
            lock = getattr(agent, "_cache_lock", None)
            if state is None or lock is None or not lock.acquire(timeout=2.0):
                return []
            try:
                articles = list(getattr(state, "breaking_news", []) or []) + list(getattr(state, "market_news", []) or [])[:300]
                return [article.to_dict() for article in articles]
            finally:
                lock.release()

        def quote(ticker: str, side: str):
            payload = get_capital_manager()._get_execution_price(ticker, "BUY" if side == "LONG" else "SELL", use_cache=True)
            return (payload or {}).get("execution_price") if isinstance(payload, dict) else None

        def excluded(ticker: str) -> bool:
            policy, tags = self._instrument_exclusion_inputs()
            probe = {"ticker": ticker, "symbol": ticker,
                     "asset_class": "CRYPTO_FUTURES" if ticker.endswith("-USD") else "US_STOCKS"}
            return autonomous_instrument_exclusion(probe, policy, tags) is not None

        def ticker_news(ticker: str) -> list:
            from core.news_pulse import build_news_pulse

            pulse = build_news_pulse(ticker, allow_blocking=False) or {}
            window = pulse.get("24h") if isinstance(pulse.get("24h"), dict) else {}
            rows = window.get("top_headlines") or pulse.get("top_headlines") or []
            return [{"title": r.get("title"), "source": r.get("source"), "age": r.get("time") or r.get("published_at")}
                    for r in rows if isinstance(r, dict) and r.get("title")]

        def should_run() -> bool:
            exec_cfg = self._execution_config()
            sources = exec_cfg.get("autonomous_sources") if isinstance(exec_cfg.get("autonomous_sources"), dict) else {}
            execution = (getattr(self, "agents", None) or {}).get("execution")
            return bool(
                self._bot_mode_active()
                and execution is not None and getattr(execution, "is_running", False)
                and exec_cfg.get("autonomous_candidates_enabled", False)
                and sources.get(LEARNING_SOURCE, True)
            )

        return LearningWorker(
            should_run=should_run,
            book=LearningBook(DATA_DIR / "learning_mode"),
            config=self._execution_config,
            scanner_state=scanner_state,
            news_rows=news_rows,
            positions=lambda: get_capital_manager().get_positions() or [],
            quote=quote,
            excluded=excluded,
            ticker_news=ticker_news,
            market_context=scanner_market_context,
        )

    def _learning_candidates(self, exec_cfg: dict) -> List[dict]:
        """Trades Opus chose on a mover or a headline (learning mode, paper only).

        The worker asks Opus in the background; this hands its waiting ideas to the
        cycle at a fixed 1% of equity each. They pass the normal Risk, price and
        Capital gates; the cycle's own AI review of them is a second opinion on record.
        """
        cfg = learning_config(exec_cfg)
        paper = str((exec_cfg or {}).get("mode", "paper")).lower() == "paper"
        enabled = bool(cfg.get("enabled", True)) and paper
        stats: Dict[str, Any] = {"enabled": enabled, "candidate_count": 0}
        found: List[dict] = []
        if enabled:
            try:
                worker = get_learning_worker(self._build_learning_worker)
                if worker is not None:
                    found = list(worker.book.candidates(exec_cfg, datetime.now(timezone.utc),
                                                        reprice=worker.latest_scanner_price,
                                                        open_learning=self._open_learning_positions()) or [])
                    stats["worker"] = {k: worker.last_result.get(k) for k in ("status", "reason", "at")}
                    stats["pending"] = len(found)
                size_usd = round(self._learning_equity() * float(cfg.get("position_pct_of_equity", 1.0)) / 100.0, 6)
                for signal in found:
                    if size_usd > 0:
                        signal["learning_size_usd"] = size_usd
                        signal["invested_amount"] = size_usd
                stats["candidate_count"] = len(found)
            except Exception as exc:
                logger.warning("AgentManager: learning-mode candidates failed: %s", exc)
                stats.update({"status": "error", "reason": str(exc)})
                found = []
        self._autonomous_source_stats[LEARNING_SOURCE] = stats
        return found

    def _strategy_lab_candidates(self, exec_cfg: dict) -> List[dict]:
        """Members of the rules the Strategy Lab is trading on paper (core/strategy_lab).

        The lab runs in its own process and writes what each rule holds; this
        only turns the members into candidates. They pass the normal pipeline;
        the AI's rejection of a single member is advisory (``_ai_block_is_advisory``).
        """
        enabled = bool(lab_live_config(exec_cfg).get("enabled", True))
        stats: Dict[str, Any] = {"enabled": enabled, "candidate_count": 0}
        found: List[dict] = []
        if enabled:
            try:
                policy, tags = self._instrument_exclusion_inputs()

                def excluded(symbol: str) -> bool:
                    probe = {"ticker": symbol, "symbol": symbol, "asset_class": "CRYPTO_FUTURES"}
                    return autonomous_instrument_exclusion(probe, policy, tags) is not None

                baskets = get_lab_baskets()
                found = list(baskets.candidates(exec_cfg, exclude=excluded) or [])
                stats.update(baskets.status())
                stats["candidate_count"] = len(found)
            except Exception as exc:
                logger.warning("AgentManager: strategy-lab candidates failed: %s", exc)
                stats.update({"status": "error", "reason": str(exc)})
                found = []
        self._autonomous_source_stats[STRATEGY_LAB_SOURCE] = stats
        return found

    @staticmethod
    def _note_basket_member_blocked(signal: dict) -> None:
        """Record that a Strategy Lab member could not be entered because another strategy holds the instrument.

        Capital keeps one position per instrument, so the member is skipped; without a
        record the rule's paper results would silently be those of a partial basket.
        """
        if not is_lab_signal(signal):
            return
        try:
            from core.capital_manager import get_capital_manager

            ticker = str((signal or {}).get("ticker") or "").upper().strip()
            own = str((signal or {}).get("strategy_type") or "").upper().rsplit("_", 1)[0]
            for position in get_capital_manager().get_positions() or []:
                if (
                    isinstance(position, dict)
                    and str(position.get("status") or "").upper() == "OPEN"
                    and str(position.get("ticker") or "").upper().strip() == ticker
                ):
                    holder = str(position.get("strategy_type") or "").upper()
                    if holder.rsplit("_", 1)[0] != own:          # not this rule's own position
                        get_lab_baskets().note_blocked((signal or {}).get("id"), f"held by {holder or 'another strategy'}")
                    return
        except Exception as exc:
            logger.warning("AgentManager: lab member conflict note failed: %s", exc)

    @staticmethod
    def _xs_basket_concluded(exec_cfg: dict) -> str:
        """Why the weekly momentum basket opens nothing more, or "" while it may trade.

        The per-leg experiment budget does not fit a two-legged basket, so the basket
        has its own stopping rule over both legs together: enough closed trades and no
        net profit.
        """
        limit = int(xs_momentum_config(exec_cfg).get("max_closed_without_profit", 100) or 100)
        try:
            from core.capital_manager import get_capital_manager

            closed = [
                position for position in get_capital_manager().get_positions() or []
                if isinstance(position, dict) and str(position.get("status") or "").upper() != "OPEN"
                and str(position.get("strategy_type") or "").upper().startswith("XS_MOMENTUM_")
            ]
            net = sum(float(position.get("net_pnl") or 0.0) for position in closed)
        except Exception as exc:
            logger.warning("AgentManager: momentum-basket results unavailable: %s", exc)
            return ""
        if len(closed) >= limit and net <= 0:
            return f"concluded after {len(closed)} closed trades of both legs with a net result of {net:+.2f} USD"
        return ""

    @staticmethod
    def _note_autonomous_review(signal: dict) -> None:
        """Tell a rule source that one of its candidates was sent to review."""
        source = str((signal or {}).get("autonomous_source") or "").lower()
        try:
            if source == XS_MOMENTUM_SOURCE:
                get_xs_momentum_basket().note_reviewed((signal or {}).get("id"))
            elif source == STRATEGY_LAB_SOURCE:
                get_lab_baskets().note_reviewed((signal or {}).get("id"))
            elif source == LEARNING_SOURCE:
                worker = get_learning_worker()
                if worker is not None:
                    worker.book.note_reviewed((signal or {}).get("id"))
        except Exception as exc:
            logger.warning("AgentManager: rule-basket review note failed: %s", exc)

    def _autonomous_monitor_once(self):
        if not self._bot_mode_active():
            return {"status": "bot_stopped", "candidate_count": 0, "submitted": 0}
        exec_agent = self.agents.get("execution")
        if not exec_agent or not exec_agent.is_running:
            return {"status": "execution_agent_stopped", "candidate_count": 0, "submitted": 0}

        exec_cfg = self._execution_config()
        if not bool(exec_cfg.get("autonomous_candidates_enabled", False)):
            return {"status": "disabled", "candidate_count": 0, "submitted": 0}

        sources = exec_cfg.get("autonomous_sources") if isinstance(exec_cfg.get("autonomous_sources"), dict) else {}
        max_candidates_raw = exec_cfg.get("max_autonomous_candidates_per_cycle", 4)
        max_attempts_raw = exec_cfg.get("max_autonomous_trades_per_cycle", 1)
        parsed_max_candidates = int(max_candidates_raw if max_candidates_raw is not None else 4)
        parsed_max_attempts = int(max_attempts_raw if max_attempts_raw is not None else 1)
        max_candidates = None if parsed_max_candidates <= 0 else max(1, parsed_max_candidates)
        max_attempts = None if parsed_max_attempts <= 0 else max(1, parsed_max_attempts)
        cycle_limits = {
            "candidate_reviews_per_cycle": max(0, parsed_max_candidates),
            "trade_submissions_per_cycle": max(0, parsed_max_attempts),
            "scanner_symbols_per_bucket": max(
                1,
                int(exec_cfg.get("autonomous_scanner_max_symbols_per_bucket", 25) or 25),
            ),
            "watchlist_symbols": max(
                1,
                int(exec_cfg.get("autonomous_watchlist_max_symbols", 100) or 100),
            ),
        }
        cooldown_minutes = float(exec_cfg.get("autonomous_cooldown_minutes", 45.0) or 45.0)
        allow_scale_in = bool(exec_cfg.get("autonomous_allow_scale_in", False))
        allocation_policy = _autonomous_allocation_policy(exec_cfg)
        # Per-ticker post-close re-entry lockout (Capital Profitability A/B,
        # 2026-07). Default 6h; set to 0 to disable. See
        # _has_recent_close_lockout docstring for the before/after math.
        ticker_lockout_hours = float(exec_cfg.get("autonomous_ticker_lockout_hours", 6.0) or 0.0)

        self._autonomous_source_stats = {}
        candidates: List[dict] = []

        # Forward V2 and cross-sectional discovery are independent network
        # workloads. Evaluate them together, then stamp scanner candidates as
        # close as possible to admission so their freshness reflects decision
        # time rather than the start of a potentially slow research cycle.
        enabled_research_sources = {
            "v2_forward": (
                bool(sources.get("v2_forward", False)),
                self._forward_v2_candidates,
            ),
            "cross_sectional_momentum": (
                bool(sources.get("cross_sectional_momentum", False)),
                self._cross_sectional_momentum_candidates,
            ),
        }
        research_results: Dict[str, List[dict]] = {}
        active_sources = {
            name: builder
            for name, (enabled, builder) in enabled_research_sources.items()
            if enabled
        }
        if active_sources:
            with ThreadPoolExecutor(max_workers=len(active_sources), thread_name_prefix="autonomous-source") as pool:
                futures = {
                    name: pool.submit(builder, exec_cfg)
                    for name, builder in active_sources.items()
                }
                for name, future in futures.items():
                    try:
                        research_results[name] = future.result()
                    except Exception as exc:
                        logger.warning("AgentManager: autonomous source %s failed: %s", name, exc)
                        research_results[name] = []
                        self._autonomous_source_stats[name] = {
                            "enabled": True,
                            "candidate_count": 0,
                            "status": "error",
                            "reason": str(exc),
                        }
        for name, (enabled, _builder) in enabled_research_sources.items():
            if enabled:
                candidates.extend(research_results.get(name, []))
            else:
                self._autonomous_source_stats[name] = {"enabled": False, "candidate_count": 0}

        if bool(sources.get("scanner", True)):
            scanner_candidates = self._scanner_candidates(exec_cfg)
            scanner_stats = dict(self._autonomous_source_stats.get("scanner") or {})
            scanner_stats.update({"enabled": True, "candidate_count": len(scanner_candidates)})
            self._autonomous_source_stats["scanner"] = scanner_stats
            candidates.extend(scanner_candidates)
        else:
            self._autonomous_source_stats["scanner"] = {"enabled": False, "candidate_count": 0}
            self._autonomous_source_stats["watchlist"] = {"enabled": False, "candidate_count": 0}
        if bool(sources.get("pre_earnings", True)):
            pre_earnings_candidates = self._pre_earnings_candidates(exec_cfg)
            self._autonomous_source_stats.setdefault("pre_earnings", {
                "enabled": True,
                "candidate_count": len(pre_earnings_candidates),
            })
            candidates.extend(pre_earnings_candidates)
        else:
            self._autonomous_source_stats["pre_earnings"] = {"enabled": False, "candidate_count": 0}
        # Event rule (paper experiment): short after a Binance spot delisting notice.
        if bool(sources.get("delist_notice", True)):
            candidates.extend(self._delist_notice_candidates(exec_cfg))
        else:
            self._autonomous_source_stats["delist_notice"] = {"enabled": False, "candidate_count": 0}
        # Factor rule (paper experiment): weekly cross-sectional momentum basket.
        if bool(sources.get(XS_MOMENTUM_SOURCE, True)):
            candidates.extend(self._xs_momentum_basket_candidates(exec_cfg))
        else:
            self._autonomous_source_stats[XS_MOMENTUM_SOURCE] = {"enabled": False, "candidate_count": 0}
        # Learning mode (paper): trades Opus chose on a mover or a headline.
        if bool(sources.get(LEARNING_SOURCE, True)):
            candidates.extend(self._learning_candidates(exec_cfg))
        else:
            self._autonomous_source_stats[LEARNING_SOURCE] = {"enabled": False, "candidate_count": 0}
        # Strategy Lab (paper experiments): members of the rules the lab found and is trading.
        if bool(sources.get(STRATEGY_LAB_SOURCE, True)):
            candidates.extend(self._strategy_lab_candidates(exec_cfg))
        else:
            self._autonomous_source_stats[STRATEGY_LAB_SOURCE] = {"enabled": False, "candidate_count": 0}

        # Tokenised stocks and stablecoin pairs are not autonomous entries
        # (autonomous.exclude_tokenized_stocks / exclude_stablecoins). One
        # filter for every source: scanner hot markets, Watchlist crypto, V2
        # forward and cross-sectional candidates all pass through here.
        excluded_instruments: List[dict] = []
        exclusion_policy, instrument_tags = self._instrument_exclusion_inputs()
        if candidates and any(exclusion_policy.values()):
            kept: List[dict] = []
            for signal in candidates:
                exclusion = autonomous_instrument_exclusion(signal, exclusion_policy, instrument_tags)
                if exclusion is None:
                    kept.append(signal)
                    continue
                excluded_instruments.append(
                    {
                        "ticker": exclusion.get("ticker") or signal.get("ticker"),
                        "reason": exclusion.get("reason"),
                        "instrument_type": exclusion.get("instrument_type"),
                        "underlying": exclusion.get("underlying"),
                        "source": signal.get("autonomous_source") or signal.get("source"),
                        "detail": exclusion.get("detail"),
                    }
                )
                self._record_instrument_exclusion(signal, exclusion, stage="candidate_filter")
            candidates = kept
        self._autonomous_source_stats["instrument_exclusions"] = {
            "policy": dict(exclusion_policy),
            "excluded_count": len(excluded_instruments),
            "excluded": excluded_instruments[:25],
        }

        candidates, concluded_experiments = self._drop_concluded_experiments(candidates)
        self._autonomous_source_stats["concluded_experiments"] = {
            "left_out": sum(concluded_experiments.values()),
            "by_strategy": dict(concluded_experiments),
            "note": "these paper experiments used up their budget without a net edge; their candidates are not reviewed",
        }

        if not candidates:
            return {
                "status": "no_candidates",
                "candidate_count": 0,
                "submitted": 0,
                "excluded_instruments": excluded_instruments[:25],
                "sources": dict(self._autonomous_source_stats),
                "limits": cycle_limits,
                "allocation": {
                    **allocation_policy,
                    "target_bucket": None,
                    "actual_buckets": [],
                    "candidate_counts": {bucket: 0 for bucket in _AUTONOMOUS_ALLOCATION_BUCKETS},
                    "fallback_used": False,
                },
            }

        candidate_counts = {bucket: 0 for bucket in _AUTONOMOUS_ALLOCATION_BUCKETS}
        for signal in candidates:
            candidate_counts[_candidate_allocation_bucket(signal)] += 1
        allocation_target = self._next_autonomous_allocation_bucket(allocation_policy)
        allocation_order = list(_AUTONOMOUS_ALLOCATION_BUCKETS)
        if allocation_target:
            current_weights = dict(getattr(self, "_autonomous_allocation_current", {}) or {})
            allocation_order = [allocation_target] + sorted(
                (bucket for bucket in _AUTONOMOUS_ALLOCATION_BUCKETS if bucket != allocation_target),
                key=lambda bucket: current_weights.get(bucket, 0.0),
                reverse=True,
            )

        def _candidate_priority(signal: dict) -> tuple[int, int, int, float]:
            allowed, _reason = self._autonomous_trade_allowed_now(signal)
            ticker = str((signal or {}).get("ticker") or "").upper().strip()
            asset_class = str((signal or {}).get("asset_class") or "").upper().strip()
            crypto = is_crypto_ticker(ticker) or "CRYPTO" in asset_class
            prefer_us_stocks = bool(exec_cfg.get("research_prefer_us_stocks", False))
            if allocation_policy.get("enabled"):
                bucket = _candidate_allocation_bucket(signal)
                allocation_priority = len(allocation_order) - allocation_order.index(bucket)
            else:
                allocation_priority = 1 if (not crypto if prefer_us_stocks else crypto) else 0
            return (
                1 if allowed else 0,
                self._learning_turn(signal),
                allocation_priority,
                float((signal or {}).get("signal_score", 0.0) or 0.0),
            )

        candidates.sort(key=_candidate_priority, reverse=True)
        attempts = 0
        reviewed = 0
        skipped = {
            "market_unavailable": 0,
            "cooldown": 0,
            "open_position_conflict": 0,
            "ticker_lockout": 0,
            "allocation_bucket": 0,
            "excluded_instrument": len(excluded_instruments),
            "experiment_concluded": sum(concluded_experiments.values()),
        }
        actual_buckets: List[str] = []
        for signal in candidates:
            if max_attempts is not None and attempts >= max_attempts:
                break
            if max_candidates is not None and reviewed >= max_candidates:
                break
            allocation_bucket = _candidate_allocation_bucket(signal)
            if (
                allocation_policy.get("enabled")
                and allocation_target
                and allocation_bucket != allocation_target
                and not allocation_policy.get("fallback_when_empty", True)
            ):
                skipped["allocation_bucket"] += 1
                continue
            key = self._autonomous_key(signal)
            trade_allowed, blocked_reason = self._autonomous_trade_allowed_now(signal)
            if not trade_allowed:
                logger.info(
                    "AgentManager: autonomous candidate %s skipped before execution: %s",
                    signal.get("ticker"),
                    blocked_reason,
                )
                skipped["market_unavailable"] += 1
                continue
            if self._cooldown_active(key, cooldown_minutes):
                skipped["cooldown"] += 1
                continue
            if self._has_open_ticker_conflict(signal, allow_scale_in=allow_scale_in):
                self._autonomous_last_decisions[key] = time.time()
                skipped["open_position_conflict"] += 1
                self._note_basket_member_blocked(signal)
                continue
            # A rule basket closes everything before each ranking and re-enters the names that stay in it:
            # the re-entry is the rule itself, not the "chasing a ticker that just closed" the lockout is for.
            locked, lock_reason = (
                (False, "") if _is_rule_basket_in_paper(signal, exec_cfg)
                else self._has_recent_close_lockout(signal, ticker_lockout_hours)
            )
            if locked:
                logger.info(
                    "AgentManager: autonomous candidate %s skipped: %s",
                    signal.get("ticker"),
                    lock_reason,
                )
                self._autonomous_last_decisions[key] = time.time()
                skipped["ticker_lockout"] += 1
                continue

            signal["_autonomous_candidate"] = True
            signal["autonomous_allocation"] = {
                "bucket": allocation_bucket,
                "target_bucket": allocation_target,
                "target_pct": (allocation_policy.get("normalized_pct") or {}).get(allocation_bucket),
                "fallback_used": bool(allocation_target and allocation_bucket != allocation_target),
            }
            self._attach_candidate_price_integrity(signal)
            # Stamp the per-ticker learning adjustment on the candidate so the
            # candidate-card chip can render it pre-execution. execution_agent
            # recomputes a fresh adjustment for its own gating (closed positions may
            # change between monitor and execution), but this snapshot drives the UI.
            try:
                from core.capital_manager import get_capital_manager as _get_cm
                _closed_positions = _get_cm().get_positions() or []
                signal["ticker_learning"] = get_ticker_adjustment(
                    str(signal.get("ticker") or ""),
                    str(signal.get("action") or signal.get("direction") or ""),
                    _closed_positions,
                    exec_cfg,
                )
            except Exception:
                signal["ticker_learning"] = None
            reviewed += 1
            record_trade_event(
                event_type="autonomous_candidate",
                status="reviewing",
                ticker=signal.get("ticker", ""),
                signal_id=signal.get("id", ""),
                strategy_type=signal.get("strategy_type", ""),
                summary="Autonomous Scanner/Watchlist/Pre-Earnings candidate sent to risk and AI review",
                payload={"signal": {k: v for k, v in signal.items() if not str(k).startswith("_")}},
            )
            self.on_new_signal(signal)
            self._note_autonomous_review(signal)
            self._last_review_was_learning = _is_learning(signal)
            self._autonomous_last_decisions[key] = time.time()
            attempts += 1
            actual_buckets.append(allocation_bucket)
        return {
            "status": "reviewed" if reviewed else "no_eligible_candidates",
            "candidate_count": len(candidates),
            "submitted": attempts,
            "reviewed": reviewed,
            "skipped": skipped,
            "excluded_instruments": excluded_instruments[:25],
            "sources": dict(self._autonomous_source_stats),
            "limits": cycle_limits,
            "allocation": {
                **allocation_policy,
                "target_bucket": allocation_target,
                "actual_buckets": actual_buckets,
                "candidate_counts": candidate_counts,
                "fallback_used": bool(
                    allocation_target
                    and any(bucket != allocation_target for bucket in actual_buckets)
                ),
            },
        }

    def run_autonomous_cycle_now(self) -> Dict[str, Any]:
        """Run one autonomous review cycle without overlapping the background loop.

        This is an operator control-plane action. It preserves every normal
        evidence gate and only makes the existing scanner/pre-earnings review
        run immediately instead of waiting for the next interval.
        """
        if not self._autonomous_cycle_lock.acquire(blocking=False):
            return {"status": "already_running", "accepted": False}
        try:
            cycle = self._autonomous_monitor_once()
            return {"status": "completed", "accepted": True, "cycle": cycle or {}}
        except Exception as exc:
            logger.error("AgentManager: manual autonomous cycle failed: %s", exc)
            return {"status": "failed", "accepted": False, "reason": str(exc)}
        finally:
            self._autonomous_cycle_lock.release()

    def _ensure_autonomous_signal_registered(self, signal_data: dict) -> bool:
        if not bool((signal_data or {}).get("_autonomous_candidate")):
            return True
        try:
            from core.signals import get_signal_manager

            sid = str((signal_data or {}).get("id") or "").strip()
            if not sid:
                return False
            payload = {k: v for k, v in dict(signal_data or {}).items() if not str(k).startswith("_")}
            payload["size_factor"] = float(
                payload.get("adjusted_size_factor", payload.get("size_factor", 1.0)) or 1.0
            )
            sm = get_signal_manager()
            tkr = str(payload.get("ticker") or "").upper().strip()
            same_signal = sm.open_signals.get(sid)
            if isinstance(same_signal, dict) and same_signal.get("paper_position_id"):
                for key in ("invested_amount", "base_risk_pct", "risk_pct_used", "size_factor"):
                    if key in same_signal:
                        signal_data[key] = same_signal[key]
                return True
            if tkr:
                for osid, osig in sm.open_signals.items():
                    if (
                        isinstance(osig, dict)
                        and str(osig.get("ticker") or osig.get("symbol") or "").upper().strip() == tkr
                        and osig.get("paper_position_id")
                        and osid != sid
                    ):
                        logger.info(
                            "AgentManager: %s already has paper-linked signal %s; candidate %s is not registered",
                            tkr, osid, sid,
                        )
                        return False
                superseded = sm.supersede_unexecuted_autonomous_signals(tkr, sid)
                if superseded:
                    logger.info(
                        "AgentManager: archived %s obsolete autonomous signal(s) for %s before %s",
                        superseded, tkr, sid,
                    )

            # Re-adding the same id refreshes its gate, timestamp, evidence, and
            # levels. A newly minted id replaces obsolete ticker decisions above.
            sm.add_signal(payload)
            stored = sm.open_signals.get(sid, {})
            if isinstance(stored, dict):
                for key in ("invested_amount", "base_risk_pct", "risk_pct_used", "size_factor"):
                    if key in stored:
                        signal_data[key] = stored[key]
            return True
        except Exception as exc:
            logger.warning(f"AgentManager: could not register autonomous signal: {exc}")
            return False

    def _attempt_research_cohort_entry(
        self,
        signal_data: dict,
        risk_result: dict,
        execution_cfg: dict,
        capital_positions: list,
        capital_entry_gate: dict,
    ) -> dict:
        """Open a bounded forward-paper cohort position, never a Capital order.

        This path intentionally runs only after the executable Capital gate
        rejects the candidate. It enforces the asset-appropriate News policy
        and is excluded from the performance cache on close.
        """
        from core.research_cohort import (
            evaluate_research_cohort_entry,
            research_cohort_config,
            research_cohort_notional,
        )

        cfg = research_cohort_config(execution_cfg)
        if not bool(cfg.get("enabled", False)):
            return {"opened": False, "attempted": False, "status": "disabled"}

        # A forward-paper cohort still requires a verified executable market
        # price. If normal Capital admission already failed its data gate, stop
        # before News/AI work and report the real blocker instead of converting
        # the later open_position failure into a generic exception.
        if str((capital_entry_gate or {}).get("check") or "").lower() == "data":
            gate_reason = str(
                (capital_entry_gate or {}).get("reason")
                or "Verified live execution-price evidence is unavailable"
            )
            reason = f"Research paper entry also requires verified execution data: {gate_reason}"
            record_trade_event(
                event_type="research_cohort_skipped",
                status="data_gate",
                ticker=signal_data.get("ticker", ""),
                signal_id=signal_data.get("id", ""),
                strategy_type=signal_data.get("strategy_type", ""),
                summary=reason,
                payload={"capital_entry_gate": capital_entry_gate},
            )
            return {
                "opened": False,
                "attempted": True,
                "status": "data_gate",
                "reason": reason,
            }

        # §45/§74 autonomy gate — a forward-paper cohort order is still an autonomous
        # order; it requires a mode that allows paper orders (PAPER_AUTO+). Placed
        # after the data gate so the data-gate blocker is reported when it applies.
        _allowed, _gate_reason = self._autonomy_order_gate(execution_cfg)
        if not _allowed:
            reason = f"Autonomy gate blocked research-cohort order: {_gate_reason}"
            record_trade_event(
                event_type="research_cohort_skipped",
                status="autonomy_blocked",
                ticker=signal_data.get("ticker", ""),
                signal_id=signal_data.get("id", ""),
                strategy_type=signal_data.get("strategy_type", ""),
                summary=reason,
                payload={"capital_entry_gate": capital_entry_gate, "gate_reason": _gate_reason},
            )
            return {"opened": False, "attempted": True, "status": "autonomy_blocked", "reason": reason}

        try:
            decision = evaluate_research_cohort_entry(
                signal_data,
                execution_cfg,
                capital_positions,
            )
            news_gate = decision.get("news_gate") if isinstance(decision.get("news_gate"), dict) else None
            if isinstance(news_gate, dict):
                record_autonomous_news_evidence(
                    signal_data,
                    news_gate,
                    evaluation_stage="research_cohort",
                )
            if not decision.get("allowed"):
                record_trade_event(
                    event_type="research_cohort_skipped",
                    status=str(decision.get("status") or "rejected"),
                    ticker=signal_data.get("ticker", ""),
                    signal_id=signal_data.get("id", ""),
                    strategy_type=signal_data.get("strategy_type", ""),
                    summary=str(decision.get("reason") or "Research cohort admission rejected"),
                    payload={"research_cohort": decision, "capital_entry_gate": capital_entry_gate},
                )
                return {"opened": False, "attempted": True, "decision": decision}

            from core.capital_manager import get_capital_manager, resolve_deployable_balance
            from core.signals import get_signal_manager

            capital_manager = get_capital_manager()
            deployable_balance = resolve_deployable_balance(capital_manager)
            notional = research_cohort_notional(deployable_balance, execution_cfg)
            minimum_notional = max(0.0, float(cfg.get("min_notional_usd", 20.0) or 20.0))
            if notional < minimum_notional:
                reason = (
                    f"Research cohort notional ${notional:.2f} is below the configured "
                    f"minimum ${minimum_notional:.2f}"
                )
                record_trade_event(
                    event_type="research_cohort_skipped",
                    status="insufficient_capital",
                    ticker=signal_data.get("ticker", ""),
                    signal_id=signal_data.get("id", ""),
                    strategy_type=signal_data.get("strategy_type", ""),
                    summary=reason,
                    payload={
                        "research_cohort": decision,
                        "available_balance": deployable_balance,
                    },
                )
                return {"opened": False, "attempted": True, "decision": decision, "reason": reason}

            signal_data["research_only"] = True
            signal_data["capital_execution_eligible"] = False
            signal_data["capital_execution_block_reason"] = str(
                capital_entry_gate.get("reason") or "Not eligible for executable Capital"
            )
            decision_context = build_signal_decision_context(
                signal_data,
                decision_status="accepted",
                risk_evaluation=risk_result,
            )
            decision_context["capital_entry_gate"] = capital_entry_gate
            decision_context["research_cohort"] = decision
            decision_context.setdefault("execution", {}).update(
                {
                    "mode": "paper",
                    "position_type": "research_cohort",
                    "research_only": True,
                    "notional_usd": notional,
                }
            )
            signal_data["decision_context"] = decision_context
            signal_data["entry_reason"] = "Forward paper research cohort; excluded from Capital validation"
            if not self._ensure_autonomous_signal_registered(signal_data):
                raise RuntimeError("autonomous signal registration failed")

            metrics = decision.get("metrics") if isinstance(decision.get("metrics"), dict) else {}
            thresholds = decision.get("thresholds") if isinstance(decision.get("thresholds"), dict) else {}
            ollama_review = {}
            if bool(cfg.get("ai_review_enabled", False)):
                try:
                    review_config = getattr(self.config_manager, "config", {})
                    if isinstance(review_config, dict):
                        review_config = dict(review_config)
                        ollama_cfg = dict(review_config.get("ollama") or {})
                        timeout = float(
                            execution_cfg.get(
                                "ai_review_timeout_seconds",
                                ollama_cfg.get("decision_timeout_seconds", 6.0),
                            )
                            or 6.0
                        )
                        ollama_cfg["decision_timeout_seconds"] = max(3.0, min(120.0, timeout))
                        review_config["ollama"] = ollama_cfg
                    _attach_etoro_context_for_ai(signal_data)
                    ollama_review = evaluate_trade_decision_with_ollama(
                        signal_data,
                        risk_result,
                        config=review_config,
                    )
                except Exception as exc:
                    logger.warning("Research cohort AI review failed for %s: %s", signal_data.get("ticker"), exc)
                    ollama_review = {"available": False, "reason": str(exc), "fallback_used": "exception"}

                if bool(cfg.get("block_ai_veto", True)) and (
                    bool((ollama_review or {}).get("critic_veto"))
                    # A reply about another ticker / the opposite side is no
                    # review of this position at all.
                    or str((ollama_review or {}).get("fallback_used") or "") == "ai_reply_mismatch"
                    or should_veto_trade(ollama_review, getattr(self.config_manager, "config", {}))
                ):
                    reason = str(ollama_review.get("reason") or "AI review rejected the paper research position")
                    record_trade_event(
                        event_type="research_cohort_skipped",
                        status="ai_veto",
                        ticker=signal_data.get("ticker", ""),
                        signal_id=signal_data.get("id", ""),
                        strategy_type=signal_data.get("strategy_type", ""),
                        summary=reason,
                        payload={"research_cohort": decision, "ollama_review": ollama_review},
                    )
                    return {"opened": False, "attempted": True, "decision": decision, "reason": reason}

            if isinstance(ollama_review, dict) and ollama_review:
                decision_context["ollama"] = ollama_review
                execution_context = decision_context.setdefault("execution", {})
                if ollama_review.get("available"):
                    execution_context["analysis_provider"] = ollama_review.get("provider", "ollama")
                    execution_context["ai_available"] = True
                    execution_context["fallback_used"] = ""
                else:
                    execution_context["ai_available"] = False
                    execution_context["fallback_used"] = ollama_review.get("fallback_used") or "ollama_unavailable"

            position = capital_manager.open_position(
                ticker=str(signal_data.get("ticker") or ""),
                direction=str(decision.get("direction") or ""),
                size_usd=notional,
                stop_loss_pct=float(metrics.get("stop_loss_pct", 0.0) or 0.0) / 100.0,
                take_profit_pct=float(metrics.get("take_profit_pct", 0.0) or 0.0) / 100.0,
                signal_id=str(signal_data.get("id") or ""),
                note="Forward paper research cohort — not Capital executable",
                leverage=1,
                decision_context=decision_context,
                strategy_type=str(signal_data.get("strategy_type") or ""),
                entry_reason=signal_data["entry_reason"],
                position_type="research_cohort",
                research_only=True,
                expected_entry_price=metrics.get("expected_entry_price"),
                max_entry_deviation_pct=thresholds.get("max_entry_deviation_pct"),
            )
            get_signal_manager().attach_paper_position(str(signal_data.get("id") or ""), position)
            record_trade_event(
                event_type="research_cohort_position_opened",
                status="opened",
                ticker=signal_data.get("ticker", ""),
                signal_id=signal_data.get("id", ""),
                position_id=position.get("position_id", ""),
                strategy_type=signal_data.get("strategy_type", ""),
                summary="Forward paper research position opened; excluded from Capital validation",
                payload={
                    "position": dict(position),
                    "research_cohort": decision,
                    "capital_entry_gate": capital_entry_gate,
                },
            )
            logger.info(
                "Research cohort opened %s %s at verified execution price %.8f",
                position.get("direction"),
                position.get("ticker"),
                float(position.get("entry_price", 0.0) or 0.0),
            )
            return {"opened": True, "attempted": True, "decision": decision, "position": position}
        except Exception as exc:
            logger.warning("Research cohort admission failed closed for %s: %s", signal_data.get("ticker"), exc)
            record_trade_event(
                event_type="research_cohort_skipped",
                status="error",
                ticker=signal_data.get("ticker", ""),
                signal_id=signal_data.get("id", ""),
                strategy_type=signal_data.get("strategy_type", ""),
                summary=f"Research cohort admission failed: {exc}",
                payload={"capital_entry_gate": capital_entry_gate},
            )
            return {"opened": False, "attempted": True, "status": "error", "reason": str(exc)}


    def on_new_signal(self, signal_data: dict):
        """Event handler for new signals."""
        signal_data = dict(signal_data)  # shallow copy to avoid mutating caller's state
        execution_cfg = self.config_manager.agents.get("execution", {}) if hasattr(self.config_manager, "agents") else {}
        if _is_scanner_signal(signal_data):
            # Scanner discovery is the trigger for ticker-specific News
            # hydration. This is data acquisition, not order authorization, so
            # it also runs while the master switch is off. Risk/Capital/AI then
            # consume the exact same persisted evidence snapshot when enabled.
            _attach_cached_news_context_for_ai(signal_data, execution_cfg)
            news_context = (
                signal_data.get("news_context")
                if isinstance(signal_data.get("news_context"), dict)
                else {}
            )
            hydration = (
                news_context.get("hydration")
                if isinstance(news_context.get("hydration"), dict)
                else {}
            )
            record_trade_event(
                event_type="scanner_news_hydrated",
                status=str(hydration.get("status") or news_context.get("status") or "unknown"),
                ticker=signal_data.get("ticker", ""),
                signal_id=signal_data.get("id", ""),
                strategy_type=signal_data.get("strategy_type", ""),
                summary=(
                    f"Scanner requested ticker news before AI review; "
                    f"{news_context.get('article_count_24h', 0)} article(s) available in 24h"
                ),
                payload={"news_context": news_context},
            )

        if not self._bot_mode_active():
            record_trade_event(
                event_type="execution_skipped",
                status="bot_stopped",
                ticker=signal_data.get("ticker", ""),
                signal_id=signal_data.get("id", ""),
                strategy_type=signal_data.get("strategy_type", ""),
                summary="Bot master switch is off",
                payload={"master_switch": "off"},
            )
            return

        # Tokenised stocks / stablecoin pairs never open a new automated paper
        # position, whichever path produced the signal. Open positions in them
        # are not affected: exits do not pass through this handler.
        try:
            exclusion_policy, instrument_tags = self._instrument_exclusion_inputs()
            instrument_exclusion = (
                autonomous_instrument_exclusion(signal_data, exclusion_policy, instrument_tags)
                if any(exclusion_policy.values())
                else None
            )
        except Exception as exc:
            logger.debug("AgentManager: instrument exclusion check failed: %s", exc)
            instrument_exclusion = None
        if instrument_exclusion is not None:
            signal_data["instrument_exclusion"] = instrument_exclusion
            self._record_instrument_exclusion(signal_data, instrument_exclusion, stage="entry_handler")
            return

        self._apply_signal_size_caps(signal_data)
        risk_result = {
            "accepted": True,
            "reason": "risk_agent_not_running",
            "reasons": [],
            "checks": [],
            "metrics": {},
        }
        risk_agent = self.agents.get("risk")
        if risk_agent and risk_agent.is_running:
            risk_result = risk_agent.evaluate_signal(signal_data)

        # --- Multi-agent evaluation ---
        ticker = str(signal_data.get("ticker", "")).upper().strip()

        sentiment_score = 0.0
        sentiment_label = "neutral"
        sentiment_reason = "sentiment_agent_not_running"
        sentiment_agent = self.agents.get("sentiment")
        if sentiment_agent and sentiment_agent.is_running:
            try:
                s = sentiment_agent.get_sentiment()
                sentiment_score = float(s.get("score", 0.0))
                sentiment_label = str(s.get("sentiment", "neutral"))
                sentiment_reason = f"sentiment={sentiment_label}({sentiment_score:.2f})"
            except Exception as e:
                logger.warning(f"Sentiment evaluation failed: {e}")
                sentiment_reason = f"sentiment_error:{e}"

        try:
            gap_risk = int(signal_data.get("gap_risk", 0) or 0)
        except Exception:
            gap_risk = 0
        pre_earnings_reason = f"provided_gap_risk={gap_risk}" if gap_risk > 0 else "pre_earnings_agent_not_available"
        try:
            from agents.pre_earnings_agent import get_pre_earnings_agent_if_ready
            pea = get_pre_earnings_agent_if_ready()
            if pea is not None:
                try:
                    state = pea.get_state()
                    items = state.get("items", []) if isinstance(state, dict) else []
                    for item in items:
                        if isinstance(item, dict) and str(item.get("symbol", "")).upper().strip() == ticker:
                            gap_risk = int(item.get("gap_risk", 0) or 0)
                            pre_earnings_reason = f"gap_risk={gap_risk}"
                            break
                    else:
                        if gap_risk <= 0:
                            pre_earnings_reason = "ticker_not_in_pre_earnings"
                except Exception as e:
                    logger.warning(f"Pre-earnings state lookup failed: {e}")
                    pre_earnings_reason = f"pre_earnings_lookup_error:{e}"
            else:
                if gap_risk <= 0:
                    pre_earnings_reason = "pre_earnings_agent_not_ready"
        except Exception as e:
            logger.warning(f"Pre-earnings evaluation failed: {e}")
            pre_earnings_reason = f"pre_earnings_error:{e}"

        opportunity = 0.0
        trade_radar_reason = "trade_radar_agent_not_available"
        try:
            from agents.trade_radar_agent import get_trade_radar_agent_if_ready
            tra = get_trade_radar_agent_if_ready()
            if tra is not None:
                try:
                    state = tra.get_state(limit=50)
                    candidates = state.get("candidates", []) if isinstance(state, dict) else []
                    opportunity, trade_radar_reason = _trade_radar_opportunity(candidates, ticker)
                except Exception as e:
                    logger.warning(f"Trade radar state lookup failed: {e}")
                    trade_radar_reason = f"trade_radar_lookup_error:{e}"
            else:
                trade_radar_reason = "trade_radar_agent_not_ready"
        except Exception as e:
            logger.warning(f"Trade radar evaluation failed: {e}")
            trade_radar_reason = f"trade_radar_error:{e}"

        signal_score = self._effective_signal_score(signal_data)
        if signal_score > 0:
            signal_data["signal_score"] = signal_score
            signal_data["score"] = signal_score
            signal_data.setdefault("raw_signal_score", signal_data.get("raw_score", signal_score))
        normalized_sentiment = ((sentiment_score + 1.0) / 2.0) * 100.0
        if str(signal_data.get("autonomous_source") or "").lower().strip() == "pre_earnings":
            normalized_pre_earnings = float(gap_risk)
        else:
            normalized_pre_earnings = 100.0 - gap_risk
        composite = (signal_score * 0.50) + (normalized_sentiment * 0.20) + (normalized_pre_earnings * 0.15) + (opportunity * 0.15)

        signal_data["sentiment_score"] = round(sentiment_score, 3)
        signal_data["sentiment_label"] = sentiment_label
        signal_data["gap_risk"] = gap_risk
        signal_data["trade_radar_opportunity"] = round(opportunity, 2)
        signal_data["composite_score"] = round(composite, 2)
        signal_data["agent_reasons"] = [sentiment_reason, pre_earnings_reason, trade_radar_reason]

        ollama_cfg = getattr(self.config_manager, "config", {}).get("ollama", {}) if isinstance(getattr(self.config_manager, "config", {}), dict) else {}
        confidence_result = None
        perf_cache = None
        profitability_gate = None
        news_gate = None
        capital_entry_gate = None
        capital_positions = []

        # Every order produced by the engine follows the same fail-closed route.
        # Visibility in Signals is intentionally separate from eligibility to open
        # Capital: exploratory V1/legacy output remains observable but cannot
        # bypass data, faithful OOS, realised-edge, or AI News evidence.
        if risk_result.get("accepted"):
            try:
                from core.capital_manager import get_capital_manager

                signal_data["_capital_automated_entry"] = True
                capital_positions = get_capital_manager().get_positions()
                capital_entry_gate = evaluate_capital_entry(
                    signal_data,
                    capital_positions,
                    getattr(self.config_manager, "config", None) or execution_cfg,
                )
                signal_data["capital_entry_gate"] = capital_entry_gate
                checks = capital_entry_gate.get("checks", {}) if isinstance(capital_entry_gate, dict) else {}
                if bool(capital_entry_gate.get("paper_capital_experiment")):
                    signal_data["paper_capital_experiment"] = True
                    signal_data["research_only"] = False
                    signal_data["capital_execution_eligible"] = True
                    signal_data["capital_execution_block_reason"] = ""
                    signal_data["paper_capital_experiment_reason"] = str(
                        capital_entry_gate.get("reason") or "S7 admitted to the paper Capital experiment"
                    )
                profitability_gate = checks.get("realized_performance")
                news_gate = checks.get("news")
                if isinstance(news_gate, dict) and isinstance(news_gate.get("context"), dict):
                    signal_data["news_score"] = news_gate["context"].get("score", signal_data.get("news_score", 0))
                    signal_data["news_sentiment"] = news_gate["context"].get("sentiment", signal_data.get("news_sentiment", "unknown"))
                    signal_data["top_headlines"] = list(news_gate["context"].get("top_headlines") or [])
                    signal_data["news_headlines"] = [
                        str(item.get("title") or "")
                        for item in signal_data["top_headlines"]
                        if isinstance(item, dict) and str(item.get("title") or "").strip()
                    ]
                if not capital_entry_gate.get("allowed"):
                    decision_context = build_signal_decision_context(
                        signal_data,
                        decision_status="rejected",
                        risk_evaluation=risk_result,
                    )
                    decision_context["capital_entry_gate"] = capital_entry_gate
                    if isinstance(profitability_gate, dict):
                        decision_context["autonomous_profitability_gate"] = profitability_gate
                    if isinstance(news_gate, dict):
                        decision_context["autonomous_news_gate"] = news_gate
                    failed_check = str(capital_entry_gate.get("check") or "evidence")
                    event_status = {
                        "realized_performance": "profitability_gate",
                        "news": "news_gate",
                        "backtest": "backtest_gate",
                        "data": "data_gate",
                    }.get(failed_check, "capital_entry_gate")
                    research_cohort = self._attempt_research_cohort_entry(
                        signal_data,
                        risk_result,
                        execution_cfg,
                        capital_positions,
                        capital_entry_gate,
                    )
                    if research_cohort.get("opened"):
                        record_trade_event(
                            event_type="capital_execution_blocked",
                            status=event_status,
                            ticker=signal_data.get("ticker", ""),
                            signal_id=signal_data.get("id", ""),
                            strategy_type=decision_context.get("strategy_type", ""),
                            summary=(
                                "Executable Capital entry blocked; a separately labelled "
                                "forward paper research position was opened"
                            ),
                            payload={
                                "decision_context": decision_context,
                                "risk_evaluation": risk_result,
                                "capital_entry_gate": capital_entry_gate,
                                "research_cohort": research_cohort,
                            },
                        )
                        return
                    record_trade_event(
                        event_type="signal_skipped",
                        status=event_status,
                        ticker=signal_data.get("ticker", ""),
                        signal_id=signal_data.get("id", ""),
                        strategy_type=decision_context.get("strategy_type", ""),
                        summary=capital_entry_gate.get("reason", "Capital entry evidence blocked execution"),
                        payload={
                            "decision_context": decision_context,
                            "risk_evaluation": risk_result,
                            "capital_entry_gate": capital_entry_gate,
                            "research_cohort": research_cohort,
                        },
                    )
                    logger.info(
                        "Signal %s blocked by %s gate: %s",
                        signal_data.get("ticker"),
                        failed_check,
                        capital_entry_gate.get("reason"),
                    )
                    return
            except Exception as exc:
                logger.error("Capital entry evidence gate failed closed: %s", exc)
                record_trade_event(
                    event_type="signal_skipped",
                    status="capital_entry_gate_error",
                    ticker=signal_data.get("ticker", ""),
                    signal_id=signal_data.get("id", ""),
                    strategy_type=str(signal_data.get("strategy_type") or ""),
                    summary=f"Capital entry evidence unavailable: {exc}",
                    payload={"risk_evaluation": risk_result},
                )
                return

        if risk_result.get("accepted"):
            try:
                if hasattr(self, "_perf_cache") and self._perf_cache is not None:
                    perf_cache = self._perf_cache
                else:
                    from core.strategy_v2.performance_cache import StrategyPerformanceCache
                    self._perf_cache = StrategyPerformanceCache()
                    perf_cache = self._perf_cache

                confidence_result = self.confidence_gates.evaluate(signal_data, perf_cache=perf_cache)
                if confidence_result.get("tier") == "SKIP":
                    signal_data["confidence_tier"] = "SKIP"
                    signal_data["adjusted_size_factor"] = 0.0
                    signal_data["confidence_reasons"] = confidence_result.get("reasons", [])
                    decision_context = build_signal_decision_context(
                        signal_data,
                        decision_status="rejected",
                        risk_evaluation=risk_result,
                    )
                    decision_context["confidence"] = {
                        "tier": "SKIP",
                        "adjusted_size_factor": 0.0,
                        "reasons": list(confidence_result.get("reasons", [])),
                        "strategy_source": signal_data.get("strategy_source", "V1"),
                        # None when the setup has no validation: 0.0 would read as a measured score.
                        "validation_score": signal_data.get("validation_score"),
                    }
                    record_trade_event(
                        event_type="signal_skipped",
                        status="confidence_skip",
                        ticker=signal_data.get("ticker", ""),
                        signal_id=signal_data.get("id", ""),
                        strategy_type=decision_context.get("strategy_type", ""),
                        summary="; ".join(confidence_result.get("reasons", [])),
                        payload={
                            "decision_context": decision_context,
                            "risk_evaluation": risk_result,
                            "confidence_result": confidence_result,
                        },
                    )
                    logger.info(
                        f"Signal {signal_data.get('ticker')} skipped before AI review: "
                        f"{'; '.join(confidence_result.get('reasons', []))}"
                    )
                    _release_confidence_reservation(self.confidence_gates, signal_data, confidence_result)
                    return
            except Exception as e:
                logger.error(f"Pre-AI confidence gate error: {e}")
                record_trade_event(
                    event_type="signal_skipped",
                    status="confidence_error",
                    ticker=signal_data.get("ticker", ""),
                    signal_id=signal_data.get("id", ""),
                    strategy_type=str(signal_data.get("strategy_type") or ""),
                    summary=f"Confidence gate error: {e}",
                    payload={"risk_evaluation": risk_result},
                )
                return

        # §45/§46/§48/§74 autonomy gate — the final gate before autonomous order
        # preparation (AI review, sizing, placement). Fail-safe to ANALYZE; kill
        # switch blocks new automated risk; LIVE_AUTO is owner-only. Placed before
        # the AI review block so blocked orders do not burn an AI call.
        _allowed, _gate_reason = self._autonomy_order_gate(execution_cfg)
        if not _allowed:
            record_trade_event(
                event_type="autonomous_order_blocked",
                status=_gate_reason,
                ticker=signal_data.get("ticker", ""),
                signal_id=signal_data.get("id", ""),
                strategy_type=str(signal_data.get("strategy_type") or ""),
                summary=f"Autonomy gate blocked order: {_gate_reason}",
                payload={"risk_evaluation": risk_result},
            )
            logger.info("AgentManager: autonomy gate blocked %s: %s", signal_data.get("ticker"), _gate_reason)
            _release_confidence_reservation(self.confidence_gates, signal_data, confidence_result)
            return

        # Paper Scanner candidates bypass the normal Capital News gate by design,
        # but the AI/Critic review must still see cached ticker catalysts.  This
        # is especially important for post-earnings movers such as OKTA where the
        # price move itself was caused by a fresh report.
        _attach_cached_news_context_for_ai(signal_data, execution_cfg)
        _attach_etoro_context_for_ai(signal_data)

        # P2: attach the system's own realized base rates for this strategy so
        # the §41 reviewer calibrates on measured outcomes, not impressions.
        try:
            from core.ai.base_rates import strategy_base_rates

            base_rates = strategy_base_rates(str(signal_data.get("strategy_type") or ""))
            if base_rates:
                signal_data["strategy_base_rates"] = base_rates
        except Exception:
            pass  # evidence must never break the decision path
        # TradingAgents adoption: inject the latest realized-outcome lessons for
        # this strategy family (reflection loop output) into the §41 evidence.
        try:
            from core.ai.reflection import load_lessons

            lessons = load_lessons(str(signal_data.get("strategy_type") or "").split("_")[0])
            if lessons:
                signal_data["strategy_lessons"] = [
                    {"lesson": str(row.get("lesson") or "")[:400],
                     "ticker": row.get("ticker"),
                     "pnl_pct": row.get("pnl_pct")}
                    for row in lessons
                ]
        except Exception:
            pass  # evidence must never break the decision path
        # Self-measurement loop: the reviewer sees its own track record for this
        # strategy and, for crypto, the recorded futures positioning.
        _attach_learning_evidence(signal_data)

        ollama_review = {}
        _eval_result = None
        ai_entry_blocked = False
        ai_entry_block_reason = ""
        ai_entry_block_detail = ""
        # TradingAgents debate result — computed inside the review try block
        # (before decision_context exists) and attached to the context after
        # build_signal_decision_context below. Declared here so the debate can
        # never die on an UnboundLocalError (2026-09-03 audit P0: the verdict
        # was computed and then silently discarded on every call).
        debate_result = None
        if risk_result.get("accepted"):
            try:
                ollama_review_config = getattr(self.config_manager, "config", {})
                ai_required_for_timeout = (
                    bool(execution_cfg.get("require_ai_review", True))
                    or (
                        bool(signal_data.get("_autonomous_candidate"))
                        and bool(execution_cfg.get("autonomous_require_ai_review", True))
                    )
                )
                fallback_default_for_timeout = str(execution_cfg.get("mode", "paper")).lower() == "paper"
                fallback_allowed_for_timeout = bool(
                    execution_cfg.get("allow_signal_only_ai_fallback", fallback_default_for_timeout)
                )
                if ai_required_for_timeout and fallback_allowed_for_timeout and isinstance(ollama_review_config, dict):
                    ollama_review_config = dict(ollama_review_config)
                    ollama_cfg_for_timeout = dict(ollama_review_config.get("ollama") or {})
                    try:
                        decision_timeout = float(
                            execution_cfg.get(
                                "ai_review_timeout_seconds",
                                ollama_cfg_for_timeout.get("decision_timeout_seconds", 6.0),
                            )
                            or 6.0
                        )
                    except Exception:
                        decision_timeout = 6.0
                    ollama_cfg_for_timeout["decision_timeout_seconds"] = max(3.0, min(120.0, decision_timeout))
                    ollama_review_config["ollama"] = ollama_cfg_for_timeout

                from core.ai.decision_engine import evaluate_trade_decision as _evaluate_decision
                _eval_result = _evaluate_decision(signal_data, risk_result, config=ollama_review_config)
                ai_decision, ai_validation, ai_critic, ollama_review = _json_safe_ai_artifacts(_eval_result)
                # §41/§42/§44 audit enrichment (additive — review shape unchanged):
                signal_data["ai_decision"] = ai_decision
                signal_data["ai_validation"] = ai_validation
                if ai_critic is not None:
                    signal_data["ai_critic"] = ai_critic
                ai_entry_blocked, ai_entry_block_reason = _ai_entry_block(_eval_result, ollama_review)
                if not ai_entry_blocked:
                    pe_gate_reason = _pre_earnings_entry_gate(signal_data, ollama_review, execution_cfg)
                    if pe_gate_reason:
                        ai_entry_blocked = True
                        ai_entry_block_reason = pe_gate_reason
                if not ai_entry_blocked:
                    # Minimum AI confidence for every other entry
                    # (ai.decision.min_entry_confidence, default 0.5).
                    low_conf_reason, low_conf_detail = _ai_min_entry_confidence_gate(
                        signal_data, ollama_review, getattr(self.config_manager, "config", None)
                    )
                    if low_conf_reason:
                        ai_entry_blocked = True
                        ai_entry_block_reason = low_conf_reason
                        ai_entry_block_detail = low_conf_detail
                # TradingAgents adoption: bull/bear entry debate + research-manager
                # verdict on surviving entry proposals (pre-earnings scope by
                # default). Fail-open: debate unavailable → §41 verdict stands.
                if not ai_entry_blocked:
                    debate_due, _debate_why = _debate_should_run(signal_data, ollama_review, execution_cfg)
                    if debate_due:
                        try:
                            from core.ai.debate import run_entry_debate

                            debate_result = run_entry_debate(
                                signal_data,
                                ollama_review if isinstance(ollama_review, dict) else {},
                                config=getattr(self.config_manager, "config", {}),
                            )
                            if debate_result.get("available"):
                                verdict = debate_result.get("verdict") or {}
                                verdict_rec = str(verdict.get("recommendation") or "").lower()
                                # The manager verdict is the post-debate synthesis:
                                # watch/avoid blocks the entry; approve/reduce adopts
                                # the manager's confidence for the downstream gates.
                                if verdict_rec in {"watch", "avoid"}:
                                    ai_entry_blocked = True
                                    ai_entry_block_reason = f"ai_debate_{verdict_rec}"
                                elif isinstance(ollama_review, dict):
                                    # The manager's own confidence, including 0.0:
                                    # `x or reviewer` silently restored the reviewer's
                                    # higher figure when the manager rated its verdict 0.
                                    manager_confidence = verdict.get("confidence")
                                    if manager_confidence is None:
                                        manager_confidence = ollama_review.get("confidence") or 0.0
                                    ollama_review["confidence"] = float(manager_confidence)
                                    signal_data["ollama_confidence"] = ollama_review["confidence"]
                        except Exception as debate_exc:
                            logger.warning(
                                "Entry debate failed for %s (fail-open): %s",
                                signal_data.get("ticker"), debate_exc,
                            )
                        if not ai_entry_blocked:
                            # The debate may have replaced the confidence with
                            # the manager's own figure: the minimum applies to
                            # the confidence the entry is finally taken at.
                            low_conf_reason, low_conf_detail = _ai_min_entry_confidence_gate(
                                signal_data, ollama_review, getattr(self.config_manager, "config", None)
                            )
                            if low_conf_reason:
                                ai_entry_blocked = True
                                ai_entry_block_reason = low_conf_reason
                                ai_entry_block_detail = low_conf_detail
                if isinstance(ollama_review, dict):
                    signal_data["ollama_decision"] = ollama_review
                    signal_data["ollama_recommendation"] = ollama_review.get("recommendation")
                    signal_data["ollama_confidence"] = ollama_review.get("confidence")
                    signal_data["ai_leverage"] = ollama_review.get("leverage", 1)
                    if ollama_review.get("available"):
                        signal_data["agent_reasons"].append(
                            f"ollama={ollama_review.get('recommendation')}({float(ollama_review.get('confidence', 0.0) or 0.0):.2f}, x{int(float(ollama_review.get('leverage', 1) or 1))})"
                        )
                    else:
                        signal_data["agent_reasons"].append(f"ollama_unavailable:{ollama_review.get('reason', 'unknown')}")
            except Exception as e:
                logger.warning(f"Ollama decision review failed: {e}")
                ollama_review = {"available": False, "reason": str(e), "fallback_used": "exception"}

        ai_review_policy = _ai_review_availability_policy(
            signal_data,
            execution_cfg,
            ollama_cfg if isinstance(ollama_cfg, dict) else {},
            ollama_review,
        )
        ai_fallback_used = bool(ai_review_policy["fallback"])
        legacy_review_veto = (
            _eval_result is None
            and should_veto_trade(ollama_review, getattr(self.config_manager, "config", {}))
        )
        # Rule baskets measured as a whole (paper only): the AI's rejection is
        # kept on record but does not stop the entry.
        ai_advisory_override = None
        if _advisory_override_applies(
            ai_entry_blocked or legacy_review_veto, ai_entry_block_reason, signal_data, execution_cfg
        ):
            ai_advisory_override = {
                "reason": ai_entry_block_reason or "legacy_ai_veto",
                "detail": ai_entry_block_detail,
                "policy": "rule basket: AI review is advisory in paper mode",
            }
            ai_entry_blocked = False
            ai_entry_block_reason = ""
            ai_entry_block_detail = ""
            legacy_review_veto = False
            signal_data["agent_reasons"].append(f"ai_advisory_overridden:{ai_advisory_override['reason']}")

        decision_context = build_signal_decision_context(
            signal_data,
            decision_status=(
                "rejected"
                if (
                    not risk_result.get("accepted")
                    or ai_entry_blocked
                    or legacy_review_veto
                    or ai_review_policy["blocked"]
                )
                else "accepted"
            ),
            risk_evaluation=risk_result,
        )
        if ai_advisory_override is not None:
            decision_context["ai_advisory_override"] = ai_advisory_override
            decision_context.setdefault("warnings", []).append(
                f"AI advised against this entry ({ai_advisory_override['reason']}); "
                "the basket rule enters anyway (paper experiment)"
            )
        if ai_entry_blocked and ai_entry_block_reason:
            decision_context["ai_entry_block"] = {
                "reason": ai_entry_block_reason,
                "detail": ai_entry_block_detail,
            }
            if ai_entry_block_detail:
                decision_context.setdefault("warnings", []).append(ai_entry_block_detail)
        # Attach the debate result now that the context exists (P0 fix: the
        # verdict used to be computed and then lost to an UnboundLocalError).
        if debate_result is not None:
            decision_context["ai_debate"] = debate_result
            if debate_result.get("available"):
                decision_context.setdefault("sizing", {})["post_debate_confidence"] = (
                    (debate_result.get("verdict") or {}).get("confidence")
                )
        # §41/§42/§44 audit enrichment — mirror engine artifacts into the context.
        if "ai_decision" in signal_data:
            decision_context["ai_decision"] = signal_data["ai_decision"]
            decision_context["ai_validation"] = signal_data.get("ai_validation")
            if "ai_critic" in signal_data:
                decision_context["ai_critic"] = signal_data["ai_critic"]
        if capital_entry_gate is not None:
            decision_context["capital_entry_gate"] = capital_entry_gate
        if bool((capital_entry_gate or {}).get("paper_capital_experiment")):
            decision_context["paper_capital_experiment"] = True
            decision_context.setdefault("execution", {}).update(
                {
                    "mode": "paper",
                    "paper_capital_experiment": True,
                    "experiment_reason": str(capital_entry_gate.get("reason") or "S7 paper Capital experiment"),
                }
            )
        if profitability_gate is not None:
            decision_context["autonomous_profitability_gate"] = profitability_gate
        if news_gate is not None:
            decision_context["autonomous_news_gate"] = news_gate
        if isinstance(ollama_review, dict) and ollama_review:
            decision_context["ollama"] = ollama_review
            execution_ctx = decision_context.setdefault("execution", {})
            if ollama_review.get("available"):
                execution_ctx["analysis_provider"] = ollama_review.get("provider", "ollama")
                execution_ctx["ai_available"] = True
                execution_ctx["fallback_used"] = ""
            else:
                execution_ctx["fallback_used"] = ollama_review.get("fallback_used") or "ollama_unavailable"
        if ai_fallback_used:
            signal_data["ai_fallback_used"] = True
            fallback_reason = (
                ollama_review.get("reason", "Ollama unavailable")
                if isinstance(ollama_review, dict)
                else "Ollama unavailable"
            )
            signal_data["agent_reasons"].append(f"ai_review_unavailable_fallback:{fallback_reason}")
            execution_ctx = decision_context.setdefault("execution", {})
            execution_ctx["fallback_used"] = "signal_only_ai_unavailable"
            execution_ctx["ai_available"] = False
            decision_context["ai_fallback_used"] = True
            decision_context.setdefault("warnings", []).append(
                f"AI review unavailable; continuing in paper mode with reduced signal-only sizing ({fallback_reason})"
            )
        signal_data["decision_context"] = decision_context
        signal_data["entry_reason"] = decision_context.get("reason", "")
        signal_data["decision_reasons"] = decision_context.get("reasons", [])
        signal_data["strategy_type"] = decision_context.get("strategy_type", "")
        # Phase 13 §97/§90 — persist EVERY decision (accepted AND rejected) to
        # the Decision Ledger. Fail-open: an audit failure must never block trading.
        try:
            from core.decision_ledger import get_decision_ledger_store, record_decision_fail_open
            record_decision_fail_open(
                get_decision_ledger_store(getattr(self.config_manager, "config", {})),
                signal_data,
                decision_context,
                eval_result=_eval_result,
                review=ollama_review or None,
            )
        except Exception:
            logger.exception("Decision ledger hook failed (fail-open)")

        if not risk_result.get("accepted"):
            record_trade_event(
                event_type="signal_rejected",
                status="rejected",
                ticker=signal_data.get("ticker", ""),
                signal_id=signal_data.get("id", ""),
                strategy_type=decision_context.get("strategy_type", ""),
                summary=decision_context.get("reason", ""),
                payload={
                    "decision_context": decision_context,
                    "risk_evaluation": risk_result,
                },
            )
            logger.warning(f"Signal {signal_data.get('ticker')} rejected by Risk Agent")
            _release_confidence_reservation(self.confidence_gates, signal_data, confidence_result)
            return

        if ai_review_policy["blocked"]:
            unavailable_reason = (
                ollama_review.get("reason", "Ollama unavailable")
                if isinstance(ollama_review, dict)
                else "Ollama unavailable"
            )
            record_trade_event(
                event_type="signal_rejected",
                status="ollama_unavailable",
                ticker=signal_data.get("ticker", ""),
                signal_id=signal_data.get("id", ""),
                strategy_type=decision_context.get("strategy_type", ""),
                summary=f"Autonomous candidate requires AI review: {unavailable_reason}",
                payload={
                    "decision_context": decision_context,
                    "risk_evaluation": risk_result,
                    "ollama_review": ollama_review,
                },
            )
            logger.info(
                f"Signal {signal_data.get('ticker')} rejected because required Ollama review is unavailable"
            )
            _release_confidence_reservation(self.confidence_gates, signal_data, confidence_result)
            return

        if ai_entry_blocked or legacy_review_veto:
            rejection_status = "critic_rejected" if ai_entry_block_reason == "critic_veto" else "ai_rejected"
            rejection_reason = (
                str(ollama_review.get("reason") or ai_entry_block_reason or "AI rejected trade")
                if isinstance(ollama_review, dict)
                else (ai_entry_block_reason or "AI rejected trade")
            )
            if ai_entry_block_detail:
                # A policy block (not the model's own rejection): the named
                # reason leads, the model's text is not presented as the cause.
                rejection_reason = f"{ai_entry_block_reason}: {ai_entry_block_detail}"
            record_trade_event(
                event_type="signal_rejected",
                status=rejection_status,
                ticker=signal_data.get("ticker", ""),
                signal_id=signal_data.get("id", ""),
                strategy_type=decision_context.get("strategy_type", ""),
                summary=rejection_reason,
                payload={
                    "decision_context": decision_context,
                    "risk_evaluation": risk_result,
                    "ollama_review": ollama_review,
                    "ai_block_reason": ai_entry_block_reason,
                },
            )
            logger.info(
                "Signal %s rejected by AI entry decision (%s): %s",
                signal_data.get("ticker"),
                ai_entry_block_reason or "legacy_ai_veto",
                rejection_reason,
            )
            _release_confidence_reservation(self.confidence_gates, signal_data, confidence_result)
            return

        record_trade_event(
            event_type="signal_accepted",
            status="accepted",
            ticker=signal_data.get("ticker", ""),
            signal_id=signal_data.get("id", ""),
            strategy_type=decision_context.get("strategy_type", ""),
            summary=decision_context.get("reason", ""),
            payload={
                "decision_context": decision_context,
                "risk_evaluation": risk_result,
            },
        )

        # Confidence gate — determine tier and adjusted size
        try:
            if confidence_result is None:
                if perf_cache is None:
                    if hasattr(self, "_perf_cache") and self._perf_cache is not None:
                        perf_cache = self._perf_cache
                    else:
                        from core.strategy_v2.performance_cache import StrategyPerformanceCache
                        self._perf_cache = StrategyPerformanceCache()
                        perf_cache = self._perf_cache
                confidence_result = self.confidence_gates.evaluate(signal_data, perf_cache=perf_cache)
            signal_data["confidence_tier"] = confidence_result["tier"]
            signal_data["adjusted_size_factor"] = confidence_result["adjusted_size_factor"]
            signal_data["confidence_reasons"] = confidence_result["reasons"]

            if ai_fallback_used:
                try:
                    ai_fallback_size = float(execution_cfg.get("ai_unavailable_size_factor", 0.5) or 0.5)
                except Exception:
                    ai_fallback_size = 0.5
                ai_fallback_size = max(0.05, min(1.0, ai_fallback_size))
                signal_data["adjusted_size_factor"] = round(
                    float(signal_data["adjusted_size_factor"] or 0.0) * ai_fallback_size,
                    4,
                )
                signal_data["confidence_reasons"] = list(signal_data.get("confidence_reasons") or [])
                signal_data["confidence_reasons"].append(
                    f"AI review unavailable; signal-only fallback size x{ai_fallback_size:.2f}"
                )

            if (
                isinstance(ollama_review, dict)
                and ollama_review.get("available")
                and str(ollama_review.get("recommendation") or "").lower() in {"reduce", "watch"}
                # A rule basket keeps equal weights: the AI's size advice is not applied.
                and not _ai_block_is_advisory(signal_data, execution_cfg)
            ):
                try:
                    raw_ai_size = ollama_review.get("size_factor")
                    ai_size_factor = 1.0 if raw_ai_size is None else max(0.0, min(1.0, float(raw_ai_size)))
                except Exception:
                    ai_size_factor = 1.0
                if ai_size_factor < 1.0:
                    confidence_size_factor = max(
                        0.0,
                        min(1.0, float(signal_data["adjusted_size_factor"] or 0.0)),
                    )
                    compounded_size_factor = round(
                        confidence_size_factor * ai_size_factor,
                        4,
                    )
                    effective_size_factor = _combine_independent_size_caps(
                        confidence_size_factor,
                        ai_size_factor,
                    )
                    signal_data["adjusted_size_factor"] = effective_size_factor
                    signal_data["confidence_reasons"] = list(signal_data.get("confidence_reasons") or [])
                    signal_data["confidence_reasons"].append(
                        "AI/Critic independent size cap "
                        f"{ai_size_factor:.2f}; effective {effective_size_factor:.2f} "
                        f"(confidence cap {confidence_size_factor:.2f}, not compounded to "
                        f"{compounded_size_factor:.4f}): {ollama_review.get('reason', '')}"
                    )
                    decision_context.setdefault("sizing", {}).update(
                        {
                            "sizing_policy": "minimum_of_independent_caps",
                            "confidence_size_cap": round(confidence_size_factor, 4),
                            "ai_critic_size_cap": round(ai_size_factor, 4),
                            "effective_size_factor": effective_size_factor,
                            "prevented_compound_size_factor": compounded_size_factor,
                        }
                    )

            if bool(execution_cfg.get("allow_ai_leverage", True)):
                max_ai_leverage = max(1, min(5, int(execution_cfg.get("max_ai_leverage", 2) or 2)))
                try:
                    ai_leverage = int(round(float(signal_data.get("ai_leverage", 1) or 1)))
                except Exception:
                    ai_leverage = 1
                ai_leverage = max(1, min(max_ai_leverage, ai_leverage))
            else:
                ai_leverage = 1
            signal_data["leverage"] = ai_leverage
            signal_data["ai_leverage"] = ai_leverage

            # P1 pre-earnings gap-risk sizing: an overnight earnings gap can
            # blow through the stop (history: closes at -5.4%..-9% on ~-3%
            # stops), so medium gap risk halves the size; high is blocked.
            pe_gap_size_factor = _pre_earnings_gap_size_factor(signal_data, execution_cfg)
            if pe_gap_size_factor < 1.0:
                signal_data["adjusted_size_factor"] = round(
                    max(0.0, float(signal_data.get("adjusted_size_factor", 0.0) or 0.0)) * pe_gap_size_factor,
                    4,
                )
                signal_data["confidence_reasons"] = list(signal_data.get("confidence_reasons") or [])
                signal_data["confidence_reasons"].append(
                    f"Pre-earnings gap-risk size factor x{pe_gap_size_factor:.2f}"
                )
                decision_context.setdefault("sizing", {})[
                    "pre_earnings_gap_risk_size_factor"
                ] = pe_gap_size_factor

            decision_context["confidence"] = {
                "tier": signal_data.get("confidence_tier"),
                "adjusted_size_factor": signal_data.get("adjusted_size_factor"),
                "reasons": list(signal_data.get("confidence_reasons") or []),
                "strategy_source": signal_data.get("strategy_source", "V1"),
                # None when the setup has no validation: 0.0 would read as a measured score.
                "validation_score": signal_data.get("validation_score"),
            }
            decision_context.setdefault("sizing", {})["leverage"] = ai_leverage
            signal_data["decision_context"] = decision_context

            if float(signal_data.get("adjusted_size_factor", 0.0) or 0.0) <= 0.0:
                confidence_result["tier"] = "SKIP"
                confidence_result["reasons"] = list(signal_data.get("confidence_reasons") or [])
                confidence_result["reasons"].append("Adjusted size factor is zero")
                signal_data["confidence_tier"] = "SKIP"
                signal_data["confidence_reasons"] = confidence_result["reasons"]
                decision_context["confidence"]["tier"] = "SKIP"
                decision_context["confidence"]["reasons"] = confidence_result["reasons"]

            if confidence_result["tier"] == "SKIP":
                record_trade_event(
                    event_type="signal_skipped",
                    status="skipped",
                    ticker=signal_data.get("ticker", ""),
                    signal_id=signal_data.get("id", ""),
                    strategy_type=decision_context.get("strategy_type", ""),
                    summary="; ".join(confidence_result["reasons"]),
                    payload={
                        "decision_context": decision_context,
                        "confidence_result": confidence_result,
                    },
                )
                logger.info(
                    f"Signal {signal_data.get('ticker')} SKIPPED by confidence gate: "
                    f"{'; '.join(confidence_result['reasons'])}"
                )
                # 2026-09-03 audit P1: the immutable ledger row is written before
                # these final gates, so a skipped candidate kept an "accepted"
                # row with no trade behind it. Append an explicitly-skipped
                # superseding row so the ledger tells the truth.
                try:
                    from core.decision_ledger import (
                        get_decision_ledger_store,
                        record_decision_fail_open,
                    )

                    skip_context = dict(decision_context)
                    skip_context["decision_id"] = f"{decision_context.get('decision_id') or 'dec'}#SKIPPED"
                    skip_context["decision_status"] = "skipped"
                    skip_context["skip_reasons"] = list(confidence_result["reasons"])
                    record_decision_fail_open(
                        get_decision_ledger_store(getattr(self.config_manager, "config", {})),
                        signal_data,
                        skip_context,
                        eval_result=_eval_result,
                        review=ollama_review or None,
                    )
                except Exception:
                    logger.exception("Skip-ledger row failed (fail-open)")
                _release_confidence_reservation(self.confidence_gates, signal_data, confidence_result)
                return

        except Exception as e:
            logger.error(f"Confidence gate error: {e}")
            record_trade_event(
                event_type="signal_skipped",
                status="confidence_error",
                ticker=signal_data.get("ticker", ""),
                signal_id=signal_data.get("id", ""),
                strategy_type=decision_context.get("strategy_type", ""),
                summary=f"Confidence gate error: {e}",
                payload={"decision_context": decision_context, "risk_evaluation": risk_result},
            )
            return

        exec_agent = self.agents.get("execution")
        if exec_agent and exec_agent.is_running:
            if not self._ensure_autonomous_signal_registered(signal_data):
                record_trade_event(
                    event_type="execution_skipped",
                    status="signal_register_failed",
                    ticker=signal_data.get("ticker", ""),
                    signal_id=signal_data.get("id", ""),
                    strategy_type=decision_context.get("strategy_type", ""),
                    summary="Autonomous signal could not be registered before execution",
                    payload={"decision_context": decision_context},
                )
                return
            executed = exec_agent.process_signal(signal_data, risk_evaluation=risk_result)
            if executed is False:
                self.confidence_gates.release_trade_reservation(signal_data)
            return

        record_trade_event(
            event_type="execution_skipped",
            status="skipped",
            ticker=signal_data.get("ticker", ""),
            signal_id=signal_data.get("id", ""),
            strategy_type=decision_context.get("strategy_type", ""),
            summary="Execution agent not running",
            payload={"decision_context": decision_context},
        )

    def update_config(self):
        """Called when config changes to restart/stop agents dynamically."""
        self.start_agents()

    def set_bot_mode(self, enabled: bool):
        """Toggle bot mode — when True, risk+execution agents start regardless of individual config."""
        self.bot_enabled = enabled
        self.start_agents()
        logger.info(f"AgentManager: bot_mode={'ON' if enabled else 'OFF'}")
