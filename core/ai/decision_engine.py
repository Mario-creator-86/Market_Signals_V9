"""Phase 4C decision engine — the bridge from the 3C/4A/4B platform into the
autonomous decision loop.

evaluate_trade_decision() composes:
  generate_with_fallback (3C) -> no_new_autonomous_trade_guard (GOLD-014) ->
  run_pipeline (4B §42: PARSE/SCHEMA/SEMANTIC/AUTHORITATIVE_DATA_CHECK/POLICY/RISK_GATE) ->
  run_critic (4C §44, post-pipeline)
and returns an EvaluationResult carrying the §41 Decision, §42 ValidationResult,
§44 CriticVerdict, and a backward-compatible `review` dict in the exact shape
agent_manager consumes today (evolve-not-rewrite, §5/§7).

Review-shape distinction (preserves §115 — does not weaken behavior):
  * AI-unavailable (GOLD-014) / malformed output (PARSE/SCHEMA/SEMANTIC) ->
    available=False -> agent_manager's UNAVAILABLE path (reject if AI-required,
    or signal-only paper fallback). "Could not produce a usable review."
  * Decisive rejection (POLICY gate, or NO_TRADE from AI/RISK_GATE veto/critic) ->
    available=True, recommendation="avoid" -> should_veto_trade vetoes ->
    agent_manager's ollama_rejected path. A policy rejection must NOT fall through
    to a paper-only fallback, so it is NEVER mapped to available=False.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.ai.critic import CriticVerdict, run_critic
from core.ai.assignment import ModelAssignment
from core.ai.decision import Action, Decision, _clamp, _extract_json, parse_decision  # noqa: F401  (parse_decision re-exported by __init__)
from core.ai.fallback import all_unavailable, generate_with_fallback, no_new_autonomous_trade_guard, served_by
from core.ai.pipeline import (
    PARSE, POLICY, RISK_GATE, SCHEMA, SEMANTIC, ValidationResult,
    default_services as _pipeline_default_services, run_pipeline,
)
from core.ai.provider import UNAVAILABLE, general_provider_names
from core.ai.router import ModelRouter
from core.ollama_decision import (
    CONFIDENCE_BASIS, TRADE_REVIEW_PROMPT_VERSION, build_trade_review_prompt, default_trade_review,
    parse_trade_review,
)


_ACTION_TO_RECOMMENDATION = {
    Action.OPEN_LONG: "approve", Action.OPEN_SHORT: "approve",
    Action.HOLD: "approve", Action.CLOSE: "approve",
    Action.REDUCE: "reduce", Action.WATCH: "watch", Action.NO_TRADE: "avoid",
}

# Pipeline stages whose failure means the AI output was malformed/incomplete ->
# unavailable-class review (NOT a decisive rejection).
_MALFORMED_STAGES = frozenset({PARSE, SCHEMA, SEMANTIC})


@dataclass(frozen=True)
class EvaluationResult:
    decision: Decision
    validation: ValidationResult
    critic: Optional[CriticVerdict]
    review: Dict[str, Any]
    ai_available: bool
    # Phase 13 §97 capture — defaulted for back-compat with every existing
    # five-positional-arg constructor call; populated by evaluate_trade_decision.
    prompt: Optional[str] = None
    model: str = ""
    prompt_version: str = ""


def _strict_schema(config: dict) -> bool:
    return bool((((config or {}).get("ai") or {}).get("decision") or {}).get("strict_schema", False))


def _critic_enabled(config: dict) -> bool:
    return bool((((config or {}).get("ai") or {}).get("decision") or {}).get("critic_enabled", True))


def _build_providers(config: dict, services: dict, *, role: str = "market_analyst"):
    """Resolve primary provider + fallback chain. Injectable via services; built from config otherwise."""
    primary = services.get("primary_provider")
    primary_model = services.get("primary_model")
    fallback = services.get("fallback_chain")
    primary_name: Optional[str] = None
    if primary is None and "primary_provider" not in services:
        from api.ai_config_service import build_provider
        names = general_provider_names(config)
        # §37/§108 — the owner-selected global provider leads the chain.
        preferred = str(((config or {}).get("ai") or {}).get("primary") or "").strip()
        if preferred and preferred in names:
            names = [preferred] + [n for n in names if n != preferred]
        if names:
            primary_name = names[0]
            primary = build_provider(config, primary_name)
    if primary_model is None and "primary_model" not in services:
        try:
            assignment = ModelAssignment.from_config(config)
            primary_model = ModelRouter.from_config(config, assignment).select_model(
                role, tier_hint="analysis"
            )
        except Exception:
            primary_model = None
    if fallback is None and "fallback_chain" not in services:
        from api.ai_config_service import build_provider
        ai_cfg = ((config or {}).get("ai") or {})
        fb = ai_cfg.get("fallback") or {}
        fb_names = fb if isinstance(fb, list) else (fb.get("chain") if isinstance(fb, dict) else [])
        if not fb_names:
            # No explicit chain configured: degrade to every OTHER configured
            # provider so a primary outage (Ollama 429/quota) still completes
            # on the remaining keys instead of dying with "unavailable".
            fb_names = [n for n in general_provider_names(config) if n != primary_name]
        fallback = [p for p in (build_provider(config, n) for n in (fb_names or [])) if p is not None]
    return primary, primary_model, (fallback or [])


def _decision_to_review(decision: Decision, validation: ValidationResult, critic: Optional[CriticVerdict],
                        *, provider_name: str, ai_available: bool, fallback_used: str = "") -> Dict[str, Any]:
    recommendation = _ACTION_TO_RECOMMENDATION.get(decision.action, "watch")
    review = {
        "available": ai_available,
        "provider": provider_name,
        "recommendation": recommendation,
        "confidence": round(_clamp(decision.confidence, 0.0, 1.0), 4),
        # Self-rated by the model, never calibrated: not a probability of profit.
        "confidence_basis": CONFIDENCE_BASIS,
        "leverage": 1,  # §43 — leverage is not AI-authoritative; agent_manager caps via config
        "reason": decision.thesis or validation.reason or "",
        "fallback_used": fallback_used,
        "decision": decision,
        "validation_stage": validation.stage,
        "critic_veto": bool(critic and critic.veto),
        "critic_model_veto": bool(critic and critic.model_veto),
        "critic_severity": round(float(critic.severity), 4) if critic else 0.0,
        "critic_quality_score": round(float(critic.quality_score), 2) if critic else None,
        "critic_veto_threshold": round(float(critic.veto_threshold), 4) if critic else None,
        "critic_policy_reason": critic.policy_reason if critic else "",
        "critic_categories": list(critic.categories) if critic else [],
    }
    if critic and not critic.veto and critic.size_factor < 1.0 and decision.action not in (Action.NO_TRADE, Action.WATCH):
        review["recommendation"] = "reduce"
        review["size_factor"] = round(float(critic.size_factor), 4)
    return review


def default_engine_services(config: Dict[str, Any]) -> Dict[str, Any]:
    """Pipeline stage services (from 4B) — the engine merges injected services over these."""
    return _pipeline_default_services(config)


def _no_trade_validation() -> ValidationResult:
    return ValidationResult(ok=True, decision=Decision(action=Action.NO_TRADE), stage=RISK_GATE)


def _signal_open_action(signal: Dict[str, Any]) -> Action:
    side = str(
        (signal or {}).get("action")
        or (signal or {}).get("direction")
        or (signal or {}).get("type")
        or ""
    ).upper().strip()
    if side in {"SELL", "SHORT"}:
        return Action.OPEN_SHORT
    if side in {"BUY", "LONG"}:
        return Action.OPEN_LONG
    return Action.WATCH


_QUOTE_SUFFIXES = ("USDT", "USDC", "USD")
_LONG_WORDS = frozenset({"LONG", "BUY", "OPEN_LONG"})
_SHORT_WORDS = frozenset({"SHORT", "SELL", "OPEN_SHORT"})


def _ticker_key(value: Any) -> str:
    """Comparable form of a ticker: PEPE-USD, PEPE/USD, $pepeusd and PEPE agree."""
    key = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    for suffix in _QUOTE_SUFFIXES:
        if len(key) > len(suffix) and key.endswith(suffix):
            return key[: -len(suffix)]
    return key


def _side_of(value: Any) -> str:
    word = str(value or "").upper().strip()
    if word in _LONG_WORDS:
        return "LONG"
    if word in _SHORT_WORDS:
        return "SHORT"
    return ""


def reply_mismatch(raw_text: Any, signal: Dict[str, Any]) -> str:
    """Why the AI reply does not answer the proposal it was asked about, or "".

    The reviewer is asked about ONE proposal (signal.ticker, signal.action). A
    reply that names another instrument, or the opposite side, is an answer to
    a different question: using its recommendation/confidence for this signal
    would attach somebody else's verdict to this trade. Only what the reply
    states explicitly is compared; a reply that does not echo the ticker or the
    side is not judged here.
    """
    if isinstance(raw_text, dict):
        parsed = raw_text
    else:
        snippet = _extract_json(str(raw_text or ""))
        if not snippet:
            return ""
        try:
            parsed = json.loads(snippet)
        except Exception:
            return ""
    if not isinstance(parsed, dict):
        return ""

    asked_ticker = str((signal or {}).get("ticker") or (signal or {}).get("symbol") or "").strip()
    reply_ticker = str(parsed.get("ticker") or parsed.get("symbol") or "").strip()
    if asked_ticker and reply_ticker and _ticker_key(asked_ticker) != _ticker_key(reply_ticker):
        return (
            f"AI reply is about {reply_ticker[:32].upper()} but the proposal under review is "
            f"{asked_ticker.upper()}"
        )

    asked_side = _side_of(
        (signal or {}).get("action") or (signal or {}).get("direction") or (signal or {}).get("type")
    )
    if asked_side:
        for key in ("direction", "side", "action"):
            reply_side = _side_of(parsed.get(key))
            if reply_side and reply_side != asked_side:
                return (
                    f"AI reply is for a {reply_side} position but the proposal under review is "
                    f"{asked_side}"
                )
    return ""


def _pipeline_response_payload(raw_text: str, signal: Dict[str, Any]):
    """Bridge the production trade-review JSON into the §41 pipeline schema.

    The established prompt returns ``recommendation`` while the Phase-4 schema
    validates ``action``.  Accept both explicitly so a valid provider response
    cannot silently become NO_TRADE merely because it used the documented
    production prompt.
    """
    lowered = str(raw_text or "").lower()
    if '"action"' in lowered or '"recommendation"' not in lowered:
        return raw_text, None
    legacy = parse_trade_review(raw_text)
    recommendation = str(legacy.get("recommendation") or "watch").lower()
    if recommendation == "avoid":
        action = Action.NO_TRADE
    elif recommendation == "watch":
        action = Action.WATCH
    else:
        action = _signal_open_action(signal)
    payload = {
        "action": action.value,
        "ticker": str((signal or {}).get("ticker") or ""),
        "confidence": legacy.get("confidence", 0.0),
        "thesis": str(legacy.get("reason") or ""),
        "risk_flags": list(legacy.get("risks") or []),
    }
    return payload, legacy


def _merge_legacy_review(
    review: Dict[str, Any],
    legacy: Optional[Dict[str, Any]],
    decision: Decision,
) -> Dict[str, Any]:
    if not isinstance(legacy, dict):
        return review
    merged = dict(review)
    if decision.action is Action.NO_TRADE:
        merged["recommendation"] = "avoid"
    else:
        recommendation = str(legacy.get("recommendation") or "").lower()
        if recommendation in {"approve", "reduce", "watch"}:
            merged["recommendation"] = recommendation
    for key in ("size_factor", "sentiment", "supports_direction", "reason", "risks"):
        if key in legacy:
            merged[key] = legacy[key]
    # Leverage remains app-authoritative (§43), regardless of provider output.
    merged["leverage"] = 1
    return merged


def _with_flow_evidence(signal: Dict[str, Any], *, services: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``signal`` with free flow evidence attached (never mutates).

    Hermetic callers (paper auto acceptance, deterministic scenario runners)
    declare ``services["skip_flow_evidence"] = True`` to keep their
    network-free contract; explicitly provided evidence wins either way.
    """
    if not isinstance(signal, dict) or "flow_evidence" in signal:
        return signal
    if services.get("skip_flow_evidence"):
        return signal
    try:
        from core.flow_indicators import flow_evidence_for_signal
        evidence = flow_evidence_for_signal(signal)
    except Exception:
        evidence = {"alignment": "UNAVAILABLE",
                    "note": "flow evidence error (attached in decision engine)"}
    enriched = dict(signal)
    enriched["flow_evidence"] = evidence
    return enriched


def evaluate_trade_decision(signal: Dict[str, Any], risk_result: Optional[Dict[str, Any]],
                            *, config: Dict[str, Any], services: Optional[Dict[str, Any]] = None,
                            role: str = "market_analyst") -> EvaluationResult:
    svc = {**default_engine_services(config), **(services or {})}
    primary, primary_model, fallback_chain = _build_providers(config, svc, role=role)
    provider_name = getattr(primary, "name", "") or "ai"

    no_trade = Decision(action=Action.NO_TRADE)

    # 1. No AI provider configured -> unavailable.
    if primary is None:
        review = default_trade_review(available=False, reason="no AI provider configured")
        review["fallback_used"] = "no_provider"
        review["provider"] = "ai"
        review["decision"] = no_trade
        review["validation_stage"] = ""
        return EvaluationResult(no_trade, _no_trade_validation(), None, review, False)

    # Free order-flow evidence (whale / institutional proxy): attached to the
    # signal so the §41 prompt AND the §44 critic both see it. Evidence-only —
    # CONFIRM/NEUTRAL/CONTRADICT vs the setup's direction; UNAVAILABLE must
    # never influence the verdict (owner spec: no new veto, no parallel engine).
    # Attached AFTER the no-provider early return so an unconfigured pipeline
    # performs no live network I/O, and to a local copy so the caller-owned
    # signal dict is never mutated (hermetic callers reuse their scenarios).
    signal = _with_flow_evidence(signal, services=svc)

    # 2. Generate (multi-provider fallback). §41 prompt is the legacy contract.
    prompt = build_trade_review_prompt(signal, risk_result)
    prompt_version = TRADE_REVIEW_PROMPT_VERSION
    # Honour the configured review timeout; without it every provider in the
    # chain waits its own full default and blocks the trading cycle.
    call_opts: Dict[str, Any] = {}
    try:
        review_timeout = float(((config or {}).get("ollama") or {}).get("decision_timeout_seconds") or 0.0)
    except (TypeError, ValueError):
        review_timeout = 0.0
    if review_timeout > 0:
        call_opts["timeout_override"] = review_timeout
    try:
        resp = generate_with_fallback(primary, prompt, model=primary_model, fallback_chain=fallback_chain,
                                      system="You are a disciplined trading analyst.", **call_opts)
    except Exception as exc:
        review = default_trade_review(available=False, reason=str(exc))
        review["fallback_used"] = "exception"
        review["provider"] = provider_name
        review["decision"] = no_trade
        review["validation_stage"] = ""
        return EvaluationResult(no_trade, _no_trade_validation(), None, review, False,
                                prompt=prompt, model="", prompt_version=prompt_version)

    # 3. AI all-unavailable (GOLD-014) -> unavailable review (NOT a decisive rejection).
    if resp is UNAVAILABLE or all_unavailable(resp):
        blocked = no_new_autonomous_trade_guard(resp, config)
        review = default_trade_review(available=False, reason="all AI providers unavailable")
        review["fallback_used"] = "all_unavailable" + ("_blocked" if blocked else "")
        review["provider"] = provider_name
        review["decision"] = no_trade
        review["validation_stage"] = ""
        return EvaluationResult(no_trade, _no_trade_validation(), None, review, False,
                                prompt=prompt, model=getattr(resp, "model", "") or "",
                                prompt_version=prompt_version)

    # 4. §42 validation pipeline (PARSE/SCHEMA/SEMANTIC/AUTHORITATIVE/POLICY/RISK_GATE).
    provider_name = served_by(resp, provider_name)

    # 3b. The reply must be about the proposal that was asked. A verdict for
    # another ticker or for the opposite side is a decisive rejection (never a
    # paper-only "AI unavailable" fallback, never an approval).
    mismatch = reply_mismatch(resp.text, signal)
    if mismatch:
        mismatch_validation = ValidationResult(
            ok=False, decision=no_trade, stage=SEMANTIC, reason=f"SEMANTIC: {mismatch}",
            checks={"reply_mismatch": mismatch},
        )
        review = _decision_to_review(no_trade, mismatch_validation, None, provider_name=provider_name,
                                     ai_available=True, fallback_used="ai_reply_mismatch")
        review["reason"] = mismatch
        return EvaluationResult(no_trade, mismatch_validation, None, review, True,
                                prompt=prompt, model=getattr(resp, "model", "") or "",
                                prompt_version=prompt_version)

    pipeline_payload, legacy_review = _pipeline_response_payload(resp.text, signal)
    validation = run_pipeline(pipeline_payload, signal, [], config=config, services=svc)
    used_model = getattr(resp, "model", "") or ""

    if not validation.ok:
        rejected = validation.decision if validation.decision is not None else no_trade
        if validation.stage in _MALFORMED_STAGES:
            # Malformed/incomplete AI output -> unavailable-class (legacy invalid_json behavior).
            review = default_trade_review(available=False, reason=validation.reason or "AI output malformed")
            review["fallback_used"] = "invalid_json" if validation.stage == PARSE else f"{validation.stage.lower()}_fail"
            review["provider"] = provider_name
            review["decision"] = rejected
            review["validation_stage"] = validation.stage
            return EvaluationResult(rejected, validation, None, review, False,
                                    prompt=prompt, model=used_model, prompt_version=prompt_version)
        # POLICY (decisive rejection): available=True, avoid — must NOT fall through to paper fallback.
        review = _decision_to_review(no_trade, validation, None, provider_name=provider_name,
                                     ai_available=True, fallback_used=f"pipeline_{validation.stage}")
        return EvaluationResult(rejected, validation, None, review, True,
                                prompt=prompt, model=used_model, prompt_version=prompt_version)

    # 5. ok=True — pipeline accepted (decision may already be NO_TRADE from RISK_GATE veto or AI).
    decision = validation.decision if validation.decision is not None else no_trade

    # 6. §44 critic — only for active decisions, not NO_TRADE/WATCH.
    critic = None
    if decision.action not in (Action.NO_TRADE, Action.WATCH) and _critic_enabled(config):
        critic = run_critic(decision, signal, config=config, services=svc)
        if critic.veto:
            decision = Decision(
                action=Action.NO_TRADE, ticker=decision.ticker, confidence=decision.confidence,
                thesis=critic.rationale or "critic veto", raw=decision.raw,
                rejected_fields=decision.rejected_fields,
            )

    review = _decision_to_review(decision, validation, critic, provider_name=provider_name, ai_available=True)
    review = _merge_legacy_review(review, legacy_review, decision)
    if critic and not critic.veto and critic.size_factor < 1.0 and decision.action not in (Action.NO_TRADE, Action.WATCH):
        review["recommendation"] = "reduce"
        try:
            raw_size = review.get("size_factor")
            proposed_size = 1.0 if raw_size is None else float(raw_size)
        except (TypeError, ValueError):
            proposed_size = 1.0
        review["size_factor"] = round(min(proposed_size, float(critic.size_factor)), 4)
    return EvaluationResult(decision, validation, critic, review, True,
                            prompt=prompt, model=used_model, prompt_version=prompt_version)
