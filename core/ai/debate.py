"""Structured bull/bear entry debate with a research-manager verdict.

Adopted from the TradingAgents framework (TauricResearch): instead of a single
reviewer opinion, two adversarial researchers argue the strongest honest case
for and against the proposed entry, then a research manager judges the debate
and issues a structured verdict on the app's own 4-action vocabulary.

Scope discipline (owner spec, 2026-09-02):
- runs only for autonomous candidates that the §41 reviewer already scored as
  approve/reduce (pre-earnings first — the measured 18% win-rate cohort);
- bounded: 3 LLM calls per candidate, fail-open (any error/timeout → the §41
  verdict stands, honestly labeled `unavailable`);
- the manager verdict is the post-debate recommendation and confidence; the
  existing §44 critic, confidence gates and gap-risk gates still apply.
"""
from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from core.prompt_evidence import UNTRUSTED_NOTICE, clean_text, evidence_block

logger = logging.getLogger("AgentManager")

# Seconds one debate call may take when the config names no review timeout.
_DEFAULT_DEBATE_CALL_TIMEOUT = 45.0

DEBATE_PROMPT_VERSION = "entry_debate_v1"

_VALID_RECOMMENDATIONS = {"approve", "reduce", "watch", "avoid"}
_VALID_ACTIONS = {"OPEN_LONG", "OPEN_SHORT", "REDUCE", "WATCH", "NO_TRADE", "HOLD", "CLOSE"}


def _action_phrase(review: Dict[str, Any]) -> str:
    for key in ("action", "recommendation"):
        value = str(review.get(key) or "").strip().upper()
        if value:
            return value
    return "PROPOSED ENTRY"


def _compact_evidence(signal: Dict[str, Any], review: Dict[str, Any], limit: int = 900) -> str:
    parts: List[str] = []
    # 2026-09-03 owner feedback: the debate was data-starved — no price, no
    # volume, no trends. Render the market block first so both researchers
    # argue over the same facts a trader would check.
    market_keys = ("price", "change_pct", "rsi", "macd_hist", "trend_1d", "trend_4h",
                   "trend_1h", "trend_30m", "trend_5m", "market_regime", "sector",
                   "average_daily_volume_usd", "volume", "market_cap", "earnings_date")
    market_rows = [f"{k}={signal.get(k)}" for k in market_keys if signal.get(k) is not None]
    if market_rows:
        parts.append("MARKET: " + ", ".join(market_rows))
    for key in ("ticker", "action", "strategy_type", "signal_score", "validation_score",
                "news_sentiment", "news_score"):
        value = signal.get(key)
        if value is not None:
            parts.append(f"{key}={value}")
    flow = signal.get("flow_evidence")
    if isinstance(flow, dict) and flow.get("alignment"):
        parts.append(f"flow_alignment={flow.get('alignment')}")
    base_rates = signal.get("strategy_base_rates")
    if isinstance(base_rates, dict):
        bucket = base_rates.get("exact") or base_rates.get("family")
        if isinstance(bucket, dict) and bucket.get("n") is not None:
            parts.append(
                f"strategy_base_rates(n={bucket.get('n')}, win={bucket.get('win_rate_pct')}%, "
                f"avg={bucket.get('avg_pnl_pct')}%)"
            )
    conf = review.get("confidence")
    if conf is not None:
        parts.append(f"reviewer_self_rated_confidence={conf} (uncalibrated, not a probability)")
    news_lines = []
    for headline in list(signal.get("news_headlines") or [])[:3]:
        if isinstance(headline, dict):
            headline = headline.get("title") or headline.get("headline") or ""
        if str(headline or "").strip():
            news_lines.append(f"news: {headline}")
    news_block = evidence_block("news_headlines", news_lines, limit=3, max_chars=170)
    if news_block:
        parts.append(news_block)
    reviewer_lines = [f"review_flag: {flag}" for flag in list(review.get("risk_flags") or review.get("risks") or [])[:3]]
    thesis = str(review.get("reason") or review.get("thesis") or "")
    if thesis:
        reviewer_lines.append(f"reviewer_thesis: {thesis}")
    reviewer_block = evidence_block("reviewer_output", reviewer_lines, limit=4, max_chars=limit)
    if reviewer_block:
        parts.append(reviewer_block)
    return "\n".join(parts)


def _bull_prompt(signal: Dict[str, Any], review: Dict[str, Any]) -> str:
    action = _action_phrase(review)
    return (
        "You are the Bull Researcher in a trading firm. Argue the STRONGEST honest case for "
        f"taking the proposed {action} on {signal.get('ticker')}. "
        "Use only the evidence below — never invent data. Address the reviewer's risk flags "
        "head-on and explain why the position still merits taking. "
        "Write 3-5 tight sentences, plain prose.\n"
        f"{UNTRUSTED_NOTICE}\n\n"
        f"EVIDENCE:\n{_compact_evidence(signal, review)}"
    )


def _bear_prompt(signal: Dict[str, Any], review: Dict[str, Any]) -> str:
    action = _action_phrase(review)
    return (
        "You are the Bear Researcher in a trading firm. Argue the STRONGEST honest case "
        f"AGAINST taking the proposed {action} on {signal.get('ticker')}. "
        "Use only the evidence below — never invent data. Attack the weakest link: thesis "
        "assumptions, data quality, gap/liquidity risk, base rates. "
        "Write 3-5 tight sentences, plain prose.\n"
        f"{UNTRUSTED_NOTICE}\n\n"
        f"EVIDENCE:\n{_compact_evidence(signal, review)}"
    )


def _plain(text: Any) -> str:
    """One line of printable text, so a headline cannot pose as a new prompt section."""
    return clean_text(text, max_chars=4000)


def _debate_call_timeout(config_root: Dict[str, Any]) -> float:
    """Seconds one debate call may take: the configured review timeout."""
    try:
        seconds = float(((config_root or {}).get("ollama") or {}).get("decision_timeout_seconds") or 0.0)
    except (TypeError, ValueError):
        seconds = 0.0
    if not math.isfinite(seconds) or seconds <= 0:
        return _DEFAULT_DEBATE_CALL_TIMEOUT
    return seconds


def _manager_prompt(signal: Dict[str, Any], review: Dict[str, Any], bull: str, bear: str) -> str:
    direction = str(signal.get("action") or review.get("action") or "").upper()
    return (
        "You are the Research Manager judging a one-round bull/bear debate about a proposed "
        f"paper trade on {signal.get('ticker')} ({direction or 'ENTRY'}). Weigh both arguments "
        "against the evidence summary and the system's own reviewer verdict, then issue YOUR "
        "post-debate verdict.\n\n"
        "Return STRICT JSON, no markdown fences, with exactly these keys:\n"
        '{"recommendation": "approve|reduce|watch|avoid", "confidence": 0.0-1.0, '
        '"rationale": "2-4 sentences naming the argument that decided it", '
        '"bull_score": 0-10, "bear_score": 0-10}\n'
        "recommendation must be one of approve, reduce, watch, avoid. Commit to a clear stance "
        "when one side clearly wins; reserve watch for genuinely balanced evidence.\n"
        "confidence is your own 0..1 rating of the verdict; it is not a probability of profit.\n"
        f"{UNTRUSTED_NOTICE}\n\n"
        f"REVIEWER VERDICT: action={_action_phrase(review)} "
        f"self_rated_confidence={review.get('confidence')}\n\n"
        f"BULL ARGUMENT:\n{evidence_block('bull_argument', [bull], limit=1, max_chars=2000)}\n\n"
        f"BEAR ARGUMENT:\n{evidence_block('bear_argument', [bear], limit=1, max_chars=2000)}\n\n"
        f"EVIDENCE SUMMARY:\n{_compact_evidence(signal, review, limit=500)}"
    )


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    raw = str(text or "").strip()
    if not raw:
        return None
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        raw = match.group(0)
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _norm_verdict(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    recommendation = str(data.get("recommendation") or "").strip().lower()
    if recommendation not in _VALID_RECOMMENDATIONS:
        return {}
    # A verdict without a usable confidence is not a verdict: `min(1.0, nan)`
    # is 1.0, so NaN used to become full confidence, and a missing value 0.0.
    confidence = _finite(data.get("confidence"))
    if confidence is None:
        return {}
    confidence = max(0.0, min(1.0, confidence))
    bull_score = int(max(0.0, min(10.0, _finite(data.get("bull_score")) or 0.0)))
    bear_score = int(max(0.0, min(10.0, _finite(data.get("bear_score")) or 0.0)))
    return {
        "recommendation": recommendation,
        "confidence": round(confidence, 4),
        # Self-rated by the manager model, never calibrated: not a probability.
        "confidence_basis": "model_self_reported_uncalibrated",
        "rationale": str(data.get("rationale") or "").strip()[:1200],
        "bull_score": bull_score,
        "bear_score": bear_score,
    }


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def run_entry_debate(
    signal: Dict[str, Any],
    review: Dict[str, Any],
    *,
    config: Dict[str, Any],
    services: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run the bull/bear debate + manager verdict. Never raises; on any failure
    returns {"available": False, ...} and the caller keeps the §41 verdict."""
    result: Dict[str, Any] = {
        "available": False,
        "prompt_version": DEBATE_PROMPT_VERSION,
        "bull": "",
        "bear": "",
        "verdict": {},
        "models": {},
        "error": "",
    }
    if not isinstance(signal, dict) or not isinstance(review, dict):
        result["error"] = "invalid_input"
        return result
    try:
        from core.ai.decision_engine import _build_providers
        from core.ai.fallback import generate_with_fallback, all_unavailable

        # _build_providers expects a mapping (config.get("ai")); the callers
        # may hand us a ConfigManager wrapper — normalize first.
        config_root = getattr(config, "config", config)
        if not isinstance(config_root, dict):
            config_root = {}
        svc = dict(services or {})
        # Capture the full fallback_chain (not discarded as _) so that when the
        # primary provider (Ollama Cloud) hits quota limits the debate can still
        # complete via OpenAI / Gemini / Anthropic rather than returning
        # error="bull_argument_unavailable" immediately.
        bull_provider, bull_model, bull_fallback = _build_providers(config_root, svc, role="research_scientist")
        manager_provider, manager_model, mgr_fallback = _build_providers(config_root, svc, role="portfolio_manager")
        if bull_provider is None or manager_provider is None:
            result["error"] = "no_provider"
            return result
        result["models"] = {
            "debater": str(getattr(bull_provider, "name", "ai")) + (f"/{bull_model}" if bull_model else ""),
            "manager": str(getattr(manager_provider, "name", "ai")) + (f"/{manager_model}" if manager_model else ""),
        }
        system = "You are a disciplined trading researcher. Plain prose only."
        # Three sequential calls inside the trading cycle: each one gets the
        # configured review timeout, never the providers' own defaults.
        call_opts = {"timeout_override": _debate_call_timeout(config_root)}

        bull_resp = generate_with_fallback(
            bull_provider, _bull_prompt(signal, review),
            model=bull_model, fallback_chain=bull_fallback, system=system, **call_opts,
        )
        bull_text = str(getattr(bull_resp, "text", "") or "").strip()
        if not bull_text or all_unavailable(bull_resp):
            result["error"] = "bull_argument_unavailable"
            return result
        result["bull"] = bull_text[:2000]

        bear_resp = generate_with_fallback(
            bull_provider, _bear_prompt(signal, review),
            model=bull_model, fallback_chain=bull_fallback, system=system, **call_opts,
        )
        bear_text = str(getattr(bear_resp, "text", "") or "").strip()
        if not bear_text or all_unavailable(bear_resp):
            # A manager who hears only the bull has not judged a debate. Report
            # it as unavailable so the caller keeps the reviewer's verdict.
            result["error"] = "bear_argument_unavailable"
            return result
        result["bear"] = bear_text[:2000]

        mgr_resp = generate_with_fallback(
            manager_provider,
            _manager_prompt(signal, review, result["bull"], result["bear"]),
            model=manager_model, fallback_chain=mgr_fallback,
            system="You are the Research Manager. Return strict JSON only.", **call_opts,
        )
        verdict = _norm_verdict(_extract_json(str(getattr(mgr_resp, "text", "") or "")))
        if not verdict:
            result["error"] = "manager_verdict_unparseable"
            return result
        result["verdict"] = verdict
        result["available"] = True
        result.pop("error", None)
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return result


def should_debate(
    signal: Dict[str, Any],
    review: Dict[str, Any],
    execution_cfg: Dict[str, Any],
) -> Tuple[bool, str]:
    """Debate policy: which candidates earn the 3-call budget."""
    if not bool((execution_cfg or {}).get("debate_enabled", True)):
        return False, "disabled"
    if not isinstance(review, dict) or not review.get("available"):
        return False, "review_unavailable"
    if str(review.get("recommendation") or "").lower() not in {"approve", "reduce"}:
        return False, "not_an_entry"
    strategy = str((signal or {}).get("strategy_type") or "").upper()
    scope = str((execution_cfg or {}).get("debate_scope") or "pre_earnings").lower()
    is_pre_earnings = strategy.startswith("PRE_EARNINGS")
    if scope == "pre_earnings":
        if not is_pre_earnings:
            return False, "scope_pre_earnings_only"
    elif scope == "all":
        pass
    else:  # "none" or unknown → off
        return False, "scope_off"
    return True, ""


__all__ = ["run_entry_debate", "should_debate", "DEBATE_PROMPT_VERSION"]
